"""How strictly geometry qualification is enforced.

Plan 23 §9.1. The fidelity and resolution gates ship **dark**: they compute,
they persist, they record — and until thresholds have been calibrated on the
WP8 corpus they contribute ``unrated`` to the summary and cannot block anything.

That has to be a real setting rather than a module constant. A constant would
be invisible to the GUI, absent from ``facade_coverage.json``, and unsettable
from the CLI or the API, which the parity rule in §9 forbids. It also has to be
readable from ``core`` without importing ``settings``, so the enum lives here
and the stored value is a plain string — the same split
:func:`~foammesh.core.geometry.diagnostics.budget.budget_from_settings` uses.
"""
from __future__ import annotations

from enum import Enum


class QualificationMode(str, Enum):
    """What a failing or unrated geometry verdict is allowed to do."""

    #: Compute and persist every report, contribute ``unrated`` to the summary,
    #: block nothing. The default, and the state the capability ships in.
    REPORT_ONLY = 'report_only'
    #: Verdicts gate: a blocking gate can stop a pipeline, and an export must
    #: prove it is qualified or waived. Reached only when WP8 promotes
    #: thresholds.
    ENFORCING = 'enforcing'

    @property
    def enforces(self) -> bool:
        return self is QualificationMode.ENFORCING


#: What a fresh install, an unreadable settings store, and a headless test all
#: get. Enforcing by accident would block exports on thresholds nobody has
#: calibrated, so the safe direction is unambiguous.
DEFAULT_MODE = QualificationMode.REPORT_ONLY


def qualification_mode() -> QualificationMode:
    """The configured mode, falling back to report-only rather than raising.

    Never propagates a settings failure: a corrupt or absent store means the
    capability behaves like a fresh install, not that meshing stops.
    """
    try:
        from foammesh.settings.app_settings import AppSettings

        return AppSettings().getQualificationMode()
    except Exception:
        return DEFAULT_MODE


def coerce(value) -> QualificationMode:
    """Read a stored value, tolerating anything that is not a known mode.

    Members pass through before the string conversion. ``str()`` on a
    ``(str, Enum)`` member gives ``'QualificationMode.ENFORCING'`` rather than
    ``'enforcing'``, so converting first would send every enum argument to the
    default -- and a ``set_mode(ENFORCING)`` that silently stores
    ``report_only`` would leave the capability dark with nothing to show for it.
    """
    if isinstance(value, QualificationMode):
        return value
    try:
        return QualificationMode(str(value))
    except ValueError:
        return DEFAULT_MODE
