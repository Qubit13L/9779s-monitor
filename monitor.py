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

USERNAME = os.environ["TWITTER_USERNAME"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")


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
        return e.code, e.read().decode("utf-8", "ignore")


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


def parse_tweet(item: ET.Element) -> Tweet:
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
    if status >= 300:
        print(f"[analyze] HTTP {status}: {resp_text[:300]}", flush=True)
        return {}

    try:
        data = json.loads(resp_text)
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[analyze] api parse error: {e}", flush=True)
        return {}

    return parse_sections(content)


def parse_sections(content: str) -> dict:
    """Parse the section-marker format from analyze_text."""
    sections: dict[str, str] = {}
    pattern = re.compile(
        r"===\s*(IS_CHINESE|IS_QUOTE|SUMMARY|TRANSLATION)\s*===\s*(.*?)"
        r"(?=\n===|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    for m in pattern.finditer(content):
        sections[m.group(1).upper()] = m.group(2).strip()

    def to_bool(s: str) -> bool:
        return s.strip().lower().startswith("true")

    return {
        "is_chinese": to_bool(sections.get("IS_CHINESE", "false")),
        "is_quote": to_bool(sections.get("IS_QUOTE", "false")),
        "summary": sections.get("SUMMARY", "").strip(),
        "translation": sections.get("TRANSLATION", "").strip(),
    }


def determine_type(tw: Tweet, analysis: dict) -> tuple[str, str, int]:
    """Returns (emoji, label, color)."""
    if tw.is_retweet:
        return ("🔁", "转发", 0x9146FF)
    if tw.is_reply:
        return ("💬", "回复", 0xFF8C00)
    if analysis.get("is_quote"):
        return ("🔗", "引用", 0x00C875)
    return ("📌", "原创", 0x1DA1F2)


def _pub_to_iso(rfc822: str) -> str:
    try:
        return parsedate_to_datetime(rfc822).isoformat()
    except Exception:
        return ""


def build_embeds(tw: Tweet, analysis: dict) -> list[dict]:
    emoji, label, color = determine_type(tw, analysis)

    if tw.is_retweet:
        title = f"{emoji} {label} · 原作者 @{tw.author}"
        author_for_footer = tw.author or USERNAME
    elif tw.is_reply:
        title = f"{emoji} {label} · @{USERNAME}"
        author_for_footer = USERNAME
    else:
        title = f"{emoji} {label} · @{USERNAME}"
        author_for_footer = USERNAME

    is_chinese = analysis.get("is_chinese") if analysis else is_likely_chinese(tw.text)
    summary = analysis.get("summary", "") if analysis else ""
    translation = analysis.get("translation", "") if analysis else ""

    parts: list[str] = []
    if summary:
        parts.append(f"**📋 内容摘要**\n{summary}")

    if is_chinese:
        parts.append(f"**📝 原文**\n{tw.text[:1500]}")
    elif translation:
        parts.append(f"**📝 中文译文**\n{translation[:1500]}")
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
    payload = {"embeds": embeds}
    if tw.videos:
        payload["content"] = f"📹 含视频，建议直接看原推: {tw.link}"

    status, body = http_post_json(DISCORD_WEBHOOK, payload)
    if status >= 300:
        print(f"[discord] failed {status}: {body[:300]}", flush=True)
        return False
    return True


def main() -> int:
    bootstrap = os.environ.get("BOOTSTRAP", "").lower() in ("1", "true", "yes")

    raw = fetch_rss(USERNAME)
    items = ET.fromstring(raw).findall(".//item")
    print(f"[parse] got {len(items)} items", flush=True)

    tweets = [parse_tweet(it) for it in items]
    tweets = [t for t in tweets if t.guid]
    tweets.sort(key=lambda t: t.pub_date)

    seen = load_state()
    new_tweets = [t for t in tweets if t.guid not in seen]
    print(f"[diff] {len(new_tweets)} new of {len(tweets)} total", flush=True)

    if bootstrap or not seen:
        for t in tweets:
            seen.add(t.guid)
        save_state(seen)
        print(f"[bootstrap] marked {len(tweets)} as seen, no push", flush=True)
        return 0

    pushed = 0
    for t in new_tweets:
        analysis = analyze_text(t.text) if t.text else {}
        ok = push_discord(t, analysis)
        if ok:
            seen.add(t.guid)
            pushed += 1
            time.sleep(1)

    save_state(seen)
    print(f"[done] pushed {pushed}/{len(new_tweets)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
