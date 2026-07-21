import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication

from alert_email import send_alert_safely

load_dotenv()


# =========================
# Config
# =========================
MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")

ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH", "IG Auto Publisher")
ONEDRIVE_POSTS_FOLDER_NAME = os.getenv("ONEDRIVE_POSTS_FOLDER_NAME", "posts")
ONEDRIVE_STORIES_FOLDER_NAME = os.getenv("ONEDRIVE_STORIES_FOLDER_NAME", "stories")
ONEDRIVE_POSTED_FOLDER_NAME = os.getenv("ONEDRIVE_POSTED_FOLDER_NAME", "posted")
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL", "info@rebornaesthetics.com.au")

CLEANUP_DAYS = int(os.getenv("CLEANUP_RETENTION_DAYS", "30"))

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]

REQUIRED_ENV_VARS = [
    "MS_TENANT_ID",
    "MS_CLIENT_ID",
    "MS_CLIENT_SECRET",
]


def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


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


def request_with_retry(method, url, token, timeout=30, label="Microsoft Graph request"):
    headers = {"Authorization": f"Bearer {token}"}
    retry_delays = [5, 10]
    last_error = None

    for attempt in range(1, 4):
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            last_error = e
            status_code = e.response.status_code if e.response is not None else None
            should_retry = status_code == 429 or (
                status_code is not None and 500 <= status_code < 600
            )
            if not should_retry:
                print(f"FAIL: {label} non-retryable HTTP error: {e}")
                raise
            if attempt < 3:
                time.sleep(retry_delays[attempt - 1])
            else:
                print(f"FAIL: {label} failed after retries: {e}")
        except requests.RequestException as e:
            last_error = e
            if attempt < 3:
                time.sleep(retry_delays[attempt - 1])
            else:
                print(f"FAIL: {label} failed after retries: {e}")

    raise last_error


def graph_get(url, token):
    return request_with_retry("GET", url, token, label="Microsoft Graph GET").json()


def graph_delete(url, token):
    return request_with_retry("DELETE", url, token, label="Microsoft Graph DELETE")


def find_named_folder(items, folder_name):
    if isinstance(items, dict):
        items = items.get("value", [])
    for item in items:
        if item.get("name") == folder_name and "folder" in item:
            return item
    return None


def get_children(token, folder_id):
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{folder_id}/children"
    return graph_get(url, token).get("value", [])


def get_root_children(token):
    url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/root/children"
    return graph_get(url, token).get("value", [])


def get_posted_subfolder(token, subfolder_name):
    root_items = get_root_children(token)
    project = find_named_folder(root_items, ONEDRIVE_ROOT_PATH)
    if not project:
        raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")

    project_children = get_children(token, project["id"])
    posted = find_named_folder(project_children, ONEDRIVE_POSTED_FOLDER_NAME)
    if not posted:
        print(f"Posted folder not found: {ONEDRIVE_POSTED_FOLDER_NAME}; nothing to clean.")
        return None

    posted_children = get_children(token, posted["id"])
    return find_named_folder(posted_children, subfolder_name)


# =========================
# Cleanup
# =========================
def parse_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def cleanup_folder(token, subfolder_name, cutoff):
    subfolder = get_posted_subfolder(token, subfolder_name)
    if not subfolder:
        print(f"posted/{subfolder_name} not found; skipping.")
        return 0, 0

    items = get_children(token, subfolder["id"])
    deleted = 0
    kept = 0

    for item in items:
        if "folder" in item:
            continue

        name = item.get("name", "")
        modified = parse_timestamp(item.get("lastModifiedDateTime"))
        if modified is None:
            print(f"KEEP (no timestamp): {name}")
            kept += 1
            continue

        if modified < cutoff:
            url = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}/drive/items/{item['id']}"
            graph_delete(url, token)
            print(f"DELETED (modified {modified.date()}): posted/{subfolder_name}/{name}")
            deleted += 1
        else:
            kept += 1

    print(f"posted/{subfolder_name}: deleted {deleted}, kept {kept}")
    return deleted, kept


def main():
    try:
        print("Starting OneDrive posted cleanup...")
        validate_env()
        cutoff = datetime.now(timezone.utc) - timedelta(days=CLEANUP_DAYS)
        print(f"Retention: {CLEANUP_DAYS} days (deleting items modified before {cutoff.date()}).\n")

        token = get_access_token()

        total_deleted = 0
        for subfolder in (ONEDRIVE_POSTS_FOLDER_NAME, ONEDRIVE_STORIES_FOLDER_NAME):
            print(f"Cleaning posted/{subfolder}...")
            deleted, _ = cleanup_folder(token, subfolder, cutoff)
            total_deleted += deleted
            print()

        print(f"Cleanup complete. Total deleted: {total_deleted}")
        sys.exit(0)

    except Exception as e:
        print("\nERROR:", str(e))
        send_alert_safely(
            "Reborn Auto Publisher: cleanup failed",
            "\n".join([
                "The scheduled OneDrive posted cleanup failed.",
                f"Error: {e}",
                "",
                "Please check GitHub Actions logs.",
            ]),
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
