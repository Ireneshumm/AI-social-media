import os
import sys
import time
import requests
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from msal import ConfidentialClientApplication
from openai import OpenAI
from asset_helpers import is_supported_media_file, get_media_kind, filename_to_brief
from wordpress_media import upload_media
from alert_email import send_alert_safely
from facebook_publish import publish_facebook_post, FB_PUBLISH_ENABLED

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
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

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
def match_post_assets(items):
    matched = []
    for item in items:
        if "folder" in item:
            continue

        name = item.get("name", "")
        if is_supported_media_file(name):
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


def generate_caption(brief_text):
    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
You are writing an Instagram caption for Reborn Aesthetics, a premium aesthetics clinic in Brisbane.

Use the following content brief:
{brief_text}

Requirements:
- Tone: premium, warm, professional
- Length: short to medium
- Make it suitable for an Instagram post
- Include a soft call to action
- Add 3 to 5 relevant hashtags
"""

    retry_delays = [5, 10]
    last_error = None

    for attempt in range(1, 4):
        try:
            response = client.responses.create(
                model=OPENAI_MODEL,
                input=prompt
            )
            return response.output_text.strip()
        except Exception as e:
            last_error = e

            if attempt < 3:
                delay = retry_delays[attempt - 1]
                print(f"WARNING: OpenAI caption attempt {attempt} failed: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: OpenAI caption attempt {attempt} failed: {e}")

    raise last_error


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
            should_retry = status_code is not None and 500 <= status_code < 600

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
    payload = {
        "image_url": image_url,
        "caption": caption,
        "access_token": PAGE_ACCESS_TOKEN,
    }
    resp = post_with_retry(url, payload, timeout=60, label="create_media_container")
    return resp.json()


def create_video_media_container(video_url, caption):
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    payload = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": PAGE_ACCESS_TOKEN,
    }
    resp = post_with_retry(url, payload, timeout=60, label="create_video_media_container")
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


def publish_instagram_post(media_url, caption, media_kind):
    if media_kind == "video":
        container = create_video_media_container(media_url, caption)
        creation_id = container["id"]
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
def main():
    selected_post = None
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

        print("Step 3: Finding post image assets...")
        matched = match_post_assets(items)

        if not matched:
            print("No valid post image assets found. Exit gracefully.")
            sys.exit(0)

        selected_post = matched[0]
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

        print("Step 6: Uploading media to WordPress Media Library...")
        media_result = upload_media(Path(media_path))
        media_url = media_result.get("source_url")

        if not media_url:
            raise RuntimeError("WordPress media upload did not return source_url.")

        print("WordPress source_url:")
        print(media_url)
        print()

        print("Step 7: Generating caption with OpenAI...")
        caption = generate_caption(brief_text)
        print("Caption generated.\n")

        print("Generated caption:")
        print(caption)
        print()

        if DRY_RUN:
            print("DRY_RUN=true, skipping Instagram publish.")
            sys.exit(0)

        print("Step 8: Publishing to Instagram...")
        publish_result = publish_instagram_post(media_url, caption, media_kind)
        print("Instagram publish completed.\n")

        print("Publish result:")
        print(f"creation_id: {publish_result['creation_id']}")
        print(f"media_id   : {publish_result['media_id']}")
        print()

        if FB_PUBLISH_ENABLED:
            print("Step 8b: Cross-posting to Facebook Page...")
            try:
                fb_result = publish_facebook_post(media_url, caption, media_kind)
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

        print("Step 9: Archiving success item to posted/posts...")
        archive_result = archive_post_assets(token, selected_post, success=True)
        print("Archive completed.")
        print(f"Moved to: {archive_result['target_folder']}")
        print(f"Media: {archive_result['media_name']}")
        print()

        print("Post MVP completed successfully.")
        sys.exit(0)

    except Exception as e:
        print("\nERROR:", str(e))
        archive_status = "No archive attempted."

        if token and selected_post:
            try:
                print("\nStep X: Archiving failed items to failed/posts...")
                archive_result = archive_post_assets(token, selected_post, success=False)
                print("Failed asset archive completed.")
                print(f"Moved to: {archive_result['target_folder']}")
                print(f"Media: {archive_result['media_name']}")
                archive_status = (
                    "Failed asset archive completed. "
                    f"Moved to {archive_result['target_folder']}. "
                    f"Media: {archive_result['media_name']}."
                )
            except Exception as archive_error:
                print("Failed to archive failed items:", str(archive_error))
                archive_status = f"Failed to archive failed item: {archive_error}"

        failed_media_name = (
            selected_post["media"]["name"]
            if selected_post and selected_post.get("media")
            else "not available"
        )
        send_alert_safely(
            "Reborn IG Auto Publisher Failed: post",
            "\n".join([
                "Instagram Post publishing failed.",
                f"Failed media: {failed_media_name}",
                f"Error: {e}",
                f"Archive status: {archive_status}",
                "",
                "Please check GitHub Actions logs and the OneDrive failed/posts folder.",
            ]),
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
