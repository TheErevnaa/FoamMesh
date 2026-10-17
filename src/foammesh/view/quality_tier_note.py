"""The line that names which tier of max non-orthogonality a value belongs to.

Plan 40 UXD9 (N7). Max non-orthogonality has three values -- the strict gate
snappyHexMesh holds Snap and Layers to (65), the checkMesh report threshold
the verdict is judged at (70) and the relaxed gate it falls back to while
adding layers (75). The user decided on 2026-10-10 to keep all three, so the
interface has to say which is which wherever a threshold shows. This note is
that sentence, built from the one quality policy and the case's own values.
No verdict reads anything here.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.core.quality.policy import (
    TIER_RELAXED, TIER_REPORT, TIER_ROLES, TIER_STRICT, tier_default_value,
    tier_label, tier_legend)

#: Registry field id of the value each tier is.
TIER_FIELDS = {
    TIER_STRICT: 'quality.thresholds.max_non_orthogonality',
    TIER_REPORT: 'quality.check.non_orth_threshold',
    TIER_RELAXED: 'quality.thresholds.relaxed.max_non_orthogonality',
}


def tier_values(client=None) -> dict:
    """The three numbers as the case holds them; the policy's where unset."""
    values = {tier: tier_default_value(tier) for tier in TIER_FIELDS}
    reader = getattr(client, 'field_values', None)
    if reader is None:
        return values
    try:
        held = reader(tuple(TIER_FIELDS.values())) or {}
    except Exception:                      # a client with no case is fine
        return values
    for tier, field_id in TIER_FIELDS.items():
        value = held.get(field_id)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[tier] = value
    return values


def tier_note_text(tier: str, values: dict) -> str:
    if not tier:
        return tier_legend(values)
    return (f'{tier_label(tier, values.get(tier))}: {TIER_ROLES[tier]}. '
            + tier_legend(values, current=tier))


class TierNote(QLabel):
    """One group's tier, and the other two beside it."""

    def __init__(self, tier: str, client=None, parent=None):
        super().__init__(parent)
        self._tier = tier
        self._client = client
        self.setObjectName('maxNonOrthoTierNote')
        self.setWordWrap(True)
        self.setProperty('foammeshTone', 'secondary')
        self.refresh()

    def tier(self) -> str:
        return self._tier

    def refresh(self) -> None:
        self.setText(tier_note_text(self._tier, tier_values(self._client)))


def tier_tag(tier: str, parent=None) -> QLabel:
    """A short label naming a value's tier, to sit beside the value."""
    from foammesh.core.quality.policy import TIER_NAMES

    tag = QLabel(TIER_NAMES[tier], parent)
    tag.setObjectName('maxNonOrthoTierTag')
    tag.setProperty('foammeshTone', 'secondary')
    tag.setToolTip(f'{TIER_NAMES[tier]}: {TIER_ROLES[tier]}.')
    return tag
