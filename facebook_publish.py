import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()


# =========================
# Config
# =========================
# Strip surrounding whitespace/newlines: a token pasted into a secret often
# carries a trailing "\n", which is harmless in form fields but illegal in an
# HTTP header (breaks the video-story resumable upload).
_RAW_PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
PAGE_ACCESS_TOKEN = _RAW_PAGE_ACCESS_TOKEN.strip() if _RAW_PAGE_ACCESS_TOKEN else _RAW_PAGE_ACCESS_TOKEN
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Optional: the Facebook Page id. If unset, it is auto-detected from the
# Page access token via GET /me.
FB_PAGE_ID = os.getenv("FB_PAGE_ID")

# Facebook cross-posting is on by default; only an explicit off value disables
# it. An unset workflow secret arrives as an empty string (not "unset"), so an
# empty value must still count as enabled.
_FB_FLAG = os.getenv("FB_PUBLISH_ENABLED", "true").strip().lower()
FB_PUBLISH_ENABLED = _FB_FLAG not in ("false", "0", "no", "off")

VIDEO_UPLOAD_TIMEOUT = int(os.getenv("FB_VIDEO_UPLOAD_TIMEOUT", "300"))

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
        print("Facebook Graph API error response:")
        print(body)


def request_with_retry(method, url, label, data=None, params=None, headers=None, files=None, timeout=60):
    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.request(
                method,
                url,
                data=data,
                params=params,
                headers=headers,
                files=files,
                timeout=timeout,
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


def get_page_id():
    if FB_PAGE_ID:
        return FB_PAGE_ID

    url = f"{GRAPH_BASE}/me"
    resp = request_with_retry(
        "GET",
        url,
        "facebook get_page_id",
        params={"fields": "id", "access_token": PAGE_ACCESS_TOKEN},
        timeout=30,
    )
    return resp.json()["id"]


# =========================
# Page credentials
# =========================
def get_page_credentials():
    """Return (page_id, page_token) for publishing.

    Works whether PAGE_ACCESS_TOKEN is a Page token or a User token: /me/accounts
    lists the Pages the user manages together with a real Page access token, so
    Page operations use the right id and token either way. Falls back to the
    configured token if /me/accounts is unavailable (already a page token)."""
    try:
        resp = request_with_retry(
            "GET",
            f"{GRAPH_BASE}/me/accounts",
            "facebook get_page_credentials",
            params={"fields": "id,name,access_token", "access_token": PAGE_ACCESS_TOKEN},
            timeout=30,
        )
        pages = resp.json().get("data", [])
    except Exception as e:
        print(f"WARNING: /me/accounts unavailable ({e}); using configured token.")
        pages = []

    if pages:
        page = None
        if FB_PAGE_ID:
            page = next((p for p in pages if p.get("id") == FB_PAGE_ID), None)
        page = page or pages[0]
        return page["id"], page.get("access_token", PAGE_ACCESS_TOKEN)

    return (FB_PAGE_ID or get_page_id()), PAGE_ACCESS_TOKEN


# =========================
# Feed post (image / video)
# =========================
def _image_mime(path):
    ext = os.path.splitext(path)[1].lower()
    return {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")


def _upload_unpublished_photo(page_id, token, image_path, label):
    # Upload the raw image bytes (source=) instead of handing Facebook a URL to
    # fetch: the WordPress host blocks Facebook's image scraper, so a url= upload
    # comes back as "Missing or invalid image file" (error 324 / 2069019).
    with open(image_path, "rb") as f:
        image_bytes = f.read()
    photos_url = f"{GRAPH_BASE}/{page_id}/photos"
    upload = request_with_retry(
        "POST",
        photos_url,
        label,
        data={"published": "false", "access_token": token},
        files={"source": (os.path.basename(image_path), image_bytes, _image_mime(image_path))},
        timeout=60,
    )
    return upload.json()["id"]


def publish_photo_post(page_id, token, image_path, caption):
    # Two-step: upload the photo unpublished to get a photo id, then attach it
    # to a feed post so the caption/message is preserved.
    photo_id = _upload_unpublished_photo(page_id, token, image_path, "facebook feed photo upload")

    feed_url = f"{GRAPH_BASE}/{page_id}/feed"
    payload = {
        "message": caption,
        "attached_media[0]": json.dumps({"media_fbid": photo_id}),
        "access_token": token,
    }
    resp = request_with_retry("POST", feed_url, "facebook feed photo post", data=payload, timeout=60)
    return resp.json()


def publish_video_post(page_id, token, video_path, caption):
    # Upload the raw bytes directly instead of handing Facebook a file_url to
    # fetch: the WordPress host blocks Facebook's video fetcher (HTTP 418).
    with open(video_path, "rb") as f:
        video_bytes = f.read()

    url = f"{GRAPH_BASE}/{page_id}/videos"
    files = {"source": (os.path.basename(video_path), video_bytes, "video/mp4")}
    data = {"description": caption, "access_token": token}
    resp = request_with_retry(
        "POST", url, "facebook video post", data=data, files=files, timeout=VIDEO_UPLOAD_TIMEOUT
    )
    return resp.json()


def publish_facebook_post(media_url, caption, media_kind, media_path=None, page_id=None):
    resolved_id, token = get_page_credentials()
    page_id = page_id or resolved_id

    if media_kind == "video":
        if not media_path:
            raise RuntimeError("media_path is required to publish a Facebook video post.")
        return publish_video_post(page_id, token, media_path, caption)
    if not media_path:
        raise RuntimeError("media_path is required to publish a Facebook photo post.")
    return publish_photo_post(page_id, token, media_path, caption)


# =========================
# Story (image / video)
# =========================
def publish_photo_story(page_id, token, image_path):
    # Step 1: upload the photo unpublished (raw bytes) to get a photo id.
    photo_id = _upload_unpublished_photo(page_id, token, image_path, "facebook story photo upload")

    # Step 2: publish the photo as a story.
    story_url = f"{GRAPH_BASE}/{page_id}/photo_stories"
    resp = request_with_retry(
        "POST",
        story_url,
        "facebook photo story",
        data={"photo_id": photo_id, "access_token": token},
        timeout=60,
    )
    return resp.json()


def publish_video_story(page_id, token, video_path):
    stories_url = f"{GRAPH_BASE}/{page_id}/video_stories"

    # Phase 1: start an upload session.
    start = request_with_retry(
        "POST",
        stories_url,
        "facebook video story start",
        data={"upload_phase": "start", "access_token": token},
        timeout=60,
    )
    start_data = start.json()
    video_id = start_data["video_id"]
    upload_url = start_data["upload_url"]

    # Phase 2: upload the raw bytes directly. Handing Facebook a file_url to
    # fetch fails because the WordPress host blocks its video fetcher (418).
    with open(video_path, "rb") as f:
        video_bytes = f.read()
    request_with_retry(
        "POST",
        upload_url,
        "facebook video story upload",
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(len(video_bytes)),
        },
        data=video_bytes,
        timeout=VIDEO_UPLOAD_TIMEOUT,
    )

    # Phase 3: finish and publish the story.
    finish = request_with_retry(
        "POST",
        stories_url,
        "facebook video story finish",
        data={"upload_phase": "finish", "video_id": video_id, "access_token": token},
        timeout=60,
    )
    return finish.json()


def publish_facebook_story(media_url, media_kind, media_path=None, page_id=None):
    resolved_id, token = get_page_credentials()
    page_id = page_id or resolved_id

    if media_kind == "video":
        if not media_path:
            raise RuntimeError("media_path is required to publish a Facebook video story.")
        return publish_video_story(page_id, token, media_path)
    if not media_path:
        raise RuntimeError("media_path is required to publish a Facebook photo story.")
    return publish_photo_story(page_id, token, media_path)
