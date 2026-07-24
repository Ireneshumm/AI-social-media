"""Repurpose a TikTok / Instagram video into a ready-to-publish draft.

Given a share URL, downloads the source without the on-screen watermark
(yt-dlp), normalizes it to the publishing spec (1080x1920 H.264 via the existing
transcoder), and uploads it to the OneDrive review folder ("drafts"). Nothing is
published automatically — you review the draft and move it into the queue when
happy. Only use this with your own content or content you have the rights to."""
import glob
import os
import subprocess
import sys
from datetime import datetime

import requests
from dotenv import load_dotenv
from msal import ConfidentialClientApplication

from video_transcode import ensure_h264

load_dotenv()


# =========================
# Config
# =========================
VIDEO_URL = os.getenv("VIDEO_URL")

MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")

ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH") or "IG Auto Publisher"
# Land in the review folder by default; never straight into the publish queue.
REVIEW_FOLDER_NAME = (
    os.getenv("REPURPOSE_TARGET_FOLDER")
    or os.getenv("ONEDRIVE_DRAFTS_FOLDER_NAME")
    or "drafts"
)
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL") or "info@rebornaesthetics.com.au"

AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["https://graph.microsoft.com/.default"]
GRAPH_ROOT = f"https://graph.microsoft.com/v1.0/users/{ONEDRIVE_USER_EMAIL}"

REQUIRED_ENV_VARS = ["VIDEO_URL", "MS_TENANT_ID", "MS_CLIENT_ID", "MS_CLIENT_SECRET"]

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm")


def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


# =========================
# Download (no watermark) via yt-dlp
# =========================
def download_video(url):
    os.makedirs("dl", exist_ok=True)
    for old in glob.glob("dl/*"):
        try:
            os.remove(old)
        except OSError:
            pass

    template = "dl/%(id)s.%(ext)s"
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "mp4/bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ]
    print("Downloading:", " ".join(cmd))
    subprocess.run(cmd, check=True)

    videos = [f for f in glob.glob("dl/*") if f.lower().endswith(VIDEO_EXTS)]
    if not videos:
        raise RuntimeError("yt-dlp did not produce a video file (the link may be private or need login).")
    videos.sort(key=os.path.getmtime)
    chosen = videos[-1]
    print(f"Downloaded: {chosen} ({os.path.getsize(chosen)} bytes)")
    return chosen


# =========================
# Microsoft Graph upload (to the review folder)
# =========================
def get_access_token():
    app = ConfidentialClientApplication(
        client_id=MS_CLIENT_ID, client_credential=MS_CLIENT_SECRET, authority=AUTHORITY
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    if "access_token" not in result:
        raise Exception(f"Failed to get token: {result}")
    return result["access_token"]


def _graph(method, url, token, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    resp = requests.request(method, url, headers=headers, timeout=300, **kwargs)
    resp.raise_for_status()
    return resp


def _find_folder(items, name):
    for item in items.get("value", []):
        if item.get("name") == name and "folder" in item:
            return item
    return None


def ensure_review_folder(token):
    root = _graph("GET", f"{GRAPH_ROOT}/drive/root/children", token).json()
    project = _find_folder(root, ONEDRIVE_ROOT_PATH)
    if not project:
        raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")

    children_url = f"{GRAPH_ROOT}/drive/items/{project['id']}/children"
    children = _graph("GET", children_url, token).json()
    folder = _find_folder(children, REVIEW_FOLDER_NAME)
    if folder:
        return folder["id"]

    created = _graph(
        "POST", children_url, token,
        json={"name": REVIEW_FOLDER_NAME, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
    ).json()
    return created["id"]


def upload_video(token, folder_id, filename, path):
    # Resumable upload session handles large video files reliably.
    session = _graph(
        "POST",
        f"{GRAPH_ROOT}/drive/items/{folder_id}:/{filename}:/createUploadSession",
        token,
        json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
    ).json()
    upload_url = session["uploadUrl"]

    size = os.path.getsize(path)
    chunk = 10 * 1024 * 1024  # 10 MB
    with open(path, "rb") as f:
        start = 0
        while start < size:
            data = f.read(chunk)
            end = start + len(data) - 1
            headers = {
                "Content-Length": str(len(data)),
                "Content-Range": f"bytes {start}-{end}/{size}",
            }
            resp = requests.put(upload_url, headers=headers, data=data, timeout=300)
            resp.raise_for_status()
            start = end + 1
    print(f"Uploaded to {REVIEW_FOLDER_NAME}/{filename} ({size} bytes)")


# =========================
# Main
# =========================
def main():
    try:
        validate_env()
        print(f"Repurposing: {VIDEO_URL}\n")

        print("Step 1: Downloading source (no watermark)...")
        raw_path = download_video(VIDEO_URL)

        print("\nStep 2: Normalizing to 1080x1920 H.264...")
        final_path = ensure_h264(raw_path)

        print("\nStep 3: Uploading to review folder...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.splitext(os.path.basename(raw_path))[0]
        filename = f"repost_{base}_{timestamp}.mp4"
        token = get_access_token()
        folder_id = ensure_review_folder(token)
        upload_video(token, folder_id, filename, final_path)

        print(f"\nDone. Review '{REVIEW_FOLDER_NAME}/{filename}', then move it into the posts folder to publish.")
        sys.exit(0)

    except subprocess.CalledProcessError as e:
        print("\nERROR: download failed:", e)
        print("The link may be private, region-locked, or need login (Instagram often does).")
        sys.exit(1)
    except Exception as e:
        print("\nERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
