import sys

from alert_email import send_alert


# =========================
# Main flow
# =========================
def main():
    try:
        print("Starting test alert sender...")
        sent = send_alert(
            "Reborn IG Auto Publisher Test Alert",
            "\n".join([
                "This is a test alert from Reborn IG Auto Publisher.",
                "The script name: send_test_alert.py",
                "No Instagram content was published.",
            ]),
        )

        if not sent:
            print("FAIL: Test alert email was not sent.")
            sys.exit(1)

        print("PASS: Test alert email sent successfully.")
        sys.exit(0)

    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
