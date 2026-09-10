"""Measure a published mesh against its reference, section by section.

Plan 23 §5. The last unwired piece: :mod:`.boundary` reconciles sections,
:mod:`.distance` measures two surfaces, :mod:`.report` judges and writes — and
nothing ran the measurement for each section and handed the deviations over.

**Reference-to-mesh is the direction that matters, and it is the one a
one-directional check omits.** Remove one of two parallel plates and the
mesh-to-reference sweep reports zero error at 100% coverage, because every
remaining mesh point is exactly where it should be. Only the reverse direction
sees the missing metre. §5.1 requires both; this runs both and takes the worse.

**A section is measured against the reference, not against the whole model.**
Each section gets the reference geometry it claims to represent, so a fin's
deviation is measured against the fin. Handing every section the entire
reference would let a badly-meshed fin score well by being near the base plate.

**The budget is per case and spent in section order.** When it runs out the
remaining sections are `incomplete` — named, with the reason — rather than
silently absent or, worse, `pass`. A check that quietly stops early and reports
what it managed is indistinguishable from a check that found nothing wrong.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import time
from typing import Mapping, Sequence

import numpy as np

from .policy import TolerancePolicy
from .report import FidelityReport, build, section_result


@dataclass(frozen=True)
class SectionMeasurement:
    """What measuring one section produced, or why it did not."""

    deviation: float | None
    metrics: Mapping
    seconds: float = 0.0
    #: ``(face_ids, deviations)`` for this section, or ``None``. The worst
    #: deviation per boundary face, which is the difference between knowing
    #: *which* patch left the reference and knowing *where* it did.
    hotspot: tuple | None = None
    #: Per-feature results for this section, in declaration order.
    features: tuple = ()


def _triangles_and_faces(mesh, section):
    """Triangles plus the face each came from.

    ``_triangles_of`` drops the third return value, which is the whole mapping
    the per-face field needs -- so the data was computed and discarded one step
    before it became useful.
    """
    from foammesh.core.mesh.poly_mesh_boundary import triangulate_faces

    return triangulate_faces(mesh, section.face_ids)


def measure_section(mesh, section, reference, *, tolerance: float,
                    budget_seconds: float | None = None,
                    features: Sequence = ()) -> SectionMeasurement:
    """Worst deviation between one section and the reference it represents.

    ``reference`` is a ``(vertices, triangles)`` pair. Returns a deviation of
    ``None`` when the section has no faces to measure — which the report turns
    into `incomplete` or `unrated` according to why.
    """
    from .distance import SurfaceLocator

    started = time.perf_counter()
    if section.face_ids is None or len(section.face_ids) == 0:
        return SectionMeasurement(None, {'reason': 'section has no faces'})

    subject_vertices, subject_triangles, source_face_ids = (
        _triangles_and_faces(mesh, section))
    if len(subject_triangles) == 0:
        return SectionMeasurement(None, {'reason': 'section has no triangles'})

    reference_vertices, reference_triangles = reference
    if len(reference_triangles) == 0:
        return SectionMeasurement(
            None, {'reason': 'no reference geometry for this section'})

    # Both directions. The reference-to-mesh sweep is the one that sees a
    # dropped feature; mesh-to-reference sees a mesh that wandered off the
    # surface. Neither implies the other, so the verdict takes the worse.
    subject_point_ids = np.unique(subject_triangles)
    subject_points = subject_vertices[subject_point_ids]
    reference_points = reference_vertices[np.unique(reference_triangles)]
    to_reference = SurfaceLocator(
        reference_vertices, reference_triangles).closest(subject_points)[0]
    to_mesh = SurfaceLocator(
        subject_vertices, subject_triangles).closest(reference_points)[0]

    elapsed = time.perf_counter() - started
    metrics = {
        'mesh_to_reference_max': float(to_reference.max()),
        'mesh_to_reference_mean': float(to_reference.mean()),
        'reference_to_mesh_max': float(to_mesh.max()),
        'reference_to_mesh_mean': float(to_mesh.mean()),
        'subject_triangles': int(len(subject_triangles)),
        'reference_triangles': int(len(reference_triangles)),
        'tolerance': float(tolerance),
        'seconds': round(elapsed, 3),
    }
    if budget_seconds is not None and elapsed > budget_seconds:
        metrics['over_budget'] = True

    # The mesh-to-reference direction is the one with a per-face home: its
    # samples sit on the mesh. Reference-to-mesh finds geometry that is
    # *absent*, and absence has no face to colour -- so the field carries the
    # first and the section verdict keeps taking the worse of both.
    hotspot = None
    try:
        from .hotspot import per_face_deviation

        hotspot = (
            np.asarray(section.face_ids, dtype=np.int64),
            per_face_deviation(
                subject_triangles, source_face_ids, subject_point_ids,
                to_reference, section.face_ids),
        )
    except Exception:                                       # noqa: BLE001
        # A missing per-face field costs a picture; a failed measurement costs
        # the verdict. The verdict wins.
        metrics['hotspot'] = 'unavailable'

    # §6.3. A blade's leading edge rounded by a millimetre moves almost no
    # area, so the area-weighted surface statistic barely registers it. That is
    # the whole reason features are measured separately -- and this call is the
    # one that was never made, so every feature verdict in the product was a
    # verdict nothing computed.
    featureResults: tuple = ()
    if features:
        from .features import measure_features

        try:
            featureResults = measure_features(
                features, (subject_vertices, subject_triangles),
                tolerance=tolerance)
        except Exception:                                   # noqa: BLE001
            metrics['features'] = 'unavailable'

    return SectionMeasurement(
        max(float(to_reference.max()), float(to_mesh.max())), metrics, elapsed,
        hotspot, featureResults)


def measure_case(*, task_id: str, mesh, sections: Sequence,
                 reference_for, policy: TolerancePolicy,
                 evidence: Mapping[str, str], thresholds=None,
                 budget_seconds: float | None = None,
                 category_of=None, region_of=None,
                 hotspots: dict | None = None,
                 features_for=None,
                 feature_results: dict | None = None) -> FidelityReport:
    """Measure every section and build the report.

    ``reference_for(section)`` returns that section's ``(vertices, triangles)``
    reference, or ``None`` when it has none — a section whose reference is
    absent is `unrated`, which is what its absence means.

    ``budget_seconds`` bounds the whole case. Sections still unmeasured when it
    is exhausted are reported `incomplete` by name; none is dropped.

    ``hotspots``, when given, is filled with ``solver_name -> (face_ids,
    deviations)``. A collector rather than a second return value because the
    report is frozen and immutable by design, and where a case keeps its
    artifacts is the caller's business, not this module's.

    ``features_for(section)`` returns the declared features a section should
    carry. Without it no feature is measured, which is what the product did
    until now -- and §6.3's rule that a critical feature below threshold fails
    its section could therefore never fire.
    """
    results = []
    collected = {} if hotspots is None else hotspots
    spent = 0.0
    for section in sections:
        status = str(getattr(section, 'status', '') or '')
        if status != 'matched':
            # Reconciliation already decided this one, and measuring it would
            # be answering a question identity has settled.
            results.append(section_result(
                section, deviation=None, policy=policy,
                thresholds=thresholds))
            continue

        if budget_seconds is not None and spent >= budget_seconds:
            results.append(section_result(
                section, deviation=None, policy=policy, thresholds=thresholds,
                category=_call(category_of, section),
                region=_call(region_of, section)))
            continue

        resolved = policy.resolve(
            patch_uuid=str(getattr(section, 'patch_uuid', '') or ''),
            category=_call(category_of, section),
            region=_call(region_of, section))
        if not resolved.resolved:
            # No tolerance means no denominator, so measuring would produce a
            # number with nothing to compare it to. Skip the work, keep the
            # honest verdict.
            results.append(section_result(
                section, deviation=None, policy=policy, thresholds=thresholds,
                category=_call(category_of, section),
                region=_call(region_of, section)))
            continue

        reference = reference_for(section)
        sectionFeatures = (
            tuple(features_for(section) or ()) if features_for else ())
        measurement = (
            SectionMeasurement(None, {'reason': 'no reference for section'})
            if reference is None else
            measure_section(mesh, section, reference,
                            tolerance=resolved.value,
                            budget_seconds=budget_seconds,
                            features=sectionFeatures))
        spent += measurement.seconds
        result = section_result(
            section, deviation=measurement.deviation, policy=policy,
            thresholds=thresholds, category=_call(category_of, section),
            region=_call(region_of, section),
            # R39/R99/R121: the cause is right here in the metrics -- "no
            # reference for section", "section has no faces" -- and the
            # judgement used to overwrite all of them with the budget wording.
            unmeasured_reason=('' if measurement.deviation is not None else
                               str(measurement.metrics.get('reason') or '')))
        result = _with_metrics(result, measurement.metrics)

        # §6.3: a critical feature below threshold fails its section even when
        # the area-weighted surface score is high.
        if measurement.features:
            from .features import section_verdict

            verdict, reason = section_verdict(result.verdict, measurement.features)
            if verdict != result.verdict:
                result = replace(result, verdict=verdict,
                                 reason=reason or result.reason)
            if feature_results is not None:
                feature_results[str(section.solver_name)] = tuple(
                    measurement.features)

        results.append(result)
        if measurement.hotspot is not None:
            collected[str(section.solver_name)] = measurement.hotspot

    return build(task_id, results, evidence=evidence, policy=policy,
                 thresholds=thresholds)


def _call(resolver, section) -> str:
    if resolver is None:
        return ''
    try:
        return str(resolver(section) or '')
    except Exception:
        return ''


def _with_metrics(result, metrics):
    from dataclasses import replace

    return replace(result, metrics=dict(metrics)) if metrics else result
