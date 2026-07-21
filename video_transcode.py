import os
import shutil
import subprocess


# Instagram-friendly ceilings. Videos above these are downscaled / frame-rate
# capped during transcode; already-compliant H.264 videos are left untouched.
MAX_WIDTH = 1080
MAX_FPS = 30


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
    """Return (codec, width, fps) for the first video stream, or (None, None, None)."""
    if not has_tool("ffprobe"):
        return None, None, None

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,width,r_frame_rate",
                "-of", "default=nokey=1:noprint_wrappers=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        print(f"WARNING: ffprobe failed to read video info: {e}")
        return None, None, None

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    codec = lines[0].lower() if len(lines) >= 1 else None
    width = None
    fps = None
    if len(lines) >= 2:
        try:
            width = int(lines[1])
        except ValueError:
            width = None
    if len(lines) >= 3:
        fps = _parse_fps(lines[2])

    return codec, width, fps


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

    codec, width, fps = get_video_info(input_path)
    print(f"Source video: codec={codec}, width={width}, fps={fps}")

    needs_transcode = (
        codec != "h264"
        or (width is not None and width > MAX_WIDTH)
        or (fps is not None and fps > MAX_FPS + 1)
    )
    if not needs_transcode:
        print("Video already meets requirements; no transcode needed.")
        return input_path

    print(f"Transcoding to H.264 (<= {MAX_WIDTH}px wide, <= {MAX_FPS}fps)...")
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_h264.mp4"

    # Downscale only if wider than MAX_WIDTH, keep aspect ratio, force even
    # dimensions (yuv420p / H.264 require them). Framing is preserved.
    scale_filter = f"scale='min({MAX_WIDTH},iw)':-2"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
        "-vf", scale_filter,
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
