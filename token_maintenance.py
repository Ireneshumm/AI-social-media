import os
import sys
import time
from datetime import datetime, timezone

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv()


# =========================
# Config
# =========================
META_APP_ID = os.getenv("META_APP_ID")
META_APP_SECRET = os.getenv("META_APP_SECRET")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
IG_USER_ID = os.getenv("IG_USER_ID")
META_GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION")

GRAPH_BASE = f"https://graph.facebook.com/{META_GRAPH_API_VERSION}"

REQUIRED_ENV_VARS = [
    "META_APP_ID",
    "META_APP_SECRET",
    "PAGE_ACCESS_TOKEN",
    "IG_USER_ID",
    "META_GRAPH_API_VERSION",
]


# =========================
# Validation
# =========================
def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def get_app_access_token():
    return f"{META_APP_ID}|{META_APP_SECRET}"


# =========================
# Meta helpers
# =========================
def meta_get(url, params, label):
    resp = requests.get(url, params=params, timeout=30)
    if not resp.ok:
        print(f"FAIL: {label} failed with HTTP {resp.status_code}")
        print(resp.text)
    resp.raise_for_status()
    return resp.json()


def debug_page_token():
    url = f"{GRAPH_BASE}/debug_token"
    params = {
        "input_token": PAGE_ACCESS_TOKEN,
        "access_token": get_app_access_token(),
    }
    return meta_get(url, params, "Meta debug_token")


def check_ig_user_access():
    url = f"{GRAPH_BASE}/{IG_USER_ID}"
    params = {
        "fields": "id,username",
        "access_token": PAGE_ACCESS_TOKEN,
    }
    return meta_get(url, params, "IG user access check")


# =========================
# Token reporting
# =========================
def format_expires_at(expires_at):
    if not expires_at:
        return None

    return datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()


def get_days_remaining(expires_at):
    if not expires_at:
        return None

    seconds_remaining = expires_at - int(time.time())
    return seconds_remaining // 86400


def print_token_health(data):
    token_data = data.get("data", {})
    is_valid = token_data.get("is_valid")
    app_id = token_data.get("app_id")
    token_type = token_data.get("type")
    expires_at = token_data.get("expires_at")
    expires_at_text = format_expires_at(expires_at)
    days_remaining = get_days_remaining(expires_at)

    print("Token health:")
    print(f"is_valid: {is_valid}")

    if app_id:
        print(f"app_id: {app_id}")

    if token_type:
        print(f"type: {token_type}")

    if expires_at:
        print(f"expires_at: {expires_at} ({expires_at_text})")
        print(f"days_remaining: {days_remaining}")
    else:
        print("expires_at: not provided")

    return is_valid, days_remaining


def print_ig_user_health(data):
    print("IG user access:")
    print(f"id: {data.get('id')}")

    if data.get("username"):
        print(f"username: {data.get('username')}")


# =========================
# Main flow
# =========================
def main():
    try:
        print("Starting Meta token maintenance check...")
        validate_env()
        print("Environment variables loaded successfully.")
        print("No token refresh or GitHub Secrets update will be performed.\n")

        print("Step 1: Checking token with Meta debug_token...")
        debug_result = debug_page_token()
        is_valid, days_remaining = print_token_health(debug_result)
        print()

        if not is_valid:
            print("FAIL: PAGE_ACCESS_TOKEN is invalid.")
            sys.exit(1)

        print("Step 2: Verifying token can access IG user...")
        ig_user_result = check_ig_user_access()
        print_ig_user_health(ig_user_result)
        print()

        if days_remaining is not None and days_remaining < 10:
            print("WARNING: PAGE_ACCESS_TOKEN expires within 10 days.")
            sys.exit(0)

        print("PASS: PAGE_ACCESS_TOKEN is valid and not near expiry.")
        sys.exit(0)

    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
