"""Drop leading zeros from displayed dates and times (app/utils/zero_strip.py).

One backend hook instead of editing every template:
    "Nov 07, 2026"      -> "Nov 7, 2026"
    "November 07, 2026" -> "November 7, 2026"
    "08:30 AM"          -> "8:30 AM"

It runs on finished HTML pages only (never JSON, CSS, JS files, forms posts).
It only touches a month name followed by a zero-padded day, and a
zero-padded hour directly before AM/PM, so numeric values such as
value="2026-11-07", 24-hour "08:30" and reference codes are left alone.

Register once in the app factory (app/__init__.py), after the app is created:

    from app.utils.zero_strip import register_zero_strip
    register_zero_strip(app)
"""
import re

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|"
    r"Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)

# "Nov 07" / "November 07," -> day without the zero
_DATE_RE = re.compile(r"\b(" + _MONTH + r"\.?)\s0([1-9])\b")

# "08:30 AM" / "08:30PM" -> hour without the zero (not part of a longer number)
_TIME_RE = re.compile(r"(?<![\d:.\-])0([1-9]:[0-5]\d\s?[AaPp][Mm])\b")


def strip_leading_zeros(text: str) -> str:
    text = _DATE_RE.sub(r"\1 \2", text)
    return _TIME_RE.sub(r"\1", text)


def register_zero_strip(app):
    @app.after_request
    def _strip_zeros(response):
        if (
            response.mimetype == "text/html"
            and not response.direct_passthrough
            and response.status_code != 304
        ):
            body = response.get_data(as_text=True)
            fixed = strip_leading_zeros(body)
            if fixed != body:
                response.set_data(fixed)
        return response

    return app