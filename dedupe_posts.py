"""Find (and optionally delete) duplicate files in the publish queue ("posts").

Compares files by their OneDrive content hash, so it only ever flags BYTE-FOR-BYTE
identical files — never merely similar-looking ones. Runs in report-only mode by
default; set DEDUPE_DELETE=true to actually remove the extras, always keeping one
copy of each duplicated file.
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

DEDUPE_DELETE = (os.getenv("DEDUPE_DELETE") or "").strip().lower() in ("true", "1", "yes", "on")

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
    """List every child of a folder, following pagination."""
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


def content_key(item):
    """A key that is identical only for byte-for-byte identical files. Prefers a
    real content hash; falls back to size when no hash is exposed."""
    hashes = (item.get("file") or {}).get("hashes") or {}
    for algo in ("sha256Hash", "sha1Hash", "quickXorHash"):
        value = hashes.get(algo)
        if value:
            return f"{algo}:{value}"
    return f"size:{item.get('size')}"


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

        groups = {}
        for it in files:
            groups.setdefault(content_key(it), []).append(it)

        dup_groups = [g for g in groups.values() if len(g) > 1]
        # Files whose only signal is size (no hash) are reported separately, not
        # auto-deleted — equal size is not proof of identical content.
        hashed_dups = [g for g in dup_groups if not content_key(g[0]).startswith("size:")]
        size_only_dups = [g for g in dup_groups if content_key(g[0]).startswith("size:")]

        total_extra = sum(len(g) - 1 for g in hashed_dups)
        print(f"\nExact-duplicate groups (identical content): {len(hashed_dups)}")
        print(f"Redundant copies that can be removed: {total_extra}\n")

        deleted = 0
        for g in hashed_dups:
            # Keep the shortest name (usually the original, without __tryN suffixes).
            g_sorted = sorted(g, key=lambda x: (len(x.get("name", "")), x.get("name", "")))
            keep, extras = g_sorted[0], g_sorted[1:]
            print(f"  DUPLICATE ({len(g)}x): keep '{keep['name']}'")
            for ex in extras:
                if DEDUPE_DELETE:
                    try:
                        delete_item(token, ex["id"])
                        deleted += 1
                        print(f"      deleted  '{ex['name']}'")
                    except Exception as e:  # noqa: BLE001
                        print(f"      WARNING could not delete '{ex['name']}': {e}")
                else:
                    print(f"      would delete '{ex['name']}'")

        if size_only_dups:
            print(f"\nSame-size (no content hash available) — NOT auto-removed, review manually:")
            for g in size_only_dups:
                names = ", ".join(x.get("name", "") for x in g)
                print(f"  {g[0].get('size')} bytes: {names}")

        if DEDUPE_DELETE:
            print(f"\nDone. Deleted {deleted} redundant copy(ies); kept one of each.")
        else:
            print("\nReport-only (DEDUPE_DELETE not set). Re-run with DEDUPE_DELETE=true to remove the extras.")
        sys.exit(0)

    except Exception as e:
        print("\nERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
