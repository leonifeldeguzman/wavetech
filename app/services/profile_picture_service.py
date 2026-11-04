"""Profile picture storage and validation.

Only real PNG / JPEG / WebP images are accepted. The file type is decided by
the file's actual bytes (its "magic number"), never by the filename or the
browser-supplied Content-Type, so a renamed .exe or an SVG (which can carry
scripts) is rejected.
"""
from __future__ import annotations

from app.extensions import db
from app.models.user_profile_picture import UserProfilePicture

MAX_PROFILE_PICTURE_BYTES = 2 * 1024 * 1024  # 2 MB


class ProfilePictureError(ValueError):
    """Raised when an uploaded profile picture is not acceptable."""


def _detect_image_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def read_and_validate(file_storage) -> tuple[bytes, str]:
    """Return (image_bytes, content_type) or raise ProfilePictureError."""
    data = file_storage.read(MAX_PROFILE_PICTURE_BYTES + 1)

    if not data:
        raise ProfilePictureError("The selected profile picture file is empty.")
    if len(data) > MAX_PROFILE_PICTURE_BYTES:
        raise ProfilePictureError("Profile picture must be 2 MB or smaller.")

    content_type = _detect_image_type(data)
    if content_type is None:
        raise ProfilePictureError(
            "Profile picture must be a PNG, JPG, or WebP image."
        )
    return data, content_type


def save_picture(user_id: int, data: bytes, content_type: str) -> None:
    """Create or replace the user's picture. Does NOT commit; the caller
    commits so the picture and any other profile changes save together."""
    picture = UserProfilePicture.query.filter_by(user_id=user_id).first()
    if picture is None:
        picture = UserProfilePicture(user_id=user_id)
        db.session.add(picture)
    picture.image_data = data
    picture.content_type = content_type


def delete_picture(user_id: int) -> bool:
    """Remove the user's picture, restoring the default avatar. Returns True
    if there was one to remove. Does NOT commit; the caller commits."""
    picture = UserProfilePicture.query.filter_by(user_id=user_id).first()
    if picture is None:
        return False
    db.session.delete(picture)
    return True


def get_picture(user_id: int) -> UserProfilePicture | None:
    return UserProfilePicture.query.filter_by(user_id=user_id).first()


def get_picture_version(user_id: int) -> int | None:
    """Cache-busting number for the picture URL, or None if the user has no
    picture. Reads only the timestamp column, not the image bytes."""
    updated_at = (
        db.session.query(UserProfilePicture.updated_at)
        .filter(UserProfilePicture.user_id == user_id)
        .scalar()
    )
    if updated_at is None:
        return None
    return int(updated_at.timestamp()) or 1