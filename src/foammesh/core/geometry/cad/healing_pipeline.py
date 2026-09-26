"""Staged, per-body CAD healing orchestration with a lazy OCCT backend."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from foammesh.core.quantities import agreeing


CAD_ACTION_ORDER = (
    'cad.analyze', 'cad.fix_shape', 'cad.sew', 'cad.fix_wireframe',
    'cad.remove_small_faces', 'cad.unify_same_domain',
    'cad.orient_and_solidify', 'cad.retessellate', 'cad.rediagnose')


class CadHealingBackend(Protocol):
    def clone(self, shape): ...
    def split_bodies(self, shape) -> list: ...
    def combine_bodies(self, bodies: list): ...
    def run(self, body, action: str, params: dict) -> tuple[object, dict]: ...


@dataclass(frozen=True)
class CadRepairAction:
    action: str
    params: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class CadHealingReport:
    route: str = 'cad'
    entries: list[dict] = field(default_factory=list)
    bodies: list[dict] = field(default_factory=list)
    bodies_total: int = 0
    bodies_failed: int = 0
    cancelled: bool = False
    measurement_boundary: str = (
        'Deviation is measured on before/after tessellations and is not exact B-Rep distance.')

    def to_dict(self):
        return {
            'route': self.route, 'entries': self.entries, 'bodies': self.bodies,
            'bodies_total': self.bodies_total, 'bodies_failed': self.bodies_failed,
            'cancelled': self.cancelled,
            'measurement_boundary': self.measurement_boundary,
        }


def execute(shape, actions: list[CadRepairAction], *, backend: CadHealingBackend,
            progress: Callable[[str, float], None] | None = None,
            cancelled: Callable[[], bool] | None = None):
    """Execute enabled rungs per body; one failed body never aborts its siblings."""
    progress = progress or (lambda _stage, _fraction: None)
    cancelled = cancelled or (lambda: False)
    enabled = [item for item in actions if item.enabled]
    unknown = [item.action for item in enabled if item.action not in CAD_ACTION_ORDER]
    if unknown:
        raise ValueError(
            f'unsupported CAD healing {agreeing(len(unknown), "action")}: '
            + ', '.join(unknown))
    bodies = backend.split_bodies(backend.clone(shape))
    report = CadHealingReport(bodies_total=len(bodies))
    output = []
    total = max(1, len(bodies) * max(1, len(enabled)))
    completed = 0
    for body_index, body in enumerate(bodies):
        body_entry_start = len(report.entries)
        current = body
        body_failed = False
        for item in enabled:
            if cancelled():
                report.cancelled = True
                output.extend([current, *bodies[body_index + 1:]])
                progress('cancelled', completed / total)
                return backend.combine_bodies(output), report
            started = time.perf_counter()
            try:
                candidate, metrics = backend.run(current, item.action, dict(item.params))
                elapsed = time.perf_counter() - started
                timeout = float(item.params.get('timeout_per_action_s', 600))
                if timeout < 30:
                    raise ValueError('timeout_per_action_s must be at least 30 seconds')
                if elapsed > timeout:
                    raise TimeoutError(
                        f'action exceeded {timeout:g} second wall-clock guard')
                current = candidate
                before_metrics = metrics.get('metrics_before')
                after_metrics = metrics.get('metrics_after')
                status = ('no_effect' if before_metrics is not None
                          and before_metrics == after_metrics
                          and int(metrics.get('entities_touched', 0)) == 0
                          else 'applied')
                detail = ''
            except Exception as error:  # body-level isolation is contractual
                status, detail, metrics = 'failed', str(error), {}
                body_failed = True
            completed += 1
            report.entries.append({
                'body_index': body_index, 'action': item.action,
                'params': dict(item.params), 'status': status,
                'metrics_before': metrics.get('metrics_before', {}),
                'metrics_after': metrics.get('metrics_after', {}),
                'entities_touched': int(metrics.get('entities_touched', 0)),
                'metrics': {key: value for key, value in metrics.items()
                            if key not in {'metrics_before', 'metrics_after'}},
                'duration_ms': round(
                    (time.perf_counter() - started) * 1000, 3), 'detail': detail,
            })
            progress(item.action, completed / total)
            if body_failed:
                break
        report.bodies_failed += int(body_failed)
        body_entries = report.entries[body_entry_start:]
        body_status = ('failed' if body_failed else
                       'applied' if any(item['status'] == 'applied'
                                        for item in body_entries)
                       else 'no_effect')
        report.bodies.append({
            'body_index': body_index, 'status': body_status,
            'actions_applied': sum(item['status'] == 'applied'
                                   for item in body_entries),
            'actions_no_effect': sum(item['status'] == 'no_effect'
                                     for item in body_entries),
        })
        output.append(current if not body_failed else body)
    progress('complete', 1.0)
    return backend.combine_bodies(output), report


#: DP-530. The tolerance a rung is run at when its plan states none, in the
#: shape's own units: OCCT's confusion scale, not a length the user chose.
_NATIVE_TOLERANCE = 1e-6


class OcctHealingBackend:
    """Real pythonocc adapter. Imports remain lazy so non-CAD installs stay usable."""

    def __init__(self, unit_factor: float = 1.0):
        #: DP-530. Metres per unit of the shape's coordinates -- 0.001 for a
        #: STEP or IGES read, which comes back in millimetres. A plan's
        #: ``tolerance`` is in metres, the unit the Repair page labels it in
        #: and suggests it in; OCCT takes the shape's own units, and
        #: :meth:`run` converts at the one place the two meet. The census it
        #: reports is converted back, so every length a report carries is in
        #: metres too.
        self.unit_factor = float(unit_factor or 1.0)
        self._history_steps = []

    def applied_tolerance(self, params: dict) -> float:
        """The OCCT tolerance, in the shape's units, for a rung's *params*.

        A ``tolerance`` the plan states is in metres and is converted. With
        none stated, the rung keeps OCCT's own confusion-scale default of
        ``1e-6`` of the shape's units -- a resolution of the data, not a
        length anybody typed.
        """
        if params.get('tolerance') is None:
            return _NATIVE_TOLERANCE
        return float(params['tolerance']) / self.unit_factor

    def _delta(self, before_shape, after_shape, tolerance: float,
               metrics: dict) -> dict:
        return _census_delta(before_shape, after_shape, tolerance, metrics,
                             unit_factor=self.unit_factor)

    def clone(self, shape):
        from .availability import require
        require()
        self._history_steps = []
        # Shapes are loaded afresh from the immutable revision for every
        # preview.  OCCT healing algorithms produce replacement shapes; using
        # the loaded handle as the lineage root preserves their history maps
        # without risking any persisted artifact.
        return shape

    def split_bodies(self, shape) -> list:
        from OCC.Core.TopAbs import TopAbs_COMPOUND, TopAbs_COMPSOLID, TopAbs_SHELL, TopAbs_SOLID
        from OCC.Core.TopoDS import TopoDS_Iterator
        bodies = []

        def visit(item):
            kind = item.ShapeType()
            if kind in {TopAbs_SOLID, TopAbs_SHELL}:
                bodies.append(item)
                return  # do not count a solid's nested shells as extra bodies
            if kind in {TopAbs_COMPOUND, TopAbs_COMPSOLID}:
                iterator = TopoDS_Iterator(item)
                while iterator.More():
                    visit(iterator.Value())
                    iterator.Next()

        visit(shape)
        solids = [item for item in bodies if item.ShapeType() == TopAbs_SOLID]
        shells = [item for item in bodies if item.ShapeType() == TopAbs_SHELL]
        if solids and shells:
            return [*solids, self.combine_bodies(shells)]
        if solids:
            return solids
        # STEP face soups commonly arrive as one compound containing one shell
        # per face.  They must be sewn as one logical body, not six isolated
        # one-face repairs.
        return [shape] if shells else (bodies or [shape])

    def combine_bodies(self, bodies: list):
        if len(bodies) == 1:
            return bodies[0]
        from OCC.Core.BRep import BRep_Builder
        from OCC.Core.TopoDS import TopoDS_Compound
        compound, builder = TopoDS_Compound(), BRep_Builder()
        builder.MakeCompound(compound)
        for body in bodies:
            builder.Add(compound, body)
        return compound

    def run(self, body, action: str, params: dict):
        # DP-530. In the shape's units from here on; `stated` is the metres
        # the plan asked for, and is what the report records.
        tolerance = self.applied_tolerance(params)
        stated = tolerance * self.unit_factor
        if action == 'cad.analyze':
            census = _analyze(body, tolerance, self.unit_factor)
            return body, {'metrics_before': census, 'metrics_after': census,
                          'entities_touched': 0, **census}
        if action == 'cad.fix_shape':
            from OCC.Core.ShapeFix import ShapeFix_Shape
            fixer = ShapeFix_Shape(body)
            fixer.SetPrecision(tolerance)
            fixer.SetMinTolerance(tolerance / 100)
            fixer.SetMaxTolerance(tolerance * 10)
            fixer.Perform()
            output = fixer.Shape()
            self._history_steps.append(('reshape', fixer.Context()))
            return output, self._delta(body, output, tolerance, {'precision': stated})
        if action == 'cad.sew':
            from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_Sewing

            def _sew(tol):
                sewing = BRepBuilderAPI_Sewing(tol)
                sewing.Add(body)
                sewing.Perform()
                return sewing, sewing.SewedShape()

            sewing, output = _sew(tolerance)
            delta = self._delta(body, output, tolerance, {'tolerance': stated})
            after_free_edges = delta['metrics_after']['free_edges']
            escalated = False
            # Appendix A §3.2: one bounded, recorded escalation (×5) — only when
            # the plan opts in and the first pass leaves an unresolved opening.
            if params.get('allow_escalation') and after_free_edges > 0:
                escalated_tolerance = tolerance * 5
                sewing2, output2 = _sew(escalated_tolerance)
                delta2 = self._delta(body, output2, escalated_tolerance, {
                    'tolerance': escalated_tolerance * self.unit_factor,
                    'escalated_from': stated})
                if delta2['metrics_after']['free_edges'] < after_free_edges:
                    sewing, output, delta = sewing2, output2, delta2
                    escalated = True
            delta['escalation_attempted'] = bool(
                params.get('allow_escalation') and after_free_edges > 0)
            delta['escalated'] = escalated
            self._history_steps.append(('sewing', sewing))
            return output, delta
        if action == 'cad.fix_wireframe':
            from OCC.Core.ShapeFix import ShapeFix_Wireframe
            fixer = ShapeFix_Wireframe(body)
            fixer.SetPrecision(tolerance)
            small = bool(fixer.FixSmallEdges())
            gaps = bool(fixer.FixWireGaps())
            output = fixer.Shape()
            self._history_steps.append(('reshape', fixer.Context()))
            return output, self._delta(body, output, tolerance, {
                'small_edges_fixed': small, 'wire_gaps_fixed': gaps})
        if action == 'cad.remove_small_faces':
            from OCC.Core.ShapeFix import ShapeFix_FixSmallFace
            fixer = ShapeFix_FixSmallFace()
            fixer.Init(body)
            fixer.SetPrecision(tolerance)
            fixer.Perform()
            output = fixer.FixShape()
            self._history_steps.append(('reshape', fixer.Context()))
            return output, self._delta(body, output, tolerance, {'tolerance': stated})
        if action == 'cad.unify_same_domain':
            from OCC.Core.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
            unify = ShapeUpgrade_UnifySameDomain(
                body, bool(params.get('unify_edges', True)),
                bool(params.get('unify_faces', True)), False)
            unify.Build()
            output = unify.Shape()
            self._history_steps.append(('history', unify.History()))
            return output, self._delta(body, output, tolerance, {'unified': True})
        if action == 'cad.orient_and_solidify':
            from OCC.Core.BRepClass3d import BRepClass3d_SolidClassifier
            from OCC.Core.TopAbs import TopAbs_IN, TopAbs_SHELL, TopAbs_SOLID
            from OCC.Core.TopoDS import topods
            current, solidified = body, False
            if body.ShapeType() == TopAbs_SHELL:
                from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_MakeSolid
                maker = BRepBuilderAPI_MakeSolid(topods.Shell(body))
                if maker.IsDone():
                    current, solidified = maker.Solid(), True
            reversed_body = False
            if current.ShapeType() == TopAbs_SOLID:
                classifier = BRepClass3d_SolidClassifier(current)
                classifier.PerformInfinitePoint(tolerance)
                if classifier.State() == TopAbs_IN:
                    current, reversed_body = current.Reversed(), True
            return current, self._delta(body, current, tolerance, {
                'solidified': solidified, 'reversed': reversed_body,
                'open_shell': current.ShapeType() != TopAbs_SOLID})
        if action in {'cad.retessellate', 'cad.rediagnose'}:
            # These rungs are performed by the store's tessellation/diagnostic boundary.
            census = _analyze(body, tolerance, self.unit_factor)
            return body, {'delegated_to_artifact_store': True,
                          'metrics_before': census, 'metrics_after': census,
                          'entities_touched': 0}
        raise ValueError(f'unsupported CAD healing action: {action}')

    def patch_map(self, before_shape, after_shape, patches: list[dict],
                  working_tolerance: float) -> dict:
        mapped = history_patch_map(
            before_shape, after_shape, patches,
            getattr(self, '_history_steps', ()))
        # DP-530. The working tolerance is in metres; the shapes are not.
        return mapped or geometric_patch_map(
            before_shape, after_shape, patches,
            float(working_tolerance) / self.unit_factor)


def _count(shape, kind) -> int:
    from OCC.Core.TopExp import TopExp_Explorer
    explorer, count = TopExp_Explorer(shape, kind), 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


def _analyze(shape, tolerance: float, unit_factor: float = 1.0) -> dict:
    """Native validity, free-wire, tolerance and small-entity census.

    *tolerance* is in the shape's units, as OCCT takes it. *unit_factor* is
    metres per unit of the shape (DP-530): the tolerances, lengths and areas
    the census reports are converted by it, so a STEP read in millimetres
    reports metres like everything else -- its 1 mm edge had been reported as
    an edge of ``1.0`` under a finding that says it speaks in metres.
    """
    factor = float(unit_factor or 1.0)
    from OCC.Core.BRepCheck import BRepCheck_Analyzer
    from OCC.Core.BRepGProp import brepgprop
    from OCC.Core.GProp import GProp_GProps
    from OCC.Core.ShapeAnalysis import ShapeAnalysis_FreeBounds, ShapeAnalysis_ShapeTolerance
    from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHAPE, TopAbs_WIRE
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopTools import TopTools_IndexedDataMapOfShapeListOfShape
    from OCC.Core.TopExp import topexp
    free = ShapeAnalysis_FreeBounds(shape, tolerance, False, False)
    census = ShapeAnalysis_ShapeTolerance()
    census.Tolerance(shape, TopAbs_SHAPE)
    face_areas, edge_lengths = [], []
    for kind, values, measure in (
            (TopAbs_FACE, face_areas, brepgprop.SurfaceProperties),
            (TopAbs_EDGE, edge_lengths, brepgprop.LinearProperties)):
        explorer = TopExp_Explorer(shape, kind)
        while explorer.More():
            props = GProp_GProps()
            measure(explorer.Current(), props)
            values.append(float(props.Mass()))
            explorer.Next()
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    topexp.MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    free_edges = sum(edge_faces.FindFromIndex(index).Size() == 1
                     for index in range(1, edge_faces.Size() + 1))
    nonmanifold_edges = sum(edge_faces.FindFromIndex(index).Size() > 2
                            for index in range(1, edge_faces.Size() + 1))
    return {
        'valid': bool(BRepCheck_Analyzer(shape).IsValid()),
        'free_closed_wires': _count(free.GetClosedWires(), TopAbs_WIRE),
        'free_open_wires': _count(free.GetOpenWires(), TopAbs_WIRE),
        'free_edges': free_edges,
        'nonmanifold_edges': nonmanifold_edges,
        'tolerance_census': {
            'min': float(census.GlobalTolerance(-1)) * factor,
            'avg': float(census.GlobalTolerance(0)) * factor,
            'max': float(census.GlobalTolerance(1)) * factor},
        'faces': len(face_areas), 'edges': len(edge_lengths),
        'small_faces': sum(area < (4 * tolerance) ** 2 for area in face_areas),
        'small_edges': sum(length < 2 * tolerance for length in edge_lengths),
        'face_area_min': (min(face_areas) * factor * factor
                          if face_areas else None),
        'edge_length_min': (min(edge_lengths) * factor
                            if edge_lengths else None),
    }


def _census_delta(before_shape, after_shape, tolerance: float, metrics: dict,
                  *, unit_factor: float = 1.0) -> dict:
    before = _analyze(before_shape, tolerance, unit_factor)
    after = _analyze(after_shape, tolerance, unit_factor)
    touched = (abs(after['faces'] - before['faces']) +
               abs(after['edges'] - before['edges']) +
               abs(after['free_edges'] - before['free_edges']) +
               abs(after['free_open_wires'] - before['free_open_wires']) +
               abs(after['free_closed_wires'] - before['free_closed_wires']))
    return {**metrics, 'metrics_before': before, 'metrics_after': after,
            'entities_touched': touched}


def geometric_patch_map(before_shape, after_shape, patches: list[dict],
                        working_tolerance: float) -> dict:
    """Strict geometric face lineage including bounded split/merge detection.

    This is deliberately conservative: a relation is emitted only when surface
    type, normal/plane, total area and area-weighted centroid all agree.  Any
    ambiguity remains lost/created instead of silently changing patch identity.
    """
    before, after = _face_signatures(before_shape), _face_signatures(after_shape)
    tolerance = max(4 * float(working_tolerance), 1e-12)
    unused_old, unused_new = set(range(len(before))), set(range(len(after)))
    preserved, split, merged = [], [], []

    # One-to-one matches are resolved first and must be unique in both directions.
    forward = {old_id: [new_id for new_id in unused_new
                        if _same_face(before[old_id], after[new_id], tolerance)]
               for old_id in unused_old}
    reverse = {new_id: [old_id for old_id in unused_old
                        if _same_face(before[old_id], after[new_id], tolerance)]
               for new_id in unused_new}
    for old_id, candidates in forward.items():
        if len(candidates) != 1 or len(reverse[candidates[0]]) != 1:
            continue
        patch_uuid = _patch_uuid(patches, old_id)
        if patch_uuid:
            new_id = candidates[0]
            preserved.append({'patch_uuid': patch_uuid, 'new_face_ids': [new_id]})
            unused_old.discard(old_id)
            unused_new.discard(new_id)

    # Many old co-domain fragments becoming one new face (unify-same-domain).
    for new_id in tuple(sorted(unused_new)):
        group = [old_id for old_id in sorted(unused_old)
                 if _same_domain(before[old_id], after[new_id], tolerance)]
        if len(group) < 2 or not _same_aggregate(
                [before[index] for index in group], after[new_id], tolerance):
            continue
        uuids = [value for value in (_patch_uuid(patches, index) for index in group)
                 if value]
        if len(uuids) != len(group):
            continue
        merged.append({'patch_uuids': uuids, 'new_face_id': new_id,
                       'kept': uuids[0]})
        unused_old.difference_update(group)
        unused_new.remove(new_id)

    # One old face becoming several co-domain fragments.
    for old_id in tuple(sorted(unused_old)):
        group = [new_id for new_id in sorted(unused_new)
                 if _same_domain(before[old_id], after[new_id], tolerance)]
        if len(group) < 2 or not _same_aggregate(
                [after[index] for index in group], before[old_id], tolerance):
            continue
        patch_uuid = _patch_uuid(patches, old_id)
        if not patch_uuid:
            continue
        split.append({'patch_uuid': patch_uuid, 'new_face_ids': group})
        unused_old.remove(old_id)
        unused_new.difference_update(group)

    lost = [{'patch_uuid': patch_uuid,
             'reason': 'no unique geometric face match'}
            for old_id in sorted(unused_old)
            if (patch_uuid := _patch_uuid(patches, old_id))]
    created = [{'new_face_id': index, 'assigned_patch': f'repair_fill_{number + 1}'}
               for number, index in enumerate(sorted(unused_new))]
    return {'preserved': preserved, 'split': split, 'merged': merged, 'lost': lost,
            'created': created, 'unmapped_triangles': 0,
            'method': 'geometric_match'}


def history_patch_map(before_shape, after_shape, patches: list[dict],
                      history_steps) -> dict | None:
    """Compose OCCT reshape/sewing/history relations across healing rungs."""
    if not history_steps:
        return None
    original_faces = _faces(before_shape)
    final_faces = _faces(after_shape)
    relations = {}
    for old_id, old_face in enumerate(original_faces):
        current = [old_face]
        removed = False
        for kind, history in history_steps:
            following = []
            for shape in current:
                try:
                    if kind == 'reshape' and history is not None and history.IsRecorded(shape):
                        value = history.Value(shape)
                        if value.IsNull():
                            removed = True
                        else:
                            following.extend(_faces(value) or [value])
                    elif kind == 'sewing' and history.IsModifiedSubShape(shape):
                        value = history.ModifiedSubShape(shape)
                        following.extend(_faces(value) or [value])
                    elif kind == 'make_shape':
                        values = list(history.Modified(shape))
                        if not values:
                            value = history.ModifiedShape(shape)
                            values = [] if value.IsNull() else [value]
                        following.extend(values or [shape])
                    elif kind == 'history' and history is not None:
                        if history.IsRemoved(shape):
                            removed = True
                            continue
                        values = list(history.Modified(shape)) + list(history.Generated(shape))
                        following.extend(value for value in values if not value.IsNull())
                        if not values:
                            following.append(shape)
                    else:
                        following.append(shape)
                except Exception:
                    return None
            current = following
        final_ids = sorted({index for index, final in enumerate(final_faces)
                            for value in current if value.IsSame(final)})
        if not final_ids and not removed:
            return None  # incomplete history: use the strict geometric fallback
        relations[old_id] = final_ids

    by_final = {}
    for old_id, final_ids in relations.items():
        for final_id in final_ids:
            by_final.setdefault(final_id, []).append(old_id)
    preserved, split, merged, lost = [], [], [], []
    consumed_old, consumed_new = set(), set()
    for final_id, old_ids in sorted(by_final.items()):
        if len(old_ids) < 2:
            continue
        uuids = [_patch_uuid(patches, index) for index in old_ids]
        if all(uuids):
            merged.append({'patch_uuids': uuids, 'new_face_id': final_id,
                           'kept': uuids[0]})
            consumed_old.update(old_ids)
            consumed_new.add(final_id)
    for old_id, final_ids in relations.items():
        if old_id in consumed_old:
            continue
        patch_uuid = _patch_uuid(patches, old_id)
        if not patch_uuid:
            continue
        if len(final_ids) == 1:
            preserved.append({'patch_uuid': patch_uuid, 'new_face_ids': final_ids})
            consumed_new.update(final_ids)
        elif len(final_ids) > 1:
            split.append({'patch_uuid': patch_uuid, 'new_face_ids': final_ids})
            consumed_new.update(final_ids)
        else:
            lost.append({'patch_uuid': patch_uuid, 'reason': 'removed by OCCT history'})
    created = [{'new_face_id': final_id,
                'assigned_patch': f'repair_fill_{number + 1}'}
               for number, final_id in enumerate(
                   sorted(set(range(len(final_faces))) - consumed_new))]
    return {'preserved': preserved, 'split': split, 'merged': merged,
            'lost': lost, 'created': created, 'unmapped_triangles': 0,
            'method': 'occt_history'}


def _faces(shape):
    from OCC.Core.TopAbs import TopAbs_FACE
    from OCC.Core.TopExp import TopExp_Explorer
    result, explorer = [], TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        result.append(explorer.Current())
        explorer.Next()
    return result


def _patch_uuid(patches: list[dict], index: int):
    return patches[index].get('patch_uuid') if index < len(patches) else None


def _same_normal(left: dict, right: dict) -> bool:
    if left.get('normal') is None or right.get('normal') is None:
        return True
    dot = abs(sum(a * b for a, b in zip(left['normal'], right['normal'])))
    return dot >= math.cos(math.radians(5))


def _same_domain(left: dict, right: dict, tolerance: float) -> bool:
    if left['surface_type'] != right['surface_type'] or not _same_normal(left, right):
        return False
    # Plane offset prevents unrelated parallel walls from being grouped.
    if left.get('plane_offset') is not None and right.get('plane_offset') is not None:
        if abs(left['plane_offset'] - right['plane_offset']) > tolerance:
            return False
    return True


def _same_face(left: dict, right: dict, tolerance: float) -> bool:
    return (_same_domain(left, right, tolerance)
            and abs(right['area'] - left['area']) / max(left['area'], 1e-30) <= .02
            and math.dist(right['centroid'], left['centroid']) <= tolerance)


def _same_aggregate(parts: list[dict], whole: dict, tolerance: float) -> bool:
    area = sum(item['area'] for item in parts)
    if abs(area - whole['area']) / max(whole['area'], 1e-30) > .02 or area <= 0:
        return False
    centroid = tuple(sum(item['centroid'][axis] * item['area'] for item in parts) / area
                     for axis in range(3))
    return math.dist(centroid, whole['centroid']) <= tolerance


def _face_signatures(shape):
    from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
    from OCC.Core.BRepGProp import brepgprop
    from OCC.Core.GProp import GProp_GProps
    from OCC.Core.TopAbs import TopAbs_FACE
    from OCC.Core.TopExp import TopExp_Explorer
    signatures, explorer = [], TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = explorer.Current()
        props = GProp_GProps()
        brepgprop.SurfaceProperties(face, props)
        center = props.CentreOfMass()
        adaptor = BRepAdaptor_Surface(face)
        normal = None
        try:
            from OCC.Core.gp import gp_Pnt, gp_Vec
            point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
            u = (adaptor.FirstUParameter() + adaptor.LastUParameter()) / 2
            v = (adaptor.FirstVParameter() + adaptor.LastVParameter()) / 2
            adaptor.D1(u, v, point, du, dv)
            cross = du.Crossed(dv)
            if cross.Magnitude() > 1e-15:
                cross.Normalize()
                values = [cross.X(), cross.Y(), cross.Z()]
                # Canonical direction makes reversed topology compare equally.
                dominant = max(range(3), key=lambda index: abs(values[index]))
                if values[dominant] < 0:
                    values = [-value for value in values]
                normal = tuple(values)
        except Exception:
            pass
        centroid = (center.X(), center.Y(), center.Z())
        signatures.append({'area': float(props.Mass()), 'centroid': centroid,
                           'surface_type': int(adaptor.GetType()), 'normal': normal,
                           'plane_offset': (sum(a * b for a, b in zip(normal, centroid))
                                            if normal is not None and int(adaptor.GetType()) == 0
                                            else None)})
        explorer.Next()
    return signatures
