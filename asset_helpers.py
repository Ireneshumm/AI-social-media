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


# Files produced by the AI generator carry this prefix, so cleanup can delete
# only auto-generated content and keep the user's own uploads forever.
AI_GENERATED_PREFIX = "ai_"


def is_ai_generated(filename):
    return bool(filename) and Path(filename).name.lower().startswith(AI_GENERATED_PREFIX)


def get_item_dimensions(item):
    """Return (width, height) from a OneDrive driveItem's image/video facet."""
    for facet in ("image", "video"):
        data = item.get(facet)
        if isinstance(data, dict):
            width, height = data.get("width"), data.get("height")
            if width and height:
                return int(width), int(height)
    return None


def is_vertical_item(item, threshold=1.5):
    """Route media by shape from a single folder.

    Returns True for a tall/story shape (9:16 ≈ 1.78), False for a feed shape
    (1:1, 4:5 ≈ 1.25, landscape), and None when dimensions are unavailable.
    Callers treat None as feed so every file is claimed by exactly one channel."""
    dims = get_item_dimensions(item)
    if not dims:
        return None
    width, height = dims
    if width <= 0:
        return None
    return (height / width) >= threshold


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

