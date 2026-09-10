"""Assemble §6's five tolerance levels from what a case actually stores.

Plan 23 §6. :mod:`.policy` decides which level wins; this decides what is *in*
each level, by reading the places a user's intent is actually recorded.

**A level with no source is empty, not defaulted.** Two of the five have no
storage in the product yet — there is no UI for a per-patch tolerance override
and none for a boundary-category policy. Those levels come back empty here, and
the honest consequence is that sections relying on them are `unrated`. The
alternative, inventing a plausible number so the level "works", is precisely
what §6 forbids: no hidden default may turn an uncalibrated case green. An
empty level is a visible gap; a fabricated one is an invisible claim.

**The feature manifest is the one level with real data today.**
``FeaturePolicy.max_distance_m`` is a tolerance a user set on a named feature,
carried across re-preparation with its ``feature_uuid``. That is level 1, the
one that outranks everything, and it is the level that matters most — the WP8
corpus showed a 1.5 mm fin needs its own tolerance or its loss reads as
acceptable against the model it sits on.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .policy import TolerancePolicy

#: Where a project-wide fallback tolerance is stored, when one is set.
#:
#: Not under ``geometry``: that node is the geometry *list*, so a settings
#: leaf cannot live below it. It sits with the other geometry-preparation
#: settings instead, which is where the page that edits it already is.
PROJECT_TOLERANCE_KEY = 'geometryPreparation/qualificationToleranceM'


def suggested_tolerance(diagonal_m: float) -> float:
    """What to offer a project that has not chosen a tolerance.

    A thousandth of the prepared bounding-box diagonal: the scale at which a
    deviation starts to matter on a model of that size, and the number a
    reviewer would reach for first. Plan 28 WP6 puts it in front of the user
    on the Reference Readiness page rather than applying it silently -- a
    fabricated level 5 would turn an uncalibrated case green without anyone
    having decided anything, which is exactly what this module refuses to do.
    """
    diagonal = float(diagonal_m)
    if not diagonal > 0:
        return 0.0
    return diagonal * 1e-3


def from_feature_manifest(manifest) -> dict:
    """Level 1: ``feature_uuid -> tolerance``, from authored feature policy.

    Only features whose policy states a distance tolerance contribute. A
    feature that was detected but never given one does not silently acquire the
    project fallback at level 1 — it falls through to whichever level does
    apply, which is what precedence means.
    """
    out: dict[str, float] = {}
    for feature in _features_of(manifest):
        uuid = str(_get(feature, 'feature_uuid') or '').strip()
        policy = _get(feature, 'policy')
        value = _get(policy, 'max_distance_m') if policy is not None else None
        if uuid and value is not None and float(value) > 0:
            out[uuid] = float(value)
    return out


def _features_of(manifest):
    if manifest is None:
        return ()
    features = _get(manifest, 'features')
    return features or ()


def _get(item, key):
    if item is None:
        return None
    if isinstance(item, Mapping):
        return item.get(key)
    return getattr(item, key, None)


def project_tolerance(db) -> float | None:
    """Level 5, and only when the project actually set one.

    Returns ``None`` for an unset, unreadable or non-positive value. A project
    that has not chosen a fallback has no level 5 — not a default nobody chose.
    """
    if db is None:
        return None
    for accessor in ('getValue', 'get'):
        method = getattr(db, accessor, None)
        if method is None:
            continue
        try:
            value = method(PROJECT_TOLERANCE_KEY)
        except Exception:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def for_case(case_path: str | Path, *, db=None, manifest=None
             ) -> TolerancePolicy:
    """The policy in force for one case.

    ``patch`` and ``category`` are deliberately empty: the product stores
    neither yet, and this says so by leaving them so rather than by filling
    them in.
    """
    if manifest is None:
        manifest = _read_feature_manifest(case_path)
    return TolerancePolicy(
        feature=from_feature_manifest(manifest),
        patch={},      # no per-patch override storage yet
        category={},   # no boundary-category policy storage yet
        region={},     # populated from region policy when one is authored
        project=project_tolerance(db))


def _read_feature_manifest(case_path: str | Path):
    path = (Path(case_path) / 'foammesh' / 'geometry' / 'features.json')
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def unresolved_levels(policy: TolerancePolicy) -> tuple[str, ...]:
    """Which of §6's levels have no data, for a report that explains itself.

    A section reported `unrated` should be able to say *why* no tolerance
    applied, and "levels 2 and 3 have no storage in this build" is a more
    actionable answer than "none applied".
    """
    empty = []
    for level in ('feature', 'patch', 'category', 'region'):
        if not getattr(policy, level):
            empty.append(level)
    if policy.project is None:
        empty.append('project')
    return tuple(empty)


def tightest(policy: TolerancePolicy | None) -> float:
    """The smallest tolerance this policy can hand any section.

    §16.1 sizes the validation reference from ``tau_min``: the surface a mesh
    is judged against has to be finer than the tightest thing it will be
    asked to judge, or the tessellator's own error is spent tolerance.

    Zero when no level holds a tolerance, which keeps the reference unrated
    rather than inventing a number nobody set -- the same rule the rest of
    this module keeps.
    """
    if policy is None:
        return 0.0
    values = [float(value)
              for level in ('feature', 'patch', 'category', 'region')
              for value in (getattr(policy, level, None) or {}).values()
              if value is not None and float(value) > 0]
    project = getattr(policy, 'project', None)
    if project is not None and float(project) > 0:
        values.append(float(project))
    return min(values) if values else 0.0
