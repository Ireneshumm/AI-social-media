"""Weekly performance report for Reborn Aesthetics.

Pulls Instagram insights (7-day reach, follower count, and the best-performing
recent posts) plus basic Facebook follower count via the Meta Graph API, then
emails a concise summary. Read-only: it never publishes or changes anything."""
import os
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

from facebook_publish import get_page_credentials
from alert_email import send_alert, alert_email_enabled

load_dotenv()

IG_USER_ID = os.getenv("IG_USER_ID")
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

REQUIRED_ENV_VARS = ["IG_USER_ID", "PAGE_ACCESS_TOKEN"]


def validate_env():
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


def graph_get(path, params):
    resp = requests.get(f"{GRAPH_BASE}/{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def try_get(path, params, label):
    try:
        return graph_get(path, params)
    except Exception as e:
        body = getattr(getattr(e, "response", None), "text", "")
        print(f"WARN: {label} failed: {e} {body[:200]}")
        return None


def ig_seven_day_reach(token):
    until = int(time.time())
    since = until - 7 * 86400
    data = try_get(
        f"{IG_USER_ID}/insights",
        {"metric": "reach", "period": "day", "since": since, "until": until, "access_token": token},
        "ig 7-day reach",
    )
    if not data:
        return None
    total = 0
    for metric in data.get("data", []):
        for point in metric.get("values", []):
            total += point.get("value") or 0
    return total


def ig_top_posts(token, lookback=25, top_n=5):
    media = try_get(
        f"{IG_USER_ID}/media",
        {
            "fields": "id,caption,media_type,media_product_type,timestamp,permalink,like_count,comments_count",
            "limit": lookback,
            "access_token": token,
        },
        "ig media list",
    )
    if not media:
        return []

    scored = []
    for post in media.get("data", []):
        reach = None
        ins = try_get(
            f"{post['id']}/insights",
            {"metric": "reach", "access_token": token},
            f"media insights {post['id']}",
        )
        if ins:
            try:
                reach = ins["data"][0]["values"][0]["value"]
            except (KeyError, IndexError, TypeError):
                reach = None
        post["reach"] = reach or 0
        scored.append(post)

    scored.sort(key=lambda p: p["reach"], reverse=True)
    return scored[:top_n]


def short_caption(caption, limit=70):
    if not caption:
        return "(无文字)"
    caption = " ".join(caption.split())
    return caption if len(caption) <= limit else caption[:limit] + "…"


def short_date(ts):
    if not ts:
        return "?"
    try:
        return datetime.fromisoformat(ts.replace("+0000", "+00:00")).strftime("%m-%d")
    except ValueError:
        return ts[:10]


def build_report(token, page_id):
    lines = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines.append(f"Reborn Aesthetics 每周表现报告  ·  {today}")
    lines.append("=" * 44)

    # --- Instagram ---
    ig = try_get(
        f"{IG_USER_ID}",
        {"fields": "username,followers_count,media_count", "access_token": token},
        "ig basic",
    ) or {}
    reach7 = ig_seven_day_reach(token)

    lines.append("\n📷 Instagram")
    lines.append(f"  账号        : @{ig.get('username', '?')}")
    lines.append(f"  粉丝数      : {ig.get('followers_count', '?')}")
    lines.append(f"  发帖总数    : {ig.get('media_count', '?')}")
    lines.append(f"  近7天总触达 : {reach7 if reach7 is not None else '读取失败'}")

    top = ig_top_posts(token)
    if top:
        lines.append("\n  🔥 近期表现最好的帖子 (按触达排序):")
        for i, post in enumerate(top, 1):
            lines.append(
                f"   {i}. [{short_date(post.get('timestamp'))}] "
                f"触达 {post['reach']}  ·  ❤ {post.get('like_count', 0)}  ·  💬 {post.get('comments_count', 0)}"
            )
            lines.append(f"      {short_caption(post.get('caption'))}")
            if post.get("permalink"):
                lines.append(f"      {post['permalink']}")
    else:
        lines.append("\n  (帖子表现数据暂时读取不到)")

    # --- Facebook (basic only) ---
    fb = try_get(
        f"{page_id}",
        {"fields": "name,followers_count,fan_count", "access_token": token},
        "fb basic",
    ) or {}
    lines.append("\n📘 Facebook")
    lines.append(f"  主页        : {fb.get('name', '?')}")
    lines.append(f"  粉丝数      : {fb.get('followers_count', fb.get('fan_count', '?'))}")

    lines.append("\n—— 由 Reborn 自动发布系统生成 ——")
    return "\n".join(lines)


def main():
    try:
        validate_env()
        page_id, token = get_page_credentials()
        report = build_report(token, page_id)

        print("\n" + report + "\n")

        subject = f"Reborn 每周表现报告 · {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        if alert_email_enabled():
            sent = send_alert(subject, report)
            print("Email sent." if sent else "Email not sent (disabled or misconfigured).")
        else:
            print("ALERT_EMAIL_ENABLED is not true; report printed above but not emailed.")
        sys.exit(0)
    except Exception as e:
        print("\nERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
