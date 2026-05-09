#!/usr/bin/env python3
"""Conversational layer for #行程表.

Polls a Discord text channel via the bot REST API on every monitor tick,
interprets new user messages with DeepSeek (natural-language NLU rather
than slash commands — the user prefers free-form Chinese), and applies
the resulting intent against state/events.json.

Supported intents:
  - query  → list events for a date / range / artist
  - add    → insert a new event
  - update → set time / title / notes on an existing event
  - delete → mark cancelled (we keep the row for history)
  - other  → silently ignore

Replies go through the existing #行程表 webhook so all bot output looks
visually consistent with the scheduled cards. The bot does not delete
or react to user messages; it just answers and records that it has
processed them in state/bot_state.json (last_processed_id).

No-ops gracefully when DISCORD_BOT_TOKEN or DISCORD_ITINERARY_CHANNEL_ID
is unset, so committing this file before secrets are configured is safe.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import events as itinerary

ROOT = Path(__file__).resolve().parent
BOT_STATE_FILE = ROOT / "state" / "bot_state.json"

DISCORD_API = "https://discord.com/api/v10"
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_ID = os.environ.get("DISCORD_ITINERARY_CHANNEL_ID", "").strip()

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE = os.environ.get("DEEPSEEK_BASE", "https://api.deepseek.com")

BKK = timezone(timedelta(hours=7))
PEK = timezone(timedelta(hours=8))

# How many messages to look back per poll. Discord caps at 100. We're
# polling every ~2 min so 50 is plenty of headroom for bursty chat.
MAX_FETCH = 50

ARTIST_KEYS = {
    "jane": "janeeeyeh",
    "janeeeyeh": "janeeeyeh",
    "kao": "kaosupassara9",
    "kaosupassara9": "kaosupassara9",
}


# ---------- discord http ----------

def _discord_request(method: str, path: str,
                     body: dict | None = None,
                     query: str = "") -> tuple[int, dict | list | str]:
    if not BOT_TOKEN:
        return 0, "no_bot_token"
    url = f"{DISCORD_API}{path}"
    if query:
        url += f"?{query}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bot {BOT_TOKEN}",
            "User-Agent": "9779s-bot/1.0 (https://github.com/Qubit13L/9779s-monitor)",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            try:
                return resp.status, json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, str(e)


def list_guild_text_channels() -> list[dict]:
    """Helper used during initial setup: list channels in every guild the bot is in."""
    status, guilds = _discord_request("GET", "/users/@me/guilds")
    if status >= 300 or not isinstance(guilds, list):
        print(f"[bot] list guilds HTTP {status}: {guilds}", flush=True)
        return []
    out = []
    for g in guilds:
        gid = g.get("id")
        gname = g.get("name", "?")
        s2, channels = _discord_request("GET", f"/guilds/{gid}/channels")
        if s2 >= 300 or not isinstance(channels, list):
            continue
        for c in channels:
            if c.get("type") == 0:  # GUILD_TEXT
                out.append({
                    "guild": gname,
                    "channel_id": c.get("id"),
                    "channel_name": c.get("name"),
                })
    return out


def fetch_messages(after_id: str = "") -> list[dict]:
    if not CHANNEL_ID:
        return []
    query = f"limit={MAX_FETCH}"
    if after_id:
        query += f"&after={after_id}"
    status, data = _discord_request(
        "GET", f"/channels/{CHANNEL_ID}/messages", query=query
    )
    if status >= 300 or not isinstance(data, list):
        print(f"[bot] fetch messages HTTP {status}: {data}", flush=True)
        return []
    # Discord returns newest first; we want oldest first for processing order.
    return list(reversed(data))


# ---------- bot state ----------

def load_bot_state() -> dict:
    if not BOT_STATE_FILE.exists():
        return {}
    try:
        return json.loads(BOT_STATE_FILE.read_text())
    except Exception:
        return {}


def save_bot_state(state: dict) -> None:
    BOT_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    BOT_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------- DeepSeek intent parsing ----------

INTENT_PROMPT = """你是一个粉丝行程表助手的意图识别器。用户用自由中文跟你聊行程，
你需要把消息分类成下面 5 种意图之一，并提取参数。

意图类型：
- query：询问某天/某周/某月某艺人有什么行程
- add：新增一个事件
- update：修改一个已有事件（设具体时间、改标题、加备注、确认信息）
- delete：取消/删除一个事件
- other：闲聊或不相关

输出严格的 JSON（不要 markdown 代码块，不要解释），格式如下：
{
  "intent": "query|add|update|delete|other",
  "artist": "jane|kao|all|null",
  "date": "YYYY-MM-DD（单日，曼谷日期）或 null",
  "date_range_end": "YYYY-MM-DD 或 null（多日活动的结束日）",
  "relative": "today|tomorrow|this_week|next_week|this_month|null（用户用相对词时填）",
  "title": "活动标题（add/update 用），原样保留中英文",
  "time_bkk": "HH:MM 24小时制（曼谷时间），用户没说就 null",
  "notes": "备注信息，没有就 null",
  "match_hint": "用于 update/delete 定位事件，如 '5/17 nataraja' 或 '本周日的拍摄'"
}

参考信息：
- 今天日期（曼谷）：{today_bkk}
- 当前所在 ISO 周：{iso_week}
- 已知艺人：jane (janeeeyeh)、kao (kaosupassara9)
- 用户在北京时间，"明天"按北京当前日期 +1 计算后转曼谷日期（差不大，按北京日期填即可）

如果不确定哪种意图，倾向于 other，不要瞎猜。"""


def deepseek_parse(text: str) -> dict:
    if not DEEPSEEK_API_KEY or not text.strip():
        return {"intent": "other"}

    today_bkk = datetime.now(BKK).strftime("%Y-%m-%d")
    iso_year, iso_week, _ = datetime.now(BKK).isocalendar()
    system = INTENT_PROMPT.format(
        today_bkk=today_bkk,
        iso_week=f"{iso_year}-W{iso_week:02d}",
    )

    body = {
        "model": DEEPSEEK_MODEL,
        "max_tokens": 400,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text[:1500]},
        ],
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{DEEPSEEK_BASE}/chat/completions",
        data=data, method="POST",
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        print(f"[bot] deepseek parse error: {e}", flush=True)
        return {"intent": "other"}


# ---------- intent handlers ----------

def _resolve_artist(artist: str | None) -> list[str]:
    if not artist or artist == "null":
        return ["janeeeyeh", "kaosupassara9"]
    if artist == "all":
        return ["janeeeyeh", "kaosupassara9"]
    key = ARTIST_KEYS.get(artist.lower())
    return [key] if key else ["janeeeyeh", "kaosupassara9"]


def _resolve_date_window(intent: dict) -> tuple[str, str]:
    """Returns (start_bkk_date, end_bkk_date) inclusive."""
    today_bkk = datetime.now(BKK)
    today_str = today_bkk.strftime("%Y-%m-%d")

    rel = intent.get("relative")
    explicit = intent.get("date")
    explicit_end = intent.get("date_range_end")

    if explicit:
        return (explicit, explicit_end or explicit)

    if rel == "today":
        return (today_str, today_str)
    if rel == "tomorrow":
        d = (today_bkk + timedelta(days=1)).strftime("%Y-%m-%d")
        return (d, d)
    if rel == "this_week":
        monday = today_bkk - timedelta(days=today_bkk.weekday())
        sunday = monday + timedelta(days=6)
        return (monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))
    if rel == "next_week":
        monday = today_bkk - timedelta(days=today_bkk.weekday()) + timedelta(days=7)
        sunday = monday + timedelta(days=6)
        return (monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))
    if rel == "this_month":
        first = today_bkk.replace(day=1).strftime("%Y-%m-%d")
        # last day of month: jump to next month, subtract 1 day
        if today_bkk.month == 12:
            nxt = today_bkk.replace(year=today_bkk.year + 1, month=1, day=1)
        else:
            nxt = today_bkk.replace(month=today_bkk.month + 1, day=1)
        last = (nxt - timedelta(days=1)).strftime("%Y-%m-%d")
        return (first, last)

    # default: today
    return (today_str, today_str)


def _events_in_window(events: list[dict], usernames: list[str],
                      start: str, end: str) -> list[dict]:
    return sorted(
        [
            e for e in events
            if e["username"] in usernames
            and e["start_date_bkk"] <= end
            and (e.get("end_date_bkk") or e["start_date_bkk"]) >= start
            and not e.get("cancelled")
        ],
        key=lambda e: e["start_date_bkk"],
    )


def _find_event(events: list[dict], hint: str, artist_filter: list[str]) -> dict | None:
    """Best-effort fuzzy match for update/delete by date + keyword."""
    hint = (hint or "").lower()
    # extract date pattern like 5/17 or 05-17 or 2026-05-17
    date_match = re.search(r"(\d{4}-)?(\d{1,2})[-/](\d{1,2})", hint)
    target_date = None
    if date_match:
        y = date_match.group(1) or f"{datetime.now(BKK).year}-"
        target_date = f"{y}{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
    keywords = [w for w in re.split(r"\s+", hint) if w and not re.match(r"^\d", w)]

    candidates = [e for e in events if e["username"] in artist_filter and not e.get("cancelled")]
    if target_date:
        candidates = [
            e for e in candidates
            if e["start_date_bkk"] <= target_date <= (e.get("end_date_bkk") or e["start_date_bkk"])
        ]
    if keywords:
        scored = []
        for e in candidates:
            blob = f"{e['title']} {e.get('title_zh', '')}".lower()
            score = sum(1 for k in keywords if k in blob)
            if score:
                scored.append((score, e))
        if scored:
            scored.sort(key=lambda x: -x[0])
            return scored[0][1]
    return candidates[0] if candidates else None


def reply(text: str = "", embeds: list[dict] | None = None) -> bool:
    payload: dict = {}
    if text:
        payload["content"] = text
    if embeds:
        payload["embeds"] = embeds
    if not payload:
        return False
    return itinerary.push_to_itinerary(payload)


def handle_query(intent: dict, data: dict) -> None:
    usernames = _resolve_artist(intent.get("artist"))
    start, end = _resolve_date_window(intent)
    matched = _events_in_window(data["events"], usernames, start, end)

    label = (
        start if start == end
        else f"{start} 到 {end}"
    )
    if not matched:
        names = "/".join(itinerary._name(u) for u in usernames)
        reply(f"📭 {label}（曼谷）{names} 暂无行程")
        return

    # group by artist for nicer display
    by_user: dict[str, list[dict]] = {}
    for e in matched:
        by_user.setdefault(e["username"], []).append(e)

    embeds = []
    for username, evs in by_user.items():
        lines = [itinerary._line(ev) for ev in evs]
        no_time_count = sum(1 for ev in evs if not ev.get("start_time_bkk"))
        footer_extra = f" · 其中 {no_time_count} 项暂无具体时间" if no_time_count else ""
        embeds.append({
            "title": f"🤖 {itinerary._name(username)} · {label}",
            "description": "\n".join(lines),
            "color": itinerary._color(username),
            "footer": {"text": f"🎯 公开 · 🔒 保密{footer_extra}"},
        })
    reply(embeds=embeds)


def handle_add(intent: dict, data: dict) -> None:
    title = (intent.get("title") or "").strip()
    artist_list = _resolve_artist(intent.get("artist"))
    if not title or len(artist_list) != 1:
        reply("⚠️ 添加事件需要明确告诉我：哪位艺人 + 哪一天 + 标题（时间可选）")
        return
    username = artist_list[0]
    date = intent.get("date")
    if not date:
        rel = intent.get("relative")
        if rel == "today":
            date = datetime.now(BKK).strftime("%Y-%m-%d")
        elif rel == "tomorrow":
            date = (datetime.now(BKK) + timedelta(days=1)).strftime("%Y-%m-%d")
    if not date:
        reply("⚠️ 添加事件需要日期（YYYY-MM-DD 或 '5/12' 或 '明天'）")
        return

    end_date = intent.get("date_range_end") or date
    time_bkk = intent.get("time_bkk") or ""
    notes = intent.get("notes") or ""

    eid = itinerary.event_id(username, date, title)
    # If event already exists, treat as update
    for e in data["events"]:
        if e["id"] == eid:
            return _apply_update(e, intent, data, label="（已存在，已合并更新）")

    is_conf = any(k in title.upper() for k in ("CONFIDENTIAL", "CONFIDENTLE", "TBA"))
    new_ev = {
        "id": eid,
        "username": username,
        "title": title,
        "title_zh": "",
        "type": "confidential" if is_conf else "public",
        "start_date_bkk": date,
        "end_date_bkk": end_date,
        "start_time_bkk": time_bkk,
        "raw_text": title,
        "notes": notes,
        "source": {"platform": "user_input", "url": "", "post_caption": "用户在 Discord 中添加"},
        "reminded_24h": False,
        "reminded_2h": False,
        "created_at": int(time.time()),
        "updated_at": int(time.time()),
    }
    data["events"].append(new_ev)
    itinerary.save_events(data)

    embed = {
        "title": "🤖 ✅ 已添加事件",
        "description": itinerary._line(new_ev),
        "color": itinerary._color(username),
        "footer": {"text": "可继续发消息更新时间或备注" if not time_bkk else "已设具体时间，将启用 T-24h / T-2h 提醒"},
    }
    reply(embeds=[embed])


def _apply_update(ev: dict, intent: dict, data: dict, label: str = "") -> None:
    fields_changed = []
    if intent.get("time_bkk"):
        old = ev.get("start_time_bkk") or "(无)"
        ev["start_time_bkk"] = intent["time_bkk"]
        fields_changed.append(f"时间 {old} → {intent['time_bkk']} (曼谷)")
        # Reset reminder flags when time is set/changed so the new time can fire reminders
        ev["reminded_24h"] = False
        ev["reminded_2h"] = False
    if intent.get("title") and intent["title"] != ev["title"]:
        old = ev["title"]
        ev["title"] = intent["title"]
        fields_changed.append(f"标题 {old} → {intent['title']}")
    if intent.get("notes"):
        ev["notes"] = intent["notes"]
        fields_changed.append(f"备注：{intent['notes']}")
    if intent.get("date_range_end"):
        ev["end_date_bkk"] = intent["date_range_end"]
        fields_changed.append(f"结束日 → {intent['date_range_end']}")

    ev["updated_at"] = int(time.time())
    itinerary.save_events(data)

    embed = {
        "title": f"🤖 ✏️ 已更新事件{label}",
        "description": itinerary._line(ev) + "\n\n" + "\n".join(f"· {c}" for c in fields_changed),
        "color": itinerary._color(ev["username"]),
    }
    reply(embeds=[embed])


def handle_update(intent: dict, data: dict) -> None:
    artists = _resolve_artist(intent.get("artist"))
    hint = intent.get("match_hint") or intent.get("title") or intent.get("date") or ""
    ev = _find_event(data["events"], hint, artists)
    if not ev:
        reply(f"⚠️ 没找到匹配的事件（线索：{hint}）。可以发更具体的日期或标题。")
        return
    _apply_update(ev, intent, data)


def handle_delete(intent: dict, data: dict) -> None:
    artists = _resolve_artist(intent.get("artist"))
    hint = intent.get("match_hint") or intent.get("date") or intent.get("title") or ""
    ev = _find_event(data["events"], hint, artists)
    if not ev:
        reply(f"⚠️ 没找到要删除的事件（线索：{hint}）")
        return
    ev["cancelled"] = True
    ev["updated_at"] = int(time.time())
    itinerary.save_events(data)
    embed = {
        "title": "🤖 🗑️ 已取消事件",
        "description": itinerary._line(ev),
        "color": 0x808080,
    }
    reply(embeds=[embed])


# ---------- main poll ----------

def poll() -> None:
    if not BOT_TOKEN or not CHANNEL_ID:
        return  # bot not configured yet, no-op
    if not DEEPSEEK_API_KEY:
        print("[bot] DEEPSEEK_API_KEY missing, skipping NLU", flush=True)
        return

    state = load_bot_state()
    last_id = state.get("last_processed_id", "")

    messages = fetch_messages(after_id=last_id)
    if not messages:
        return

    bot_user_id = state.get("bot_user_id", "")
    if not bot_user_id:
        s, me = _discord_request("GET", "/users/@me")
        if s < 300 and isinstance(me, dict):
            bot_user_id = me.get("id", "")
            state["bot_user_id"] = bot_user_id

    data = itinerary.load_events()
    processed = 0

    for msg in messages:
        msg_id = msg.get("id", "")
        author = msg.get("author") or {}
        # Skip our own bot's messages, webhook messages, and other bots
        if msg.get("webhook_id"):
            state["last_processed_id"] = msg_id
            continue
        if author.get("bot") or author.get("id") == bot_user_id:
            state["last_processed_id"] = msg_id
            continue
        content = (msg.get("content") or "").strip()
        if not content:
            state["last_processed_id"] = msg_id
            continue

        print(f"[bot] processing: {content[:80]}", flush=True)
        intent = deepseek_parse(content)
        kind = intent.get("intent", "other")

        try:
            if kind == "query":
                handle_query(intent, data)
            elif kind == "add":
                handle_add(intent, data)
            elif kind == "update":
                handle_update(intent, data)
            elif kind == "delete":
                handle_delete(intent, data)
            # 'other' silently ignored
        except Exception as e:
            print(f"[bot] handler error for {kind}: {e}", flush=True)
            reply(f"⚠️ 处理消息时出错：{e}")

        state["last_processed_id"] = msg_id
        processed += 1
        # Reload events in case a previous handler in this batch wrote to disk
        data = itinerary.load_events()
        time.sleep(0.5)

    save_bot_state(state)
    if processed:
        print(f"[bot] processed {processed} messages", flush=True)


# ---------- CLI helpers (used during initial setup) ----------

def cli_main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: bot.py {channels|poll|whoami}", flush=True)
        return 1
    cmd = argv[1]
    if cmd == "channels":
        chans = list_guild_text_channels()
        for c in chans:
            print(f"{c['guild']} #{c['channel_name']} → {c['channel_id']}")
        return 0
    if cmd == "whoami":
        s, me = _discord_request("GET", "/users/@me")
        print(f"HTTP {s}: {me}")
        return 0
    if cmd == "poll":
        poll()
        return 0
    print(f"unknown command: {cmd}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv))
