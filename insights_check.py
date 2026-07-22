"""Read-only probe: does the configured Meta token have permission to read
Instagram + Facebook insights? Changes nothing — only issues GET requests and
reports which data is reachable, so we know whether an automated performance
report is possible with the current token or whether extra permissions
(read_insights / instagram_manage_insights) are needed."""
import os
import sys

import requests
from dotenv import load_dotenv

from facebook_publish import get_page_credentials

load_dotenv()

IG_USER_ID = os.getenv("IG_USER_ID")
GRAPH_VERSION = os.getenv("META_GRAPH_API_VERSION", "v23.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"


def probe(label, path, params):
    url = f"{GRAPH_BASE}/{path}"
    try:
        resp = requests.get(url, params=params, timeout=30)
    except requests.RequestException as e:
        print(f"  [ERROR ] {label}: request failed: {e}")
        return False

    if resp.status_code == 200:
        data = resp.json()
        preview = str(data)
        if len(preview) > 220:
            preview = preview[:220] + "..."
        print(f"  [OK    ] {label}: {preview}")
        return True

    # Surface the Graph error so we can tell a permission problem from other issues.
    detail = resp.text
    try:
        err = resp.json().get("error", {})
        detail = f"code={err.get('code')} subcode={err.get('error_subcode')} :: {err.get('message')}"
    except ValueError:
        pass
    print(f"  [DENIED] {label}: HTTP {resp.status_code} :: {detail}")
    return False


def main():
    if not IG_USER_ID:
        print("IG_USER_ID is not set.")
        sys.exit(1)

    page_id, token = get_page_credentials()
    print(f"Resolved Page id: {page_id}")
    print(f"Instagram user id: {IG_USER_ID}\n")

    results = {}

    print("Instagram — basic account fields (followers, media count):")
    results["ig_basic"] = probe(
        "ig_basic",
        IG_USER_ID,
        {"fields": "username,followers_count,media_count", "access_token": token},
    )

    print("\nInstagram — account insights (reach):")
    results["ig_insights"] = probe(
        "ig_insights_reach",
        f"{IG_USER_ID}/insights",
        {"metric": "reach", "period": "day", "access_token": token},
    )

    print("\nFacebook Page — basic fields (fan count):")
    results["fb_basic"] = probe(
        "fb_basic",
        page_id,
        {"fields": "name,fan_count,followers_count", "access_token": token},
    )

    print("\nFacebook Page — insights (impressions):")
    results["fb_insights"] = probe(
        "fb_insights_impressions",
        f"{page_id}/insights/page_impressions",
        {"period": "day", "access_token": token},
    )

    print("\n=== Summary ===")
    for key, ok in results.items():
        print(f"  {key}: {'READABLE' if ok else 'NOT readable'}")

    can_report = results.get("ig_insights") or results.get("fb_insights")
    if can_report:
        print("\nInsights are readable → an automated performance report is possible now.")
    else:
        print("\nInsights are NOT readable with this token → extra permissions "
              "(read_insights / instagram_manage_insights) would be needed.")
    # Always exit 0: this is a diagnostic, not a failure condition.
    sys.exit(0)


if __name__ == "__main__":
    main()
