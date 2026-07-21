import base64
import os
import subprocess

from video_transcode import has_tool


IMAGE_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


def get_video_duration(path):
    if not has_tool("ffprobe"):
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nokey=1:noprint_wrappers=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return float(result.stdout.strip())
    except (ValueError, Exception):
        return None


def _spread_fractions(count):
    if count <= 1:
        return [0.5]
    return [(i + 1) / (count + 1) for i in range(count)]


def extract_keyframes(video_path, count=3, out_dir=None):
    """Grab up to `count` evenly spaced frames from the video as JPEGs.
    Returns the list of frame paths (empty if extraction is unavailable)."""
    if not has_tool("ffmpeg"):
        print("WARNING: ffmpeg not found; cannot extract video keyframes.")
        return []

    out_dir = out_dir or os.path.dirname(video_path) or "."
    base = os.path.splitext(os.path.basename(video_path))[0]

    duration = get_video_duration(video_path)
    if duration and duration > 0:
        timestamps = [duration * frac for frac in _spread_fractions(count)]
    else:
        timestamps = [0.0]

    frames = []
    for index, ts in enumerate(timestamps, start=1):
        out_path = os.path.join(out_dir, f"{base}_frame_{index:02d}.jpg")
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(max(0.0, ts)),
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "3",
            out_path,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and os.path.exists(out_path):
                frames.append(out_path)
        except Exception as e:
            print(f"WARNING: keyframe extraction failed at {ts:.1f}s: {e}")

    return frames


def image_to_data_uri(image_path):
    ext = os.path.splitext(image_path)[1].lower()
    mime = IMAGE_MIME_TYPES.get(ext, "image/jpeg")
    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def get_caption_image_uris(media_path, media_kind, max_frames=3):
    """Return data URIs the caption model can look at: the image itself for
    an image, or sampled keyframes for a video. Empty list on any failure so
    the caller can fall back to a filename-only caption."""
    if media_kind == "video":
        paths = extract_keyframes(media_path, count=max_frames)
    else:
        paths = [media_path]

    uris = []
    for path in paths:
        try:
            uris.append(image_to_data_uri(path))
        except Exception as e:
            print(f"WARNING: could not encode image {path}: {e}")
    return uris
