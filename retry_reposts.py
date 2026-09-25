"""Retry failed AUTO reposts saved in the OneDrive retry queue.

When a one-tap 直发 repost fails to download (most often because tikwm's free API
is briefly rate-limiting the shared GitHub runner IP), repurpose.py saves the link
to `repost_retry_queue.json` in the OneDrive project folder. This script runs on a
schedule, re-tries each queued link, and:
  - removes a link once it downloads + queues successfully,
  - keeps a link (with an incremented attempt count) if it fails again,
  - drops a link after RETRY_MAX_ATTEMPTS tries or once it is older than
    RETRY_MAX_AGE_HOURS (so a genuinely dead/private link doesn't retry forever).

So a failed link is never lost and never needs re-pasting — it just keeps trying
itself in the background until it goes through.
"""

import sys
from datetime import datetime, timezone

import repurpose as R
from onedrive_store import read_json, write_json


def _age_hours(iso):
    try:
        added = datetime.fromisoformat(iso)
        if added.tzinfo is None:
            added = added.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - added).total_seconds() / 3600.0
    except Exception:  # noqa: BLE001
        return 0.0


def main():
    # NOTE: we do NOT call R.validate_env() here — it requires VIDEO_URL, which a
    # scheduled retry run intentionally has none of. get_access_token() fails
    # clearly if the Microsoft credentials are actually missing.
    token = R.get_access_token()
    queue = read_json(token, R.RETRY_QUEUE_FILE, []) or []
    if not queue:
        print("Retry queue is empty — nothing to do.")
        sys.exit(0)

    print(f"Retry queue: {len(queue)} pending repost(s).")
    still_pending = []
    succeeded = 0

    for entry in queue:
        url = entry.get("url") or entry.get("clean_url")
        if not url:
            continue
        attempts = int(entry.get("attempts", 0)) + 1
        age = _age_hours(entry.get("added_at", ""))

        if age > R.RETRY_MAX_AGE_HOURS:
            print(f"Dropping (too old, {age:.0f}h): {url}")
            continue

        print(f"\n=== Retry attempt {attempts} for: {url} ===")
        try:
            R.process_auto_repost(url)
            succeeded += 1
            print(f"Retry SUCCEEDED, removing from queue: {url}")
        except Exception as e:  # noqa: BLE001
            entry["attempts"] = attempts
            entry["last_error"] = str(e)[:200]
            if attempts >= R.RETRY_MAX_ATTEMPTS:
                print(f"Dropping (reached {R.RETRY_MAX_ATTEMPTS} attempts): {url} — {e}")
            else:
                print(f"Retry failed ({attempts}/{R.RETRY_MAX_ATTEMPTS}); keeping in queue: {e}")
                still_pending.append(entry)

    write_json(token, R.RETRY_QUEUE_FILE, still_pending)
    print(f"\nDone. {succeeded} published, {len(still_pending)} still pending.")
    sys.exit(0)


if __name__ == "__main__":
    main()
