#!/usr/bin/env python3
"""Monitor a Twitter/X account via Nitter RSS, summarize + translate to
Chinese using DeepSeek, and push categorized cards to Discord webhook.

Designed to run in GitHub Actions on a schedule.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state" / "seen.json"
MAX_SEEN = 300
SUMMARY_THRESHOLD = 150  # characters

NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
]

def _parse_usernames() -> list[str]:
    raw = os.environ.get("TWITTER_USERNAMES") or os.environ.get("TWITTER_USERNAME", "")
    return [u.strip().lstrip("@") for u in raw.split(",") if u.strip()]


USERNAMES = _parse_usernames()
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()

# Friendly display name + per-artist accent color for the embeds.
# Falls back to the X handle if not in this map.
ARTIST_DISPLAY = {
    "janeeeyeh": {"name": "Jane", "color_offset": 0},
    "kaosupassara9": {"name": "Kao", "color_offset": 1},
}


def display_for(username: str) -> str:
    info = ARTIST_DISPLAY.get(username.lower())
    return info["name"] if info else f"@{username}"


@dataclass
class Tweet:
    guid: str
    title: str
    author: str
    pub_date: str
    link: str
    description_html: str
    is_retweet: bool
    is_reply: bool
    images: list[str]
    videos: list[str]
    text: str
    # Which monitored username produced this tweet (could be janeeyeh or Kaosupassara9).
    # Distinct from `author`, which is the original poster for RTs.
    monitored_user: str = ""
    # Unix timestamp of when *we* first saw this tweet in the feed.
    # For retweets we use this (±10 min approximation) as the RT time,
    # since nitter only exposes the original post's pubDate.
    detected_at: int = 0


def http_get(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_post_json(url: str, body: dict, headers: dict | None = None,
                   timeout: int = 30) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    base_headers = {
        "Content-Type": "application/json",
        "User-Agent": "9779s-monitor/1.0 (+https://github.com/Qubit13L/9779s-monitor)",
    }
    if headers:
        base_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=base_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        try:
            body_text = e.read().decode("utf-8", "ignore")
        except Exception:
            body_text = ""
        return e.code, body_text
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        return 0, f"network_error: {e}"


def fetch_rss(username: str) -> bytes:
    last_err: Exception | None = None
    for base in NITTER_INSTANCES:
        url = f"{base}/{username}/rss"
        try:
            data = http_get(url)
            if b"<rss" in data[:200] or b"<?xml" in data[:50]:
                print(f"[fetch] using {base} ({len(data)} bytes)", flush=True)
                return data
        except Exception as e:
            last_err = e
            print(f"[fetch] {base} failed: {e}", flush=True)
            time.sleep(1)
    raise RuntimeError(f"all nitter instances failed: {last_err}")


def nitter_pic_to_twimg(url: str) -> str:
    m = re.search(r"/pic/(.+)$", url)
    if not m:
        return url
    decoded = urllib.parse.unquote(m.group(1))
    if decoded.startswith(("media/", "amplify_video_thumb/", "ext_tw_video_thumb/",
                           "tweet_video_thumb/")):
        return f"https://pbs.twimg.com/{decoded}"
    return url


def parse_tweet(item: ET.Element, monitored_user: str = "") -> Tweet:
    def _t(tag: str) -> str:
        el = item.find(tag)
        return (el.text or "") if el is not None else ""

    creator_el = item.find("{http://purl.org/dc/elements/1.1/}creator")
    creator = (creator_el.text or "").lstrip("@") if creator_el is not None else ""

    title = _t("title")
    desc = _t("description")
    link = _t("link").replace("nitter.net", "x.com").rsplit("#", 1)[0]
    guid = _t("guid")
    pub_date = _t("pubDate")

    is_retweet = title.startswith("RT by ")
    is_reply = title.startswith("R to ")

    raw_imgs = re.findall(r'<img[^>]+src="([^"]+)"', desc)
    images = [nitter_pic_to_twimg(u) for u in raw_imgs]
    raw_vids = re.findall(r'<video[^>]+src="([^"]+)"', desc)
    videos = [nitter_pic_to_twimg(u) for u in raw_vids]

    text = re.sub(r"<[^>]+>", "", desc)
    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if is_retweet:
        text = re.sub(r"^RT by [^:]+:\s*", "", text)
    elif is_reply:
        text = re.sub(r"^R to [^:]+:\s*", "", text)

    return Tweet(
        guid=guid,
        title=title,
        author=creator,
        pub_date=pub_date,
        link=link,
        description_html=desc,
        is_retweet=is_retweet,
        is_reply=is_reply,
        images=images,
        videos=videos,
        text=text,
        monitored_user=monitored_user,
    )


def load_state() -> dict[str, int]:
    """State maps tweet GUID -> unix timestamp of when we first detected it.

    Backwards-compatible: if state file is the old list-of-strings format,
    convert each entry to detected_at=0 (unknown).
    """
    if not STATE_FILE.exists():
        return {}
    try:
        raw = json.loads(STATE_FILE.read_text())
        if isinstance(raw, list):
            return {g: 0 for g in raw if g}
        if isinstance(raw, dict):
            return {str(k): int(v) for k, v in raw.items()}
        return {}
    except Exception:
        return {}


def save_state(seen: dict[str, int]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    items = list(seen.items())[-MAX_SEEN:]
    payload = {k: v for k, v in items}
    STATE_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def is_likely_chinese(text: str) -> bool:
    if not text:
        return True
    cn = sum(1 for c in text if "一" <= c <= "鿿")
    letters = sum(1 for c in text if c.isalpha() and ord(c) < 128)
    return cn >= 4 and cn >= letters


def analyze_text(text: str) -> dict:
    """Send text to DeepSeek for classification + summary + translation.

    Uses a section-marker format instead of JSON to avoid escaping issues
    with multi-line translated content. Returns dict with keys:
    is_chinese, is_quote, summary, translation. Empty dict on failure.
    """
    if not DEEPSEEK_API_KEY or not text.strip():
        return {}

    system_prompt = (
        "你是社交媒体内容处理助手，处理推特/X 的推文。\n"
        "用户给你一段推文文本，你需要分析并按下面的固定格式输出，每个标记单独成行：\n\n"
        "===IS_CHINESE===\n"
        "true 或 false（原文主体是否已经是中文）\n"
        "===IS_QUOTE===\n"
        "true 或 false（是否为引用推文：含 x.com/twitter.com 的 status 链接，且作者添加了自己的评论文字）\n"
        "===SUMMARY===\n"
        f"中文一句话概括，原文长度 >= {SUMMARY_THRESHOLD} 字符时才填，否则留空。\n"
        "点出关键信息（在做什么/跟谁/什么主题），不超过 80 字。\n"
        "===TRANSLATION===\n"
        "完整中文翻译。原文已是中文则留空。\n"
        "翻译要求：自然口语风格，保留原语气情绪和网络梗，保留 emoji、@用户名、#标签、链接原样不译。\n"
        "===END===\n\n"
        "严格按上述格式输出，不要加代码块标记、解释或多余空行。"
    )

    body = {
        "model": DEEPSEEK_MODEL,
        "max_tokens": 1500,
        "temperature": 0.3,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text[:3000]},
        ],
    }
    headers = {"Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
    status, resp_text = http_post_json(
        f"{DEEPSEEK_BASE}/chat/completions", body, headers, timeout=45
    )
    if status == 0 or status >= 300:
        print(f"[analyze] HTTP {status}: {resp_text[:300]}", flush=True)
        return {}

    try:
        data = json.loads(resp_text)
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[analyze] api parse error: {e}", flush=True)
        return {}

    return parse_sections(content)


def translate_with_google(text: str) -> dict:
    """Free Google Translate fallback (gtx endpoint, no API key required).

    Used when DeepSeek fails. Returns the same dict shape as analyze_text
    but is_quote/summary are best-effort (Google can't classify).
    """
    if not text.strip():
        return {}
    try:
        url = (
            "https://translate.googleapis.com/translate_a/single"
            "?client=gtx&sl=auto&tl=zh-CN&dt=t&q="
            + urllib.parse.quote(text[:4500])
        )
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        print(f"[google] error: {e}", flush=True)
        return {}

    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        return {}

    chunks = [c[0] for c in data[0] if isinstance(c, list) and c and c[0]]
    translation = "".join(chunks).strip()
    detected_lang = data[2] if len(data) > 2 and isinstance(data[2], str) else ""
    is_chinese = detected_lang.startswith("zh") or is_likely_chinese(text)

    print(f"[google] fallback ok ({detected_lang} → zh-CN)", flush=True)
    return {
        "is_chinese": is_chinese,
        "is_quote": False,
        "summary": "",
        "translation": "" if is_chinese else translation,
        "source": "google",
    }


def parse_sections(content: str) -> dict:
    """Parse the section-marker format from analyze_text.

    Uses re.split on the marker pattern itself so adjacent or empty
    sections can't bleed into each other.
    """
    parts = re.split(
        r"===\s*(IS_CHINESE|IS_QUOTE|SUMMARY|TRANSLATION|END)\s*===",
        content,
        flags=re.IGNORECASE,
    )
    if len(parts) <= 1:
        print(f"[analyze] no markers found in: {content[:200]}", flush=True)
        return {}

    sections: dict[str, str] = {}
    for i in range(1, len(parts) - 1, 2):
        marker = parts[i].upper()
        value = parts[i + 1].strip() if i + 1 < len(parts) else ""
        if marker == "END":
            continue
        sections[marker] = value

    def to_bool(s: str) -> bool:
        return s.strip().lower().startswith("true")

    return {
        "is_chinese": to_bool(sections.get("IS_CHINESE", "false")),
        "is_quote": to_bool(sections.get("IS_QUOTE", "false")),
        "summary": sections.get("SUMMARY", "").strip(),
        "translation": sections.get("TRANSLATION", "").strip(),
    }


# Base palette: blue=original, purple=RT, green=quote, orange=reply.
# Each artist gets a slightly different shade for visual separation.
ARTIST_COLOR_PALETTES = [
    {"original": 0x1DA1F2, "retweet": 0x9146FF, "quote": 0x00C875, "reply": 0xFF8C00},
    {"original": 0xFF6B9D, "retweet": 0xC44569, "quote": 0xF8B500, "reply": 0xE17055},
]


def determine_type(tw: Tweet, analysis: dict) -> tuple[str, str, int]:
    """Returns (emoji, label, color)."""
    info = ARTIST_DISPLAY.get(tw.monitored_user.lower(), {"color_offset": 0})
    palette = ARTIST_COLOR_PALETTES[info["color_offset"] % len(ARTIST_COLOR_PALETTES)]
    if tw.is_retweet:
        return ("🔁", "转发", palette["retweet"])
    if tw.is_reply:
        return ("💬", "回复", palette["reply"])
    if analysis.get("is_quote"):
        return ("🔗", "引用", palette["quote"])
    return ("📌", "原创", palette["original"])


def _pub_to_iso(rfc822: str) -> str:
    try:
        return parsedate_to_datetime(rfc822).isoformat()
    except Exception:
        return ""


def build_embeds(tw: Tweet, analysis: dict) -> list[dict]:
    emoji, label, color = determine_type(tw, analysis)
    monitored = tw.monitored_user
    artist_name = display_for(monitored)

    if tw.is_retweet:
        title = f"{emoji} {label} · {artist_name} 转了 @{tw.author}"
        author_for_footer = tw.author or monitored
    else:
        title = f"{emoji} {label} · {artist_name}"
        author_for_footer = monitored

    is_chinese = analysis.get("is_chinese") if analysis else is_likely_chinese(tw.text)
    summary = analysis.get("summary", "") if analysis else ""
    translation = analysis.get("translation", "") if analysis else ""
    source = analysis.get("source", "deepseek") if analysis else "none"

    parts: list[str] = []

    # Discord <t:UNIX:f> renders in viewer's local timezone, <t:UNIX:R> shows relative.
    # For retweets we show TWO times: detected-at (≈ when she RT'd, ±10min) AND original pubDate.
    # For everything else there's only one time so we keep it simple.
    try:
        orig_ts = int(parsedate_to_datetime(tw.pub_date).timestamp())
    except Exception:
        orig_ts = 0

    if tw.is_retweet and tw.detected_at:
        parts.append(
            f"🔁 转推时间：<t:{tw.detected_at}:f> · <t:{tw.detected_at}:R>  "
            f"`±10分钟近似`"
        )
        if orig_ts:
            parts.append(f"📅 原推发布：<t:{orig_ts}:f>")
    elif orig_ts:
        parts.append(f"🕐 发布时间：<t:{orig_ts}:f> · <t:{orig_ts}:R>")

    if summary:
        parts.append(f"**📋 内容摘要**\n{summary}")

    if is_chinese:
        parts.append(f"**📝 原文**\n{tw.text[:1500]}")
    elif translation:
        label_tag = "（机器翻译）" if source == "google" else ""
        parts.append(f"**📝 中文译文{label_tag}**\n{translation[:1500]}")
    else:
        parts.append(f"**📝 原文**\n{tw.text[:1500]}")

    description = "\n\n".join(parts) if parts else "(无文本内容)"

    main = {
        "title": title,
        "description": description,
        "url": tw.link,
        "color": color,
        "timestamp": _pub_to_iso(tw.pub_date),
        "footer": {"text": f"@{author_for_footer}"},
    }
    if tw.images:
        main["image"] = {"url": tw.images[0]}

    embeds = [main]
    for img in tw.images[1:4]:
        embeds.append({"url": tw.link, "image": {"url": img}, "color": color})
    return embeds


def push_discord(tw: Tweet, analysis: dict) -> bool:
    embeds = build_embeds(tw, analysis)
    payload: dict = {"embeds": embeds}

    content_parts: list[str] = []
    if DISCORD_USER_ID:
        content_parts.append(f"<@{DISCORD_USER_ID}>")
    if tw.videos:
        content_parts.append(f"📹 含视频，建议直接看原推: {tw.link}")
    if content_parts:
        payload["content"] = " ".join(content_parts)

    if DISCORD_USER_ID:
        payload["allowed_mentions"] = {"parse": [], "users": [DISCORD_USER_ID]}

    status, body = http_post_json(DISCORD_WEBHOOK, payload)
    if status >= 300:
        print(f"[discord] failed {status}: {body[:300]}", flush=True)
        return False
    return True


def main() -> int:
    bootstrap = os.environ.get("BOOTSTRAP", "").lower() in ("1", "true", "yes")

    if not USERNAMES:
        print("[error] TWITTER_USERNAMES is empty", flush=True)
        return 1

    print(f"[start] monitoring {len(USERNAMES)} accounts: {USERNAMES}", flush=True)

    all_tweets: list[Tweet] = []
    for user in USERNAMES:
        try:
            raw = fetch_rss(user)
        except Exception as e:
            print(f"[fetch] giving up on {user}: {e}", flush=True)
            continue
        items = ET.fromstring(raw).findall(".//item")
        per_user = [parse_tweet(it, monitored_user=user) for it in items]
        per_user = [t for t in per_user if t.guid]
        print(f"[parse] @{user}: {len(per_user)} items", flush=True)
        all_tweets.extend(per_user)
        time.sleep(2)

    all_tweets.sort(key=lambda t: t.pub_date)

    seen = load_state()
    now = int(time.time())
    new_tweets = [t for t in all_tweets if t.guid not in seen]
    for t in new_tweets:
        t.detected_at = now
    print(f"[diff] {len(new_tweets)} new of {len(all_tweets)} total", flush=True)

    if bootstrap or not seen:
        for t in all_tweets:
            seen[t.guid] = now
        save_state(seen)
        print(f"[bootstrap] marked {len(all_tweets)} as seen, no push", flush=True)
        return 0

    pushed = 0
    for t in new_tweets:
        analysis: dict = {}
        if t.text:
            analysis = analyze_text(t.text)
            if not analysis:
                analysis = translate_with_google(t.text)
        ok = push_discord(t, analysis)
        if ok:
            seen[t.guid] = t.detected_at or now
            pushed += 1
            time.sleep(1)

    save_state(seen)
    print(f"[done] pushed {pushed}/{len(new_tweets)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
