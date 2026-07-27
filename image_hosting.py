"""Shared image hosting for Instagram publishing.

Instagram fetches feed/story images from a public URL. WordPress can serve an
image to a browser yet still block Instagram's image scraper ("media could not
be fetched", error 2207052), which breaks photo posts and photo stories. imgbb
serves a plain URL that Instagram fetches reliably, so both publishers host
images there first and only fall back to WordPress when imgbb is unavailable.
"""
import base64
import os

import requests


def upload_to_imgbb(image_path):
    """Host an image on imgbb and return its direct URL. Returns None if no
    IMGBB_API_KEY is set or the upload fails, so callers fall back to WordPress."""
    key = os.getenv("IMGBB_API_KEY")
    if not key or not key.strip():
        return None

    with open(image_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    try:
        resp = requests.post(
            "https://api.imgbb.com/1/upload",
            data={
                "key": key.strip(),
                "image": encoded,
                "name": os.path.splitext(os.path.basename(image_path))[0],
                "expiration": 604800,  # auto-delete after 7 days; IG fetches immediately
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        url = data.get("url") or data.get("display_url")
        if not url:
            raise RuntimeError(f"imgbb response had no url: {str(resp.text)[:200]}")
        return url
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: imgbb upload failed ({e}); will fall back to WordPress.")
        return None
