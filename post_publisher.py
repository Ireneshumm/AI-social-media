import os
import sys
import time
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from msal import ConfidentialClientApplication
from openai import OpenAI
import random

from asset_helpers import (
    is_supported_media_file,
    get_media_kind,
    filename_to_brief,
    is_story_media,
    recent_content_groups,
    pick_with_variety,
    content_group,
    brand_fallback_caption,
)
from wordpress_media import upload_media
from alert_email import send_alert_safely
from facebook_publish import publish_facebook_post, FB_PUBLISH_ENABLED
from video_transcode import ensure_h264
from media_analysis import get_caption_image_uris
from compliance import COMPLIANCE_RULES, scrub_caption, filename_is_noncompliant
from image_hosting import upload_to_imgbb
from onedrive_store import read_json, write_json
from ai_caption import generate_caption_body

load_dotenv()

# =========================
# Config
# =========================
MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")

ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH", "IG Auto Publisher")
ONEDRIVE_POSTS_FOLDER_NAME = os.getenv("ONEDRIVE_POSTS_FOLDER_NAME", "posts")
ONEDRIVE_POSTED_FOLDER_NAME = os.getenv("ONEDRIVE_POSTED_FOLDER_NAME", "posted")
ONEDRIVE_FAILED_FOLDER_NAME = os.getenv("ONEDRIVE_FAILED_FOLDER_NAME", "failed")
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL", "info@rebornaesthetics.com.au")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

IG_USER_ID = os.getenv("IG_USER_ID")
# Strip whitespace/newlines: a token pasted into a secret often carries a
# trailing "\n", which is illegal in the Authorization header used for the
# resumable video upload.
PAGE_ACCESS_TOKEN = (os.getenv("PAGE_ACCESS_TOKEN") or "").strip()
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# Geotag every post with a Facebook Place ID so it surfaces to locals browsing
# that location on Instagram — the single strongest local-discovery signal.
# Set IG_LOCATION_ID to the clinic's Facebook Place ID (find it by running the
# publisher with publisher_type=find_location). When unset, posts publish with
# no location, exactly as before.
IG_LOCATION_ID = (os.getenv("IG_LOCATION_ID") or "").strip()


# --- Performance-based recycling --------------------------------------------
# Each successful publish is logged (media_id -> source asset) to OneDrive so a
# separate ranking job (rank_top_performers.py) can join it with Instagram
# insights and write the list of top-performing assets. Those top assets are
# then recycled MORE often than the rest: instead of appearing once per full
# library cycle, a top asset is also injected at fixed slots within the grid
# cycle, so a strong video can run ~3x per 100 posts (e.g. at the 30th, 60th and
# 90th) while ordinary videos still cycle once.
PERFORMANCE_LOG_FILE = "performance_log.json"
TOP_ASSETS_FILE = "top_assets.json"
PERFORMANCE_LOG_MAX = int(os.getenv("PERFORMANCE_LOG_MAX", "400"))
# Length of the grid cycle the boost slots are measured against.
BOOST_CYCLE = int(os.getenv("BOOST_CYCLE", "100"))
# Positions within each cycle where a top performer is injected (default 30/60/90).
BOOST_SLOTS = {
    int(s.strip())
    for s in (os.getenv("BOOST_SLOTS") or "30,60,90").split(",")
    if s.strip().isdigit()
}


def log_publish_performance(token, media_id, asset_name, media_kind):
    """Append a {media_id, name, kind, ts} record to the OneDrive performance
    log so posts can later be ranked by their Instagram reach. Best-effort: the
    post has already published, so any failure here is only logged."""
    if not media_id or not asset_name:
        return
    try:
        log = read_json(token, PERFORMANCE_LOG_FILE, default=[]) or []
        if not isinstance(log, list):
            log = []
        log.append({
            "media_id": str(media_id),
            "name": asset_name,
            "kind": media_kind,
            "ts": datetime.now().astimezone().isoformat(),
        })
        # Keep the file bounded to the most recent entries.
        if len(log) > PERFORMANCE_LOG_MAX:
            log = log[-PERFORMANCE_LOG_MAX:]
        write_json(token, PERFORMANCE_LOG_FILE, log)
        print(f"Performance log: recorded {asset_name} -> media {media_id}.")
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not update performance log ({e}).")


def load_top_assets(token):
    """Return the set of top-performing asset filenames (as they appear in the
    posted/ archive). Empty set when no ranking has been produced yet."""
    try:
        data = read_json(token, TOP_ASSETS_FILE, default=[]) or []
        names = data.get("names") if isinstance(data, dict) else data
        return {str(n) for n in (names or [])}
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not load top assets ({e}).")
        return set()


def with_location(payload):
    """Attach the configured Facebook Place ID to a media-container payload so
    the published post carries a geotag. No-op when IG_LOCATION_ID is unset."""
    if IG_LOCATION_ID:
        payload["location_id"] = IG_LOCATION_ID
        print(f"Geotag: attaching location_id={IG_LOCATION_ID} to this post.")
    else:
        print("Geotag: IG_LOCATION_ID not set; publishing without a location.")
    return payload


def create_container_safe(url, payload, label):
    """Create a media container, retrying once WITHOUT the geotag if the request
    fails while a location_id is attached. A location_id that Instagram will not
    accept (e.g. a Page with no address, so it is not a taggable place) must
    never block publishing — worst case the post simply goes out ungeotagged."""
    try:
        return post_with_retry(url, payload, timeout=60, label=label)
    except Exception as e:
        if payload.get("location_id"):
            print(
                f"WARNING: {label} failed with a geotag ({e}); "
                "retrying without location_id so the post still publishes."
            )
            payload.pop("location_id", None)
            return post_with_retry(url, payload, timeout=60, label=f"{label} (no geotag)")
        raise

# If the chosen asset fails to publish, fall back to other assets (videos first)
# up to this many total attempts, so a run almost always publishes something.
MAX_PUBLISH_ATTEMPTS = int(os.getenv("MAX_PUBLISH_ATTEMPTS", "4"))

# Repeating video/photo layout for the Instagram profile grid. The default of
# video, video, photo produces a 2-videos-to-1-photo grid. The position advances
# with each published feed post. Override via the FEED_PATTERN env var (a
# comma-separated list, e.g. "video,video,photo").
FEED_PATTERN = [
    k.strip() for k in (os.getenv("FEED_PATTERN") or "video,video,photo").split(",") if k.strip()
]

# Video containers are processed asynchronously by Instagram, so we poll the
# container status before publishing. Defaults give up to ~5 minutes.
VIDEO_POLL_MAX_ATTEMPTS = int(os.getenv("VIDEO_POLL_MAX_ATTEMPTS", "30"))
VIDEO_POLL_INTERVAL = int(os.getenv("VIDEO_POLL_INTERVAL", "10"))

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

REQUIRED_ENV_VARS = [
    "MS_TENANT_ID",
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
    "OPENAI_API_KEY",
    "IG_USER_ID",
    "PAGE_ACCESS_TOKEN",
]


# =========================
# Validation / logging
# =========================
def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def log_startup():
    print("Starting post publisher...")
    print(f"OneDrive root path: {ONEDRIVE_ROOT_PATH}")
    print(f"Posts folder name : {ONEDRIVE_POSTS_FOLDER_NAME}")
    print(f"OpenAI model      : {OPENAI_MODEL}")
    print(f"Graph version     : {GRAPH_VERSION}")
    print(f"Dry run           : {DRY_RUN}")
    print("Environment variables loaded successfully.\n")


# =========================
# Microsoft Graph helpers
# =========================
def get_access_token():
    app = ConfidentialClientApplication(
        client_id=MS_CLIENT_ID,
        client_credential=MS_CLIENT_SECRET,
        authority=AUTHORITY,
    )
    result = app.acquire_token_for_client(scopes=SCOPES)

    if "access_token" not in result:
        raise Exception(f"Failed to get token: {result}")

    return result["access_token"]


def get_retry_delay(resp, attempt):
    retry_after = resp.headers.get("Retry-After") if resp is not None else None
    if retry_after:
        try:
            return int(retry_after)
        except ValueError:
            pass

    retry_delays = [5, 10]
    return retry_delays[attempt - 1]


def request_with_retry(method, url, token, payload=None, timeout=30, label="Microsoft Graph request"):
    headers = {"Authorization": f"Bearer {token}"}
    if payload is not None:
        headers["Content-Type"] = "application/json"

    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            last_error = e
            resp = e.response
            status_code = resp.status_code if resp is not None else None
            should_retry = status_code == 429 or (
                status_code is not None and 500 <= status_code < 600
            )

            if not should_retry:
                print(f"FAIL: {label} failed with non-retryable HTTP error on attempt {attempt}: {e}")
                raise

            if attempt < 3:
                delay = get_retry_delay(resp, attempt)
                print(f"WARNING: {label} attempt {attempt} failed with HTTP {status_code}. Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: {label} attempt {attempt} failed with HTTP {status_code}: {e}")
        except requests.RequestException as e:
            last_error = e

            if attempt < 3:
                delay = get_retry_delay(None, attempt)
                print(f"WARNING: {label} attempt {attempt} failed. Retrying in {delay} second(s): {e}")
                time.sleep(delay)
            else:
                print(f"FAIL: {label} attempt {attempt} failed: {e}")

    raise last_error


def graph_get(url, token):
    resp = request_with_retry("GET", url, token, timeout=30, label="Microsoft Graph GET")
    return resp.json()


def graph_get_bytes(url, token):
    resp = request_with_retry("GET", url, token, timeout=30, label="Microsoft Graph download")
    return resp.content


def graph_patch(url, token, payload):
    resp = request_with_retry("PATCH", url, token, payload=payload, timeout=30, label="Microsoft Graph PATCH")
    return resp.json()


def find_named_folder(items, folder_name):
    if isinstance(items, dict):
        items = items.get("value", [])

    for item in items:
        if item.get("name") == folder_name and "folder" in item:
            return item
    return None


def get_project_children(token):
    root_url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/root/children"
    root_items = graph_get(root_url, token)

    project_folder = find_named_folder(root_items, ONEDRIVE_ROOT_PATH)
    if not project_folder:
        raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")

    project_children_url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{project_folder['id']}/children"
    project_children = graph_get(project_children_url, token)

    return project_folder, project_children.get("value", [])


def get_posts_items(token):
    _, project_children = get_project_children(token)

    posts_folder = find_named_folder(project_children, ONEDRIVE_POSTS_FOLDER_NAME)
    if not posts_folder:
        raise Exception(f"Posts folder not found: {ONEDRIVE_POSTS_FOLDER_NAME}")

    posts_children_url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{posts_folder['id']}/children"
    posts_children = graph_get(posts_children_url, token)
    return posts_children.get("value", [])


def download_file(token, file_id):
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{file_id}/content"
    return graph_get_bytes(url, token)


def ensure_ig_image(path):
    """Re-encode an image to a clean 8-bit sRGB JPEG within Instagram's limits.

    Instagram rejects CMYK / odd-profile / oversized images with 'Only photo or
    video can be accepted as media type' (error 2207052) even though they open
    fine in a browser — a common trait of photos exported from pro cameras or
    Photoshop. AI-generated images are already clean RGB, but user uploads may
    not be. Converting to RGB and capping the long side makes every image
    reliably fetchable and acceptable. Falls back to the original on any error."""
    try:
        from PIL import Image
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: Pillow unavailable ({e}); skipping image normalize.")
        return path
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")  # drops alpha/CMYK/palette -> 8-bit sRGB
            max_side = 1440           # Instagram's max supported width
            width, height = img.size
            if max(width, height) > max_side:
                scale = max_side / float(max(width, height))
                img = img.resize(
                    (max(1, round(width * scale)), max(1, round(height * scale))),
                    Image.LANCZOS,
                )
            out = os.path.splitext(path)[0] + "_ig.jpg"
            img.save(out, "JPEG", quality=90)
        print(f"Normalized image for Instagram: {out} ({os.path.getsize(out)} bytes)")
        return out
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: image normalize failed ({e}); using original file.")
        return path


# =========================
# Folder / archive helpers
# =========================
def get_subfolder_by_path(token, top_folder_name, subfolder_name):
    _, project_children = get_project_children(token)

    top_folder = find_named_folder(project_children, top_folder_name)
    if not top_folder:
        raise Exception(f"Top folder not found: {top_folder_name}")

    top_children_url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{top_folder['id']}/children"
    top_children = graph_get(top_children_url, token)

    subfolder = find_named_folder(top_children, subfolder_name)
    if not subfolder:
        raise Exception(f"Subfolder not found: {top_folder_name}/{subfolder_name}")

    return subfolder


def get_folder_children(token, folder_id):
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{folder_id}/children"
    children = graph_get(url, token)
    return children.get("value", [])


def filename_exists(items, filename):
    filename_lower = filename.lower()
    return any(item.get("name", "").lower() == filename_lower for item in items)


def add_timestamp_suffix(filename, timestamp):
    base, ext = os.path.splitext(filename)
    return f"{base}_{timestamp}{ext}"


def get_conflict_safe_name(items, filename, timestamp):
    if not filename_exists(items, filename):
        return filename

    new_name = add_timestamp_suffix(filename, timestamp)
    print(f"Filename conflict detected in archive folder: {filename}")
    print(f"Renaming during move: {filename} -> {new_name}")
    return new_name


def move_item_to_folder(token, item_id, target_folder_id, new_name=None):
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{item_id}"
    payload = {
        "parentReference": {
            "id": target_folder_id
        }
    }
    if new_name:
        payload["name"] = new_name

    return graph_patch(url, token, payload)


def archive_post_assets(token, selected_post, success=True):
    target_top = ONEDRIVE_POSTED_FOLDER_NAME if success else ONEDRIVE_FAILED_FOLDER_NAME
    target_subfolder = get_subfolder_by_path(token, target_top, ONEDRIVE_POSTS_FOLDER_NAME)

    media_item = selected_post["media"]
    target_items = get_folder_children(token, target_subfolder["id"])
    archive_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    media_name = get_conflict_safe_name(
        target_items,
        media_item["name"],
        archive_timestamp,
    )

    move_item_to_folder(
        token,
        media_item["id"],
        target_subfolder["id"],
        media_name if media_name != media_item["name"] else None,
    )

    return {
        "target_folder": f"{target_top}/{ONEDRIVE_POSTS_FOLDER_NAME}",
        "media_name": media_name,
    }


# =========================
# Asset matching
# =========================
def _normalize_kind(value):
    """Map user-facing kind words to the internal kind labels ('video'/'image').
    Returns '' for anything unrecognized (meaning: no kind preference)."""
    v = (value or "").strip().lower()
    if v in ("video", "videos", "reel", "reels", "vid"):
        return "video"
    if v in ("photo", "photos", "image", "images", "img", "picture", "pic"):
        return "image"
    return ""


def match_post_assets(items):
    # A single drop folder feeds both channels: tall/vertical (9:16) media is
    # reserved for Stories, so feed posting takes everything that is NOT vertical
    # (feed-shaped, plus any file whose dimensions are unknown).
    matched = []
    for item in items:
        if "folder" in item:
            continue

        name = item.get("name", "")
        if not is_supported_media_file(name):
            continue
        if is_story_media(item):
            continue
        # Compliance safety net: never publish an injectable/peptide asset, even
        # if one is still sitting in the queue (TGA — see compliance.py).
        if filename_is_noncompliant(name):
            print(f"Skipping non-compliant asset (injectable/peptide): {name}")
            continue

        matched.append({
            "base_name": os.path.splitext(name)[0],
            "media": item,
            "kind": get_media_kind(name),
        })

    matched.sort(key=lambda x: x["base_name"])
    return matched


# =========================
# Content helpers
# =========================
def decode_text_file(content_bytes):
    return content_bytes.decode("utf-8").strip()


def parse_post_text(text_content):
    lines = [line.strip() for line in text_content.splitlines() if line.strip()]
    image_url = None
    brief_lines = []

    for line in lines:
        if line.lower().startswith("image_url:"):
            image_url = line.split(":", 1)[1].strip()
        else:
            brief_lines.append(line)

    brief = "\n".join(brief_lines).strip()
    return image_url, brief


# Fixed contact/CTA block appended to every post caption (no hashtags — those
# are built separately and rotated below for local reach). The AI writes only
# the body, so this keeps CTA, contact and locations identical on every post.
CONTACT_FOOTER = (
    "🇦🇺 In-clinic treatments in Brisbane, Australia only.\n\n"
    "All bookings are made online at https://www.rebornaesthetics.com.au/ — just click "
    "“Book Now”. If you’re unsure which treatment suits you, book a complimentary "
    "consultation for a personalised plan.\n\n"
    "📞 0410 415 415 (Brisbane clinic)\n"
    "📧 info@rebornaesthetics.com.au\n"
    "🌐 www.rebornaesthetics.com.au\n\n"
    "📍 Annerley — 69 Juliette Street (Brisbane Southside)\n"
    "📍 Fortitude Valley — 27 Brunswick Street"
)

# --- Local-first hashtags ---------------------------------------------------
# Goal: be discovered by real Brisbane locals who can actually walk in and book,
# NOT by overseas device fans, other clinics, or follow-for-follow peers. So we
# deliberately DROP: giant catch-all tags (#brisbane), peer/networker tags
# (#brisbanemakeupartist, #brisbanebeautybloggers, #brisbanephotographer), and
# global device tags (#picowaylaser). Suburb-level tags carry the strongest
# local intent, backed by "service + Brisbane" tags people actually search when
# looking for a clinic near them.

# Always included: the two clinic suburbs, the region, and the strongest
# local-intent service tags.
LOCAL_CORE_TAGS = [
    "#annerley", "#fortitudevalley", "#brisbanesouthside",
    "#skinclinicbrisbane", "#brisbanecosmeticclinic", "#brisbaneaesthetics",
]

# Rotated per post (a different subset each time) so the block is not identical
# on every post — a repeated wall of tags can suppress reach — while every
# option stays local (a nearby suburb) or local-service intent.
LOCAL_ROTATING_TAGS = [
    # suburbs near Annerley (southside, 4103) and Fortitude Valley (inner city, 4006)
    "#woolloongabba", "#westendbrisbane", "#southbrisbane", "#greenslopes",
    "#coorparoo", "#tarragindi", "#moorooka", "#yeronga", "#kangaroopoint",
    "#newfarmbrisbane", "#teneriffe", "#springhillbrisbane", "#brisbanenorthside",
    # service + Brisbane search intent
    "#brisbaneskinclinic", "#brisbaneskincare", "#brisbanefacials",
    "#brisbanebeautyclinic", "#brisbanelaserclinic", "#skinneedlingbrisbane",
    "#iplbrisbane", "#hifubrisbane", "#acnescarsbrisbane", "#brisbaneskin",
    "#hydrafacialbrisbane",
]


def build_local_hashtags(seed_text, rotating_count=12):
    """Return a local-first hashtag block. The core suburb/service tags are
    always present; a rotating subset of nearby-suburb and service tags is drawn
    from a stable per-asset seed, so the same asset stays consistent across
    retries while different assets vary (keeps the block from being identical on
    every post, which can hurt reach)."""
    rng = random.Random(seed_text or "")
    pool = list(LOCAL_ROTATING_TAGS)
    rng.shuffle(pool)
    tags = LOCAL_CORE_TAGS + pool[:rotating_count]
    return " ".join(tags)


def compose_caption(body, brief_text):
    """Body + fixed contact/CTA block + a rotated local-first hashtag block."""
    return f"{body}\n\n{CONTACT_FOOTER}\n\n{build_local_hashtags(brief_text)}"


def _build_caption_prompt(brief_text, has_images):
    """The caption instructions. Written for a strong local-Brisbane hook; the
    fixed footer (CTA, contact, hashtags) is added separately, so the model
    writes only the body."""
    source = (
        "The attached image(s) are the actual post media (for a video, they are sampled "
        "frames). Look at what is shown and write the caption about that content. Use this "
        f"filename hint only as extra context, it may name the treatment: {brief_text}"
        if has_images
        else f"Use this content brief:\n{brief_text}"
    )
    return f"""You are the social media copywriter for Reborn Aesthetics, a premium medical-aesthetics clinic in Brisbane, Australia (clinics in Annerley and Fortitude Valley).

{source}

Write ONLY the Instagram caption body — engaging, premium, warm and human, the kind locals stop scrolling for.

Requirements:
- Open with a scroll-stopping first line (a hook — a question, a relatable moment, or a striking benefit). Not "At Reborn Aesthetics...".
- Speak to LOCAL Brisbane women so nearby residents feel this is their neighbourhood clinic. Where it reads naturally, root it locally (Brisbane's southside, Annerley, Fortitude Valley, "local to you") — but do NOT stuff suburb names or sound like an ad.
- Length: short to medium (roughly 2–5 short lines). A few tasteful emoji are fine.
- Write ONLY the caption body. Do NOT include hashtags, calls to action, booking instructions, links, phone numbers, email, or address — a fixed footer with all of that is added automatically after your text.
- No medical claims and no guaranteed results.

{COMPLIANCE_RULES}"""


def generate_caption(brief_text, image_uris=None):
    prompt = _build_caption_prompt(brief_text, has_images=bool(image_uris))
    body = generate_caption_body(prompt, image_uris=image_uris)
    if body:
        return compose_caption(scrub_caption(body), brief_text)

    # No AI provider available (e.g. no keys / all out of credit). Degrade
    # gracefully with an on-brand template so the post still publishes.
    print("WARNING: no AI caption available; using brand template caption.")
    body = scrub_caption(brand_fallback_caption(brief_text))
    return compose_caption(body, brief_text)


# =========================
# Instagram publish
# =========================
def print_error_response(error):
    resp = getattr(error, "response", None)
    if resp is None:
        return

    body = resp.text
    if body:
        print("Instagram Graph API error response:")
        print(body)


# Graph errors where Instagram simply failed to fetch the media from the URL
# are effectively transient (its fetcher hiccups), so they are worth retrying.
TRANSIENT_GRAPH_SUBCODES = {2207003, 2207020, 2207052}


def is_transient_graph_error(resp):
    if resp is None:
        return False
    try:
        error = resp.json().get("error", {})
    except ValueError:
        return False
    if error.get("is_transient"):
        return True
    return error.get("error_subcode") in TRANSIENT_GRAPH_SUBCODES


def post_with_retry(url, payload, timeout=60, label="Instagram Graph request"):
    retry_delays = [5, 10]
    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, data=payload, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            last_error = e
            status_code = e.response.status_code if e.response is not None else None
            should_retry = (
                status_code is not None and 500 <= status_code < 600
            ) or is_transient_graph_error(e.response)

            if not should_retry:
                print(f"FAIL: {label} failed with non-retryable HTTP error on attempt {attempt}: {e}")
                print_error_response(e)
                raise

            if attempt < 3:
                delay = retry_delays[attempt - 1]
                print(f"WARNING: {label} attempt {attempt} failed with HTTP {status_code}: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: {label} attempt {attempt} failed with HTTP {status_code}: {e}")
                print_error_response(e)
        except requests.RequestException as e:
            last_error = e

            if attempt < 3:
                delay = retry_delays[attempt - 1]
                print(f"WARNING: {label} attempt {attempt} failed: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: {label} attempt {attempt} failed: {e}")

    raise last_error


def create_media_container(image_url, caption):
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    payload = with_location({
        "image_url": image_url,
        "caption": caption,
        "access_token": PAGE_ACCESS_TOKEN,
    })
    resp = create_container_safe(url, payload, label="create_media_container")
    return resp.json()


def create_video_media_container(video_url, caption):
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    payload = with_location({
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": PAGE_ACCESS_TOKEN,
    })
    resp = create_container_safe(url, payload, label="create_video_media_container")
    return resp.json()


def get_container_status(creation_id):
    url = f"{GRAPH_BASE}/{creation_id}"
    params = {
        "fields": "status_code,status",
        "access_token": PAGE_ACCESS_TOKEN,
    }

    retry_delays = [5, 10]
    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_error = e
            if attempt < 3:
                delay = retry_delays[attempt - 1]
                print(f"WARNING: get_container_status attempt {attempt} failed: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: get_container_status attempt {attempt} failed: {e}")

    raise last_error


def wait_for_container_ready(creation_id):
    for attempt in range(1, VIDEO_POLL_MAX_ATTEMPTS + 1):
        status = get_container_status(creation_id)
        status_code = status.get("status_code")

        if status_code == "FINISHED":
            print(f"Container {creation_id} is ready to publish.")
            return

        if status_code == "ERROR":
            raise RuntimeError(
                f"Media container processing failed for {creation_id}: {status}"
            )

        print(
            f"Container {creation_id} status: {status_code} "
            f"(attempt {attempt}/{VIDEO_POLL_MAX_ATTEMPTS}). "
            f"Waiting {VIDEO_POLL_INTERVAL}s..."
        )
        time.sleep(VIDEO_POLL_INTERVAL)

    raise RuntimeError(
        f"Timed out waiting for media container {creation_id} to finish processing."
    )


def publish_media_container(creation_id):
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media_publish"
    payload = {
        "creation_id": creation_id,
        "access_token": PAGE_ACCESS_TOKEN,
    }
    resp = post_with_retry(url, payload, timeout=60, label="publish_media_container")
    return resp.json()


def create_video_container_resumable(caption):
    # Ask Instagram for a resumable upload container so we can send the video
    # bytes directly (WordPress blocks Instagram's video fetcher).
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    payload = with_location({
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "access_token": PAGE_ACCESS_TOKEN,
    })
    resp = create_container_safe(url, payload, label="create_video_container_resumable")
    return resp.json()


def upload_video_bytes(creation_id, upload_uri, video_path):
    if not upload_uri:
        upload_uri = f"https://rupload.facebook.com/ig-api-upload/{GRAPH_VERSION}/{creation_id}"

    with open(video_path, "rb") as f:
        video_bytes = f.read()

    headers = {
        "Authorization": f"OAuth {PAGE_ACCESS_TOKEN}",
        "offset": "0",
        "file_size": str(len(video_bytes)),
    }

    retry_delays = [5, 10]
    last_error = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(upload_uri, headers=headers, data=video_bytes, timeout=300)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            last_error = e
            if attempt < 3:
                delay = retry_delays[attempt - 1]
                print(f"WARNING: upload_video_bytes attempt {attempt} failed: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: upload_video_bytes attempt {attempt} failed: {e}")
                if getattr(e, "response", None) is not None:
                    print_error_response(e)

    raise last_error


def publish_instagram_post(media_url, caption, media_kind, media_path=None):
    if media_kind == "video":
        container = create_video_container_resumable(caption)
        creation_id = container["id"]
        upload_video_bytes(creation_id, container.get("uri"), media_path)
        wait_for_container_ready(creation_id)
    else:
        container = create_media_container(media_url, caption)
        creation_id = container["id"]
        time.sleep(5)

    published = publish_media_container(creation_id)
    return {
        "creation_id": creation_id,
        "media_id": published["id"],
    }


# =========================
# Main flow
# =========================
def order_publish_candidates(matched, first, rng):
    """Order the assets we will try to publish this run.

    The variety pick goes first. If it fails, we fall back to the remaining
    assets with VIDEOS FIRST — videos are uploaded to Instagram/Facebook as raw
    bytes, so they never hit the "media could not be fetched" image problem and
    are the most reliable way to make sure *something* gets published each run.
    """
    remaining = [m for m in matched if m is not first]
    videos = [m for m in remaining if m["kind"] == "video"]
    images = [m for m in remaining if m["kind"] != "video"]
    rng.shuffle(videos)
    rng.shuffle(images)
    return [first] + videos + images


def recyclable_videos(posted_children):
    """Previously-posted videos from the archive, offered for RE-POSTING when the
    live queue has no fresh video. Returned OLDEST-POSTED FIRST so the caller can
    round-robin: always repost the video that hasn't been shown for the longest,
    so every video in the library is cycled through before any repeats. Marked
    recycled=True so a successful repost is not re-archived."""
    out = []
    for it in posted_children or []:
        if "folder" in it:
            continue
        name = it.get("name", "")
        if get_media_kind(name) != "video":
            continue
        if not is_supported_media_file(name) or filename_is_noncompliant(name):
            continue
        out.append({
            "base_name": os.path.splitext(name)[0],
            "media": it,
            "kind": "video",
            "recycled": True,
            "_posted_at": it.get("lastModifiedDateTime") or "",
        })
    # Oldest last-posted first = least recently shown.
    out.sort(key=lambda m: m["_posted_at"])
    return out


def touch_item(token, item_id):
    """Bump an archived item's modified time to now, so a recycled repost moves to
    the BACK of the round-robin and won't be picked again until the rest cycle."""
    now = datetime.now().astimezone().isoformat()
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{item_id}"
    graph_patch(url, token, {"fileSystemInfo": {"lastModifiedDateTime": now}})


def attempt_publish(token, selected_post):
    """Run the full download -> caption -> publish -> archive-success flow for a
    single asset. Returns the Instagram publish result on success. Raises on any
    failure so the caller can archive the failed asset and fall back to another.
    """
    media_kind = selected_post["kind"]
    print(f"Selected post: {selected_post['base_name']}")
    print(f"Media file: {selected_post['media']['name']} (kind: {media_kind})\n")

    print("Step 4: Generating brief from media filename...")
    brief_text = filename_to_brief(selected_post["media"]["name"])

    if not brief_text:
        raise Exception("Brief text is empty.")

    print("Generated brief text:")
    print(brief_text)
    print()

    print("Step 5: Downloading media file for local verification...")
    media_bytes = download_file(token, selected_post["media"]["id"])
    os.makedirs("temp", exist_ok=True)
    media_path = os.path.join("temp", selected_post["media"]["name"])

    with open(media_path, "wb") as f:
        f.write(media_bytes)

    print(f"Media saved to: {media_path}")
    print(f"Media size: {len(media_bytes)} bytes\n")

    if media_kind == "video":
        print("Step 5b: Ensuring video is H.264 (transcode if needed)...")
        media_path = ensure_h264(media_path)
        print()

    if media_kind == "video":
        # Videos are uploaded directly to Instagram and Facebook as bytes,
        # so WordPress hosting (which blocks their video fetchers) is skipped.
        print("Step 6: Skipping WordPress upload for video (sent directly to Instagram/Facebook).\n")
        media_url = None
    else:
        print("Step 5c: Normalizing image for Instagram (sRGB JPEG)...")
        media_path = ensure_ig_image(media_path)

        # Prefer imgbb — Instagram reliably fetches it. Fall back to
        # WordPress when no imgbb key is set or the upload fails.
        media_url = upload_to_imgbb(media_path)
        if media_url:
            print(f"Step 6: Hosted image on imgbb: {media_url}")
        else:
            print("Step 6: Uploading media to WordPress Media Library...")
            media_result = upload_media(Path(media_path))
            media_url = media_result.get("source_url")
            if not media_url:
                raise RuntimeError("WordPress media upload did not return source_url.")
            print(f"WordPress source_url: {media_url}")
        print()

    print("Step 7: Generating caption with OpenAI (from media content)...")
    content_images = get_caption_image_uris(media_path, media_kind)
    if content_images:
        print(f"Analyzing {len(content_images)} image(s) from the media for the caption.")
    else:
        print("No media images available; using filename brief only.")
    caption = generate_caption(brief_text, image_uris=content_images)
    print("Caption generated.\n")

    print("Generated caption:")
    print(caption)
    print()

    if DRY_RUN:
        print("DRY_RUN=true, skipping Instagram publish.")
        sys.exit(0)

    print("Step 8: Publishing to Instagram...")
    publish_result = publish_instagram_post(media_url, caption, media_kind, media_path=media_path)
    print("Instagram publish completed.\n")

    print("Publish result:")
    print(f"creation_id: {publish_result['creation_id']}")
    print(f"media_id   : {publish_result['media_id']}")
    print()

    if FB_PUBLISH_ENABLED:
        print("Step 8b: Cross-posting to Facebook Page...")
        try:
            fb_result = publish_facebook_post(media_url, caption, media_kind, media_path=media_path)
            print(f"Facebook post published: {fb_result}\n")
        except Exception as fb_error:
            # Instagram already succeeded; a Facebook failure must not fail
            # the run. Surface it via logs and a non-fatal alert instead.
            print(f"WARNING: Facebook cross-post failed (Instagram already succeeded): {fb_error}\n")
            send_alert_safely(
                "Reborn Auto Publisher: Facebook cross-post failed (post)",
                "\n".join([
                    "Instagram post succeeded but the Facebook cross-post failed.",
                    f"Media: {selected_post['media']['name']}",
                    f"Error: {fb_error}",
                    "",
                    "Please check GitHub Actions logs. The Instagram post was published normally.",
                ]),
            )

    # The name under which this asset now lives in the posted/ archive — this is
    # the key the performance log and the top-assets list are matched on.
    posted_name = selected_post["media"]["name"]

    if selected_post.get("recycled"):
        # A repost of an already-archived video — leave the file in place, but bump
        # its timestamp so it goes to the back of the rotation (won't repeat until
        # every other video has been shown).
        print("Step 9: Recycled repost — left original in posted/posts; moving it to the back of the rotation.\n")
        try:
            touch_item(token, selected_post["media"]["id"])
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: could not update recycle timestamp ({e}); rotation may repeat sooner.")
    else:
        print("Step 9: Archiving success item to posted/posts...")
        archive_result = archive_post_assets(token, selected_post, success=True)
        print("Archive completed.")
        print(f"Moved to: {archive_result['target_folder']}")
        print(f"Media: {archive_result['media_name']}")
        posted_name = archive_result.get("media_name") or posted_name
        print()

    # Record which asset produced which Instagram post, so it can later be ranked
    # by reach and recycled more often if it performs well. Never fails the post.
    if not DRY_RUN:
        log_publish_performance(token, publish_result.get("media_id"), posted_name, media_kind)

    return publish_result


def main():
    token = None

    try:
        validate_env()
        log_startup()

        print("Step 1: Getting Microsoft access token...")
        token = get_access_token()
        print("OK\n")

        print("Step 2: Loading posts folder items...")
        items = get_posts_items(token)
        print(f"Found {len(items)} item(s) in posts/\n")

        print("Step 3: Finding feed-shaped post assets...")
        matched = match_post_assets(items)

        if not matched:
            print("No valid feed post assets found. Exit gracefully.")
            sys.exit(0)

        # Steer away from the kinds of content most recently posted, and count how
        # many feed posts we have published so far so the grid can follow a fixed
        # video/photo layout pattern.
        recent_groups = set()
        posted_count = 0
        posted_children = []
        try:
            posted_sub = get_subfolder_by_path(token, ONEDRIVE_POSTED_FOLDER_NAME, ONEDRIVE_POSTS_FOLDER_NAME)
            posted_children = get_folder_children(token, posted_sub["id"])
            recent_groups = recent_content_groups(posted_children, n=2)
            posted_count = sum(
                1 for it in posted_children
                if "folder" not in it and is_supported_media_file(it.get("name", ""))
            )
        except Exception as e:
            print(f"Variety/pattern history unavailable ({e}); selecting at random.")

        # Decide the media kind for this run so the Instagram profile grid follows
        # the desired repeating layout (default 2 videos : 1 photo). The position
        # advances with every published post, producing V, V, P, V, V, P, ...
        # A manual PREFERRED_KIND overrides the pattern (handy for one-off tests).
        want_kind = _normalize_kind(os.getenv("PREFERRED_KIND"))
        if want_kind:
            print(f"PREFERRED_KIND override: targeting a {want_kind} post this run.")
        elif FEED_PATTERN:
            pos = posted_count % len(FEED_PATTERN)
            want_kind = _normalize_kind(FEED_PATTERN[pos])
            print(
                f"Feed grid pattern {FEED_PATTERN}: position {pos} -> {want_kind or 'any'} "
                f"({posted_count} post(s) already published)."
            )

        # Previously-posted videos available for re-posting. Video is the primary
        # content and new videos are scarce, so when a video slot has no fresh
        # video in the queue we recycle the back-catalogue instead of falling back
        # to a photo — keeping the grid video-heavy.
        recycle = recyclable_videos(posted_children)  # oldest-posted first

        # Top performers (by Instagram reach) are recycled more often: at fixed
        # slots within the grid cycle we inject a top-performing video instead of
        # the plain oldest one, so a strong video runs ~3x per cycle while the
        # rest still cycle once. No-op until a ranking has been produced.
        top_assets = load_top_assets(token)
        cycle_pos = (posted_count % BOOST_CYCLE) if BOOST_CYCLE else -1
        boost_now = cycle_pos in BOOST_SLOTS and bool(top_assets)
        if top_assets:
            print(
                f"Top performers loaded: {len(top_assets)} asset(s); grid cycle "
                f"position {cycle_pos}/{BOOST_CYCLE}; boost slot = {boost_now}."
            )

        # How repeats are chosen:
        #  - Below REPEAT_TOP_ONLY_ABOVE distinct archived videos, recycle from
        #    EVERYTHING so the whole library airs before anything repeats (maximum
        #    spacing — the priority while the library is still small).
        #  - Once the archive is larger than that, recycle ONLY the top-viewed
        #    set, so the repeats you do get are the best-performing videos.
        # (Note: with a small library, repeats are unavoidable no matter what —
        #  N distinct videos cannot fill more than N slots without repeating.)
        REPEAT_TOP_ONLY_ABOVE = int(os.getenv("REPEAT_TOP_ONLY_ABOVE", "50"))
        if top_assets and len(recycle) > REPEAT_TOP_ONLY_ABOVE:
            top_recycle = [m for m in recycle if m["media"]["name"] in top_assets]
            if top_recycle:
                print(
                    f"Archive has {len(recycle)} videos (> {REPEAT_TOP_ONLY_ABOVE}); "
                    f"recycling only the top {len(top_recycle)} performer(s)."
                )
                recycle = top_recycle

        pool = matched
        recycling = False
        if want_kind:
            same_kind = [m for m in matched if m["kind"] == want_kind]
            if same_kind:
                pool = same_kind
            elif want_kind == "video" and recycle:
                pool = recycle
                recycling = True
                print(f"No fresh video in queue; recycling from {len(recycle)} previously-posted video(s).")
            else:
                print(f"No {want_kind} asset available (queue or archive); falling back to any kind.")

        if recycling:
            # Round-robin: repost the video that has gone longest without airing
            # (skip the last couple of topics for variety), so the whole library
            # cycles through before any video repeats.
            eligible = [m for m in pool if content_group(m["media"]["name"]) not in recent_groups]
            ranked = eligible or pool
            # On a boost slot, prefer the least-recently-shown TOP performer so
            # strong videos get extra airings; otherwise take the plain oldest.
            top_ranked = [m for m in ranked if m["media"]["name"] in top_assets] if boost_now else []
            if top_ranked:
                first = top_ranked[0]
                print(f"Boost slot {cycle_pos}: reposting top performer {first['media']['name']}.")
            else:
                first = ranked[0]
        else:
            first = pick_with_variety(pool, recent_groups, random)
        tag = " (recycled repost)" if first.get("recycled") else ""
        print(
            f"{len(matched)} queued + {len(recycle)} recyclable video(s); "
            f"avoided recent {sorted(recent_groups) or 'none'}; selected {first['media']['name']}{tag}."
        )

        # Try the variety pick, then fall back to other assets — including recycled
        # videos (videos first) — so a slot is never left empty.
        candidates = order_publish_candidates(matched + recycle, first, random)[:MAX_PUBLISH_ATTEMPTS]

        failures = []
        for idx, selected_post in enumerate(candidates):
            if idx > 0:
                print(
                    f"\nPrevious asset failed; falling back to candidate {idx + 1}/{len(candidates)} "
                    f"(prefer video): {selected_post['media']['name']} (kind: {selected_post['kind']})\n"
                )
            try:
                attempt_publish(token, selected_post)
                print("Post MVP completed successfully.")
                sys.exit(0)
            except Exception as attempt_error:
                print("\nERROR:", str(attempt_error))
                failed_name = selected_post["media"]["name"]
                if selected_post.get("recycled"):
                    # An archive item — leave it in place (do not move to failed).
                    print("Recycled video failed to publish; leaving it in the archive.")
                    failures.append(f"{failed_name} (recycled): {attempt_error}")
                    continue
                try:
                    print("Archiving failed item to failed/posts...")
                    archive_result = archive_post_assets(token, selected_post, success=False)
                    print(f"Failed asset archived. Moved to: {archive_result['target_folder']}")
                    failures.append(f"{failed_name}: {attempt_error}")
                except Exception as archive_error:
                    print("Failed to archive failed item:", str(archive_error))
                    failures.append(f"{failed_name}: {attempt_error} (archive also failed: {archive_error})")

        # Every candidate we tried failed. Surface the whole set in one alert.
        send_alert_safely(
            "Reborn IG Auto Publisher Failed: post",
            "\n".join([
                f"Instagram Post publishing failed after trying {len(candidates)} asset(s).",
                "",
                "Attempts:",
                *[f"  - {f}" for f in failures],
                "",
                "Please check GitHub Actions logs and the OneDrive failed/posts folder.",
            ]),
        )
        sys.exit(1)

    except Exception as e:
        print("\nERROR:", str(e))
        send_alert_safely(
            "Reborn IG Auto Publisher Failed: post",
            "\n".join([
                "Instagram Post publishing failed before any asset could be tried.",
                f"Error: {e}",
                "",
                "Please check GitHub Actions logs.",
            ]),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
