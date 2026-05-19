import os
import sys
import requests
import msal
from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


REQUIRED_ENV_VARS = [
    "MS_TENANT_ID",
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
    "ONEDRIVE_ROOT_PATH",
    "ONEDRIVE_POSTS_FOLDER_NAME",
    "ONEDRIVE_STORIES_FOLDER_NAME",
    "ONEDRIVE_POSTED_FOLDER_NAME",
    "ONEDRIVE_FAILED_FOLDER_NAME",
    "ONEDRIVE_USER_EMAIL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "IG_USER_ID",
    "PAGE_ACCESS_TOKEN",
    "META_GRAPH_API_VERSION",
]


def log_pass(message):
    print(f"[PASS] {message}")


def log_fail(message):
    print(f"[FAIL] {message}")


def log_info(message):
    print(f"[INFO] {message}")


def check_env_vars():
    log_info("Checking required environment variables...")

    missing = []

    for var in REQUIRED_ENV_VARS:
        value = os.getenv(var)
        if value:
            log_pass(f"{var} exists")
        else:
            log_fail(f"{var} is missing")
            missing.append(var)

    return len(missing) == 0


def get_graph_access_token():
    log_info("Getting Microsoft Graph access token...")

    tenant_id = os.getenv("MS_TENANT_ID")
    client_id = os.getenv("MS_CLIENT_ID")
    client_secret = os.getenv("MS_CLIENT_SECRET")

    authority = f"https://login.microsoftonline.com/{tenant_id}"

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        authority=authority,
        client_credential=client_secret,
    )

    result = app.acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )

    if "access_token" not in result:
        log_fail("Failed to get Microsoft Graph access token")
        log_info(str(result))
        return None

    log_pass("Microsoft Graph access token acquired")
    return result["access_token"]


def graph_get(access_token, url):
    headers = {
        "Authorization": f"Bearer {access_token}",
    }

    response = requests.get(url, headers=headers, timeout=30)

    if response.status_code >= 400:
        raise RuntimeError(
            f"Graph API request failed: {response.status_code} {response.text}"
        )

    return response.json()


def get_child_folder(access_token, parent_drive_id, parent_item_id, folder_name):
    user_email = os.getenv("ONEDRIVE_USER_EMAIL")

    url = (
        f"https://graph.microsoft.com/v1.0/users/{user_email}"
        f"/drives/{parent_drive_id}/items/{parent_item_id}/children"
    )

    data = graph_get(access_token, url)

    for item in data.get("value", []):
        if item.get("name") == folder_name and "folder" in item:
            return item

    return None


def check_onedrive_folders(access_token):
    log_info("Checking OneDrive folder structure...")

    user_email = os.getenv("ONEDRIVE_USER_EMAIL")
    root_path = os.getenv("ONEDRIVE_ROOT_PATH")

    posts_folder = os.getenv("ONEDRIVE_POSTS_FOLDER_NAME")
    stories_folder = os.getenv("ONEDRIVE_STORIES_FOLDER_NAME")
    posted_folder = os.getenv("ONEDRIVE_POSTED_FOLDER_NAME")
    failed_folder = os.getenv("ONEDRIVE_FAILED_FOLDER_NAME")

    root_url = (
        f"https://graph.microsoft.com/v1.0/users/{user_email}"
        f"/drive/root:/{root_path}"
    )

    try:
        root_item = graph_get(access_token, root_url)
        log_pass(f"OneDrive root folder exists: {root_path}")
    except Exception as e:
        log_fail(f"OneDrive root folder check failed: {root_path}")
        log_info(str(e))
        return False

    drive_id = root_item.get("parentReference", {}).get("driveId")
    root_item_id = root_item.get("id")

    if not drive_id or not root_item_id:
        log_fail("Could not read driveId or root folder item id")
        return False

    all_ok = True

    posts_item = get_child_folder(access_token, drive_id, root_item_id, posts_folder)
    stories_item = get_child_folder(access_token, drive_id, root_item_id, stories_folder)
    posted_item = get_child_folder(access_token, drive_id, root_item_id, posted_folder)
    failed_item = get_child_folder(access_token, drive_id, root_item_id, failed_folder)

    top_level_checks = [
        (posts_folder, posts_item),
        (stories_folder, stories_item),
        (posted_folder, posted_item),
        (failed_folder, failed_item),
    ]

    for folder_name, item in top_level_checks:
        if item:
            log_pass(f"Folder exists: {folder_name}")
        else:
            log_fail(f"Folder missing: {folder_name}")
            all_ok = False

    if posted_item:
        posted_posts = get_child_folder(access_token, drive_id, posted_item["id"], posts_folder)
        posted_stories = get_child_folder(access_token, drive_id, posted_item["id"], stories_folder)

        if posted_posts:
            log_pass(f"Folder exists: {posted_folder}/{posts_folder}")
        else:
            log_fail(f"Folder missing: {posted_folder}/{posts_folder}")
            all_ok = False

        if posted_stories:
            log_pass(f"Folder exists: {posted_folder}/{stories_folder}")
        else:
            log_fail(f"Folder missing: {posted_folder}/{stories_folder}")
            all_ok = False

    if failed_item:
        failed_posts = get_child_folder(access_token, drive_id, failed_item["id"], posts_folder)
        failed_stories = get_child_folder(access_token, drive_id, failed_item["id"], stories_folder)

        if failed_posts:
            log_pass(f"Folder exists: {failed_folder}/{posts_folder}")
        else:
            log_fail(f"Folder missing: {failed_folder}/{posts_folder}")
            all_ok = False

        if failed_stories:
            log_pass(f"Folder exists: {failed_folder}/{stories_folder}")
        else:
            log_fail(f"Folder missing: {failed_folder}/{stories_folder}")
            all_ok = False

    return all_ok


def check_openai():
    log_info("Checking OpenAI API...")

    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        response = client.responses.create(
            model=os.getenv("OPENAI_MODEL"),
            input="Reply with only the word OK.",
            max_output_tokens=16,
        )

        output_text = response.output_text.strip()

        if output_text:
            log_pass("OpenAI API is available")
            log_info(f"OpenAI test response: {output_text}")
            return True

        log_fail("OpenAI API returned an empty response")
        return False

    except Exception as e:
        log_fail("OpenAI API check failed")
        log_info(str(e))
        return False


def check_instagram_graph():
    log_info("Checking Instagram Graph API access...")

    ig_user_id = os.getenv("IG_USER_ID")
    access_token = os.getenv("PAGE_ACCESS_TOKEN")
    graph_version = os.getenv("META_GRAPH_API_VERSION")

    url = f"https://graph.facebook.com/{graph_version}/{ig_user_id}"

    params = {
        "fields": "id,username",
        "access_token": access_token,
    }

    try:
        response = requests.get(url, params=params, timeout=30)

        if response.status_code >= 400:
            log_fail("Instagram Graph API check failed")
            log_info(f"{response.status_code} {response.text}")
            return False

        data = response.json()

        log_pass("Instagram Graph API is available")
        log_info(f"IG user data: {data}")
        return True

    except Exception as e:
        log_fail("Instagram Graph API request failed")
        log_info(str(e))
        return False


def main():
    print("=" * 60)
    print("Reborn Instagram Auto Publisher Health Check")
    print("=" * 60)

    all_ok = True

    if not check_env_vars():
        all_ok = False
        print("=" * 60)
        log_fail("Health check stopped because required env vars are missing")
        sys.exit(1)

    access_token = get_graph_access_token()

    if not access_token:
        all_ok = False
    else:
        if not check_onedrive_folders(access_token):
            all_ok = False

    if not check_openai():
        all_ok = False

    if not check_instagram_graph():
        all_ok = False

    print("=" * 60)

    if all_ok:
        log_pass("All health checks passed")
        sys.exit(0)

    log_fail("Some health checks failed")
    sys.exit(1)


if __name__ == "__main__":
    main()