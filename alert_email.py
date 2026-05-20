import os
import smtplib
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
ALERT_EMAIL_ENABLED = os.getenv("ALERT_EMAIL_ENABLED", "false")
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = os.getenv("SMTP_PORT")
SMTP_USERNAME = os.getenv("SMTP_USERNAME")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")
ALERT_EMAIL_TO = os.getenv("ALERT_EMAIL_TO")
ALERT_EMAIL_FROM = os.getenv("ALERT_EMAIL_FROM")

REQUIRED_SMTP_ENV_VARS = [
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "ALERT_EMAIL_TO",
    "ALERT_EMAIL_FROM",
]


# =========================
# Helpers
# =========================
def alert_email_enabled():
    return ALERT_EMAIL_ENABLED.lower() == "true"


def get_missing_smtp_env_vars():
    return [key for key in REQUIRED_SMTP_ENV_VARS if not os.getenv(key)]


def get_recipients():
    return [
        recipient.strip()
        for recipient in ALERT_EMAIL_TO.split(",")
        if recipient.strip()
    ]


def get_smtp_port():
    try:
        return int(SMTP_PORT)
    except ValueError:
        print("WARN: SMTP_PORT must be a number.")
        return None


def build_message(subject, body, recipients):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = ALERT_EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    return msg


# =========================
# Public API
# =========================
def send_alert_email(subject: str, body: str) -> bool:
    if not alert_email_enabled():
        print("INFO: Alert email is disabled. Set ALERT_EMAIL_ENABLED=true to enable.")
        return False

    missing = get_missing_smtp_env_vars()
    if missing:
        print(
            "WARN: Alert email not sent. Missing required environment variables: "
            + ", ".join(missing)
        )
        return False

    recipients = get_recipients()
    if not recipients:
        print("WARN: Alert email not sent. ALERT_EMAIL_TO has no valid recipients.")
        return False

    port = get_smtp_port()
    if port is None:
        return False

    msg = build_message(subject, body, recipients)

    try:
        print("Sending alert email...")
        with smtplib.SMTP(SMTP_HOST, port, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)

        print("PASS: Alert email sent successfully.")
        return True

    except Exception as e:
        print("FAIL: Alert email failed to send:", str(e))
        return False


if __name__ == "__main__":
    send_alert_email(
        "Reborn IG Auto Publisher Alert Helper Test",
        "This is a test from alert_email.py. No Instagram content was published.",
    )
