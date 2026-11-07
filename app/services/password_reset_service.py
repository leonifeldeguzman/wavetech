import hashlib
import hmac
import secrets
import re

from app.extensions import db
from app.models.user import User
from datetime import datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models.password_reset import PasswordResetOTP
from app.services import email_service

OTP_TTL = timedelta(minutes=5)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_ATTEMPTS = 5
MAX_PER_HOUR = 5


def _hash(user_id: int, code: str) -> str:
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def send_reset_code(user) -> str:
    """Returns 'sent', 'cooldown' or 'limited'. Raises if the email fails."""
    now = datetime.utcnow()

    latest = (
        PasswordResetOTP.query.filter_by(user_id=user.id)
        .order_by(PasswordResetOTP.created_at.desc())
        .first()
    )
    if latest and now - latest.created_at < RESEND_COOLDOWN:
        return "cooldown"

    recent = PasswordResetOTP.query.filter(
        PasswordResetOTP.user_id == user.id,
        PasswordResetOTP.created_at > now - timedelta(hours=1),
    ).count()
    if recent >= MAX_PER_HOUR:
        return "limited"

    # Only the newest code is ever valid.
    PasswordResetOTP.query.filter_by(user_id=user.id, used=False).update(
        {"used": True}
    )

    code = f"{secrets.randbelow(10**6):06d}"
    db.session.add(
        PasswordResetOTP(
            user_id=user.id,
            otp_hash=_hash(user.id, code),
            expires_at=now + OTP_TTL,
        )
    )
    db.session.commit()

    email_service.send_otp_email(user.email, user.full_name, code)
    return "sent"


def verify_code(user, code: str) -> str:
    """Returns 'ok', 'invalid', 'expired' or 'locked'."""
    now = datetime.utcnow()
    otp = (
        PasswordResetOTP.query.filter_by(user_id=user.id, used=False)
        .order_by(PasswordResetOTP.created_at.desc())
        .first()
    )
    if not otp or otp.expires_at < now:
        return "expired"

    if otp.attempts >= MAX_ATTEMPTS:
        otp.used = True
        db.session.commit()
        return "locked"

    otp.attempts += 1
    if not hmac.compare_digest(otp.otp_hash, _hash(user.id, code)):
        if otp.attempts >= MAX_ATTEMPTS:
            otp.used = True
        db.session.commit()
        return "locked" if otp.used else "invalid"

    otp.used = True
    db.session.commit()
    return "ok"


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'*' * max(len(name) - 2, 3)}@{domain}"


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(raw, current_user_id):
    """Returns (email, error). An empty value gives (None, None)."""
    email = (raw or "").strip().lower()
    if not email:
        return None, None
    if len(email) > 120 or not _EMAIL_RE.match(email):
        return None, "Please enter a valid email address."

    taken = User.query.filter(
        User.email == email, User.id != current_user_id
    ).first()
    if taken:
        return None, "That email is already used by another admin."
    return email, None