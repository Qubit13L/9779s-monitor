#!/usr/bin/env python3
"""Itinerary tracking layer for 9779s-monitor.

Stores upcoming work events for monitored artists in state/events.json,
fires per-event reminders (T-24h / T-2h) into a separate Discord channel
(#行程表), and pushes a weekly digest every Monday 12:00 Beijing time.

Time conventions:
  - Event dates are in Bangkok time (UTC+7); artists are based in Thailand.
  - Stored timestamps are unix UTC.
  - Discord renders <t:UNIX:f> in the viewer's local TZ (Beijing for the user).

Reminder windows are catch-up tolerant: if a tick is missed (network blip,
GH Actions queue), the next tick still fires the reminder as long as the
event hasn't started yet.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVENTS_FILE = ROOT / "state" / "events.json"

BKK = timezone(timedelta(hours=7))
PEK = timezone(timedelta(hours=8))

# start_time_bkk is intentionally optional. Events without a confirmed time
# are stored date-only and skipped by the per-event reminder logic — they
# still appear in monthly/weekly digests. Don't invent a placeholder time:
# the user has to be able to trust any time we show.

DISCORD_ITINERARY_WEBHOOK = os.environ.get("DISCORD_ITINERARY_WEBHOOK_URL", "").strip()
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "").strip()

ARTIST_NAME = {
    "janeeeyeh": "Jane",
    "kaosupassara9": "Kao",
}
ARTIST_COLOR = {
    "janeeeyeh": 0x4A90E2,
    "kaosupassara9": 0xFF6B9D,
}


# ---------- storage ----------

def load_events() -> dict:
    if not EVENTS_FILE.exists():
        return {"events": [], "weekly_pushed": {}, "monthly_pushed": {}}
    try:
        return json.loads(EVENTS_FILE.read_text())
    except Exception as e:
        print(f"[itinerary] failed to load events.json: {e}", flush=True)
        return {"events": [], "weekly_pushed": {}, "monthly_pushed": {}}


def save_events(data: dict) -> None:
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EVENTS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def slug(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:40] or "event"


def event_id(username: str, start_date_bkk: str, title: str) -> str:
    return f"{username}_{start_date_bkk}_{slug(title)}"


# ---------- time helpers ----------

def event_start_utc(ev: dict) -> datetime | None:
    """Returns the event's UTC start datetime, or None if no time is known.

    Events with no confirmed start_time_bkk are date-only and not eligible
    for T-24h/T-2h reminders — caller must handle None.
    """
    time_str = ev.get("start_time_bkk")
    if not time_str:
        return None
    date_str = ev["start_date_bkk"]
    naive = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=BKK).astimezone(timezone.utc)


def event_date_midnight_utc(ev: dict) -> datetime:
    """Returns the UTC instant of midnight at the event's start date (BKK)."""
    naive = datetime.strptime(ev["start_date_bkk"], "%Y-%m-%d")
    return naive.replace(tzinfo=BKK).astimezone(timezone.utc)


def day_before_noon_utc(ev: dict) -> datetime:
    """When the 'tomorrow you have X' reminder for a date-only event should fire.

    Returns: previous day 12:00 BKK (= 13:00 Beijing) in UTC. Stable, predictable,
    not late at night. The tick uses this as a 'fire at or after' threshold.
    """
    midnight = datetime.strptime(ev["start_date_bkk"], "%Y-%m-%d")
    day_before = midnight - timedelta(days=1)
    naive = day_before.replace(hour=12, minute=0)
    return naive.replace(tzinfo=BKK).astimezone(timezone.utc)


def date_range_label(ev: dict) -> str:
    s = ev["start_date_bkk"]
    e = ev.get("end_date_bkk") or s
    if s == e:
        return f"{s[5:7]}/{s[8:10]}"
    if s[:7] == e[:7]:
        return f"{s[5:7]}/{s[8:10]}–{e[8:10]}"
    return f"{s[5:7]}/{s[8:10]}–{e[5:7]}/{e[8:10]}"


def _iso_week_key(d: datetime) -> str:
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_monday(d: datetime) -> datetime:
    monday = d - timedelta(days=d.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


# ---------- discord ----------

def _http_post(url: str, body: dict, timeout: int = 30) -> tuple[int, str]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "9779s-monitor-itinerary/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, str(e)


def push_to_itinerary(payload: dict) -> bool:
    if not DISCORD_ITINERARY_WEBHOOK:
        print("[itinerary] DISCORD_ITINERARY_WEBHOOK_URL not set, skipping", flush=True)
        return False
    status, body = _http_post(DISCORD_ITINERARY_WEBHOOK, payload)
    if status >= 300 or status == 0:
        print(f"[itinerary] discord HTTP {status}: {body[:200]}", flush=True)
        return False
    return True


def _name(username: str) -> str:
    return ARTIST_NAME.get(username.lower(), f"@{username}")


def _color(username: str) -> int:
    return ARTIST_COLOR.get(username.lower(), 0x808080)


def _emoji(ev: dict) -> str:
    return "🔒" if ev.get("type") == "confidential" else "🎯"


def _line(ev: dict) -> str:
    emoji = _emoji(ev)
    date_label = date_range_label(ev)
    title = ev["title"]
    zh = ev.get("title_zh", "")
    if zh and zh != title:
        return f"{emoji} **{date_label}** · {title} · {zh}"
    return f"{emoji} **{date_label}** · {title}"


def build_monthly_embed(username: str, year_month: str, events: list[dict]) -> dict:
    name = _name(username)
    color = _color(username)
    y, m = year_month.split("-")
    public_count = sum(1 for e in events if e.get("type") == "public")
    confidential_count = len(events) - public_count

    lines = [_line(ev) for ev in events]
    description = "\n".join(lines) if lines else "(本月暂无行程)"
    description += (
        f"\n\n— 共 {len(events)} 项："
        f"🎯 公开活动 {public_count} · 🔒 保密/待定 {confidential_count}"
    )

    return {
        "title": f"🗓️ {name} · {y} 年 {int(m)} 月行程",
        "description": description,
        "color": color,
        "footer": {"text": "🎯 公开活动 · 🔒 保密拍摄 / 待公布  ｜ 时间按曼谷"},
    }


def build_weekly_embed(username: str, week_start_bkk: str, events: list[dict]) -> dict:
    name = _name(username)
    color = _color(username)
    week_end = (datetime.strptime(week_start_bkk, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")
    week_label = f"{week_start_bkk[5:].replace('-', '/')} – {week_end[5:].replace('-', '/')}"

    lines = [_line(ev) for ev in events]
    description = "\n".join(lines) if lines else "(本周暂无行程安排)"

    return {
        "title": f"📅 {name} · 本周行程（{week_label}）",
        "description": description,
        "color": color,
        "footer": {"text": "🎯 公开活动 · 🔒 保密 / 待定  ｜ 每周一 12:00 北京推送"},
    }


def build_reminder_embed(ev: dict, kind: str) -> dict:
    """kind is one of:
      24h        — T-24h reminder, event has a known time
      2h         — T-2h reminder, event has a known time
      tomorrow   — date-only public event, fires day-before 12:00 BKK
    """
    username = ev["username"]
    name = _name(username)
    color = _color(username)

    emoji = _emoji(ev)
    title = ev["title"]
    zh = ev.get("title_zh", "")

    when_label = {"24h": "明天", "2h": "2 小时后", "tomorrow": "明天"}.get(kind, "")

    parts: list[str] = []
    if kind in ("24h", "2h"):
        start_utc = event_start_utc(ev)
        ts = int(start_utc.timestamp())
        parts.append(f"🕐 开始时间：<t:{ts}:f> · <t:{ts}:R>")
    else:
        # date-only: show the date label, no fabricated time
        parts.append(f"📅 日期（曼谷）：{date_range_label(ev)}")

    if zh and zh != title:
        parts.append(f"📌 活动：**{title}**\n{zh}")
    else:
        parts.append(f"📌 活动：**{title}**")
    if ev.get("notes"):
        parts.append(f"📝 备注：{ev['notes']}")

    footer = {
        "24h": "提醒类型：T-24h",
        "2h": "提醒类型：T-2h",
        "tomorrow": "提醒类型：前一天预告（暂无具体时间）",
    }.get(kind, "")

    return {
        "title": f"⏰ {emoji} {name} · {when_label}",
        "description": "\n\n".join(parts),
        "color": color,
        "footer": {"text": footer},
    }


def _wrap_with_mention(payload_embeds: list[dict]) -> dict:
    payload: dict = {"embeds": payload_embeds}
    if DISCORD_USER_ID:
        payload["content"] = f"<@{DISCORD_USER_ID}>"
        payload["allowed_mentions"] = {"parse": [], "users": [DISCORD_USER_ID]}
    return payload


def push_monthly_summary(username: str, year_month: str, events: list[dict]) -> bool:
    relevant = [
        e for e in events
        if e["username"] == username and e["start_date_bkk"][:7] == year_month
    ]
    relevant.sort(key=lambda e: e["start_date_bkk"])
    embed = build_monthly_embed(username, year_month, relevant)
    return push_to_itinerary(_wrap_with_mention([embed]))


def push_weekly_digest(username: str, events: list[dict],
                       reference_dt: datetime | None = None) -> bool:
    today_bkk = (reference_dt or datetime.now(timezone.utc)).astimezone(BKK)
    monday_bkk = _week_monday(today_bkk)
    sunday_bkk = monday_bkk + timedelta(days=6)
    monday_str = monday_bkk.strftime("%Y-%m-%d")
    sunday_str = sunday_bkk.strftime("%Y-%m-%d")

    relevant = [
        e for e in events
        if e["username"] == username
        and e["start_date_bkk"] <= sunday_str
        and (e.get("end_date_bkk") or e["start_date_bkk"]) >= monday_str
    ]
    relevant.sort(key=lambda e: e["start_date_bkk"])
    embed = build_weekly_embed(username, monday_str, relevant)
    return push_to_itinerary(_wrap_with_mention([embed]))


def push_reminder(ev: dict, kind: str) -> bool:
    embed = build_reminder_embed(ev, kind)
    return push_to_itinerary(_wrap_with_mention([embed]))


# ---------- main tick ----------

def tick(now_unix: int | None = None) -> None:
    """One tick of the itinerary engine — call this from monitor.py.

    Two responsibilities, both idempotent:
      1. Per-event reminders: if T-24h or T-2h window has arrived and the
         event hasn't started yet, fire the reminder once.
      2. Weekly digest: every Monday from 12:00 Beijing onwards (until
         end of Monday), push a weekly roll-up per artist, once per ISO week.
    """
    if not DISCORD_ITINERARY_WEBHOOK:
        return

    data = load_events()
    events = data.get("events", [])
    if not events:
        return

    now = datetime.fromtimestamp(now_unix or time.time(), tz=timezone.utc)
    changed = False

    # ---- per-event reminders ----
    for ev in events:
        if ev.get("cancelled"):
            continue
        try:
            start_utc = event_start_utc(ev)
        except Exception as e:
            print(f"[itinerary] bad event {ev.get('id')}: {e}", flush=True)
            continue

        if start_utc is not None:
            # Has confirmed time → T-24h and T-2h precise reminders
            if now >= start_utc:
                continue

            if not ev.get("reminded_24h") and now >= (start_utc - timedelta(hours=24)):
                if push_reminder(ev, "24h"):
                    ev["reminded_24h"] = True
                    ev["updated_at"] = int(now.timestamp())
                    changed = True
                    time.sleep(1)

            if not ev.get("reminded_2h") and now >= (start_utc - timedelta(hours=2)):
                if push_reminder(ev, "2h"):
                    ev["reminded_2h"] = True
                    ev["updated_at"] = int(now.timestamp())
                    changed = True
                    time.sleep(1)
        else:
            # Date-only. Only public events get a day-before predictive reminder;
            # confidential shoots stay out of the per-event flow (they still show
            # up in monthly/weekly digests).
            if ev.get("type") != "public":
                continue
            event_midnight = event_date_midnight_utc(ev)
            if now >= event_midnight:
                continue  # day of event or later, no preview
            if not ev.get("reminded_tomorrow") and now >= day_before_noon_utc(ev):
                if push_reminder(ev, "tomorrow"):
                    ev["reminded_tomorrow"] = True
                    ev["updated_at"] = int(now.timestamp())
                    changed = True
                    time.sleep(1)

    # ---- weekly digest: every Mon 12:00 Beijing onwards, once per ISO week ----
    now_bjt = now.astimezone(PEK)
    if now_bjt.weekday() == 0 and now_bjt.hour >= 12:
        iso_week = _iso_week_key(now_bjt)
        weekly_pushed = data.setdefault("weekly_pushed", {})
        usernames = sorted({e["username"] for e in events})
        for username in usernames:
            user_pushed = weekly_pushed.setdefault(username, {})
            if iso_week in user_pushed:
                continue
            if push_weekly_digest(username, events, reference_dt=now):
                user_pushed[iso_week] = int(now.timestamp())
                changed = True
                time.sleep(1)

    if changed:
        save_events(data)


# ---------- CLI ----------

def cli_main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: events.py {tick|monthly|weekly|list}", flush=True)
        return 1
    cmd = argv[1]
    data = load_events()

    if cmd == "tick":
        tick()
        return 0

    if cmd == "monthly":
        if len(argv) < 4:
            print("usage: events.py monthly <username> <YYYY-MM>", flush=True)
            return 1
        username, year_month = argv[2], argv[3]
        ok = push_monthly_summary(username, year_month, data["events"])
        if ok:
            data.setdefault("monthly_pushed", {}).setdefault(username, {})[year_month] = int(time.time())
            save_events(data)
            print(f"[ok] pushed monthly summary for {username} {year_month}", flush=True)
        return 0 if ok else 1

    if cmd == "weekly":
        if len(argv) < 3:
            print("usage: events.py weekly <username>", flush=True)
            return 1
        username = argv[2]
        ok = push_weekly_digest(username, data["events"])
        if ok:
            print(f"[ok] pushed weekly digest for {username}", flush=True)
        return 0 if ok else 1

    if cmd == "list":
        for ev in data["events"]:
            print(f"{ev['id']}: {ev['start_date_bkk']}~{ev.get('end_date_bkk', '')}  {ev['title']}")
        return 0

    print(f"unknown command: {cmd}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(cli_main(sys.argv))
