import os
import sys
import time
import requests
from datetime import datetime
from dotenv import load_dotenv
from msal import ConfidentialClientApplication
from openai import OpenAI

load_dotenv()

# =========================
# Config
# =========================
MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")

ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH", "IG Auto Publisher")
ONEDRIVE_STORIES_FOLDER_NAME = os.getenv("ONEDRIVE_STORIES_FOLDER_NAME", "stories")
ONEDRIVE_POSTED_FOLDER_NAME = os.getenv("ONEDRIVE_POSTED_FOLDER_NAME", "posted")
ONEDRIVE_FAILED_FOLDER_NAME = os.getenv("ONEDRIVE_FAILED_FOLDER_NAME", "failed")
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL", "info@rebornaesthetics.com.au")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

IG_USER_ID = os.getenv("IG_USER_ID")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

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
    print("Starting story publisher...")
    print(f"OneDrive root path  : {ONEDRIVE_ROOT_PATH}")
    print(f"Stories folder name : {ONEDRIVE_STORIES_FOLDER_NAME}")
    print(f"OpenAI model        : {OPENAI_MODEL}")
    print(f"Graph version       : {GRAPH_VERSION}")
    print(f"Dry run             : {DRY_RUN}")
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


def get_stories_items(token):
    _, project_children = get_project_children(token)

    stories_folder = find_named_folder(project_children, ONEDRIVE_STORIES_FOLDER_NAME)
    if not stories_folder:
        raise Exception(f"Stories folder not found: {ONEDRIVE_STORIES_FOLDER_NAME}")

    stories_children_url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{stories_folder['id']}/children"
    stories_children = graph_get(stories_children_url, token)
    return stories_children.get("value", [])


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


def archive_story_assets(token, selected_story, success=True):
    target_top = ONEDRIVE_POSTED_FOLDER_NAME if success else ONEDRIVE_FAILED_FOLDER_NAME
    target_subfolder = get_subfolder_by_path(token, target_top, ONEDRIVE_STORIES_FOLDER_NAME)

    image_item = selected_story["image"]
    text_item = selected_story["text"]
    target_items = get_folder_children(token, target_subfolder["id"])
    archive_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    image_name = get_conflict_safe_name(
        target_items,
        image_item["name"],
        archive_timestamp,
    )
    text_name = get_conflict_safe_name(
        target_items,
        text_item["name"],
        archive_timestamp,
    )

    move_item_to_folder(
        token,
        image_item["id"],
        target_subfolder["id"],
        image_name if image_name != image_item["name"] else None,
    )
    move_item_to_folder(
        token,
        text_item["id"],
        target_subfolder["id"],
        text_name if text_name != text_item["name"] else None,
    )

    return {
        "target_folder": f"{target_top}/{ONEDRIVE_STORIES_FOLDER_NAME}",
        "image_name": image_name,
        "text_name": text_name,
    }


# =========================
# Asset matching
# =========================
def split_name(filename):
    base, ext = os.path.splitext(filename)
    return base, ext.lower()


def match_story_assets(items):
    grouped = {}

    for item in items:
        if "folder" in item:
            continue

        name = item.get("name", "")
        base, ext = split_name(name)

        if base not in grouped:
            grouped[base] = {"image": None, "text": None}

        if ext in IMAGE_EXTENSIONS:
            grouped[base]["image"] = item
        elif ext == ".txt":
            grouped[base]["text"] = item

    matched = []
    incomplete = []

    for base, assets in grouped.items():
        if assets["image"] and assets["text"]:
            matched.append({
                "base_name": base,
                "image": assets["image"],
                "text": assets["text"],
            })
        else:
            incomplete.append({
                "base_name": base,
                "has_image": assets["image"] is not None,
                "has_text": assets["text"] is not None,
            })

    matched.sort(key=lambda x: x["base_name"])
    return matched, incomplete


# =========================
# Content helpers
# =========================
def decode_text_file(content_bytes):
    return content_bytes.decode("utf-8").strip()


def parse_story_text(text_content):
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


def generate_story_caption(brief_text):
    client = OpenAI(api_key=OPENAI_API_KEY)

    prompt = f"""
You are writing a very short Instagram Story caption for Reborn Aesthetics, a premium aesthetics clinic in Brisbane.

Use the following content brief:
{brief_text}

Requirements:
- Tone: premium, warm, professional
- Length: very short
- Make it suitable for Instagram Story overlay text
- No hashtags
- No medical claims
- No overpromising results
- Use a soft call to action only if it feels natural
- Return only the caption text
"""

    retry_delays = [5, 10, 20]
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
            print(f"OpenAI caption attempt {attempt} failed: {e}")
            print(f"Exception type: {type(e).__name__}")
            print(f"Exception repr: {repr(e)}")

            if e.__cause__ is not None:
                print(f"Exception cause: {e.__cause__}")
                print(f"Exception cause repr: {repr(e.__cause__)}")

            if attempt < 3:
                delay = retry_delays[attempt - 1]
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)

    raise last_error


# =========================
# Instagram publish
# =========================
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
                raise

            if attempt < 3:
                delay = retry_delays[attempt - 1]
                print(f"WARNING: {label} attempt {attempt} failed with HTTP {status_code}: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: {label} attempt {attempt} failed with HTTP {status_code}: {e}")
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


def create_story_media_container(image_url, caption):
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media"
    payload = {
        "image_url": image_url,
        "media_type": "STORIES",
        "caption": caption,
        "access_token": PAGE_ACCESS_TOKEN,
    }
    resp = post_with_retry(url, payload, timeout=60, label="create_story_media_container")
    return resp.json()


def publish_media_container(creation_id):
    url = f"{GRAPH_BASE}/{IG_USER_ID}/media_publish"
    payload = {
        "creation_id": creation_id,
        "access_token": PAGE_ACCESS_TOKEN,
    }
    resp = post_with_retry(url, payload, timeout=60, label="publish_media_container")
    return resp.json()


def publish_instagram_story(image_url, caption):
    container = create_story_media_container(image_url, caption)
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
    selected_story = None
    token = None

    try:
        validate_env()
        log_startup()

        print("Step 1: Getting Microsoft access token...")
        token = get_access_token()
        print("OK\n")

        print("Step 2: Loading stories folder items...")
        items = get_stories_items(token)
        print(f"Found {len(items)} item(s) in stories/\n")

        print("Step 3: Matching story assets...")
        matched, incomplete = match_story_assets(items)

        if incomplete:
            print("Incomplete asset groups detected:")
            for item in incomplete:
                print(
                    f"- {item['base_name']} | has_image={item['has_image']} | has_text={item['has_text']}"
                )
            print()

        if not matched:
            print("No valid matched story assets found. Exit gracefully.")
            sys.exit(0)

        selected_story = matched[0]
        print(f"Selected story: {selected_story['base_name']}")
        print(f"Image file: {selected_story['image']['name']}")
        print(f"Text file : {selected_story['text']['name']}\n")

        print("Step 4: Downloading text brief...")
        text_bytes = download_file(token, selected_story["text"]["id"])
        text_content = decode_text_file(text_bytes)
        print("Text brief loaded.\n")

        image_url, brief_text = parse_story_text(text_content)

        if not image_url:
            raise Exception("image_url not found in txt file.")

        if not brief_text:
            raise Exception("Brief text is empty.")

        print("Parsed image_url:")
        print(image_url)
        print()

        print("Parsed brief text:")
        print(brief_text)
        print()

        print("Step 5: Generating story caption with OpenAI...")
        caption = generate_story_caption(brief_text)
        print("Story caption generated.\n")

        print("Generated story caption:")
        print(caption)
        print()

        print("Step 6: Downloading image file for local verification...")
        image_bytes = download_file(token, selected_story["image"]["id"])
        os.makedirs("temp", exist_ok=True)
        image_path = os.path.join("temp", selected_story["image"]["name"])

        with open(image_path, "wb") as f:
            f.write(image_bytes)

        print(f"Image saved to: {image_path}")
        print(f"Image size: {len(image_bytes)} bytes\n")

        if DRY_RUN:
            print("DRY_RUN=true, skipping Instagram Story publish.")
            sys.exit(0)

        print("Step 7: Publishing to Instagram Story...")
        publish_result = publish_instagram_story(image_url, caption)
        print("Instagram Story publish completed.\n")

        print("Publish result:")
        print(f"creation_id: {publish_result['creation_id']}")
        print(f"media_id   : {publish_result['media_id']}")
        print()

        print("Step 8: Archiving success items to posted/stories...")
        archive_result = archive_story_assets(token, selected_story, success=True)
        print("Archive completed.")
        print(f"Moved to: {archive_result['target_folder']}")
        print(f"Image: {archive_result['image_name']}")
        print(f"Text : {archive_result['text_name']}")
        print()

        print("Story MVP completed successfully.")
        sys.exit(0)

    except Exception as e:
        print("\nERROR:", str(e))

        if token and selected_story:
            try:
                print("\nStep X: Archiving failed items to failed/stories...")
                archive_result = archive_story_assets(token, selected_story, success=False)
                print("Failed asset archive completed.")
                print(f"Moved to: {archive_result['target_folder']}")
                print(f"Image: {archive_result['image_name']}")
                print(f"Text : {archive_result['text_name']}")
            except Exception as archive_error:
                print("Failed to archive failed items:", str(archive_error))

        sys.exit(1)


if __name__ == "__main__":
    main()
