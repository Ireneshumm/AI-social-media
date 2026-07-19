import os
import sys
import time
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
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
}

# Images upload quickly; videos are much larger and need a longer window.
# The tuple is (connect_timeout, read_timeout): the connect phase should fail
# fast so a transient network blip is retried instead of hanging.
CONNECT_TIMEOUT = 15
IMAGE_READ_TIMEOUT = 60
VIDEO_READ_TIMEOUT = 300
UPLOAD_RETRY_DELAYS = [5, 15]


# =========================
# Validation
# =========================
def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def get_content_type(media_path):
    ext = media_path.suffix.lower()
    content_type = CONTENT_TYPES.get(ext)

    if not content_type:
        raise RuntimeError(f"Unsupported media extension: {ext}")

    return content_type


def validate_media_path(media_path):
    if not media_path.exists():
        raise RuntimeError(f"Media file not found: {media_path}")

    if not media_path.is_file():
        raise RuntimeError(f"Media path is not a file: {media_path}")


# =========================
# WordPress upload
# =========================
def upload_media(media_path):
    validate_env()
    validate_media_path(media_path)

    url = f"{WP_BASE_URL.rstrip('/')}/wp-json/wp/v2/media"
    content_type = get_content_type(media_path)
    filename = media_path.name

    read_timeout = (
        VIDEO_READ_TIMEOUT
        if content_type.startswith("video/")
        else IMAGE_READ_TIMEOUT
    )
    timeout = (CONNECT_TIMEOUT, read_timeout)

    headers = {
        "Content-Type": content_type,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }

    last_error = None

    for attempt in range(1, 4):
        try:
            with media_path.open("rb") as f:
                resp = requests.post(
                    url,
                    headers=headers,
                    data=f,
                    auth=(WP_USERNAME, WP_APP_PASSWORD),
                    timeout=timeout,
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
        except requests.HTTPError:
            # The server responded with an error status; retrying won't help.
            raise
        except requests.RequestException as e:
            last_error = e
            if attempt < 3:
                delay = UPLOAD_RETRY_DELAYS[attempt - 1]
                print(f"WARNING: WordPress upload attempt {attempt} failed: {e}")
                print(f"Retrying in {delay} second(s)...")
                time.sleep(delay)
            else:
                print(f"FAIL: WordPress upload attempt {attempt} failed: {e}")

    raise last_error


# =========================
# Main flow
# =========================
def main():
    if len(sys.argv) != 2:
        print('Usage: python wordpress_media.py "test_upload.jpg"')
        sys.exit(1)

    try:
        media_path = Path(sys.argv[1])
        upload_media(media_path)
        sys.exit(0)

    except Exception as e:
        print("ERROR:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
