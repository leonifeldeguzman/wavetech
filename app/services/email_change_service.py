import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.email_change import EmailChangeRequest
from app.models.user import User
from app.services import email_service
from app.services.password_reset_service import mask_email

OTP_TTL = timedelta(minutes=5)
RESEND_COOLDOWN = timedelta(seconds=60)
MAX_ATTEMPTS = 5
MAX_PER_HOUR = 5

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(raw, current_user_id):
    """Returns (email, error)."""
    email = (raw or "").strip().lower()
    if not email or len(email) > 120 or not _EMAIL_RE.match(email):
        return None, "Please enter a valid email address."

    taken = User.query.filter(
        User.email == email, User.id != current_user_id
    ).first()
    if taken:
        return None, "That email is already used by another admin."
    return email, None


def _hash(user_id: int, new_email: str, code: str) -> str:
    key = current_app.config["SECRET_KEY"].encode()
    msg = f"{user_id}:{new_email}:{code}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def _latest_pending(user_id):
    return (
        EmailChangeRequest.query.filter_by(user_id=user_id, used=False)
        .order_by(EmailChangeRequest.created_at.desc())
        .first()
    )


def start(user, new_email: str) -> str:
    """Returns 'sent', 'cooldown' or 'limited'. Raises if the email fails."""
    now = datetime.utcnow()

    pending = _latest_pending(user.id)
    if pending and now - pending.created_at < RESEND_COOLDOWN:
        return "cooldown"

    recent = EmailChangeRequest.query.filter(
        EmailChangeRequest.user_id == user.id,
        EmailChangeRequest.created_at > now - timedelta(hours=1),
    ).count()
    if recent >= MAX_PER_HOUR:
        return "limited"

    # Only the newest request is ever valid.
    EmailChangeRequest.query.filter_by(user_id=user.id, used=False).update(
        {"used": True}
    )

    code = f"{secrets.randbelow(10**6):06d}"
    db.session.add(
        EmailChangeRequest(
            user_id=user.id,
            new_email=new_email,
            otp_hash=_hash(user.id, new_email, code),
            expires_at=now + OTP_TTL,
        )
    )
    db.session.commit()

    email_service.send_email_change_code(
        user.email, user.full_name, code, mask_email(new_email)
    )
    return "sent"


def verify(user, code: str):
    """Returns (status, new_email). Status: ok, invalid, expired, locked, taken."""
    req = _latest_pending(user.id)

    if not req or req.expires_at < datetime.utcnow():
        if req:
            req.used = True
            db.session.commit()
        return "expired", None

    new_email = req.new_email
    req.attempts += 1

    if not hmac.compare_digest(req.otp_hash, _hash(user.id, new_email, code)):
        if req.attempts >= MAX_ATTEMPTS:
            req.used = True
            db.session.commit()
            return "locked", None
        db.session.commit()
        return "invalid", None

    req.used = True
    user.email = new_email
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return "taken", None
    return "ok", new_email


def cancel(user) -> None:
    EmailChangeRequest.query.filter_by(user_id=user.id, used=False).update(
        {"used": True}
    )
    db.session.commit()