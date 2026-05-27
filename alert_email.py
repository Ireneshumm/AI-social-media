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
    return os.getenv("ALERT_EMAIL_ENABLED", "false").lower() == "true"


def get_missing_smtp_env_vars():
    return [key for key in REQUIRED_SMTP_ENV_VARS if not os.getenv(key)]


def get_recipients():
    recipients = os.getenv("ALERT_EMAIL_TO", "")
    return [
        recipient.strip()
        for recipient in recipients.split(",")
        if recipient.strip()
    ]


def get_smtp_port():
    try:
        return int(os.getenv("SMTP_PORT", ""))
    except ValueError:
        raise RuntimeError("SMTP_PORT must be a number.")


def build_message(subject, body, recipients):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("ALERT_EMAIL_FROM")
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    return msg


# =========================
# Public API
# =========================
def send_alert(subject: str, body: str) -> bool:
    if not alert_email_enabled():
        print("INFO: Alert email is disabled. Set ALERT_EMAIL_ENABLED=true to enable.")
        return False

    missing = get_missing_smtp_env_vars()
    if missing:
        raise RuntimeError(
            "Missing required alert email environment variables: "
            + ", ".join(missing)
        )

    recipients = get_recipients()
    if not recipients:
        raise RuntimeError("ALERT_EMAIL_TO has no valid recipients.")

    port = get_smtp_port()
    msg = build_message(subject, body, recipients)

    print("Sending alert email...")
    with smtplib.SMTP(os.getenv("SMTP_HOST"), port, timeout=30) as server:
        server.starttls()
        server.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD"))
        server.send_message(msg)

    print("PASS: Alert email sent successfully.")
    return True


def send_alert_safely(subject: str, body: str) -> bool:
    try:
        return send_alert(subject, body)
    except Exception as e:
        print("WARN: Alert email failed to send:", str(e))
        return False


def send_alert_email(subject: str, body: str) -> bool:
    return send_alert_safely(subject, body)


if __name__ == "__main__":
    send_alert_safely(
        "Reborn IG Auto Publisher Alert Helper Test",
        "This is a test from alert_email.py. No Instagram content was published.",
    )
