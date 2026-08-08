"""Remove previously AI-generated ad images from the publish queue ("posts").

Every image the ad generator produces is named with an "ai_" prefix. When the
brand/image standard changes, the old generated images no longer match and
should be cleared so only fresh, on-brand images (and real user uploads) remain.
This deletes queue files whose name starts with the generated prefix; real
uploads (any other name) are never touched.

Report-only by default; set PURGE_GENERATED_DELETE=true to actually delete.
"""
import os
import sys
import time

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication

from asset_helpers import is_supported_media_file

load_dotenv()

MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")

ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH", "IG Auto Publisher")
ONEDRIVE_POSTS_FOLDER_NAME = os.getenv("ONEDRIVE_POSTS_FOLDER_NAME", "posts")
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL", "info@rebornaesthetics.com.au")

# Prefix the ad generator stamps on every image it creates.
GENERATED_PREFIX = (os.getenv("GENERATED_PREFIX") or "ai_").strip().lower()
PURGE_GENERATED_DELETE = (os.getenv("PURGE_GENERATED_DELETE") or "").strip().lower() in ("true", "1", "yes", "on")

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_ROOT = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}"

REQUIRED_ENV_VARS = ["MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"]


def validate_env():
    missing = [k for k in REQUIRED_ENV_VARS if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


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


def get_all_children(token, folder_id):
    url = (
        f"{GRAPH_ROOT}/drive/items/{folder_id}/children"
        "?$select=id,name,size,file,folder&$top=200"
    )
    items = []
    while url:
        data = graph_get(url, token)
        items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return items


def delete_item(token, item_id):
    graph_request("DELETE", f"{GRAPH_ROOT}/drive/items/{item_id}", token)


def main():
    try:
        validate_env()
        token = get_access_token()

        root = graph_get(f"{GRAPH_ROOT}/drive/root/children", token).get("value", [])
        project = find_named_folder(root, ONEDRIVE_ROOT_PATH)
        if not project:
            raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")

        project_children = get_all_children(token, project["id"])
        posts = find_named_folder(project_children, ONEDRIVE_POSTS_FOLDER_NAME)
        if not posts:
            raise Exception(f"Posts folder not found: {ONEDRIVE_POSTS_FOLDER_NAME}")

        files = [
            it for it in get_all_children(token, posts["id"])
            if "folder" not in it and is_supported_media_file(it.get("name", ""))
        ]
        print(f"Scanned {len(files)} media file(s) in '{ONEDRIVE_POSTS_FOLDER_NAME}'.")

        flagged = [it for it in files if it.get("name", "").lower().startswith(GENERATED_PREFIX)]
        kept = len(files) - len(flagged)
        print(f"\nAI-generated file(s) (prefix '{GENERATED_PREFIX}'): {len(flagged)}")
        print(f"Real uploads / other files kept untouched: {kept}\n")

        deleted = 0
        for it in flagged:
            name = it.get("name", "")
            if PURGE_GENERATED_DELETE:
                try:
                    delete_item(token, it["id"])
                    deleted += 1
                    print(f"  deleted      '{name}'")
                except Exception as e:  # noqa: BLE001
                    print(f"  WARNING could not delete '{name}': {e}")
            else:
                print(f"  would delete '{name}'")

        if PURGE_GENERATED_DELETE:
            print(f"\nDone. Deleted {deleted} AI-generated asset(s); kept {kept} real upload(s)/other file(s).")
        else:
            print("\nReport-only (PURGE_GENERATED_DELETE not set). Re-run with PURGE_GENERATED_DELETE=true to remove them.")
        sys.exit(0)

    except Exception as e:
        print("\nERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
