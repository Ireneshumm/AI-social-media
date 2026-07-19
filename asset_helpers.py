import re
import sys
from pathlib import Path


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov"}


def is_supported_image_file(filename):
    if not filename:
        return False

    return Path(filename).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


def is_supported_video_file(filename):
    if not filename:
        return False

    return Path(filename).suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS


def is_supported_media_file(filename):
    return is_supported_image_file(filename) or is_supported_video_file(filename)


def get_media_kind(filename):
    if is_supported_image_file(filename):
        return "image"
    if is_supported_video_file(filename):
        return "video"
    return None


def filename_to_brief(filename):
    if not filename:
        raise ValueError("Filename is empty.")

    stem = Path(filename).stem
    brief = re.sub(r"[-_]+", " ", stem)
    brief = re.sub(r"\s+", " ", brief).strip().lower()

    if not brief:
        raise ValueError("Filename has no valid brief content.")

    return brief


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print('Usage: python asset_helpers.py "anti-wrinkle-consultation.jpg"')
        sys.exit(1)

    filename = sys.argv[1]
    print(f"filename: {filename}")
    print(f"is_supported_image: {is_supported_image_file(filename)}")
    print(f"is_supported_video: {is_supported_video_file(filename)}")
    print(f"media_kind: {get_media_kind(filename)}")
    print(f"brief: {filename_to_brief(filename)}")

