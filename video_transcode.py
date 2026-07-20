import os
import shutil
import subprocess


def has_tool(name):
    return shutil.which(name) is not None


def get_video_codec(path):
    """Return the first video stream's codec name (e.g. 'h264', 'hevc'),
    or None if it cannot be determined."""
    if not has_tool("ffprobe"):
        return None

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=codec_name",
                "-of", "default=nokey=1:noprint_wrappers=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        print(f"WARNING: ffprobe failed to read codec: {e}")
        return None

    return result.stdout.strip().lower() or None


def ensure_h264(input_path):
    """Return a path to an H.264/AAC MP4 suitable for Instagram/Facebook.

    Transcodes only when needed (e.g. HEVC/H.265 from iPhone). Falls back to
    the original file if ffmpeg is unavailable or the transcode fails, so the
    publish flow is never blocked by transcoding.
    """
    if not has_tool("ffmpeg"):
        print("WARNING: ffmpeg not found; skipping video transcode.")
        return input_path

    codec = get_video_codec(input_path)
    if codec == "h264":
        print("Video is already H.264; no transcode needed.")
        return input_path

    print(f"Video codec is '{codec or 'unknown'}'; transcoding to H.264...")
    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_h264.mp4"

    cmd = [
        "ffmpeg", "-y",
        "-i", input_path,
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
