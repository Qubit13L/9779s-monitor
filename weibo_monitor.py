#!/usr/bin/env python3
"""Monitor Weibo accounts for new posts, translate to Chinese, push to Discord.

Mirrors monitor.py's structure but talks to m.weibo.cn's getIndex container API
with a logged-in cookie. Reuses Discord webhook + DeepSeek translation, with
Google Translate fallback for non-Chinese content (rare for Weibo posts).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

# Re-use shared logic from the X monitor module to avoid duplication.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from monitor import (  # type: ignore
    analyze_text,
    translate_with_google,
    is_likely_chinese,
    http_post_json,
    ARTIST_DISPLAY,
    ARTIST_COLOR_PALETTES,
)

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "weibo_seen.json"
MAX_SEEN = 400

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1"
)


def _parse_uid_list() -> list[str]:
    raw = os.environ.get("WEIBO_UIDS", "")
    return [u.strip() for u in raw.split(",") if u.strip()]


WEIBO_UIDS = _parse_uid_list()
WEIBO_COOKIE = os.environ.get("WEIBO_COOKIE", "").strip()
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()

# Map Weibo UIDs to a friendly artist key + display name. The artist key
# matches monitor.py's ARTIST_DISPLAY so colors stay consistent across
# X and Weibo embeds for the same person.
WEIBO_ARTIST_MAP = {
    "5581907456": {"artist_key": "janeeeyeh", "name": "Jane"},
    "7941991374": {"artist_key": "kaosupassara9", "name": "Kao"},
}


@dataclass
class WeiboPost:
    post_id: str
    bid: str
    monitored_uid: str
    author: str
    pub_date: str
    text_html: str
    text: str
    images: list[str]
    has_video: bool
    is_retweet: bool
    retweeted_author: str
    retweeted_text: str
    source: str
    link: str


def http_get_json(url: str, timeout: int = 20) -> tuple[int, dict]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Cookie": WEIBO_COOKIE,
            "Referer": "https://m.weibo.cn/",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "MWeibo-Pwa": "1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        print(f"[weibo] network error: {e}", flush=True)
        return 0, {}
    except Exception as e:
        print(f"[weibo] parse error: {e}", flush=True)
        return -1, {}


def fetch_user_posts(uid: str) -> list[dict]:
    """Hits the user-feed container and returns the raw 'cards' list."""
    container = f"107603{uid}"
    url = (
        "https://m.weibo.cn/api/container/getIndex?"
        f"type=uid&value={uid}&containerid={container}"
    )
    status, data = http_get_json(url)
    if status != 200 or not data.get("ok"):
        print(f"[weibo] uid={uid} fetch failed (status={status}, ok={data.get('ok')})",
              flush=True)
        if status in (302, 401, 403):
            push_cookie_alert()
        return []
    cards = data.get("data", {}).get("cards", []) or []
    return [c for c in cards if c.get("mblog")]


def parse_post(card: dict, monitored_uid: str) -> WeiboPost | None:
    m = card.get("mblog") or {}
    post_id = str(m.get("id") or "")
    bid = str(m.get("bid") or "")
    if not post_id:
        return None

    user = m.get("user") or {}
    author = user.get("screen_name") or ""

    text_html = m.get("text") or ""
    text = re.sub(r"<[^>]+>", "", text_html)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text).strip()

    images: list[str] = []
    pics = m.get("pics") or []
    for p in pics:
        url = (p.get("large") or {}).get("url") or p.get("url")
        if url:
            images.append(url)

    has_video = bool(m.get("page_info") and (m["page_info"].get("type") == "video"))

    is_retweet = bool(m.get("retweeted_status"))
    retweeted_author = ""
    retweeted_text = ""
    if is_retweet:
        rs = m["retweeted_status"]
        retweeted_author = (rs.get("user") or {}).get("screen_name") or ""
        rt_html = rs.get("text") or ""
        retweeted_text = re.sub(r"<[^>]+>", "", rt_html).strip()
        # If the user added no comment, weibo shows "转发微博"
        if not text or text == "转发微博":
            text = retweeted_text
        # Pull through media from the retweeted post if we have nothing
        if not images:
            for p in rs.get("pics") or []:
                url = (p.get("large") or {}).get("url") or p.get("url")
                if url:
                    images.append(url)
        if not has_video:
            has_video = bool(rs.get("page_info") and rs["page_info"].get("type") == "video")

    link = f"https://weibo.com/{user.get('id', '')}/{bid}" if bid else \
           f"https://m.weibo.cn/detail/{post_id}"
    source = re.sub(r"<[^>]+>", "", m.get("source") or "").strip()

    return WeiboPost(
        post_id=post_id,
        bid=bid,
        monitored_uid=monitored_uid,
        author=author,
        pub_date=m.get("created_at") or "",
        text_html=text_html,
        text=text,
        images=images,
        has_video=has_video,
        is_retweet=is_retweet,
        retweeted_author=retweeted_author,
        retweeted_text=retweeted_text,
        source=source,
        link=link,
    )


def load_state() -> set[str]:
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text()))
    except Exception:
        return set()


def save_state(seen: set[str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    trimmed = list(seen)[-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(trimmed, indent=2, ensure_ascii=False))


def freshness_tag(pub_date: str) -> str:
    """Returns '🟢 刚发' if posted in last 5 minutes, otherwise empty."""
    try:
        from datetime import datetime, timezone
        dt = parsedate_to_datetime(pub_date)
        delta = datetime.now(timezone.utc) - dt
        if delta.total_seconds() < 5 * 60:
            return "🟢 刚发"
    except Exception:
        pass
    return ""


def palette_for(uid: str) -> dict:
    info = WEIBO_ARTIST_MAP.get(uid, {})
    artist_key = info.get("artist_key", "")
    offset = ARTIST_DISPLAY.get(artist_key, {}).get("color_offset", 0)
    return ARTIST_COLOR_PALETTES[offset % len(ARTIST_COLOR_PALETTES)]


def display_name_for(uid: str, fallback: str = "") -> str:
    info = WEIBO_ARTIST_MAP.get(uid)
    return info["name"] if info else (fallback or f"微博 UID {uid}")


def build_embeds(p: WeiboPost, analysis: dict) -> list[dict]:
    palette = palette_for(p.monitored_uid)
    artist_name = display_name_for(p.monitored_uid, p.author)

    if p.is_retweet:
        emoji, label, color = "🔁", "微博·转发", palette["retweet"]
        title = f"{emoji} {label} · {artist_name} 转了 @{p.retweeted_author}"
    else:
        emoji, label, color = "📌", "微博·原创", palette["original"]
        title = f"{emoji} {label} · {artist_name}"

    fresh = freshness_tag(p.pub_date)
    if fresh:
        title = f"{fresh}  {title}"

    is_chinese = analysis.get("is_chinese") if analysis else is_likely_chinese(p.text)
    summary = analysis.get("summary", "") if analysis else ""
    translation = analysis.get("translation", "") if analysis else ""
    source_tag = analysis.get("source", "deepseek") if analysis else "none"

    parts: list[str] = []
    if summary:
        parts.append(f"**📋 内容摘要**\n{summary}")

    if is_chinese:
        parts.append(f"**📝 原文**\n{p.text[:1500]}")
    elif translation:
        tag = "（机器翻译）" if source_tag == "google" else ""
        parts.append(f"**📝 中文译文{tag}**\n{translation[:1500]}")
    else:
        parts.append(f"**📝 原文**\n{p.text[:1500]}")

    if p.is_retweet and p.retweeted_text and p.text != p.retweeted_text:
        parts.append(f"**↪️ 被转的内容**\n{p.retweeted_text[:600]}")

    embed = {
        "title": title,
        "description": "\n\n".join(parts) if parts else "(无文本)",
        "url": p.link,
        "color": color,
        "footer": {"text": f"{p.author} · 微博{' · ' + p.source if p.source else ''}"},
    }
    try:
        embed["timestamp"] = parsedate_to_datetime(p.pub_date).isoformat()
    except Exception:
        pass
    if p.images:
        embed["image"] = {"url": p.images[0]}

    embeds = [embed]
    for img in p.images[1:4]:
        embeds.append({"url": p.link, "image": {"url": img}, "color": color})
    return embeds


def push_discord(p: WeiboPost, analysis: dict) -> bool:
    embeds = build_embeds(p, analysis)
    payload: dict = {"embeds": embeds}

    content_parts: list[str] = []
    if DISCORD_USER_ID:
        content_parts.append(f"<@{DISCORD_USER_ID}>")
    if p.has_video:
        content_parts.append(f"📹 含视频，建议直接看原微博: {p.link}")
    if content_parts:
        payload["content"] = " ".join(content_parts)

    if DISCORD_USER_ID:
        payload["allowed_mentions"] = {"parse": [], "users": [DISCORD_USER_ID]}

    status, body = http_post_json(DISCORD_WEBHOOK, payload)
    if status >= 300:
        print(f"[discord] failed {status}: {body[:300]}", flush=True)
        return False
    return True


def push_cookie_alert() -> None:
    """Push a Discord alert telling the user the cookie likely expired."""
    payload = {
        "content": (
            f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
        ),
        "embeds": [{
            "title": "🚨 微博 cookie 失效",
            "description": (
                "微博监控请求被拒（302/401/403），通常是 cookie 过期了。\n\n"
                "**修复步骤：**\n"
                "1. 用小号重新登录 m.weibo.cn\n"
                "2. DevTools 提取新的 SUB+SUBP\n"
                "3. 把新 cookie 发给 Claude 替换 GitHub Secret\n\n"
                "在替换前微博监控会一直失败，X 监控不受影响。"
            ),
            "color": 0xE74C3C,
        }],
    }
    if DISCORD_USER_ID:
        payload["allowed_mentions"] = {"parse": [], "users": [DISCORD_USER_ID]}
    http_post_json(DISCORD_WEBHOOK, payload)


def main() -> int:
    bootstrap = os.environ.get("BOOTSTRAP", "").lower() in ("1", "true", "yes")

    if not WEIBO_UIDS:
        print("[error] WEIBO_UIDS is empty", flush=True)
        return 1
    if not WEIBO_COOKIE:
        print("[error] WEIBO_COOKIE is empty", flush=True)
        return 1

    print(f"[start] monitoring {len(WEIBO_UIDS)} weibo accounts: {WEIBO_UIDS}",
          flush=True)

    all_posts: list[WeiboPost] = []
    for uid in WEIBO_UIDS:
        cards = fetch_user_posts(uid)
        per_user = [parse_post(c, uid) for c in cards]
        per_user = [p for p in per_user if p]
        print(f"[parse] uid={uid}: {len(per_user)} posts", flush=True)
        all_posts.extend(per_user)
        time.sleep(2)

    all_posts.sort(key=lambda p: p.pub_date)

    seen = load_state()
    new_posts = [p for p in all_posts if p.post_id not in seen]
    print(f"[diff] {len(new_posts)} new of {len(all_posts)} total", flush=True)

    if bootstrap or not seen:
        for p in all_posts:
            seen.add(p.post_id)
        save_state(seen)
        print(f"[bootstrap] marked {len(all_posts)} as seen, no push", flush=True)
        return 0

    pushed = 0
    for p in new_posts:
        analysis: dict = {}
        if p.text:
            analysis = analyze_text(p.text)
            if not analysis:
                analysis = translate_with_google(p.text)
        ok = push_discord(p, analysis)
        if ok:
            seen.add(p.post_id)
            pushed += 1
            time.sleep(1)

    save_state(seen)
    print(f"[done] pushed {pushed}/{len(new_posts)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
