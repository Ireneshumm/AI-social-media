import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()


# =========================
# Config
# =========================
WP_BASE_URL = os.getenv("WP_BASE_URL")
WP_USERNAME = os.getenv("WP_USERNAME")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

REQUIRED_ENV_VARS = [
    "WP_BASE_URL",
    "WP_USERNAME",
    "WP_APP_PASSWORD",
]

CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


# =========================
# Validation
# =========================
def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def get_content_type(image_path):
    ext = image_path.suffix.lower()
    content_type = CONTENT_TYPES.get(ext)

    if not content_type:
        raise RuntimeError(f"Unsupported image extension: {ext}")

    return content_type


def validate_image_path(image_path):
    if not image_path.exists():
        raise RuntimeError(f"Image file not found: {image_path}")

    if not image_path.is_file():
        raise RuntimeError(f"Image path is not a file: {image_path}")


# =========================
# WordPress upload
# =========================
def upload_media(image_path):
    validate_env()
    validate_image_path(image_path)

    url = f"{WP_BASE_URL.rstrip('/')}/wp-json/wp/v2/media"
    content_type = get_content_type(image_path)
    filename = image_path.name

    headers = {
        "Content-Type": content_type,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }

    with image_path.open("rb") as f:
        resp = requests.post(
            url,
            headers=headers,
            data=f,
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            timeout=60,
        )

    if not resp.ok:
        print(f"HTTP status code: {resp.status_code}")
        print("WordPress error response:")
        print(resp.text)
        resp.raise_for_status()

    result = resp.json()
    print(f"uploaded media id: {result.get('id')}")
    print(f"source_url: {result.get('source_url')}")
    return result


# =========================
# Main flow
# =========================
def main():
    if len(sys.argv) != 2:
        print('Usage: python wordpress_media.py "test_upload.jpg"')
        sys.exit(1)

    try:
        image_path = Path(sys.argv[1])
        upload_media(image_path)
        sys.exit(0)

    except Exception as e:
        print("ERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
