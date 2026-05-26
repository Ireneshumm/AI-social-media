import re
import sys
from pathlib import Path


SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def is_supported_image_file(filename):
    if not filename:
        return False

    return Path(filename).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS


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
    print(f"brief: {filename_to_brief(filename)}")

