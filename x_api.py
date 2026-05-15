#!/usr/bin/env python3
"""Authenticated X (Twitter) GraphQL client.

Uses logged-in session cookies (auth_token + ct0) to call X's private
GraphQL endpoints, giving us full data parity with the X web client:

- Real RT timestamps (the user's actual retweet time, not the original
  post's time that nitter exposes).
- Full media including videos.
- More tweets per call (up to 40) than nitter's 20.

Falls back to nitter at the caller level if cookies are missing or the
GraphQL hashes drift; this module is best-effort and surfaces errors
back to the caller via exceptions.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Public bearer token shipped in every X web client. Not secret, hardcoded
# in their JS bundle. Required header alongside auth_token + ct0.
BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# These hashes are versioned by X and rotate every few months. If the
# request starts 404'ing, fetch x.com/home with cookies and grep main.js
# for `queryId:"<HASH>",operationName:"<OP>"` to find current values.
DEFAULT_HASHES = {
    "UserByScreenName": "IGgvgiOx4QZndDHuD3x9TQ",
    "UserTweets": "lrMzG9qPQHpqJdP3AbM-bQ",
    "UserTweetsAndReplies": "3YJONShMAajim63A8iF-sw",
}


class XAPIError(RuntimeError):
    """X GraphQL request failed (network, auth, or schema drift)."""


def _headers(ct0: str, auth_token: str, referer: str = "https://x.com/") -> dict:
    return {
        "authorization": f"Bearer {BEARER}",
        "cookie": f"auth_token={auth_token}; ct0={ct0}",
        "x-csrf-token": ct0,
        "x-twitter-active-user": "yes",
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-client-language": "en",
        "referer": referer,
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
    }


def _graphql_get(operation: str, variables: dict, ct0: str, auth_token: str,
                 features: dict | None = None, hash_override: str | None = None,
                 referer: str = "https://x.com/", timeout: int = 25) -> dict:
    op_hash = hash_override or os.environ.get(
        f"X_HASH_{operation.upper()}", DEFAULT_HASHES.get(operation, "")
    )
    if not op_hash:
        raise XAPIError(f"no hash known for operation {operation}")

    url = (
        f"https://x.com/i/api/graphql/{op_hash}/{operation}"
        f"?variables={urllib.parse.quote(json.dumps(variables))}"
        f"&features={urllib.parse.quote(json.dumps(features or {}))}"
    )
    req = urllib.request.Request(url, headers=_headers(ct0, auth_token, referer))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")[:300]
        raise XAPIError(f"{operation} HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise XAPIError(f"{operation} network: {e}") from e

    if data.get("errors"):
        raise XAPIError(f"{operation} GraphQL errors: {data['errors']}")
    return data


def get_user_id(screen_name: str, ct0: str, auth_token: str) -> str:
    """Resolve a screen_name to a stable rest_id (user id)."""
    variables = {"screen_name": screen_name, "withSafetyModeUserFields": True}
    data = _graphql_get(
        "UserByScreenName",
        variables,
        ct0,
        auth_token,
        referer=f"https://x.com/{screen_name}",
    )
    rest_id = (
        data.get("data", {}).get("user", {}).get("result", {}).get("rest_id")
    )
    if not rest_id:
        raise XAPIError(f"no rest_id for @{screen_name}")
    return str(rest_id)


def _twimg_url(media: dict) -> str | None:
    """Pick the best image URL from a media object."""
    if media.get("type") in ("photo",):
        return media.get("media_url_https") or media.get("media_url")
    if media.get("type") in ("video", "animated_gif"):
        return media.get("media_url_https")
    return media.get("media_url_https") or media.get("media_url")


def _extract_legacy_media(legacy: dict) -> tuple[list[str], bool]:
    images: list[str] = []
    has_video = False
    media = (legacy.get("extended_entities") or {}).get("media") or \
            (legacy.get("entities") or {}).get("media") or []
    for m in media:
        if m.get("type") == "video":
            has_video = True
        url = _twimg_url(m)
        if url:
            images.append(url)
    return images, has_video


def _expand_tco_links(text: str, urls: list[dict]) -> str:
    """Replace t.co shorturls with their expanded form."""
    if not urls:
        return text
    for u in urls:
        short = u.get("url")
        expanded = u.get("expanded_url") or u.get("display_url")
        if short and expanded:
            text = text.replace(short, expanded)
    return text


def _strip_trailing_media_url(text: str) -> str:
    """Remove the trailing pic.twitter.com / pic.x.com URL that Twitter
    appends when a tweet has media attached."""
    return re.sub(r"\s*https?://t\.co/\S+\s*$", "", text).rstrip()


def _result_legacy(tw_result: dict) -> dict:
    """Pull legacy block, accommodating limited-visibility wrapper types."""
    if tw_result.get("__typename") == "TweetWithVisibilityResults":
        tw_result = tw_result.get("tweet", {}) or tw_result
    return tw_result.get("legacy") or {}


def _to_normalized(tw_result: dict, monitored_user: str) -> dict | None:
    """Turn a raw GraphQL tweet result into a flat dict our caller can map
    onto the existing Tweet dataclass."""
    if tw_result.get("__typename") == "TweetWithVisibilityResults":
        tw_result = tw_result.get("tweet", {}) or tw_result
    legacy = tw_result.get("legacy")
    if not legacy:
        return None

    user_result = (
        tw_result.get("core", {})
        .get("user_results", {})
        .get("result", {})
    )
    # X moved screen_name from `legacy` to `core` in late 2025; fall back to
    # legacy path so we keep working if they roll it back.
    author = (
        (user_result.get("core") or {}).get("screen_name")
        or (user_result.get("legacy") or {}).get("screen_name")
        or ""
    )

    rt = legacy.get("retweeted_status_result", {}).get("result", {})
    is_retweet = bool(rt)
    is_reply = bool(legacy.get("in_reply_to_status_id_str"))

    # For retweets: legacy.created_at IS the retweet time (what user wants).
    # The original post's time lives inside retweeted_status_result.
    pub_date = legacy.get("created_at", "")  # RFC822 like "Fri May 08 12:08:49 +0000 2026"

    if is_retweet:
        rt_legacy = _result_legacy(rt)
        rt_user_result = (
            rt.get("core", {}).get("user_results", {}).get("result", {})
        )
        rt_author = (
            (rt_user_result.get("core") or {}).get("screen_name")
            or (rt_user_result.get("legacy") or {}).get("screen_name")
            or ""
        )

        full_text = rt_legacy.get("full_text") or rt_legacy.get("text") or ""
        full_text = _expand_tco_links(
            full_text,
            (rt_legacy.get("entities") or {}).get("urls") or []
        )
        full_text = _strip_trailing_media_url(full_text)
        title = f"RT by @{monitored_user}: {full_text}"
        # Display author of RT card = original author
        display_author = rt_author
        # Pull media from the RT'd tweet, not the wrapper
        images, has_video = _extract_legacy_media(rt_legacy)
        original_pub_date = rt_legacy.get("created_at", "")
    else:
        full_text = legacy.get("full_text") or legacy.get("text") or ""
        full_text = _expand_tco_links(
            full_text, (legacy.get("entities") or {}).get("urls") or []
        )
        full_text = _strip_trailing_media_url(full_text)
        if is_reply:
            target = legacy.get("in_reply_to_screen_name", "")
            title = f"R to @{target}: {full_text}"
        else:
            title = full_text
        display_author = author
        images, has_video = _extract_legacy_media(legacy)
        original_pub_date = ""

    tweet_id = legacy.get("id_str", "")
    link = f"https://x.com/{display_author}/status/{tweet_id}" if tweet_id else ""

    # For RTs, also expose the underlying tweet's ID. This is what nitter
    # uses as its GUID for the same RT — tracking both lets us dedup across
    # the x_api → nitter fallback.
    retweeted_guid = ""
    if is_retweet:
        retweeted_guid = _result_legacy(rt).get("id_str", "")

    return {
        "guid": tweet_id,
        "title": title[:280],
        "author": display_author,
        "pub_date": pub_date,
        "link": link,
        "description_html": full_text,
        "is_retweet": is_retweet,
        "is_reply": is_reply,
        "images": images,
        "videos": [] if not has_video else ["video"],
        "text": full_text,
        "monitored_user": monitored_user,
        "original_pub_date": original_pub_date,
        "retweeted_guid": retweeted_guid,
    }


def fetch_user_tweets(monitored_user: str, ct0: str, auth_token: str,
                      count: int = 40, with_replies: bool = True) -> list[dict]:
    """Returns a list of normalized tweet dicts.

    Tries UserTweetsAndReplies first (gives replies). If that fails (X
    drifts the schema), falls back to UserTweets (originals + RTs only).
    """
    user_id = get_user_id(monitored_user, ct0, auth_token)

    variables = {
        "userId": user_id,
        "count": count,
        "includePromotedContent": False,
        "withCommunity": True,
        "withVoice": True,
    }

    operation = "UserTweetsAndReplies" if with_replies else "UserTweets"
    try:
        data = _graphql_get(
            operation,
            variables,
            ct0,
            auth_token,
            referer=f"https://x.com/{monitored_user}/with_replies"
                if with_replies else f"https://x.com/{monitored_user}",
        )
    except XAPIError as e:
        # If AndReplies endpoint fails, fall back to UserTweets which is
        # historically more stable.
        if with_replies:
            print(f"[x_api] {operation} failed ({e}), falling back to UserTweets",
                  flush=True)
            data = _graphql_get(
                "UserTweets",
                variables,
                ct0,
                auth_token,
                referer=f"https://x.com/{monitored_user}",
            )
        else:
            raise

    user_result = data.get("data", {}).get("user", {}).get("result", {})
    timeline = user_result.get("timeline_v2") or user_result.get("timeline") or {}
    instructions = timeline.get("timeline", {}).get("instructions", [])

    out: list[dict] = []
    for inst in instructions:
        if inst.get("type") != "TimelineAddEntries":
            continue
        for entry in inst.get("entries", []):
            content = entry.get("content", {})
            entry_type = content.get("entryType") or content.get(
                "__typename", ""
            )
            if entry_type == "TimelineTimelineCursor":
                continue
            if entry_type == "TimelineTimelineItem":
                ic = content.get("itemContent") or {}
                tw = ic.get("tweet_results", {}).get("result")
                if tw:
                    norm = _to_normalized(tw, monitored_user)
                    if norm:
                        out.append(norm)
            elif entry_type == "TimelineTimelineModule":
                # Conversation thread — pick out only the monitored user's tweets
                for item in content.get("items", []):
                    ic = item.get("item", {}).get("itemContent", {})
                    tw = ic.get("tweet_results", {}).get("result")
                    if not tw:
                        continue
                    norm = _to_normalized(tw, monitored_user)
                    if not norm:
                        continue
                    # Only keep if it's actually authored by the monitored
                    # user (modules include replies they receive too)
                    if norm["author"].lower() == monitored_user.lower():
                        out.append(norm)
    return out


if __name__ == "__main__":
    # Quick CLI: dump first few tweets for a user
    import sys
    if len(sys.argv) < 2:
        print("usage: python x_api.py <screen_name>")
        sys.exit(1)
    name = sys.argv[1]
    ct0 = os.environ["X_CT0"]
    auth = os.environ["X_AUTH_TOKEN"]
    tweets = fetch_user_tweets(name, ct0, auth, count=20)
    for t in tweets[:5]:
        kind = "RT" if t["is_retweet"] else ("REPLY" if t["is_reply"] else "ORIG")
        print(f"[{kind}] {t['pub_date']}  by @{t['author']}: {t['text'][:80]!r}")
