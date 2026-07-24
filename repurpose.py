"""Repurpose a TikTok / Instagram video into a ready-to-publish draft.

Given a share URL, downloads the source without the on-screen watermark
(yt-dlp), optionally removes burned-in subtitles / background music / all audio,
normalizes it to the publishing spec (1080x1920 H.264), and uploads it to the
OneDrive review folder ("drafts"). Nothing is published automatically — review
the draft and move it into the queue when happy. Only use this with your own
content or content you have the rights to.

Optional toggles (env, "true"/"false"):
  REMOVE_SUBTITLES  clean burned-in subtitle/text removal via the Replicate
                    model hjunior29/video-text-remover (GPU inpainting).
                    Requires REPLICATE_API_TOKEN.
  REMOVE_MUSIC      separate voice from music (Demucs) and keep only the voice
  MUTE_AUDIO        drop all audio (overrides REMOVE_MUSIC)
"""
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


def _flag(name):
    return (os.getenv(name) or "").strip().lower() in ("true", "1", "yes", "on")


REMOVE_SUBTITLES = _flag("REMOVE_SUBTITLES")
REMOVE_MUSIC = _flag("REMOVE_MUSIC")
MUTE_AUDIO = _flag("MUTE_AUDIO")

MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")

ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH") or "IG Auto Publisher"
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


def run(cmd):
    print("+ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


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
    run([
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-f", "mp4/bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", template, url,
    ])

    videos = [f for f in glob.glob("dl/*") if f.lower().endswith(VIDEO_EXTS)]
    if not videos:
        raise RuntimeError("yt-dlp did not produce a video file (the link may be private or need login).")
    videos.sort(key=os.path.getmtime)
    chosen = videos[-1]
    print(f"Downloaded: {chosen} ({os.path.getsize(chosen)} bytes)")
    return chosen


# =========================
# Optional processing steps
# =========================
def remove_subtitles(path):
    """Clean burned-in subtitle/text removal via the Replicate model
    hjunior29/video-text-remover (auto text detection + GPU inpainting)."""
    if not os.getenv("REPLICATE_API_TOKEN"):
        raise RuntimeError("REMOVE_SUBTITLES is on but REPLICATE_API_TOKEN is not set.")

    import replicate

    print("Calling Replicate video-text-remover (this runs on a cloud GPU, may take a few minutes)...")
    with open(path, "rb") as video_file:
        output = replicate.run(
            "hjunior29/video-text-remover",
            input={"video": video_file, "method": "hybrid"},
        )

    out = os.path.splitext(path)[0] + "_nosub.mp4"
    # The client may return a file-like object, a URL string, or a list of them.
    if hasattr(output, "read"):
        with open(out, "wb") as f:
            f.write(output.read())
    else:
        if isinstance(output, (list, tuple)):
            output = output[0]
        url = getattr(output, "url", None) or str(output)
        resp = requests.get(url, timeout=900)
        resp.raise_for_status()
        with open(out, "wb") as f:
            f.write(resp.content)

    print(f"Subtitle-removed video saved: {out} ({os.path.getsize(out)} bytes)")
    return out


def remove_background_music(path):
    """Separate voice from music with Demucs and keep only the voice."""
    run(["ffmpeg", "-y", "-i", path, "-vn", "-acodec", "pcm_s16le", "audio.wav"])
    run(["python", "-m", "demucs", "--two-stems=vocals", "-o", "demucs_out", "audio.wav"])
    vocals = glob.glob("demucs_out/**/vocals.wav", recursive=True)
    if not vocals:
        raise RuntimeError("Demucs did not produce a vocals track.")
    out = os.path.splitext(path)[0] + "_novox.mp4"
    run([
        "ffmpeg", "-y", "-i", path, "-i", vocals[0],
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-shortest", out,
    ])
    return out


def mute_audio(path):
    out = os.path.splitext(path)[0] + "_mute.mp4"
    run(["ffmpeg", "-y", "-i", path, "-an", "-c:v", "copy", out])
    return out


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
    session = _graph(
        "POST",
        f"{GRAPH_ROOT}/drive/items/{folder_id}:/{filename}:/createUploadSession",
        token,
        json={"item": {"@microsoft.graph.conflictBehavior": "rename"}},
    ).json()
    upload_url = session["uploadUrl"]

    size = os.path.getsize(path)
    chunk = 10 * 1024 * 1024
    with open(path, "rb") as f:
        start = 0
        while start < size:
            data = f.read(chunk)
            end = start + len(data) - 1
            resp = requests.put(
                upload_url,
                headers={"Content-Length": str(len(data)), "Content-Range": f"bytes {start}-{end}/{size}"},
                data=data, timeout=300,
            )
            resp.raise_for_status()
            start = end + 1
    print(f"Uploaded to {REVIEW_FOLDER_NAME}/{filename} ({size} bytes)")


# =========================
# Main
# =========================
def main():
    try:
        validate_env()
        print(f"Repurposing: {VIDEO_URL}")
        print(f"Toggles -> subtitles:{REMOVE_SUBTITLES}  remove_music:{REMOVE_MUSIC}  mute:{MUTE_AUDIO}\n")

        print("Step 1: Downloading source (no watermark)...")
        path = download_video(VIDEO_URL)

        if REMOVE_SUBTITLES:
            print("\nStep 2a: Removing subtitles (Replicate GPU inpainting)...")
            path = remove_subtitles(path)

        if MUTE_AUDIO:
            print("\nStep 2b: Muting all audio...")
            path = mute_audio(path)
        elif REMOVE_MUSIC:
            print("\nStep 2b: Removing background music (keeping voice)...")
            path = remove_background_music(path)

        print("\nStep 3: Normalizing to 1080x1920 H.264...")
        final_path = ensure_h264(path)

        print("\nStep 4: Uploading to review folder...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.splitext(os.path.basename(path))[0]
        filename = f"repost_{base}_{timestamp}.mp4"
        token = get_access_token()
        folder_id = ensure_review_folder(token)
        upload_video(token, folder_id, filename, final_path)

        print(f"\nDone. Review '{REVIEW_FOLDER_NAME}/{filename}', then move it into the posts folder to publish.")
        sys.exit(0)

    except subprocess.CalledProcessError as e:
        print("\nERROR: a processing step failed:", e)
        print("If it was the download, the link may be private/region-locked or need login (Instagram often does).")
        sys.exit(1)
    except Exception as e:
        print("\nERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
