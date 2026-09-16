"""Rank published posts by Instagram reach and write the top-performer list.

Reads the performance log (media_id -> source asset, written by the publisher),
asks Instagram for each post's reach + likes + comments, aggregates per asset,
and writes top_assets.json — the videos the publisher should recycle more often.
Read-only against Instagram; only writes the ranking file back to OneDrive.

Run it periodically (e.g. weekly) via the workflow: publisher_type=rank.
"""

import math
import os
import sys

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication

from onedrive_store import read_json, write_json
from facebook_publish import get_page_credentials

try:
    from alert_email import send_alert, alert_email_enabled
except Exception:  # noqa: BLE001
    send_alert = None
    def alert_email_enabled():
        return False

load_dotenv()

MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")
AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

PERFORMANCE_LOG_FILE = "performance_log.json"
TOP_ASSETS_FILE = "top_assets.json"

# How many posts we need before ranking is meaningful, and how big the top list is.
MIN_SAMPLE = int(os.getenv("RANK_MIN_SAMPLE", "8"))
TOP_PERCENT = float(os.getenv("RANK_TOP_PERCENT", "0.30"))
TOP_MIN = int(os.getenv("RANK_TOP_MIN", "3"))
TOP_MAX = int(os.getenv("RANK_TOP_MAX", "12"))
# Only rank videos — recycling (and therefore the boost) is video-based.
RANK_KIND = os.getenv("RANK_KIND", "video")


def get_ms_token():
    app = ConfidentialClientApplication(
        client_id=MS_CLIENT_ID, client_credential=MS_CLIENT_SECRET, authority=AUTHORITY
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"Failed to get Microsoft token: {result}")
    return result["access_token"]


def meta_get(path, params, label):
    try:
        resp = requests.get(f"{GRAPH_BASE}/{path}", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"WARN: {label} failed: {e} {body[:160]}")
        return None


def fetch_media_stats(media_id, token):
    """Return (reach, likes, comments) for one media_id; zeros on failure."""
    reach = 0
    ins = meta_get(f"{media_id}/insights", {"metric": "reach", "access_token": token},
                   f"insights {media_id}")
    if ins:
        try:
            reach = ins["data"][0]["values"][0]["value"] or 0
        except (KeyError, IndexError, TypeError):
            reach = 0
    likes = comments = 0
    basic = meta_get(f"{media_id}",
                     {"fields": "like_count,comments_count", "access_token": token},
                     f"basic {media_id}")
    if basic:
        likes = basic.get("like_count") or 0
        comments = basic.get("comments_count") or 0
    return reach, likes, comments


def score(reach, likes, comments):
    # Reach is the primary signal; engagement breaks ties and helps when reach is
    # unavailable for a post.
    return (reach or 0) + (likes or 0) * 3 + (comments or 0) * 5


def main():
    try:
        page_id, ig_token = get_page_credentials()
        ms_token = get_ms_token()

        log = read_json(ms_token, PERFORMANCE_LOG_FILE, default=[]) or []
        entries = [e for e in log if isinstance(e, dict) and e.get("media_id") and e.get("name")]
        if RANK_KIND:
            entries = [e for e in entries if (e.get("kind") or "") == RANK_KIND]

        print(f"Performance log: {len(log)} record(s); {len(entries)} {RANK_KIND or 'any'} post(s) to rank.")

        if len(entries) < MIN_SAMPLE:
            print(
                f"Not enough data yet ({len(entries)} < {MIN_SAMPLE}); keeping the current "
                "ranking. Top-asset recycling stays off until more posts accumulate."
            )
            sys.exit(0)

        # Aggregate per asset: keep the best score any of its posts achieved.
        by_asset = {}
        detail = []
        for e in entries:
            reach, likes, comments = fetch_media_stats(e["media_id"], ig_token)
            s = score(reach, likes, comments)
            name = e["name"]
            detail.append((name, reach, likes, comments, s))
            if s > by_asset.get(name, -1):
                by_asset[name] = s

        ranked = sorted(by_asset.items(), key=lambda kv: kv[1], reverse=True)
        ranked = [(n, s) for n, s in ranked if s > 0]

        n_top = min(TOP_MAX, max(TOP_MIN, math.ceil(len(ranked) * TOP_PERCENT)))
        top = ranked[:n_top]
        top_names = [n for n, _ in top]

        write_json(ms_token, TOP_ASSETS_FILE, {
            "names": top_names,
            "generated_at": __import__("datetime").datetime.now().astimezone().isoformat(),
            "sample": len(entries),
        })

        # Human-readable report.
        lines = ["Reborn — 表现排行（按触达）", "=" * 34,
                 f"样本：{len(entries)} 条视频帖 · 选出 top {len(top_names)} 循环加倍"]
        for i, (name, s) in enumerate(top, 1):
            lines.append(f" {i}. {name}  (分数 {s})")
        report = "\n".join(lines)
        print("\n" + report + "\n")
        print(f"Wrote {TOP_ASSETS_FILE} with {len(top_names)} top asset(s).")

        if send_alert and alert_email_enabled():
            send_alert("Reborn 表现排行更新", report)

        sys.exit(0)
    except Exception as e:  # noqa: BLE001
        print("ERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
