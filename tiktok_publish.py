"""TikTok cross-posting — self-built, official Content Posting API, DRAFT mode.

What it does
  After a video has been published to Instagram, the same video is sent to the
  Reborn TikTok account's inbox as a draft (TikTok "Upload" API, scope
  video.upload). TikTok's Upload API cannot carry a caption, so the TikTok
  caption is emailed (alert email) to be pasted in the TikTok app before
  tapping Post.

Why drafts and not automatic public posts
  Public auto-posting (Direct Post) needs TikTok's audit, and TikTok's Content
  Sharing Guidelines list "a utility tool to help upload contents to the
  account(s) you or your team manages" as not acceptable. A self-built app for
  our own account therefore stays in draft mode.

Safety rules
  * Only Reborn's own videos. repost_* files (downloaded from TikTok /
    Instagram / Douyin) and ai_* files are never sent: re-uploading platform
    content to TikTok is detected as duplicate/unoriginal content.
  * Each video goes to TikTok at most once (ledger in OneDrive) and at most
    TIKTOK_MAX_PER_DAY videos per Brisbane day.
  * TikTok allows at most 5 pending drafts per 24 hours; when the inbox is
    full the video is skipped (not recorded) and an alert is sent once a day.
  * Never raises into the Instagram/Facebook flow.
  * Tokens are never printed, and are masked in GitHub Actions logs (this
    repository is public, so its logs are public too).

Tokens
  The access token lasts 24 hours; the refresh token lasts 365 days and may be
  replaced on every refresh. Both are stored in OneDrive at
  <ONEDRIVE_ROOT_PATH>/_state/tiktok_token.json and rewritten after each
  refresh, because GitHub secrets cannot be updated from inside a run.
"""
import json
import mimetypes
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests
from dotenv import load_dotenv

from asset_helpers import is_ai_generated, is_repost
from alert_email import send_alert_safely

load_dotenv()


# =========================
# Config
# =========================
def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def _flag(name, default="false"):
    return _env(name, default).lower() in ("true", "1", "yes", "on")


TIKTOK_PUBLISH_ENABLED = _flag("TIKTOK_PUBLISH_ENABLED")
TIKTOK_CLIENT_KEY = _env("TIKTOK_CLIENT_KEY")
TIKTOK_CLIENT_SECRET = _env("TIKTOK_CLIENT_SECRET")
TIKTOK_REDIRECT_URI = _env("TIKTOK_REDIRECT_URI")
try:
    TIKTOK_MAX_PER_DAY = max(1, int(_env("TIKTOK_MAX_PER_DAY", "2")))
except ValueError:
    TIKTOK_MAX_PER_DAY = 2

TIKTOK_HASHTAGS = _env(
    "TIKTOK_HASHTAGS",
    "#brisbane #brisbaneskin #brisbanebeauty #skincare #aesthetics",
)
TIKTOK_CTA = _env(
    "TIKTOK_CTA",
    "Book online: rebornaesthetics.com.au\n📍 Annerley & Fortitude Valley, Brisbane",
)

ONEDRIVE_USER_EMAIL = _env("ONEDRIVE_USER_EMAIL", "info@rebornaesthetics.com.au")
ONEDRIVE_ROOT_PATH = _env("ONEDRIVE_ROOT_PATH", "IG Auto Publisher")
STATE_FOLDER = "_state"
TOKEN_FILE = "tiktok_token.json"
LEDGER_FILE = "tiktok_ledger.json"
OAUTH_STATE_FILE = "tiktok_oauth_state.json"

TIKTOK_API = "https://open.tiktokapis.com"
TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_SCOPES = "user.info.basic,video.upload"

# TikTok FILE_UPLOAD rules: chunks of 5–64 MB (the last one may be bigger, up
# to 128 MB); a video under 5 MB must be sent as one chunk of its full size.
# Anything up to 64 MB therefore goes as a single chunk; larger files use
# 10 MB chunks with the remainder folded into the last one.
MAX_SINGLE_CHUNK = 64 * 1024 * 1024
CHUNK_SIZE = 10 * 1024 * 1024
TIKTOK_CAPTION_LIMIT = 2200

STATUS_POLL_SECONDS = 5
STATUS_POLL_TIMEOUT = 120

BRISBANE = timezone(timedelta(hours=10))
GRAPH_USER_DRIVE = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive"


class TikTokError(Exception):
    """Base error. Messages never contain tokens."""


class TikTokNotAuthorized(TikTokError):
    pass


class TikTokApiError(TikTokError):
    def __init__(self, http_status, code, message="", log_id=None):
        self.http_status = http_status
        self.code = code or f"http_{http_status}"
        self.log_id = log_id
        detail = f" — {message}" if message else ""
        log = f" (log_id {log_id})" if log_id else ""
        super().__init__(f"TikTok API {self.code} [HTTP {http_status}]{detail}{log}")


# =========================
# Small helpers
# =========================
def mask(value):
    """Hide a runtime secret in GitHub Actions logs (no-op elsewhere)."""
    if value and os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{value}")


def today_brisbane():
    return datetime.now(BRISBANE).date().isoformat()


def now_iso():
    return datetime.now(BRISBANE).isoformat(timespec="seconds")


def missing_config():
    names = {
        "TIKTOK_CLIENT_KEY": TIKTOK_CLIENT_KEY,
        "TIKTOK_CLIENT_SECRET": TIKTOK_CLIENT_SECRET,
        "TIKTOK_REDIRECT_URI": TIKTOK_REDIRECT_URI,
    }
    return [k for k, v in names.items() if not v]


# =========================
# OneDrive state (tokens, ledger)
# =========================
def _state_path(name):
    return quote(f"{ONEDRIVE_ROOT_PATH}/{STATE_FOLDER}/{name}")


def read_state(graph_token, name, default=None):
    url = f"{GRAPH_USER_DRIVE}/root:/{_state_path(name)}:/content"
    resp = requests.get(url, headers={"Authorization": f"Bearer {graph_token}"}, timeout=30)
    if resp.status_code == 404:
        return default
    resp.raise_for_status()
    try:
        return json.loads(resp.content.decode("utf-8") or "null") or default
    except ValueError:
        print(f"WARNING: TikTok state file {name} is not valid JSON; ignoring it.")
        return default


def _ensure_state_folder(graph_token):
    headers = {"Authorization": f"Bearer {graph_token}"}
    folder_url = f"{GRAPH_USER_DRIVE}/root:/{quote(f'{ONEDRIVE_ROOT_PATH}/{STATE_FOLDER}')}"
    resp = requests.get(folder_url, headers=headers, timeout=30)
    if resp.status_code == 200:
        return
    if resp.status_code != 404:
        resp.raise_for_status()
    create_url = f"{GRAPH_USER_DRIVE}/root:/{quote(ONEDRIVE_ROOT_PATH)}:/children"
    resp = requests.post(
        create_url,
        headers={**headers, "Content-Type": "application/json"},
        json={"name": STATE_FOLDER, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
        timeout=30,
    )
    if resp.status_code not in (200, 201, 409):
        resp.raise_for_status()


def write_state(graph_token, name, data):
    url = f"{GRAPH_USER_DRIVE}/root:/{_state_path(name)}:/content"
    headers = {"Authorization": f"Bearer {graph_token}", "Content-Type": "application/json"}
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    resp = requests.put(url, headers=headers, data=payload, timeout=30)
    if resp.status_code == 404:
        _ensure_state_folder(graph_token)
        resp = requests.put(url, headers=headers, data=payload, timeout=30)
    resp.raise_for_status()


# =========================
# OAuth
# =========================
def build_authorize_url(state):
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": TIKTOK_SCOPES,
        "response_type": "code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
    }
    return f"{TIKTOK_AUTHORIZE_URL}?{urlencode(params)}"


def new_oauth_state():
    return secrets.token_urlsafe(24)


def _token_request(form):
    resp = requests.post(
        f"{TIKTOK_API}/v2/oauth/token/",
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache"},
        timeout=30,
    )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if resp.status_code != 200 or "access_token" not in body:
        raise TikTokApiError(
            resp.status_code,
            body.get("error") or "token_request_failed",
            body.get("error_description", ""),
            body.get("log_id"),
        )
    return body


def _save_token_response(graph_token, body):
    now = int(time.time())
    mask(body.get("access_token"))
    mask(body.get("refresh_token"))
    state = {
        "open_id": body.get("open_id"),
        "scope": body.get("scope"),
        "access_token": body["access_token"],
        "expires_at": now + int(body.get("expires_in") or 86400),
        "refresh_token": body["refresh_token"],
        "refresh_expires_at": now + int(body.get("refresh_expires_in") or 31536000),
        "updated_at": now_iso(),
    }
    write_state(graph_token, TOKEN_FILE, state)
    return state


def exchange_code(graph_token, code):
    body = _token_request({
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
    })
    return _save_token_response(graph_token, body)


def refresh_tokens(graph_token, state):
    body = _token_request({
        "client_key": TIKTOK_CLIENT_KEY,
        "client_secret": TIKTOK_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": state["refresh_token"],
    })
    return _save_token_response(graph_token, body)


def load_tokens(graph_token):
    state = read_state(graph_token, TOKEN_FILE, None)
    if not state or not state.get("refresh_token"):
        raise TikTokNotAuthorized("TikTok is not authorised yet (run publisher_type=tiktok_auth).")
    mask(state.get("access_token"))
    mask(state.get("refresh_token"))
    if state.get("refresh_expires_at") and state["refresh_expires_at"] < time.time():
        raise TikTokNotAuthorized("TikTok refresh token has expired; authorise again.")
    return state


def get_access_token(graph_token, force_refresh=False):
    state = load_tokens(graph_token)
    if force_refresh or int(state.get("expires_at") or 0) - time.time() < 600:
        state = refresh_tokens(graph_token, state)
    return state["access_token"]


# =========================
# Content Posting API — Upload (inbox / draft)
# =========================
def chunk_plan(size):
    """Return (chunk_size, total_chunk_count) following TikTok's rules."""
    if size <= 0:
        raise TikTokError("Video file is empty.")
    if size <= MAX_SINGLE_CHUNK:
        return size, 1
    return CHUNK_SIZE, size // CHUNK_SIZE


def _api_post(path, access_token, payload):
    resp = requests.post(
        f"{TIKTOK_API}{path}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=payload,
        timeout=60,
    )
    try:
        body = resp.json()
    except ValueError:
        body = {}
    error = body.get("error") or {}
    code = error.get("code") or ""
    if resp.status_code != 200 or (code and code != "ok"):
        raise TikTokApiError(resp.status_code, code, error.get("message", ""), error.get("log_id"))
    return body.get("data") or {}


def init_inbox_upload(access_token, size):
    chunk_size, chunk_count = chunk_plan(size)
    data = _api_post(
        "/v2/post/publish/inbox/video/init/",
        access_token,
        {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": chunk_size,
                "total_chunk_count": chunk_count,
            }
        },
    )
    if not data.get("publish_id") or not data.get("upload_url"):
        raise TikTokError("TikTok init response had no publish_id/upload_url.")
    return data["publish_id"], data["upload_url"], chunk_size, chunk_count


def upload_chunks(upload_url, video_path, size, chunk_size, chunk_count):
    mime = mimetypes.guess_type(video_path)[0] or "video/mp4"
    with open(video_path, "rb") as f:
        for index in range(chunk_count):
            start = index * chunk_size
            end = size - 1 if index == chunk_count - 1 else start + chunk_size - 1
            f.seek(start)
            blob = f.read(end - start + 1)
            headers = {
                "Content-Type": mime,
                "Content-Length": str(len(blob)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            }
            for attempt in range(1, 4):
                try:
                    resp = requests.put(upload_url, headers=headers, data=blob, timeout=300)
                except requests.RequestException as e:
                    if attempt == 3:
                        raise TikTokError(f"Chunk {index + 1}/{chunk_count} upload failed: {e}") from None
                    time.sleep(5 * attempt)
                    continue
                if resp.status_code in (200, 201, 206):
                    break
                if resp.status_code < 500 or attempt == 3:
                    raise TikTokApiError(resp.status_code, "chunk_upload_failed", f"chunk {index + 1}/{chunk_count}")
                time.sleep(5 * attempt)
            print(f"TikTok: uploaded chunk {index + 1}/{chunk_count}")


def fetch_status(access_token, publish_id):
    return _api_post("/v2/post/publish/status/fetch/", access_token, {"publish_id": publish_id})


def wait_for_inbox(access_token, publish_id, timeout=STATUS_POLL_TIMEOUT, interval=STATUS_POLL_SECONDS):
    deadline = time.time() + timeout
    status = "PROCESSING_UPLOAD"
    while True:
        data = fetch_status(access_token, publish_id)
        status = data.get("status") or status
        if status in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"):
            return status
        if status == "FAILED":
            raise TikTokApiError(200, data.get("fail_reason") or "publish_failed", "TikTok processing failed")
        if time.time() + interval > deadline:
            return status  # still processing — TikTok finishes it on its own
        time.sleep(interval)


def send_video_to_inbox(graph_token, video_path):
    """Upload a local video to the authorised account's TikTok inbox (draft)."""
    size = os.path.getsize(video_path)
    access_token = get_access_token(graph_token)
    try:
        publish_id, upload_url, chunk_size, chunk_count = init_inbox_upload(access_token, size)
    except TikTokApiError as e:
        if e.code != "access_token_invalid":
            raise
        access_token = get_access_token(graph_token, force_refresh=True)
        publish_id, upload_url, chunk_size, chunk_count = init_inbox_upload(access_token, size)
    print(f"TikTok: upload started ({size} bytes, {chunk_count} chunk(s)).")
    upload_chunks(upload_url, video_path, size, chunk_size, chunk_count)
    status = wait_for_inbox(access_token, publish_id)
    print(f"TikTok: status {status}.")
    return {"publish_id": publish_id, "status": status}


# =========================
# Eligibility, ledger, caption
# =========================
def is_eligible(media_name, media_kind):
    if media_kind != "video":
        return False, "not a video"
    if is_repost(media_name):
        return False, "repost_ file (not Reborn's own content)"
    if is_ai_generated(media_name):
        return False, "ai_ file"
    return True, ""


def ledger_allows(ledger, item_key):
    sent = (ledger or {}).get("sent", {})
    if item_key in sent:
        return False, "already sent to TikTok"
    today = today_brisbane()
    sent_today = sum(1 for v in sent.values() if v.get("date") == today)
    if sent_today >= TIKTOK_MAX_PER_DAY:
        return False, f"daily limit reached ({sent_today}/{TIKTOK_MAX_PER_DAY})"
    return True, ""


def build_tiktok_caption(caption_body):
    parts = [p for p in ((caption_body or "").strip(), TIKTOK_CTA, TIKTOK_HASHTAGS) if p]
    caption = "\n\n".join(parts)
    return caption[:TIKTOK_CAPTION_LIMIT]


def _alert_once(graph_token, ledger, key, subject, body):
    """Send an alert at most once per Brisbane day per key."""
    today = today_brisbane()
    alerts = ledger.setdefault("alerts", {})
    if alerts.get(key) == today:
        print(f"TikTok: alert '{key}' already sent today.")
        return
    send_alert_safely(subject, body)
    alerts[key] = today
    try:
        write_state(graph_token, LEDGER_FILE, ledger)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not save TikTok alert state: {e}")


def draft_email_body(media_name, caption, sent_today):
    return "\n".join([
        "一条新视频已送进 Reborn TikTok 的收件箱（草稿）。",
        "打开 TikTok → 收件箱通知 → 继续编辑 → 粘贴下面的文案 → 发布。",
        "（TikTok 不允许接口带文案，所以需要手动粘贴一次。）",
        "建议发布前打开：内容披露 →「你的品牌」（推广自己的生意）。",
        "",
        "----- 复制以下文案 -----",
        caption,
        "-----------------------",
        "",
        f"文件：{media_name}",
        f"今天已送 TikTok：{sent_today}/{TIKTOK_MAX_PER_DAY}",
    ])


def share_to_tiktok(graph_token, media_name, media_kind, item_key, media_path, caption_body):
    """Send one video to TikTok drafts if eligible. Returns a result dict or None."""
    ok, why = is_eligible(media_name, media_kind)
    if not ok:
        print(f"TikTok: skipping {media_name} — {why}.")
        return None

    missing = missing_config()
    if missing:
        print(f"TikTok: not configured (missing {', '.join(missing)}); skipping.")
        return None

    ledger = read_state(graph_token, LEDGER_FILE, None) or {"sent": {}, "alerts": {}}
    ledger.setdefault("sent", {})
    ok, why = ledger_allows(ledger, item_key)
    if not ok:
        print(f"TikTok: skipping {media_name} — {why}.")
        return None

    try:
        result = send_video_to_inbox(graph_token, media_path)
    except TikTokNotAuthorized as e:
        print(f"TikTok: {e}")
        _alert_once(
            graph_token, ledger, "not_authorized",
            "Reborn TikTok：需要重新授权",
            "TikTok 还没授权或授权已过期。到 GitHub Actions 运行 publisher_type=tiktok_auth_url 重新授权。",
        )
        return None
    except TikTokApiError as e:
        print(f"TikTok: {e}")
        if e.code == "spam_risk_too_many_pending_share":
            _alert_once(
                graph_token, ledger, "inbox_full",
                "Reborn TikTok：草稿箱已满（5 条待发）",
                "TikTok 规定 24 小时内最多 5 条待处理草稿。请先在 TikTok 里发布或删除草稿，之后的视频会继续自动送过去。",
            )
        elif e.code in ("access_token_invalid", "scope_not_authorized"):
            _alert_once(
                graph_token, ledger, "reauthorize",
                "Reborn TikTok：需要重新授权",
                f"TikTok 拒绝了授权（{e.code}）。到 GitHub Actions 运行 publisher_type=tiktok_auth_url 重新授权。",
            )
        else:
            _alert_once(
                graph_token, ledger, f"error_{e.code}",
                "Reborn TikTok：送草稿失败",
                f"视频：{media_name}\n错误：{e}\n\nInstagram / Facebook 不受影响。",
            )
        return None
    except TikTokError as e:
        print(f"TikTok: {e}")
        _alert_once(
            graph_token, ledger, "error_upload",
            "Reborn TikTok：送草稿失败",
            f"视频：{media_name}\n错误：{e}\n\nInstagram / Facebook 不受影响。",
        )
        return None

    ledger["sent"][item_key] = {
        "name": media_name,
        "date": today_brisbane(),
        "at": now_iso(),
        "publish_id": result["publish_id"],
        "status": result["status"],
    }
    try:
        write_state(graph_token, LEDGER_FILE, ledger)
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: TikTok draft sent but the ledger could not be saved ({e}); it may be re-sent once.")

    caption = build_tiktok_caption(caption_body)
    sent_today = sum(1 for v in ledger["sent"].values() if v.get("date") == today_brisbane())
    print("TikTok caption to paste:\n" + caption)
    send_alert_safely(
        f"TikTok 草稿待发布：{media_name}",
        draft_email_body(media_name, caption, sent_today),
    )
    return result


def maybe_share_to_tiktok(graph_token, selected_post, media_path, caption_body):
    """Hook used by post_publisher after Instagram succeeds. Never raises."""
    if not TIKTOK_PUBLISH_ENABLED:
        print("TikTok: disabled (set TIKTOK_PUBLISH_ENABLED=true to enable).")
        return None
    try:
        media = selected_post["media"]
        return share_to_tiktok(
            graph_token,
            media["name"],
            selected_post.get("kind"),
            media.get("id") or media["name"],
            media_path,
            caption_body,
        )
    except Exception as e:  # noqa: BLE001 — TikTok must never break Instagram/Facebook
        print(f"WARNING: TikTok step failed (Instagram/Facebook unaffected): {e}")
        return None
