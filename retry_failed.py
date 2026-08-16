"""Automatically retry items that previously failed to publish.

Moves media out of failed/posts and failed/stories back into the single publish
queue ("posts") so the publishers try again. A per-file retry counter (encoded
in the filename as __tryN) caps attempts: transient failures (an expired token,
a network blip, a temporary Instagram error) succeed on a later attempt, while
anything that keeps failing is left in place and reported so it never loops
forever or gets lost silently."""
import os
import re
import sys
import time

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication

from alert_email import send_alert_safely
from asset_helpers import is_supported_media_file

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
ONEDRIVE_FAILED_FOLDER_NAME = os.getenv("ONEDRIVE_FAILED_FOLDER_NAME", "failed")
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL", "info@rebornaesthetics.com.au")

# `or` so an empty-string secret still falls back to the default.
MAX_RETRIES = int(os.getenv("RETRY_MAX_ATTEMPTS") or "3")

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_ROOT = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}"

REQUIRED_ENV_VARS = ["MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"]


def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


# =========================
# Microsoft Graph helpers
# =========================
def get_access_token():
    app = ConfidentialClientApplication(
        client_id=MS_CLIENT_ID, client_credential=MS_CLIENT_SECRET, authority=AUTHORITY
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    if "access_token" not in result:
        raise Exception(f"Failed to get token: {result}")
    return result["access_token"]


def graph_request(method, url, token, timeout=30, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    retry_delays = [5, 10]
    last_error = None
    for attempt in range(1, 4):
        try:
            resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.HTTPError as e:
            last_error = e
            code = e.response.status_code if e.response is not None else None
            if code is not None and code < 500 and code != 429:
                raise
            if attempt < 3:
                time.sleep(retry_delays[attempt - 1])
        except requests.RequestException as e:
            last_error = e
            if attempt < 3:
                time.sleep(retry_delays[attempt - 1])
    raise last_error


def graph_get(url, token):
    return graph_request("GET", url, token).json()


def find_named_folder(items, name):
    if isinstance(items, dict):
        items = items.get("value", [])
    for item in items:
        if item.get("name") == name and "folder" in item:
            return item
    return None


def get_children(token, folder_id):
    return graph_get(f"{GRAPH_ROOT}/drive/items/{folder_id}/children", token).get("value", [])


def get_project_folder(token):
    root = graph_get(f"{GRAPH_ROOT}/drive/root/children", token).get("value", [])
    project = find_named_folder(root, ONEDRIVE_ROOT_PATH)
    if not project:
        raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")
    return project


def move_item(token, item_id, target_folder_id, new_name):
    url = f"{GRAPH_ROOT}/drive/items/{item_id}"
    payload = {"parentReference": {"id": target_folder_id}, "name": new_name}
    return graph_request("PATCH", url, token, json=payload).json()


# =========================
# Retry-count encoding in the filename
# =========================
def retry_count(name):
    match = re.search(r"__try(\d+)", name)
    return int(match.group(1)) if match else 0


def bump_name(name, count):
    base, ext = os.path.splitext(name)
    base = re.sub(r"__try\d+", "", base)
    return f"{base}__try{count + 1}{ext}"


# Marker that permanently retires an item from the retry sweep. Without this, an
# item that hit the retry cap stayed in the failed folder and was re-detected and
# re-reported on every 6-hourly sweep — an endless stream of identical alert
# emails. A "__dead" item is skipped on future sweeps and reported exactly once.
DEAD_MARKER = "__dead"


def mark_dead(name):
    base, ext = os.path.splitext(name)
    return f"{base}{DEAD_MARKER}{ext}"


# =========================
# Main
# =========================
def main():
    try:
        print("Starting failed-item retry sweep...")
        validate_env()
        token = get_access_token()

        project = get_project_folder(token)
        project_children = get_children(token, project["id"])

        posts_folder = find_named_folder(project_children, ONEDRIVE_POSTS_FOLDER_NAME)
        if not posts_folder:
            raise Exception(f"Posts (queue) folder not found: {ONEDRIVE_POSTS_FOLDER_NAME}")

        failed_folder = find_named_folder(project_children, ONEDRIVE_FAILED_FOLDER_NAME)
        if not failed_folder:
            print(f"No '{ONEDRIVE_FAILED_FOLDER_NAME}' folder; nothing to retry.")
            sys.exit(0)

        failed_children = get_children(token, failed_folder["id"])

        requeued = 0
        newly_dead = []
        # Failed items live under failed/posts and failed/stories; both go back to
        # the single "posts" queue, where aspect-ratio routing re-sorts them.
        for sub_name in (ONEDRIVE_POSTS_FOLDER_NAME, ONEDRIVE_STORIES_FOLDER_NAME):
            sub = find_named_folder(failed_children, sub_name)
            if not sub:
                continue
            for item in get_children(token, sub["id"]):
                if "folder" in item:
                    continue
                name = item.get("name", "")
                if not is_supported_media_file(name):
                    continue
                # Already retired — skip so it is neither retried nor re-reported.
                if DEAD_MARKER in name:
                    continue

                count = retry_count(name)
                if count >= MAX_RETRIES:
                    # Retire it: rename with the dead marker (staying in place) so
                    # future sweeps ignore it. This is what stops the repeating
                    # "still failing after retries" emails.
                    dead_name = mark_dead(name)
                    try:
                        move_item(token, item["id"], sub["id"], dead_name)
                        newly_dead.append(name)
                        print(f"RETIRED (failed {count}x): failed/{sub_name}/{name} -> {dead_name}")
                    except Exception as e:
                        print(f"WARNING: could not retire {name}: {e}")
                    continue

                new_name = bump_name(name, count)
                try:
                    move_item(token, item["id"], posts_folder["id"], new_name)
                    requeued += 1
                    print(f"REQUEUED (attempt {count + 1}): failed/{sub_name}/{name} -> {ONEDRIVE_POSTS_FOLDER_NAME}/{new_name}")
                except Exception as e:
                    print(f"WARNING: could not requeue {name}: {e}")

        print(f"\nRetry sweep complete. Requeued {requeued}; retired {len(newly_dead)}.")

        # Report only items retired THIS sweep — each broken item is announced
        # exactly once, then never again.
        if newly_dead:
            send_alert_safely(
                "Reborn Auto Publisher: items retired after repeated failures",
                "\n".join([
                    f"{len(newly_dead)} item(s) failed to publish {MAX_RETRIES} times and have been "
                    f"retired (renamed with '{DEAD_MARKER}') in '{ONEDRIVE_FAILED_FOLDER_NAME}'. "
                    "They will no longer be retried or reported:",
                    "",
                    *[f"  - {n}" for n in newly_dead],
                    "",
                    "These usually mean the media itself has a problem (format/length/aspect).",
                    "Delete them, or fix and re-upload. Check the GitHub Actions logs for the exact "
                    "Instagram/Facebook error.",
                ]),
            )

        sys.exit(0)

    except Exception as e:
        print("\nERROR:", str(e))
        send_alert_safely(
            "Reborn Auto Publisher: retry sweep failed",
            f"The failed-item retry sweep itself failed.\nError: {e}\n\nPlease check GitHub Actions logs.",
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
