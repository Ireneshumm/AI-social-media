import os
import shutil
import subprocess


# Instagram-friendly ceilings. Videos above these are downscaled / frame-rate
# capped during transcode; already-compliant H.264 videos are left untouched.
MAX_WIDTH = 1080
MAX_FPS = 30

# Target 9:16 canvas for Reels / Stories. Non-9:16 videos are centred on a
# blurred, filled copy of themselves (no black bars, no cropping).
TARGET_WIDTH = 1080
TARGET_HEIGHT = 1920
TARGET_ASPECT = TARGET_WIDTH / TARGET_HEIGHT  # 0.5625
ASPECT_TOLERANCE = 0.02


def blur_pad_filter():
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_WIDTH}:{TARGET_HEIGHT},boxblur=luma_radius=40:luma_power=1[bg2];"
        f"[fg]scale={TARGET_WIDTH}:{TARGET_HEIGHT}:force_original_aspect_ratio=decrease[fg2];"
        f"[bg2][fg2]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def has_tool(name):
    return shutil.which(name) is not None


def _parse_fps(rate):
    # ffprobe reports r_frame_rate like "30/1" or "60000/1001".
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            den = float(den)
            return float(num) / den if den else None
        return float(rate)
    except (ValueError, ZeroDivisionError):
        return None


def get_video_info(path):
    """Return (codec, width, height, fps, duration) for the first video stream.
    Missing values come back as None."""
    if not has_tool("ffprobe"):
        return None, None, None, None, None

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,height,r_frame_rate:format=duration",
                "-of", "default=nokey=1:noprint_wrappers=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        print(f"WARNING: ffprobe failed to read video info: {e}")
        return None, None, None, None, None

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    codec = lines[0].lower() if len(lines) >= 1 else None
    width = _int(lines[1]) if len(lines) >= 2 else None
    height = _int(lines[2]) if len(lines) >= 3 else None
    fps = _parse_fps(lines[3]) if len(lines) >= 4 else None
    duration = _float(lines[4]) if len(lines) >= 5 else None

    return codec, width, height, fps, duration


def ensure_h264(input_path):
    """Return a path to an Instagram/Facebook-friendly H.264/AAC MP4.

    Transcodes when the video is not H.264, or is wider than MAX_WIDTH, or has
    a frame rate above MAX_FPS (e.g. 4K / 60fps / slow-motion iPhone clips).
    Already-compliant videos and any environment without ffmpeg fall through
    with the original file, so publishing is never blocked by transcoding.
    """
    if not has_tool("ffmpeg"):
        print("WARNING: ffmpeg not found; skipping video transcode.")
        return input_path

    codec, width, height, fps, duration = get_video_info(input_path)
    print(
        f"Source video: codec={codec}, width={width}, height={height}, "
        f"fps={fps}, duration={duration}s"
    )
    if duration is not None and duration < 3:
        print(
            "WARNING: video is shorter than 3s; Instagram Reels/Story require "
            "at least 3 seconds and will reject it (error 2207077)."
        )

    aspect = (width / height) if (width and height) else None
    aspect_is_916 = aspect is not None and abs(aspect - TARGET_ASPECT) <= ASPECT_TOLERANCE

    needs_transcode = (
        codec != "h264"
        or (width is not None and width > MAX_WIDTH)
        or (height is not None and height > TARGET_HEIGHT)
        or (fps is not None and fps > MAX_FPS + 1)
        or not aspect_is_916
    )
    if not needs_transcode:
        print("Video already meets requirements; no transcode needed.")
        return input_path

    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_h264.mp4"

    if aspect_is_916:
        # Already 9:16, just cap size/fps and normalise codec. Framing is kept.
        print(f"Transcoding to H.264 (<= {MAX_WIDTH}px wide, <= {MAX_FPS}fps)...")
        video_filter = f"scale='min({MAX_WIDTH},iw)':-2"
    else:
        # Not 9:16: fit onto a 1080x1920 blurred-background canvas.
        print(f"Transcoding to H.264 and padding to {TARGET_WIDTH}x{TARGET_HEIGHT} (blurred background)...")
        video_filter = blur_pad_filter()

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", video_filter,
        "-r", str(MAX_FPS),
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception as e:
        print(f"WARNING: ffmpeg transcode raised an error; using original file: {e}")
        return input_path

    if result.returncode != 0 or not os.path.exists(output_path):
        print("WARNING: ffmpeg transcode failed; using original file.")
        if result.stderr:
            print(result.stderr[-1000:])
        return input_path

    print(f"Transcoded to H.264: {output_path}")
    print(f"Transcoded size: {os.path.getsize(output_path)} bytes")
    return output_path


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python video_transcode.py <video_path>")
        sys.exit(1)

    print(ensure_h264(sys.argv[1]))
