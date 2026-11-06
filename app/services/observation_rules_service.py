"""Operator Observation Rules (Admin-editable).

Replaces the fixed Calm / Moderate / Rough rules. Admins add, rename, change
the effect of, and delete observations in Settings -> Safety Thresholds.

    Monitoring form  -> list_rules()            (dropdown options)
    Monitoring save  -> valid_keys()            (validate the submitted value)
    Scheduling       -> apply_observation()     (effect on the assessment)
"""
from __future__ import annotations

import re

from app.extensions import db
from app.models.observation_rule import ObservationRule

EFFECT_NONE = "none"
EFFECT_CAUTION = "caution"
EFFECT_UNSAFE = "unsafe"
EFFECTS = (EFFECT_NONE, EFFECT_CAUTION, EFFECT_UNSAFE)

# Same behaviour as the old fixed rules.
DEFAULT_RULES = (
    ("calm", "Calm", EFFECT_NONE),
    ("moderate", "Moderate", EFFECT_NONE),
    ("rough", "Rough", EFFECT_CAUTION),
)

MAX_LABEL_LENGTH = 30
_LABEL_RE = re.compile(r"^[A-Za-z][A-Za-z -]*$")
_RANK = {"safe": 0, "proceed": 0, "caution": 1, "unsafe": 2}


class ObservationRuleError(ValueError):
    """Raised when an observation-rule update fails validation."""


def _key_for(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip()).lower()


def list_rules() -> list[ObservationRule]:
    """All rules in display order. Seeds the three defaults the first time
    (the table is never left empty: the last rule cannot be deleted)."""
    rules = (
        ObservationRule.query.order_by(
            ObservationRule.sort_order, ObservationRule.id
        ).all()
    )
    if rules:
        return rules

    for order, (key, label, effect) in enumerate(DEFAULT_RULES):
        db.session.add(
            ObservationRule(key=key, label=label, effect=effect, sort_order=order)
        )
    db.session.commit()
    return list_rules()


def valid_keys() -> set[str]:
    return {r.key for r in list_rules()}


def get_rule(key: str | None) -> ObservationRule | None:
    if not key:
        return None
    key = _key_for(key)
    return next((r for r in list_rules() if r.key == key), None)


def save_rules(items: list[dict], updated_by_user_id=None) -> list[str]:
    """Replace the rule set with `items` (add / edit / delete in one save).

    Each item: {"id": int | None, "label": str, "effect": str}. Order of the
    list becomes the display order. Nothing is written if any item is invalid.
    Returns human-readable change lines for the Activity Log.
    """
    if not isinstance(items, list) or not items:
        raise ObservationRuleError("Keep at least one observation.")

    existing = {r.id: r for r in list_rules()}
    taken_keys = {r.key for r in existing.values()}
    seen_labels: set[str] = set()
    cleaned: list[tuple[ObservationRule | None, str, str]] = []

    for item in items:
        label = re.sub(r"\s+", " ", str(item.get("label", "")).strip())
        effect = str(item.get("effect", "")).strip().lower()

        if not label:
            raise ObservationRuleError("Observation name cannot be empty.")
        if len(label) > MAX_LABEL_LENGTH:
            raise ObservationRuleError(
                f"Observation name must be {MAX_LABEL_LENGTH} characters or fewer."
            )
        if not _LABEL_RE.match(label):
            raise ObservationRuleError(
                f'"{label}": use letters, spaces and hyphens only.'
            )
        if effect not in EFFECTS:
            raise ObservationRuleError(f'"{label}": choose a valid effect.')

        norm = label.lower()
        if norm in seen_labels:
            raise ObservationRuleError(f'"{label}" is listed more than once.')
        seen_labels.add(norm)

        rule = None
        raw_id = item.get("id")
        if raw_id not in (None, ""):
            try:
                rule = existing[int(raw_id)]
            except (KeyError, TypeError, ValueError):
                raise ObservationRuleError("An observation no longer exists. Reload and try again.")

        if rule is None and _key_for(label) in taken_keys:
            raise ObservationRuleError(f'"{label}" already exists.')

        cleaned.append((rule, label, effect))

    changes: list[str] = []
    kept_ids = {r.id for r, _, _ in cleaned if r is not None}

    for rule_id, rule in existing.items():
        if rule_id not in kept_ids:
            changes.append(f"Removed observation '{rule.label}'.")
            db.session.delete(rule)

    for order, (rule, label, effect) in enumerate(cleaned):
        if rule is None:
            db.session.add(
                ObservationRule(
                    key=_key_for(label),
                    label=label,
                    effect=effect,
                    sort_order=order,
                    updated_by_user_id=updated_by_user_id,
                )
            )
            changes.append(f"Added observation '{label}' (effect: {effect}).")
            continue

        if rule.label != label:
            changes.append(f"Renamed observation '{rule.label}' -> '{label}'.")
            rule.label = label
        if rule.effect != effect:
            changes.append(f"'{label}' effect: {rule.effect} -> {effect}.")
            rule.effect = effect
        rule.sort_order = order
        rule.updated_by_user_id = updated_by_user_id

    db.session.commit()
    return changes


def apply_observation(level: str | None, observed_key: str | None) -> dict:
    """Effect of an operator observation on an overall result.

    `level` is the overall result already calculated from the readings
    ("safe"/"proceed", "caution", "unsafe", or anything else such as
    "unavailable"). An observation never lowers a result and never replaces
    an Unavailable one.

    Returns {"level": str, "raised": bool, "note": str | None}.
    """
    current = (level or "").strip().lower()
    rule = get_rule(observed_key)

    if rule is None or rule.effect == EFFECT_NONE or current not in _RANK:
        return {"level": current, "raised": False, "note": None}

    if _RANK[current] < _RANK[rule.effect]:
        return {
            "level": rule.effect,
            "raised": True,
            "note": f"Operator observed {rule.label.lower()} water.",
        }

    return {
        "level": current,
        "raised": False,
        "note": f"Operator also observed {rule.label.lower()} water.",
    }