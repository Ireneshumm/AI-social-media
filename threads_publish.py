"""Threads cross-posting, bolted onto the same queue as Instagram/Facebook.

Threads is Meta's text-first platform. Its API is a separate product from the
Instagram/Facebook Graph API: a different host (graph.threads.net) and a
SEPARATE access token tied to a Threads account (not the Facebook Page token).

Publishing is two steps, like an Instagram container:
  1) create a media container  -> POST /{threads-user-id}/threads
  2) publish that container     -> POST /{threads-user-id}/threads_publish

Design choices that match the rest of this pipeline:
- Images reuse the public URL we already host on imgbb for Instagram, so no new
  image hosting is introduced.
- Videos are NOT cross-posted by default. The feed pipeline uploads video to
  Instagram/Facebook as raw bytes and never produces a public video URL, but
  the Threads API requires a public `video_url`. Rather than add a video host,
  a video asset is skipped on Threads (or, if THREADS_VIDEO_AS_TEXT=true, its
  caption is posted as a text-only Thread).
- A Threads failure is always non-fatal to the caller: Instagram has already
  published by the time this runs.
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()


# =========================
# Config
# =========================
# Strip surrounding whitespace/newlines: a token pasted into a secret often
# carries a trailing "\n", which is illegal in an HTTP header.
_RAW_THREADS_TOKEN = os.getenv("THREADS_ACCESS_TOKEN")
THREADS_ACCESS_TOKEN = _RAW_THREADS_TOKEN.strip() if _RAW_THREADS_TOKEN else _RAW_THREADS_TOKEN

_RAW_THREADS_USER_ID = os.getenv("THREADS_USER_ID")
THREADS_USER_ID = _RAW_THREADS_USER_ID.strip() if _RAW_THREADS_USER_ID else _RAW_THREADS_USER_ID

THREADS_API_VERSION = os.getenv("THREADS_API_VERSION", "v1.0")
THREADS_BASE = f"https://graph.threads.net/{THREADS_API_VERSION}"

# Threads cross-posting is on by default, but it is only actually enabled when a
# token AND a user id are configured — otherwise it is a silent no-op so the
# feature can ship before the credentials exist. An unset workflow secret
# arrives as an empty string, so an empty flag must still count as enabled.
_THREADS_FLAG = os.getenv("THREADS_PUBLISH_ENABLED", "true").strip().lower()
THREADS_PUBLISH_ENABLED = (
    _THREADS_FLAG not in ("false", "0", "no", "off")
    and bool(THREADS_ACCESS_TOKEN)
    and bool(THREADS_USER_ID)
)

# When a video asset is cross-posted, post its caption as a text-only Thread
# instead of skipping it. Off by default (see module docstring).
_THREADS_VIDEO_TEXT = os.getenv("THREADS_VIDEO_AS_TEXT", "false").strip().lower()
THREADS_VIDEO_AS_TEXT = _THREADS_VIDEO_TEXT in ("true", "1", "yes", "on")

# Threads caps post text at 500 characters.
THREADS_TEXT_LIMIT = 500

# Seconds to wait for Threads to fetch and process a remote image before publish.
IMAGE_PROCESS_WAIT = int(os.getenv("THREADS_IMAGE_PROCESS_WAIT", "6"))

RETRY_DELAYS = [5, 10]


# =========================
# HTTP helpers
# =========================
def print_error_response(error):
    resp = getattr(error, "response", None)
    if resp is None:
        return
    body = resp.text
    if body:
        print("Threads API error response:")
        print(body)


def request_with_retry(method, url, label, data=None, params=None, headers=None, timeout=60):
    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.request(
                method, url, data=data, params=params, headers=headers, timeout=timeout
            )
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            last_error = e
            status_code = e.response.status_code if e.response is not None else None
            should_retry = status_code is not None and 500 <= status_code < 600

            if not should_retry:
                print(f"FAIL: {label} failed with non-retryable HTTP error on attempt {attempt}: {e}")
                print_error_response(e)
                raise

            if attempt < 3:
                delay = RETRY_DELAYS[attempt - 1]
                print(f"WARNING: {label} attempt {attempt} failed with HTTP {status_code}: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: {label} attempt {attempt} failed with HTTP {status_code}: {e}")
                print_error_response(e)
        except requests.RequestException as e:
            last_error = e
            if attempt < 3:
                delay = RETRY_DELAYS[attempt - 1]
                print(f"WARNING: {label} attempt {attempt} failed: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: {label} attempt {attempt} failed: {e}")

    raise last_error


# =========================
# Publishing
# =========================
def _truncate(text):
    text = text or ""
    if len(text) > THREADS_TEXT_LIMIT:
        return text[: THREADS_TEXT_LIMIT - 1].rstrip() + "…"
    return text


def _create_container(payload, label):
    url = f"{THREADS_BASE}/{THREADS_USER_ID}/threads"
    payload = {**payload, "access_token": THREADS_ACCESS_TOKEN}
    resp = request_with_retry("POST", url, label, data=payload, timeout=60)
    return resp.json()["id"]


def _publish_container(creation_id, label):
    url = f"{THREADS_BASE}/{THREADS_USER_ID}/threads_publish"
    resp = request_with_retry(
        "POST",
        url,
        label,
        data={"creation_id": creation_id, "access_token": THREADS_ACCESS_TOKEN},
        timeout=60,
    )
    return resp.json()


def publish_text_thread(text):
    creation_id = _create_container(
        {"media_type": "TEXT", "text": _truncate(text)},
        "threads text container",
    )
    time.sleep(3)
    return _publish_container(creation_id, "threads text publish")


def publish_image_thread(image_url, text):
    creation_id = _create_container(
        {"media_type": "IMAGE", "image_url": image_url, "text": _truncate(text)},
        "threads image container",
    )
    # Threads fetches the remote image asynchronously; give it a moment before
    # asking it to publish, or the publish can race the fetch.
    time.sleep(IMAGE_PROCESS_WAIT)
    return _publish_container(creation_id, "threads image publish")


def publish_threads_post(media_url, caption, media_kind, media_path=None):
    """Cross-post one asset to Threads.

    Returns the Threads publish response on success, or None when the asset is
    intentionally skipped (a video with no public URL and text fallback off).
    Mirrors publish_facebook_post's signature so the caller treats all
    cross-posts the same way. media_path is accepted for signature parity and
    is unused (Threads has no raw-byte upload path).
    """
    if media_kind == "video":
        if THREADS_VIDEO_AS_TEXT:
            return publish_text_thread(caption)
        print(
            "Threads: skipping video asset (no public video URL in this pipeline; "
            "set THREADS_VIDEO_AS_TEXT=true to post the caption as a text Thread)."
        )
        return None

    if media_url:
        return publish_image_thread(media_url, caption)

    # No hosted image URL available -> fall back to a text-only Thread so the
    # written content still lands on Threads.
    return publish_text_thread(caption)
