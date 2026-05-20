import os
import smtplib
import sys
from email.message import EmailMessage

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

if load_dotenv:
    load_dotenv()


# =========================
# Config
# =========================
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM")

REQUIRED_ENV_VARS = [
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "ALERT_EMAIL_TO",
    "ALERT_EMAIL_FROM",
]


# =========================
# Validation / email
# =========================
def validate_env():
    missing = [key for key in REQUIRED_ENV_VARS if not os.getenv(key)]
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


def get_smtp_port():
    try:
        return int(SMTP_PORT)
    except ValueError:
        raise RuntimeError("SMTP_PORT must be a number.")


def build_message():
    msg = EmailMessage()
    msg["Subject"] = "Reborn IG Auto Publisher Test Alert"
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ALERT_EMAIL_TO
    msg.set_content(
        "\n".join([
            "This is a test alert from Reborn IG Auto Publisher.",
            "The script name: send_test_alert.py",
            "No Instagram content was published.",
        ])
    )
    return msg


def send_email():
    port = get_smtp_port()
    msg = build_message()

    print(f"Connecting to SMTP server: {SMTP_HOST}:{port}")

    with smtplib.SMTP(SMTP_HOST, port, timeout=30) as server:
        server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)

# =========================
# Main flow
# =========================
def main():
    try:
        print("Starting test alert sender...")
        validate_env()
        print("Environment variables loaded successfully.")

        print("Sending test alert email...")
        send_email()

        print("PASS: Test alert email sent successfully.")
        sys.exit(0)

    except Exception as e:
        print("FAIL:", str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
