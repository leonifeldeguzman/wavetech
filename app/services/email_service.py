import os

import requests

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


def _send(to_email: str, subject: str, body: str) -> None:
    api_key = os.environ["BREVO_API_KEY"]
    sender = os.environ["MAIL_SENDER"]

    resp = requests.post(
        BREVO_URL,
        headers={
            "api-key": api_key,
            "accept": "application/json",
            "content-type": "application/json",
        },
        json={
            "sender": {"name": "WaveTech", "email": sender},
            "to": [{"email": to_email}],
            "subject": subject,
            "textContent": body,
        },
        timeout=10,
    )
    resp.raise_for_status()


def send_otp_email(to_email: str, name: str, code: str) -> None:
    _send(
        to_email,
        "WaveTech password reset code",
        f"Hello {name},\n\n"
        f"Your WaveTech password reset code is: {code}\n\n"
        "It expires in 5 minutes. If you didn't request this, "
        "you can ignore this email.\n",
    )


def send_email_change_code(to_email: str, name: str, code: str, new_email_masked: str) -> None:
    _send(
        to_email,
        "WaveTech email change code",
        f"Hello {name},\n\n"
        f"A request was made to change your WaveTech recovery email to {new_email_masked}.\n"
        f"Your confirmation code is: {code}\n\n"
        "It expires in 5 minutes. If you didn't request this, ignore this "
        "email and change your password.\n",
    )