"""Find the Facebook Place ID to geotag Instagram posts with.

Instagram lets a post carry a `location_id`, which must be a Facebook Page that
represents a physical place. Tagging every post with the clinic's location is
the single strongest "show this to locals" signal on Instagram, so this helper
prints the candidate IDs. Pick the one whose name/address matches the clinic and
set it as the IG_LOCATION_ID secret.

Run via the workflow: publisher_type=find_location (optionally set
location_query, e.g. "Reborn Aesthetics"). Read the log, copy the id.
Publishing itself is untouched by this script.
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

PAGE_ACCESS_TOKEN = (os.getenv("PAGE_ACCESS_TOKEN") or "").strip()
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
FB_PAGE_ID = (os.getenv("FB_PAGE_ID") or "").strip()
LOCATION_QUERY = (os.getenv("LOCATION_QUERY") or "Reborn Aesthetics").strip()


def _fmt_location(loc):
    if not isinstance(loc, dict):
        return "(no address on record)"
    parts = [
        loc.get("street"),
        loc.get("city"),
        loc.get("state"),
        loc.get("zip"),
        loc.get("country"),
    ]
    addr = ", ".join(p for p in parts if p)
    return addr or "(no address on record)"


def _show(item):
    loc = item.get("location")
    print(f"  • location_id = {item.get('id')}")
    print(f"    name        : {item.get('name')}")
    print(f"    address     : {_fmt_location(loc)}")
    if item.get("link"):
        print(f"    link        : {item.get('link')}")
    print()


def get_page(page_id, label):
    url = f"{GRAPH_BASE}/{page_id}"
    params = {"fields": "id,name,location,link", "access_token": PAGE_ACCESS_TOKEN}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print(f"[{label}]")
        _show(data)
        return data
    except requests.RequestException as e:
        body = getattr(e.response, "text", "")
        print(f"[{label}] could not be read: {e}\n{body}\n")
        return None


def search_places(query):
    url = f"{GRAPH_BASE}/pages/search"
    params = {
        "q": query,
        "fields": "id,name,location,link",
        "access_token": PAGE_ACCESS_TOKEN,
    }
    print(f"[Place search for: {query!r}]")
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        results = resp.json().get("data", [])
        if not results:
            print("  (no places matched — try a different location_query)\n")
            return
        for item in results:
            _show(item)
    except requests.RequestException as e:
        body = getattr(e.response, "text", "")
        print(
            "  Place search unavailable with this token "
            f"({e}). This is normal — use the Page ID above instead.\n{body}\n"
        )


def main():
    if not PAGE_ACCESS_TOKEN:
        raise SystemExit("PAGE_ACCESS_TOKEN is not set; cannot look up locations.")

    print("=" * 66)
    print("Finding a Facebook Place ID to geotag Instagram posts")
    print("=" * 66)
    print(
        "\nPick the entry whose name + address is the clinic, then set its\n"
        "location_id as the IG_LOCATION_ID repository secret. Every post will\n"
        "then be geotagged there so locals browsing that place see it.\n"
    )

    # 1) The Facebook Page whose token we hold — often itself a valid place.
    get_page("me", "Connected Facebook Page (from PAGE_ACCESS_TOKEN)")

    # 2) The configured FB page id, if different.
    if FB_PAGE_ID:
        get_page(FB_PAGE_ID, f"Configured FB_PAGE_ID ({FB_PAGE_ID})")

    # 3) A place search for good measure.
    search_places(LOCATION_QUERY)

    print("-" * 66)
    print(
        "Next: set IG_LOCATION_ID (repo secret) to the chosen id.\n"
        "Leave it unset to keep publishing with no geotag."
    )


if __name__ == "__main__":
    main()
