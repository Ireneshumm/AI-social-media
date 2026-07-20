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


def request_with_retry(method, url, label, data=None, params=None, headers=None, timeout=60):
    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.request(
                method,
                url,
                data=data,
                params=params,
                headers=headers,
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
# Feed post (image / video)
# =========================
def publish_photo_post(page_id, image_url, caption):
    # Two-step: upload the photo unpublished to get a photo id, then attach it
    # to a feed post. A single published /photos?url= call validates the image
    # more strictly and can reject it with error 2069019, so we avoid it.
    photos_url = f"{GRAPH_BASE}/{page_id}/photos"
    upload = request_with_retry(
        "POST",
        photos_url,
        "facebook feed photo upload",
        data={
            "url": image_url,
            "published": "false",
            "access_token": PAGE_ACCESS_TOKEN,
        },
        timeout=60,
    )
    photo_id = upload.json()["id"]

    feed_url = f"{GRAPH_BASE}/{page_id}/feed"
    payload = {
        "message": caption,
        "attached_media[0]": json.dumps({"media_fbid": photo_id}),
        "access_token": PAGE_ACCESS_TOKEN,
    }
    resp = request_with_retry("POST", feed_url, "facebook feed photo post", data=payload, timeout=60)
    return resp.json()


def publish_video_post(page_id, video_url, caption):
    url = f"{GRAPH_BASE}/{page_id}/videos"
    payload = {
        "file_url": video_url,
        "description": caption,
        "access_token": PAGE_ACCESS_TOKEN,
    }
    resp = request_with_retry("POST", url, "facebook video post", data=payload, timeout=120)
    return resp.json()


def publish_facebook_post(media_url, caption, media_kind, page_id=None):
    page_id = page_id or get_page_id()

    if media_kind == "video":
        return publish_video_post(page_id, media_url, caption)
    return publish_photo_post(page_id, media_url, caption)


# =========================
# Story (image / video)
# =========================
def publish_photo_story(page_id, image_url):
    # Step 1: upload the photo unpublished to get a photo id.
    photos_url = f"{GRAPH_BASE}/{page_id}/photos"
    upload = request_with_retry(
        "POST",
        photos_url,
        "facebook story photo upload",
        data={
            "url": image_url,
            "published": "false",
            "access_token": PAGE_ACCESS_TOKEN,
        },
        timeout=60,
    )
    photo_id = upload.json()["id"]

    # Step 2: publish the photo as a story.
    story_url = f"{GRAPH_BASE}/{page_id}/photo_stories"
    resp = request_with_retry(
        "POST",
        story_url,
        "facebook photo story",
        data={"photo_id": photo_id, "access_token": PAGE_ACCESS_TOKEN},
        timeout=60,
    )
    return resp.json()


def publish_video_story(page_id, video_url):
    stories_url = f"{GRAPH_BASE}/{page_id}/video_stories"

    # Phase 1: start an upload session.
    start = request_with_retry(
        "POST",
        stories_url,
        "facebook video story start",
        data={"upload_phase": "start", "access_token": PAGE_ACCESS_TOKEN},
        timeout=60,
    )
    start_data = start.json()
    video_id = start_data["video_id"]
    upload_url = start_data["upload_url"]

    # Phase 2: hand Facebook the hosted video url to fetch.
    request_with_retry(
        "POST",
        upload_url,
        "facebook video story upload",
        headers={
            "Authorization": f"OAuth {PAGE_ACCESS_TOKEN}",
            "file_url": video_url,
        },
        timeout=VIDEO_UPLOAD_TIMEOUT,
    )

    # Phase 3: finish and publish the story.
    finish = request_with_retry(
        "POST",
        stories_url,
        "facebook video story finish",
        data={
            "upload_phase": "finish",
            "video_id": video_id,
            "access_token": PAGE_ACCESS_TOKEN,
        },
        timeout=60,
    )
    return finish.json()


def publish_facebook_story(media_url, media_kind, page_id=None):
    page_id = page_id or get_page_id()

    if media_kind == "video":
        return publish_video_story(page_id, media_url)
    return publish_photo_story(page_id, media_url)
