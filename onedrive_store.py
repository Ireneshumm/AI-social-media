"""Tiny JSON key-value store backed by a file in the OneDrive project folder.

Used to persist small pieces of state between runs (the auto-publisher's
containers are ephemeral GitHub Actions runners, so anything worth keeping must
live in OneDrive). Two files use it:
  - performance_log.json : maps each published media_id to its source asset, so
    we can later join it with Instagram insights.
  - top_assets.json      : the list of top-performing assets to recycle more often.

Everything here is best-effort: on any failure it logs a warning and returns a
default, so a storage hiccup can never break publishing.
"""

import json
import os

import requests

ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL", "info@rebornaesthetics.com.au")
ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH", "IG Auto Publisher")


def _content_url(filename):
    root = ONEDRIVE_ROOT_PATH.strip("/")
    return (
        f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}"
        f"/drive/root:/{root}/{filename}:/content"
    )


def read_json(token, filename, default=None):
    """Return the parsed JSON stored at project-root/<filename>, or `default`
    when the file does not exist yet or cannot be read."""
    url = _content_url(filename)
    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code == 404:
            return default
        resp.raise_for_status()
        return resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not read {filename} from OneDrive ({e}); using default.")
        return default


def write_json(token, filename, data):
    """Write `data` as JSON to project-root/<filename>. Returns True on success,
    False on any failure (logged, never raised)."""
    url = _content_url(filename)
    try:
        resp = requests.put(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: could not write {filename} to OneDrive ({e}).")
        return False
