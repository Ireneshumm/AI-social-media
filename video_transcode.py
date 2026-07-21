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


def has_audio_stream(path):
    """True if the file has at least one audio stream. Assumes yes when ffprobe
    is unavailable so we never strip real audio by mistake."""
    if not has_tool("ffprobe"):
        return True
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=codec_type",
                "-of", "default=nokey=1:noprint_wrappers=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return True
    return "audio" in result.stdout


def ensure_h264(input_path):
    """Return a path to an Instagram/Facebook-friendly H.264/AAC MP4.

    Always re-encodes the video to a clean, uniform 1080x1920 H.264/AAC MP4
    (blurred-background padding for non-9:16 sources). Even videos that look
    compliant on paper can be rejected by Instagram (error 2207077) for subtle
    reasons - low resolution, odd audio codec, unusual pixel format/profile -
    so a full normalising pass is the reliable option. Falls back to the
    original file if ffmpeg is unavailable or the transcode fails, so
    publishing is never blocked.
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

    base, _ = os.path.splitext(input_path)
    output_path = f"{base}_h264.mp4"

    # Blurred-background pad to a uniform 1080x1920 canvas. For an already-9:16
    # source the foreground fills the frame and the blur is invisible; for
    # other shapes it fills the sides/top-and-bottom without cropping.
    audio_present = has_audio_stream(input_path)
    print(
        f"Normalising video to {TARGET_WIDTH}x{TARGET_HEIGHT} H.264/AAC "
        f"(<= {MAX_FPS}fps, audio_present={audio_present})..."
    )

    # Videos with no audio track are frequently rejected by Instagram
    # (error 2207077), so inject a silent stereo track when one is missing.
    cmd = ["ffmpeg", "-y", "-i", input_path]
    if not audio_present:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]

    cmd += ["-filter_complex", f"[0:v]{blur_pad_filter()}[v]", "-map", "[v]"]
    cmd += ["-map", "1:a:0"] if not audio_present else ["-map", "0:a:0?"]

    cmd += [
        "-r", str(MAX_FPS),
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-shortest",
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
