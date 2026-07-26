"""Repurpose a TikTok / Instagram video for publishing.

Given a share URL, downloads the source without the on-screen watermark
(yt-dlp) and normalizes it to the publishing spec (1080x1920 H.264). Two modes
(REPURPOSE_MODE):
  review (default)  optionally removes burned-in subtitles / background music /
                    all audio, then uploads to the OneDrive review folder
                    ("drafts") — nothing is published until you move it into the
                    queue.
  auto              makes NO changes and uploads straight into the publish queue
                    ("posts"), so it is auto-published on the next scheduled run.
Only use this with your own content or content you have the rights to.

Optional toggles (env, "true"/"false"; review mode only):
  REMOVE_SUBTITLES  clean burned-in subtitle/text removal (GPU inpainting).
                    Requires REPLICATE_API_TOKEN.
  REMOVE_MUSIC      separate voice from music (Demucs) and keep only the voice
  MUTE_AUDIO        drop all audio (overrides REMOVE_MUSIC)

Subtitle-removal tuning (env):
  SUBTITLE_METHOD   "region" (default) inpaints a FIXED lower band on every
                    frame with ProPainter (jd7h/propainter). Because the masked
                    region is fixed — not per-frame detection — subtitles can
                    never flash back, which is the failure mode of detection.
                    "detect" uses the per-frame detector hjunior29/
                    video-text-remover (faster, but can flicker on hard clips).
  SUBTITLE_BAND     "auto" (default) auto-detects the on-screen text region so
                    the mask lands in the right place and stays small. Or give
                    "top-bottom" fractions of frame height (e.g. "0.25-0.42") to
                    force a fixed band when auto-detect misses.
"""
import glob
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

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

# "region" (ProPainter, flicker-free) or "detect" (per-frame model).
SUBTITLE_METHOD = (os.getenv("SUBTITLE_METHOD") or "region").strip().lower()
# "auto" auto-detects the text region; or "top-bottom" fractions to force a band.
SUBTITLE_BAND = (os.getenv("SUBTITLE_BAND") or "auto").strip()

MS_TENANT_ID = os.getenv("MS_TENANT_ID")
MS_CLIENT_ID = os.getenv("MS_CLIENT_ID")
MS_CLIENT_SECRET = os.getenv("MS_CLIENT_SECRET")

ONEDRIVE_ROOT_PATH = os.getenv("ONEDRIVE_ROOT_PATH") or "IG Auto Publisher"
REVIEW_FOLDER_NAME = (
    os.getenv("REPURPOSE_TARGET_FOLDER")
    or os.getenv("ONEDRIVE_DRAFTS_FOLDER_NAME")
    or "drafts"
)
ONEDRIVE_POSTS_FOLDER_NAME = os.getenv("ONEDRIVE_POSTS_FOLDER_NAME") or "posts"
ONEDRIVE_USER_EMAIL = os.getenv("ONEDRIVE_USER_EMAIL") or "info@rebornaesthetics.com.au"

# "review": download + optional edits -> drafts, approve before publishing.
# "auto":   download raw, no edits    -> posts, auto-published on the next run.
REPURPOSE_MODE = (os.getenv("REPURPOSE_MODE") or "review").strip().lower()

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
def extract_url(text):
    """Pull the first http(s) link out of a share string. TikTok/Douyin/IG
    'copy link' often yields a whole promo blob (e.g. Douyin's "6.10 复制打开抖音…
    https://v.douyin.com/xxx/ …") rather than a bare URL, which yt-dlp rejects.
    Grabbing the embedded URL makes those work without touching the shortcut."""
    if not text:
        return text
    match = re.search(r"https?://[^\s]+", text)
    return match.group(0) if match else text.strip()


def download_video(url):
    url = extract_url(url)
    os.makedirs("dl", exist_ok=True)
    for old in glob.glob("dl/*"):
        try:
            os.remove(old)
        except OSError:
            pass

    template = "dl/%(id)s.%(ext)s"
    cmd = [
        "yt-dlp", "--no-playlist", "--no-warnings",
        "-f", "mp4/bestvideo*+bestaudio/best",
        "--merge-output-format", "mp4",
    ]

    # Instagram and Douyin usually require a logged-in session. If cookies are
    # provided (Netscape cookies.txt content in the YTDLP_COOKIES secret), write
    # them to a file and hand them to yt-dlp so those sites download too.
    cookies = os.getenv("YTDLP_COOKIES")
    if cookies and cookies.strip():
        with open("cookies.txt", "w", encoding="utf-8") as f:
            f.write(cookies)
        cmd += ["--cookies", "cookies.txt"]
        print("Using provided login cookies for download.")

    cmd += ["-o", template, url]
    run(cmd)

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
def _require_replicate():
    if not os.getenv("REPLICATE_API_TOKEN"):
        raise RuntimeError("REMOVE_SUBTITLES is on but REPLICATE_API_TOKEN is not set.")
    import replicate

    return replicate


def _replicate_ref(replicate, model_name):
    """Resolve "owner/model" to a pinned "owner/model:version" reference.

    Running by the bare name returns 404 on some client versions, so we always
    pin. Also logs the model's real input field names so a mismatch is visible
    in the run log instead of failing silently."""
    model = replicate.models.get(model_name)
    version = getattr(model, "latest_version", None)
    if version is None:
        raise RuntimeError(f"Replicate model '{model_name}' has no runnable version.")
    try:
        props = version.openapi_schema["components"]["schemas"]["Input"]["properties"]
        print(f"{model_name} inputs: {sorted(props.keys())}")
    except Exception:
        pass
    return f"{model_name}:{version.id}"


def _save_replicate_output(output, out_path):
    """Persist a Replicate result (file-like object, URL string, or list)."""
    if hasattr(output, "read"):
        with open(out_path, "wb") as f:
            f.write(output.read())
        return out_path
    if isinstance(output, (list, tuple)):
        output = output[0] if output else None
    if output is None:
        raise RuntimeError("Replicate returned no output.")
    url = getattr(output, "url", None) or str(output)
    resp = requests.get(url, timeout=1800)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)
    return out_path


def _parse_band(spec):
    """Parse "top-bottom" fractions of frame height into a clamped (top, bottom)."""
    try:
        top_s, bot_s = spec.split("-", 1)
        top, bot = float(top_s), float(bot_s)
    except (ValueError, AttributeError):
        top, bot = 0.60, 0.92
    top = min(max(top, 0.0), 0.98)
    bot = min(max(bot, top + 0.02), 1.0)
    return top, bot


def _data_uri(path, mime):
    """Encode a file as a base64 data URI with an explicit MIME type."""
    import base64

    with open(path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _build_band_mask_video(src_path, band_top, band_bottom, out_path):
    """Derive a black clip with a white subtitle band FROM the source video, so
    it has exactly the same frame count. ProPainter matches mask frames to
    video frames one-for-one and errors on any mismatch ("size of tensor a must
    match tensor b"), so the mask must not be even one frame longer/shorter.
    Painting each source frame black, then the band white, guarantees an exact
    match. White = inpaint."""
    band_h = max(band_bottom - band_top, 0.02)
    vf = (
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill,"
        f"drawbox=x=0:y=ih*{band_top:.4f}:w=iw:h=ih*{band_h:.4f}:color=white:t=fill"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", src_path, "-an",
            "-vf", vf,
            "-pix_fmt", "yuv420p", "-c:v", "libx264", out_path,
        ],
        check=True,
    )
    return out_path


def _detect_text_boxes(src_path, samples=14):
    """Sample frames and detect on-screen text with EasyOCR. Returns
    (width, height, boxes) where boxes are pixel (x0, y0, x1, y1) tuples of all
    detected text across the sampled frames, or None if detection is
    unavailable / nothing is found.

    We deliberately keep the FULL union of detections (rather than filtering
    down to one tight line): covering a generous region around the text lets
    ProPainter reconstruct the whole area from neighbouring frames, which came
    out looking the most natural on real clips. A single implausibly tall/wide
    box is still dropped so one bad frame can't cover the whole screen."""
    try:
        import cv2
        import easyocr
    except Exception as e:  # noqa: BLE001
        print(f"Text auto-detect unavailable ({e}); will use the manual band instead.")
        return None

    cap = cv2.VideoCapture(src_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 0
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 0
    if not total or not width or not height:
        cap.release()
        return None

    indexes = [int(total * i / (samples + 1)) for i in range(1, samples + 1)]
    reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    boxes = []
    for idx in indexes:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        try:
            horizontal, _free = reader.detect(frame)
        except Exception:  # noqa: BLE001
            continue
        for b in horizontal[0]:
            x_min, x_max, y_min, y_max = b
            x0, y0 = max(0, int(x_min)), max(0, int(y_min))
            x1, y1 = min(width, int(x_max)), min(height, int(y_max))
            if x1 <= x0 or y1 <= y0:
                continue
            if (y1 - y0) > 0.35 * height or (x1 - x0) > 0.97 * width:
                continue
            boxes.append((x0, y0, x1, y1))

    cap.release()
    if not boxes:
        return None
    return width, height, boxes


def _build_mask_png(width, height, boxes, out_png, pad_frac=0.02):
    """White (=inpaint) rounded a little around each detected text box, on black."""
    from PIL import Image, ImageDraw

    img = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(img)
    pad_x, pad_y = int(width * pad_frac), int(height * pad_frac)
    for x0, y0, x1, y1 in boxes:
        draw.rectangle(
            [max(0, x0 - pad_x), max(0, y0 - pad_y),
             min(width, x1 + pad_x), min(height, y1 + pad_y)],
            fill=255,
        )
    img.save(out_png)
    return out_png


def _build_mask_video_from_png(src_path, mask_png, out_path):
    """Turn a static white-on-black mask PNG into a mask video with EXACTLY the
    source's frame count: black-fill each source frame, then lighten-blend the
    looped PNG over it (white boxes win). Driving off the source frames keeps
    the mask and video frame-for-frame aligned for ProPainter."""
    filter_complex = (
        "[0:v]drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill[bg];"
        "[bg][1:v]blend=all_mode=lighten:shortest=1[m]"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", src_path, "-loop", "1", "-i", mask_png,
            "-filter_complex", filter_complex, "-map", "[m]", "-an",
            "-pix_fmt", "yuv420p", "-c:v", "libx264", out_path,
        ],
        check=True,
    )
    return out_path


def remove_subtitles_region(path):
    """Flicker-free subtitle removal: inpaint the text region on every frame
    with ProPainter (jd7h/propainter, flow-based video inpainting). The mask is
    a fixed region (not per-frame detection), so subtitles never flash back. By
    default the region is auto-detected from the actual on-screen text so it
    lands in the right place and stays small; a manual SUBTITLE_BAND overrides."""
    replicate = _require_replicate()
    from video_transcode import get_video_info, has_audio_stream, has_tool

    mask_path = os.path.join(os.path.dirname(path) or ".", "submask.mp4")
    spec = (SUBTITLE_BAND or "auto").strip().lower()

    if spec in ("", "auto"):
        detected = _detect_text_boxes(path)
        if detected:
            width, height, boxes = detected
            mask_png = os.path.join(os.path.dirname(path) or ".", "submask.png")
            _build_mask_png(width, height, boxes, mask_png)
            _build_mask_video_from_png(path, mask_png, mask_path)
            top = min(b[1] for b in boxes) / height
            bottom = max(b[3] for b in boxes) / height
            print(f"Auto-detected on-screen text in {len(boxes)} region(s); "
                  f"masking rows {top:.0%}-{bottom:.0%} of {width}x{height}")
        else:
            print("No on-screen text auto-detected. Skipping subtitle removal to "
                  "avoid smearing a wrong region — re-run with a manual "
                  "'subtitle_band' (e.g. 0.25-0.42) if there is text to remove.")
            return path
    else:
        band_top, band_bottom = _parse_band(spec)
        _build_band_mask_video(path, band_top, band_bottom, mask_path)
        print(f"Subtitle band mask (manual): rows {band_top:.0%}-{band_bottom:.0%}")

    ref = _replicate_ref(replicate, "jd7h/propainter")
    print(f"Calling Replicate {ref} (video inpainting, may take a few minutes)...")
    # Both inputs go in as base64 data URIs with an explicit video/mp4 type.
    # Uploading a file/Path routes through Replicate's Files API, whose
    # delivered file lands server-side WITHOUT an extension (e.g. .../download):
    # ProPainter then (a) rejects the mask on its ".mp4/.avi/.png/.jpg" suffix
    # check and (b) treats the extension-less video as a frame *folder* and
    # os.listdir()s it, raising "Not a directory". A data URI carries the type
    # explicitly, so the cog writes each input as .mp4. mask_dilation grows the
    # band a few px to swallow anti-aliased text edges.
    video_uri = _data_uri(path, "video/mp4")
    mask_uri = _data_uri(mask_path, "video/mp4")
    # fp16=True: this cog loads part of the model (the RAFT flow net) in half
    # precision, so with the default fp32 path the inputs and weights mismatch
    # ("Input FloatTensor / weight HalfTensor"). Running the whole pipeline in
    # half precision keeps them consistent.
    output = replicate.run(
        ref,
        input={"video": video_uri, "mask": mask_uri, "mask_dilation": 8, "fp16": True},
    )

    out = os.path.splitext(path)[0] + "_nosub.mp4"
    _save_replicate_output(output, out)

    # ProPainter drops the audio track; splice the original sound back so
    # downstream music/mute steps and the final file keep it.
    if has_audio_stream(path) and has_tool("ffmpeg"):
        merged = os.path.splitext(path)[0] + "_nosub_a.mp4"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", out, "-i", path,
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-shortest", merged],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and os.path.exists(merged):
            out = merged
        else:
            print("WARNING: could not re-attach original audio; keeping silent video.")

    print(f"Subtitle-removed video saved: {out} ({os.path.getsize(out)} bytes)")
    return out


def remove_subtitles_detect(path):
    """Per-frame subtitle/text removal via hjunior29/video-text-remover (YOLO
    detection + inpainting). Faster, but can flicker when detection misses a
    frame; prefer the "region" method for a guaranteed-clean result."""
    replicate = _require_replicate()
    ref = _replicate_ref(replicate, "hjunior29/video-text-remover")
    print(f"Calling Replicate {ref} (per-frame detection, may take a few minutes)...")
    with open(path, "rb") as video_file:
        output = replicate.run(ref, input={"video": video_file, "method": "hybrid"})

    out = os.path.splitext(path)[0] + "_nosub.mp4"
    _save_replicate_output(output, out)
    print(f"Subtitle-removed video saved: {out} ({os.path.getsize(out)} bytes)")
    return out


def remove_subtitles(path):
    if SUBTITLE_METHOD == "detect":
        return remove_subtitles_detect(path)
    return remove_subtitles_region(path)


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


def ensure_target_folder(token, folder_name):
    root = _graph("GET", f"{GRAPH_ROOT}/drive/root/children", token).json()
    project = _find_folder(root, ONEDRIVE_ROOT_PATH)
    if not project:
        raise Exception(f"Project folder not found: {ONEDRIVE_ROOT_PATH}")

    children_url = f"{GRAPH_ROOT}/drive/items/{project['id']}/children"
    children = _graph("GET", children_url, token).json()
    folder = _find_folder(children, folder_name)
    if folder:
        return folder["id"]

    created = _graph(
        "POST", children_url, token,
        json={"name": folder_name, "folder": {}, "@microsoft.graph.conflictBehavior": "rename"},
    ).json()
    return created["id"]


def upload_video(token, folder_id, folder_name, filename, path):
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
    print(f"Uploaded to {folder_name}/{filename} ({size} bytes)")


# =========================
# Main
# =========================
def main():
    try:
        validate_env()
        auto = REPURPOSE_MODE == "auto"
        target_folder = ONEDRIVE_POSTS_FOLDER_NAME if auto else REVIEW_FOLDER_NAME

        print(f"Repurposing: {VIDEO_URL}")
        if auto:
            print(f"Mode: AUTO-PUBLISH — raw download straight into '{target_folder}' "
                  "(no edits; auto-published on the next scheduled run).\n")
        else:
            print(f"Mode: REVIEW — edited copy into '{target_folder}' for approval.")
            print(f"Toggles -> subtitles:{REMOVE_SUBTITLES} (method:{SUBTITLE_METHOD} "
                  f"band:{SUBTITLE_BAND})  remove_music:{REMOVE_MUSIC}  mute:{MUTE_AUDIO}\n")

        print("Step 1: Downloading source (no watermark)...")
        path = download_video(VIDEO_URL)

        # Auto-publish mode intentionally makes NO changes to the content.
        if not auto:
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

        print(f"\nStep 4: Uploading to '{target_folder}'...")
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.splitext(os.path.basename(path))[0]
        filename = f"repost_{base}_{timestamp}.mp4"
        token = get_access_token()
        folder_id = ensure_target_folder(token, target_folder)
        upload_video(token, folder_id, target_folder, filename, final_path)

        if auto:
            print(f"\nDone. '{target_folder}/{filename}' will be auto-published on the next scheduled run.")
        else:
            print(f"\nDone. Review '{target_folder}/{filename}', then move it into "
                  f"the '{ONEDRIVE_POSTS_FOLDER_NAME}' folder to publish.")
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
