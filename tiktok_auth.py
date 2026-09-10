"""One-off TikTok setup and maintenance commands (run from GitHub Actions).

  python tiktok_auth.py url        Print the TikTok authorisation link.
  python tiktok_auth.py exchange   Finish authorisation: reads the address TikTok
                                   redirected to (workflow input
                                   tiktok_redirect_url) and stores the tokens.
  python tiktok_auth.py test       Send one of Reborn's own videos to the TikTok
                                   inbox as a draft (works while publishing is
                                   still switched off).
  python tiktok_auth.py status     Show authorisation / ledger status.
  python tiktok_auth.py keepalive  Refresh the tokens (weekly token check) so
                                   the 365-day refresh token never lapses.

The pasted redirect address is read from the workflow event file, never from
an env var or command line, so it is not echoed into the (public) logs.
"""
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

import tiktok_publish as tt


def _graph_token():
    from post_publisher import get_access_token

    return get_access_token()


def _dispatch_input(name):
    event_path = os.getenv("GITHUB_EVENT_PATH")
    if event_path and os.path.exists(event_path):
        with open(event_path, encoding="utf-8") as f:
            event = json.load(f)
        value = ((event.get("inputs") or {}).get(name) or "").strip()
        if value:
            return value
    # Local runs: fall back to an env var / argument.
    return (os.getenv(name.upper()) or (sys.argv[2] if len(sys.argv) > 2 else "")).strip()


def parse_redirect(value):
    """Return (code, state, error) from the pasted redirect address or a bare code."""
    value = (value or "").strip()
    if not value:
        return None, None, "nothing pasted"
    if "://" not in value and "code=" not in value:
        return value, None, None
    query = urlparse(value).query if "://" in value else value.lstrip("?")
    params = parse_qs(query)
    if params.get("error"):
        detail = (params.get("error_description") or [""])[0]
        return None, None, f"{params['error'][0]} {detail}".strip()
    code = (params.get("code") or [None])[0]
    state = (params.get("state") or [None])[0]
    if not code:
        return None, None, "no code= in the pasted address"
    return code, state, None


def _require_config():
    missing = tt.missing_config()
    if missing:
        print(f"FAIL: missing {', '.join(missing)} (see the setup steps in the PR).")
        sys.exit(1)


def cmd_url():
    if not tt.TIKTOK_CLIENT_KEY or not tt.TIKTOK_REDIRECT_URI:
        print("FAIL: set repository variables TIKTOK_CLIENT_KEY and TIKTOK_REDIRECT_URI first.")
        sys.exit(1)
    graph = _graph_token()
    state = tt.new_oauth_state()
    tt.write_state(graph, tt.OAUTH_STATE_FILE, {"state": state, "created_at": int(time.time())})
    print("Open this link while logged in to the Reborn TikTok account, then approve:")
    print()
    print(tt.build_authorize_url(state))
    print()
    print("TikTok will send you to your website. Copy that whole address from the")
    print("address bar and run this workflow again with publisher_type=tiktok_auth,")
    print("pasting it into 'tiktok_redirect_url'. Do it within a few minutes.")


def cmd_exchange():
    _require_config()
    raw = _dispatch_input("tiktok_redirect_url")
    tt.mask(raw)
    code, state, error = parse_redirect(raw)
    tt.mask(code)
    if error:
        print(f"FAIL: could not read the TikTok redirect ({error}).")
        sys.exit(1)
    graph = _graph_token()
    saved = tt.read_state(graph, tt.OAUTH_STATE_FILE, None) or {}
    if state and saved.get("state") and state != saved["state"]:
        print("FAIL: this link does not match the latest tiktok_auth_url run. Start again with tiktok_auth_url.")
        sys.exit(1)
    tokens = tt.exchange_code(graph, code)
    tt.write_state(graph, tt.OAUTH_STATE_FILE, {"state": None, "used_at": int(time.time())})
    scopes = tokens.get("scope") or ""
    print(f"OK: TikTok authorised. Scopes granted: {scopes}")
    if "video.upload" not in scopes:
        print("WARNING: video.upload was not granted — drafts will not work. Check the app's scopes.")


def _candidate_videos(graph):
    from asset_helpers import get_media_kind
    from post_publisher import (
        ONEDRIVE_POSTED_FOLDER_NAME,
        ONEDRIVE_POSTS_FOLDER_NAME,
        get_folder_children,
        get_posts_items,
        get_subfolder_by_path,
    )

    items = []
    try:
        items.extend(get_posts_items(graph))
    except Exception as e:  # noqa: BLE001
        print(f"Queue listing unavailable: {e}")
    try:
        posted = get_subfolder_by_path(graph, ONEDRIVE_POSTED_FOLDER_NAME, ONEDRIVE_POSTS_FOLDER_NAME)
        items.extend(get_folder_children(graph, posted["id"]))
    except Exception as e:  # noqa: BLE001
        print(f"Archive listing unavailable: {e}")

    videos = [
        it for it in items
        if "folder" not in it and get_media_kind(it.get("name", "")) == "video"
        and tt.is_eligible(it["name"], "video")[0]
    ]
    videos.sort(key=lambda it: it.get("lastModifiedDateTime", ""), reverse=True)
    return videos


def cmd_test():
    _require_config()
    from asset_helpers import brand_fallback_caption, filename_to_brief
    from compliance import scrub_caption
    from post_publisher import download_file
    from video_transcode import ensure_h264

    graph = _graph_token()
    ledger = tt.read_state(graph, tt.LEDGER_FILE, None) or {"sent": {}}
    videos = [v for v in _candidate_videos(graph) if v["id"] not in ledger.get("sent", {})]
    if not videos:
        print("No eligible Reborn video found (repost_ and ai_ files are never sent to TikTok).")
        sys.exit(1)
    item = videos[0]
    print(f"Test video: {item['name']}")
    os.makedirs("temp", exist_ok=True)
    path = os.path.join("temp", item["name"])
    with open(path, "wb") as f:
        f.write(download_file(graph, item["id"]))
    path = ensure_h264(path)
    body = scrub_caption(brand_fallback_caption(filename_to_brief(item["name"])))
    result = tt.share_to_tiktok(graph, item["name"], "video", item["id"], path, body)
    if not result:
        print("FAIL: the test draft was not sent (see the messages above).")
        sys.exit(1)
    print(f"OK: sent to TikTok drafts — status {result['status']}. Check the TikTok app inbox.")


def cmd_status():
    graph = _graph_token()
    tokens = tt.read_state(graph, tt.TOKEN_FILE, None)
    if not tokens:
        print("TikTok: not authorised yet.")
    else:
        now = time.time()
        print(f"TikTok: authorised, scopes: {tokens.get('scope')}")
        print(f"Access token valid for {max(0, int((tokens.get('expires_at', 0) - now) / 3600))} more hour(s).")
        print(f"Refresh token valid for {max(0, int((tokens.get('refresh_expires_at', 0) - now) / 86400))} more day(s).")
    ledger = tt.read_state(graph, tt.LEDGER_FILE, None) or {"sent": {}}
    sent = ledger.get("sent", {})
    today = tt.today_brisbane()
    print(f"Publishing enabled: {tt.TIKTOK_PUBLISH_ENABLED}; limit {tt.TIKTOK_MAX_PER_DAY}/day")
    print(f"Videos sent to TikTok drafts: {len(sent)} total, {sum(1 for v in sent.values() if v.get('date') == today)} today")
    for v in sorted(sent.values(), key=lambda v: v.get("at", ""), reverse=True)[:5]:
        print(f"  {v.get('at')}  {v.get('name')}  ({v.get('status')})")


def cmd_keepalive():
    if tt.missing_config():
        print("TikTok keepalive: not configured; nothing to do.")
        return
    try:
        graph = _graph_token()
        state = tt.load_tokens(graph)
        state = tt.refresh_tokens(graph, state)
        days = int((state["refresh_expires_at"] - time.time()) / 86400)
        print(f"TikTok keepalive: tokens refreshed; refresh token valid for {days} day(s).")
    except tt.TikTokNotAuthorized as e:
        print(f"TikTok keepalive: {e}")
    except Exception as e:  # noqa: BLE001 — never fail the weekly token check
        print(f"WARNING: TikTok keepalive failed: {e}")


COMMANDS = {
    "url": cmd_url,
    "exchange": cmd_exchange,
    "test": cmd_test,
    "status": cmd_status,
    "keepalive": cmd_keepalive,
}


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command not in COMMANDS:
        print(f"Usage: python tiktok_auth.py [{'|'.join(COMMANDS)}]")
        sys.exit(2)
    COMMANDS[command]()
