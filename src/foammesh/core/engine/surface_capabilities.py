"""Plan 49 SR7. One capability table and one validator for surface intent.

Before SR7 each route refused what it cannot build in its own place, in its
own words: the snappy case builder (``openfoam/case_builder.py``, SR4), the
Gmsh job check (``core/gmsh/surface_roles.py``, SR5), the periodic relation
measurement (``core/geometry/periodic_relations.py``, SR3) and the surface
tab (``db/surface_authoring.py``, SR2). The person saw a different sentence
-- or none -- in the dialog, at Run and at Export.

This module is the one answer to "can this route build this surface for this
target?":

* :data:`CAPABILITIES` -- role / mesh type / periodic transform x route x
  target -> supported, refused (with reason and remedy) or supported only on
  a condition of the case (a two-dimensional or axisymmetric section, a solid
  import). :func:`capability_table` lists it.
* :func:`validate` -- every issue of a project's surface intent for one
  route and target: unresolved surfaces and relations (plan D7: saved as a
  visible draft, blocked at run and export), unsupported constructions, a
  periodic pair that failed or could not be measured, a side that is two
  things at once, and a mesh built from other intent than the project holds.
  Each issue says the surface, the route, the target, the reason and the
  remedy, and prints as one plain line ``surface: reason. remedy``.
* :func:`refusal` -- the same list as the sentence a run or an export is
  refused with (:class:`SurfaceIntentRefused`).
* :func:`intent_digest`, :func:`write_built_record`,
  :func:`write_exported_record` and :func:`stage_report` -- what was
  requested, supported, geometrically validated, built and exported, kept
  apart; a record made for another route, target or intent is stale.

The engine-specific builders keep their own refusals as a second line of
defence; the gates call this first, so the sentence the person reads is this
one. Nothing here imports Qt, Gmsh or the facade.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

from foammesh.db import surface_intent as model

ROUTE_SNAPPY = 'snappy'
ROUTE_GMSH = 'gmsh'
ROUTES = (ROUTE_SNAPPY, ROUTE_GMSH)

TARGET_OPENFOAM = 'openfoam'
TARGET_SU2 = 'su2'
#: The Gmsh route's own files (``.msh``, ``.med``, ``.unv``, ...) and the
#: dataset conversions (VTK, CGNS) of a mesh either route made: named groups,
#: no solver coupling.
TARGET_NATIVE = 'native'
TARGETS = (TARGET_OPENFOAM, TARGET_SU2, TARGET_NATIVE)

ROUTE_NAMES = {ROUTE_SNAPPY: 'snappyHexMesh', ROUTE_GMSH: 'Gmsh'}
TARGET_NAMES = {TARGET_OPENFOAM: 'OpenFOAM', TARGET_SU2: 'SU2',
                TARGET_NATIVE: 'native mesh files'}

#: Conditions a supported entry may carry; checked against the case.
TWO_D = 'two_d'
AXISYMMETRIC = 'axisymmetric'
SOLID_IMPORT = 'solid_import'

#: Subjects of the table: the five roles, the free mesh types of an external
#: boundary, the two shapes of a conformal interface and the periodic
#: transforms.
CONFORMAL_SEPARATE = 'regionInterfaceConformal:separate'
TYPE_PREFIX = 'type:'
PERIODIC_PREFIX = 'periodic:'

_TESSELLATED = ('.stl', '.obj', '.ply')


@dataclass(frozen=True)
class Capability:
    supported: bool
    reason: str = ''
    remedy: str = ''
    #: A requirement of the case a supported entry is subject to.
    condition: str = ''
    #: What "supported" means when the target carries less than the role.
    note: str = ''

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items()
                if value not in ('', None)}


_YES = Capability(True)

_SNAPPY_3D = ('snappyHexMesh builds a three-dimensional mesh, and {what} '
              'needs a mesh one cell thick')
_SNAPPY_3D_REMEDY = ('Mesh the 2-D or axisymmetric case on the Gmsh route, '
                     'or give the surface another type.')
_SU2_ONE_ZONE = ('SU2 is written by Gmsh as one zone, so {what} would reach '
                 'it as plain markers with the volumes on either side merged '
                 'and no coupling')
_SU2_REMEDY = ('Export OpenFOAM for a multi-region case, or make the surface '
               'an external boundary.')
_AFFINE = ('an affine periodic pair is a native Gmsh control: {target} '
           'periodic patches take only a translation or a rotation')
_AFFINE_REMEDY = ('Give the pair a translation or a rotation, or export only '
                  'the native Gmsh mesh.')


def _table() -> dict:
    table: dict[tuple[str, str, str], Capability] = {}

    def put(subject, route, target, capability):
        table[(subject, route, target)] = capability

    pairs = ((ROUTE_SNAPPY, TARGET_OPENFOAM), (ROUTE_SNAPPY, TARGET_NATIVE),
             (ROUTE_GMSH, TARGET_OPENFOAM), (ROUTE_GMSH, TARGET_SU2),
             (ROUTE_GMSH, TARGET_NATIVE))
    for route, target in pairs:
        snappy = route == ROUTE_SNAPPY
        su2 = target == TARGET_SU2
        put(model.EXTERNAL, route, target, _YES)
        for patch_type in ('patch', 'wall', 'symmetry'):
            put(TYPE_PREFIX + patch_type, route, target, _YES)
        if snappy:
            put(TYPE_PREFIX + 'symmetryPlane', route, target, Capability(
                False, 'snappyHexMesh snaps the surface, so its faces are '
                'never exactly planar and OpenFOAM refuses them as a '
                'symmetryPlane', 'Use the symmetry type.'))
            put(TYPE_PREFIX + 'empty', route, target, Capability(
                False, _SNAPPY_3D.format(what='an empty patch'),
                _SNAPPY_3D_REMEDY))
            put(TYPE_PREFIX + 'wedge', route, target, Capability(
                False, _SNAPPY_3D.format(what='a wedge patch'),
                _SNAPPY_3D_REMEDY))
        else:
            put(TYPE_PREFIX + 'symmetryPlane', route, target, _YES)
            put(TYPE_PREFIX + 'empty', route, target, Capability(
                True, 'an empty patch is the front or back of a mesh one cell '
                'thick', 'Choose the two-dimensional section (Dimension) or '
                'give the surface another type.', condition=TWO_D))
            put(TYPE_PREFIX + 'wedge', route, target, Capability(
                True, 'a wedge patch is a side of a revolved sector one cell '
                'thick', 'Choose the axisymmetric section (Dimension) or give '
                'the surface another type.', condition=AXISYMMETRIC))
        # Internal face: a face zone. SU2 has none (DP-5883).
        put(model.INTERNAL, route, target, Capability(
            False, 'SU2 has no face zone: Gmsh would write it as a boundary '
            'marker inside the domain', 'Export OpenFOAM to keep it as a face '
            'zone, or make it a blocking baffle or an external boundary.')
            if su2 else _YES)
        # Blocking baffle: two walls (snappy faceType baffle; Gmsh Crack).
        put(model.BAFFLE, route, target, Capability(
            True, note='two markers on duplicated nodes') if su2 else _YES)
        # Conformal interface: one shared surface, or two surfaces.
        conformal = (Capability(False, _SU2_ONE_ZONE.format(
            what='a region interface'), _SU2_REMEDY) if su2 else _YES)
        put(model.CONFORMAL, route, target, conformal)
        if snappy:
            put(CONFORMAL_SEPARATE, route, target, Capability(
                False, 'a conformal interface between two separate surfaces '
                'cannot be built: snappyHexMesh cuts one mesh, so a conformal '
                'contact is one surface shared by both regions',
                'Use one shared surface for the contact (Split interfaces '
                'makes it), or make the interface non-conformal.'))
        else:
            put(CONFORMAL_SEPARATE, route, target, conformal)
        # Non-conformal interface.
        if su2:
            put(model.NON_CONFORMAL, route, target, Capability(
                False, _SU2_ONE_ZONE.format(
                    what='a non-conformal interface'), _SU2_REMEDY))
        elif snappy:
            put(model.NON_CONFORMAL, route, target, Capability(
                True, note=('coupled by createNonConformalCouples after the '
                            'region split' if target == TARGET_OPENFOAM else
                            'two boundary patches, not coupled')))
        else:
            put(model.NON_CONFORMAL, route, target, Capability(
                True, 'a non-conformal interface is met on the Gmsh route by '
                'meshing its two solids apart, and a surface-mesh import has '
                'no solids to keep apart', 'Make the interface conformal, or '
                'import the solid model.', condition=SOLID_IMPORT,
                note=('coupled by createNonConformalCouples after the region '
                      'split' if target == TARGET_OPENFOAM else
                      'two boundary groups, not coupled')))
        # Periodic pairs.
        for transform in ('translational', 'rotational'):
            put(PERIODIC_PREFIX + transform, route, target, Capability(
                True, note='two markers; the MARKER_PERIODIC line is in '
                'the export notes and the SU2 configuration is not written '
                '(Plan 49 SR6)') if su2 else _YES)
        affine_ok = route == ROUTE_GMSH and target == TARGET_NATIVE
        put(PERIODIC_PREFIX + 'affine', route, target, _YES if affine_ok else
            Capability(False, _AFFINE.format(
                target=('snappyHexMesh cyclic' if snappy
                        else TARGET_NAMES[target])), _AFFINE_REMEDY))
    return table


CAPABILITIES = _table()

#: A whole route/target that is not offered, whatever the surfaces are.
ROUTE_REFUSALS = {
    (ROUTE_SNAPPY, TARGET_SU2): Capability(
        False, 'SU2 is produced on the Gmsh route only (Plan 28)',
        'Choose the Gmsh route for SU2, or export OpenFOAM.'),
}


def capability(subject: str, route: str, target: str) -> Capability:
    """The table's entry; an unknown subject is refused, never assumed."""
    route, target = normalise_route(route), normalise_target(target)
    found = ROUTE_REFUSALS.get((route, target))
    if found is not None:
        return found
    found = CAPABILITIES.get((subject, route, target))
    if found is None:
        return Capability(False, f'"{subject}" is not a construction the '
                                 f'{ROUTE_NAMES.get(route, route)} route '
                                 'knows', 'Choose another role or type.')
    return found


def capability_table() -> list[dict]:
    """The table as rows: subject, route, target and the verdict."""
    rows = []
    for (subject, route, target), entry in sorted(CAPABILITIES.items()):
        rows.append({'subject': subject, 'route': route, 'target': target,
                     **entry.to_dict()})
    for (route, target), entry in sorted(ROUTE_REFUSALS.items()):
        rows.append({'subject': '*', 'route': route, 'target': target,
                     **entry.to_dict()})
    return rows


def normalise_route(route) -> str:
    token = str(getattr(route, 'value', route) or '').strip().lower()
    return {'snappyhexmesh': ROUTE_SNAPPY}.get(token, token)


def normalise_target(target, route: str = '') -> str:
    """``unselected`` (a project from before Plan 28) is what the route
    publishes by default: an OpenFOAM mesh."""
    token = str(getattr(target, 'value', target) or '').strip().lower()
    if token in ('', 'unselected'):
        return TARGET_OPENFOAM
    return token


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #

#: Issue codes; the API carries them (SR8).
UNRESOLVED = 'unresolved'
UNSUPPORTED = 'unsupported'
CONDITION = 'condition_not_met'
UNVERIFIED = 'unverified'
FAILED = 'verification_failed'
CONFLICT = 'side_conflict'
STALE = 'stale_mesh'
#: DP-6071. The route's own builder (the snappy dictionaries, the Gmsh
#: layer carve) cannot build this combination, though the table supports
#: each part of it; the reason and remedy are the builder's.
NOT_BUILDABLE = 'not_buildable'
ISSUE_CODES = (UNRESOLVED, UNSUPPORTED, CONDITION, UNVERIFIED, FAILED,
               CONFLICT, STALE, NOT_BUILDABLE, 'region_sides')


@dataclass(frozen=True)
class IntentIssue:
    subject: str
    reason: str
    remedy: str
    route: str
    target: str
    code: str
    kind: str = 'surface'
    #: A draft may be saved with it (plan D7); a run or an export may not.
    blocking: bool = True

    def text(self) -> str:
        reason = self.reason.rstrip('. ')
        return f'{self.subject}: {reason}. {self.remedy}'.strip()

    def to_dict(self) -> dict:
        data = asdict(self)
        data['route_name'] = ROUTE_NAMES.get(self.route, self.route)
        data['target_name'] = TARGET_NAMES.get(self.target, self.target)
        data['text'] = self.text()
        return data


class SurfaceIntentRefused(ValueError):
    """A run or an export blocked by the surface intent (plan D7)."""

    def __init__(self, issues: Iterable[IntentIssue], *, stage: str):
        self.issues = list(issues)
        self.stage = stage
        super().__init__(refusal_text(self.issues, stage=stage))

    def details(self) -> dict:
        return {'error': 'surface_intent_refused', 'stage': self.stage,
                'issues': [issue.to_dict() for issue in self.issues]}


def refusal_text(issues, *, stage: str) -> str:
    if not issues:
        return ''
    first = issues[0]
    what = {'run': 'The mesh was not run', 'export': 'Nothing was exported',
            'dictionaries': 'The dictionaries were not written'
            }.get(stage, 'The surface settings cannot be built')
    return (f'{what} ({ROUTE_NAMES.get(first.route, first.route)} route, '
            f'{TARGET_NAMES.get(first.target, first.target)}): '
            + ' '.join(issue.text() for issue in issues))


def assessed_document(document) -> dict:
    """A copy of *document* with every surface assessed (load migration)."""
    if not isinstance(document, dict):
        return {}
    copied = copy.deepcopy(document)
    model.migrate_surface_intent(copied, [])
    model.refresh_surface_intent(copied)
    return copied


def document_of(db) -> dict:
    content = getattr(db, 'data', None)
    if not callable(content):
        return {}
    try:
        document = content()
    except Exception:  # noqa: BLE001 - a db with no document has no intent
        return {}
    return document if isinstance(document, dict) else {}


def route_target_of(document) -> tuple[str, str]:
    """The configured ``(route, target)`` a project document holds."""
    mesh = (document or {}).get('mesh')
    mesh = mesh if isinstance(mesh, dict) else {}

    def token(value):
        value = getattr(value, 'value', value)
        return str(value or '').split('.')[-1].strip().lower()

    return (normalise_route(token(mesh.get('engine'))),
            normalise_target(token(mesh.get('targetSolver'))))


def dimensionality_of(document) -> str:
    gmsh = (document or {}).get('gmsh')
    section = gmsh.get('dimensionality') if isinstance(gmsh, dict) else None
    mode = model._text((section or {}).get('mode')) if isinstance(
        section, dict) else ''
    return mode.split('.')[-1] or 'three_d'


def _condition_met(condition, *, dimensionality, geometry) -> bool:
    if condition == TWO_D:
        return dimensionality == 'two_d'
    if condition == AXISYMMETRIC:
        return dimensionality == 'axisymmetric'
    if condition == SOLID_IMPORT:
        return not any(str(item).lower().endswith(_TESSELLATED)
                       for item in geometry or ())
    return True


def _name(row, key) -> str:
    return model._text((row or {}).get('name')) or f'surface {key}'


def _relation_label(relation, key) -> str:
    return (model._text(relation.get('name'))
            or model._text(relation.get('relationId')) or f'relation {key}')


def _surface_tokens(geometry) -> dict:
    return {model._text(row.get('patchUuid')): str(key)
            for key, row in geometry.items()
            if model._is_surface(row) and model._text(row.get('patchUuid'))}


def _in_scope(row, patch_uuids) -> bool:
    if patch_uuids is None:
        return True
    return model._text(row.get('patchUuid')) in patch_uuids


def validate(document, route, target, *, stage: str = 'edit',
             dimensionality: str | None = None, geometry: Iterable = (),
             keys: Iterable | None = None,
             patch_uuids: Iterable | None = None,
             assessed: bool = False) -> list[IntentIssue]:
    """Every issue of *document*'s surface intent on *route* for *target*.

    ``stage`` is ``edit`` (the dialog and the periodic panels: the draft is
    saved, the issues are shown), ``run`` or ``export`` (each issue blocks).
    ``keys`` limits the surfaces to those rows (and the relations naming
    them); ``patch_uuids`` limits them to the patches a prepared revision
    holds. ``geometry`` is the files being meshed (a surface-mesh import
    cannot keep a non-conformal contact apart on Gmsh). The document is read
    on an assessed copy unless ``assessed``.
    """
    route = normalise_route(route)
    target = normalise_target(target, route)
    document = document if assessed else assessed_document(document)
    geometry_rows = document.get('geometry') or {}
    if not isinstance(geometry_rows, dict):
        geometry_rows = {}
    dimensionality = dimensionality or dimensionality_of(document)
    blocking = stage != 'edit'
    wanted = None if keys is None else {str(key) for key in keys}
    scope = None if patch_uuids is None else {str(item) for item in
                                              patch_uuids}
    issues: list[IntentIssue] = []

    def add(subject, reason, remedy, code, kind='surface', block=None):
        issue = IntentIssue(subject, reason, remedy, route, target, code,
                            kind, blocking if block is None else block)
        if issue not in issues:
            issues.append(issue)

    route_refusal = ROUTE_REFUSALS.get((route, target))
    if route_refusal is not None:
        if stage == 'export':
            # A mesh this route made is exported as SU2 only if the export's
            # own census accepts it; the route rule is the run's.
            return issues
        add('Case', route_refusal.reason, route_refusal.remedy, UNSUPPORTED,
            kind='case')
        return issues

    def judge(subject, label, kind):
        entry = capability(subject, route, target)
        if not entry.supported:
            add(label, entry.reason, entry.remedy, UNSUPPORTED, kind)
        elif entry.condition and not _condition_met(
                entry.condition, dimensionality=dimensionality,
                geometry=geometry):
            add(label, entry.reason, entry.remedy, CONDITION, kind)

    relations = document.get('surfaceRelations') or {}
    if not isinstance(relations, dict):
        relations = {}
    shown_relations = set()
    for key, row in model._sorted_items(geometry_rows):
        key = str(key)
        if not model._is_surface(row) or not _in_scope(row, scope):
            continue
        if wanted is not None and key not in wanted:
            continue
        name = _name(row, key)
        role = model._text(row.get('surfaceRole'))
        state = model._text(row.get('intentState'))
        mine = model.relations_of(document, key)
        if state == model.UNRESOLVED:
            reason = model._text(row.get('intentIssue')) or 'it has no role'
            own = [part for part in reason.split('; ')
                   if part and part != 'its relation is unresolved']
            if own or not mine:
                from foammesh.db.surface_authoring import remedy_for
                text = '; '.join(own) or reason
                remedy = ('' if not role else remedy_for(text))
                if not role:
                    # A migration repair item: its text carries the remedy
                    # when it has one.
                    add(name, text, '' if '. ' in text.rstrip('. ') else
                        'Choose its role in the surface dialog.', UNRESOLVED)
                else:
                    add(name, text, remedy, UNRESOLVED)
        if role == model.EXTERNAL:
            judge(model.EXTERNAL, name, 'surface')
            patch_type = model._text(row.get('meshPatchType'))
            if patch_type:
                judge(TYPE_PREFIX + patch_type, name, 'surface')
        elif role in (model.INTERNAL, model.BAFFLE, model.NON_CONFORMAL):
            judge(role, name, 'surface')
        elif role == model.CONFORMAL:
            separate = any(
                model._text(relation.get('kind')) == model.CONFORMAL
                and not model._flag(relation.get('sharedSurface'))
                for _relation_key, relation in mine)
            judge(CONFORMAL_SEPARATE if separate else model.CONFORMAL, name,
                  'surface')
        for relation_key, _relation in mine:
            shown_relations.add(str(relation_key))

    for key, relation in model._sorted_items(relations):
        if not isinstance(relation, dict):
            continue
        tokens = {model._text(relation.get('ownerPatchUuid')),
                  model._text(relation.get('neighbourPatchUuid'))} - {''}
        if scope is not None and not tokens & scope:
            continue
        if wanted is not None and str(key) not in shown_relations:
            continue
        label = _relation_label(relation, key)
        kind = model._text(relation.get('kind'))
        what = 'periodic pair' if kind == model.PERIODIC else 'relation'
        if model._text(relation.get('state')) == model.UNRESOLVED:
            from foammesh.db.surface_authoring import remedy_for
            reason = (model._text(relation.get('stateReason'))
                      or 'the relation is unresolved')
            remedy = ('Correct the transform or the sides of the pair.'
                      if kind == model.PERIODIC and 'does not map' in reason
                      else 'Repair the pair: its two sides and its transform.'
                      if kind == model.PERIODIC else remedy_for(reason))
            add(label, reason, remedy, UNRESOLVED, what)
            continue
        if kind != model.PERIODIC:
            continue
        transform = model._text(relation.get('transform'))
        judge(PERIODIC_PREFIX + transform, label, what)
        verification = model._text(relation.get('verification')) or \
            'unverified'
        if verification == 'failed':
            add(label, 'its two sides do not match under its transform: '
                + (model._text(relation.get('verificationReason'))
                   or 'measured on the prepared geometry'),
                'Correct the transform or the sides of the pair.', FAILED,
                what)
        elif verification != 'verified' and stage != 'edit':
            # Plan 49 D7 (SR7 decision): a pair nobody could measure is not
            # coupled on trust; createNonConformalCouples or setPeriodic
            # finding it wrong after meshing is the failure this prevents.
            add(label, 'its transform was not measured on the geometry ('
                + (model._text(relation.get('verificationReason'))
                   or 'no verdict') + ')',
                'Prepare the geometry so the pair can be measured, then run '
                'again.', UNVERIFIED, what)

    by_token = _surface_tokens(geometry_rows)
    for source, pair in model.affine_gmsh_pairs(document):
        tokens = {model._text(pair.get('masterScopeToken')),
                  model._text(pair.get('slaveScopeToken'))} - {''}
        if scope is not None and not tokens & scope:
            continue
        sides = [by_token.get(token) for token in tokens]
        if wanted is not None and not {side for side in sides if side} & \
                wanted:
            continue
        label = model._text(pair.get('name')) or source
        judge(PERIODIC_PREFIX + 'affine', label, 'periodic pair')
        conflict = model.affine_side_conflict(document, source)
        if conflict is not None:
            add(conflict['surface'], conflict['reason'], conflict['remedy'],
                CONFLICT)
    return issues


def blocking(issues: Iterable[IntentIssue]) -> list[IntentIssue]:
    return [issue for issue in issues if issue.blocking]


def export_verdict(db, target, route=None) -> list[dict] | None:
    """The export-time verdict as plain items, for the hand-off sidecar.

    Plan 49 SR6/SR7 (``validation.sharedValidation``). Judged on *route*,
    else the configured engine; ``None`` when there is none to judge for.
    Nothing is measured or written: the export gate measured already.
    """
    document = document_of(db)
    if route is None:
        mesh = route_target_of(document)[0] if document else ''
        route = mesh
    route = normalise_route(route)
    if not document or route not in ROUTES:
        return None
    return [issue.to_dict() for issue in validate(
        document, route, target, stage='export')]


def prepared_mapping(case_path) -> dict | None:
    """The case's current prepared geometry as a mapping, or ``None``."""
    if case_path is None:
        return None
    try:
        from foammesh.core.geometry import PreparedGeometryStore
        current = PreparedGeometryStore(Path(case_path)).current()
    except (ImportError, OSError, ValueError, TypeError):
        return None
    if current is None:
        return None
    if isinstance(current, Mapping):
        return dict(current)
    to_dict = getattr(current, 'to_dict', None)
    try:
        return to_dict() if callable(to_dict) else None
    except (OSError, ValueError, TypeError):
        return None


def measured_document(document, prepared) -> dict:
    """An assessed copy of *document* with its periodic pairs measured.

    Plan 49 SR3/SR7: the gates do not trust a stored verdict. Every periodic
    relation whose verdict is stale or missing is measured on *prepared* (the
    project itself is not written), then the relation states are refreshed.
    """
    copied = assessed_document(document)
    if not copied:
        return copied
    from foammesh.core.geometry.periodic_relations import (
        verify_periodic_relations)
    verify_periodic_relations(copied, prepared)
    model.refresh_surface_intent(copied)
    return copied


#: The stages that build a mesh, and so meet the builders' own refusals.
BUILD_STAGES = ('run', 'dictionaries')

_REFUSAL_KINDS = {'Relation': 'relation', 'Periodic pair': 'periodic pair'}


def _snappy_refusals(db, prepared) -> list:
    """The snappy dictionaries' surface-role refusals, written nowhere."""
    from foammesh.core.geometry import BBox
    from foammesh.openfoam.case_builder import CaseBuilder
    builder = CaseBuilder(db, BBox(0, 1, 0, 1, 0, 1))
    return builder.surface_role_refusals(prepared or None)


def build_issues(db, route, target, *, document=None, prepared=None,
                 dimensionality: str | None = None,
                 known: Iterable[IntentIssue] = ()) -> list[IntentIssue]:
    """DP-6071. What the route's builder would refuse, as blocking issues.

    The capability table judges each role on its own; some refusals are
    made only where the route builds them (Plan 49 D7), and the run met
    them after the pre-flight had passed and the stage report had said
    "not blocking":

    * snappy -- the case builder lowers the roles as it would to write the
      dictionaries (``CaseBuilder.surface_role_refusals``), without writing:
      a zone role sharing its surface file with another role, a conformal
      relation between two separate surfaces, an unbuildable relation;
    * Gmsh -- an internal face or blocking baffle standing on a wall of a
      volume that grows a boundary layer (``core.gmsh.layer_sheets``).

    Each issue carries the builder's own reason and remedy, with the code
    :data:`NOT_BUILDABLE`. One already said by *known* (the same reason, or
    a blocking issue on the same subject) is not said twice. Nothing is
    written; anything that cannot be judged adds nothing.
    """
    route = normalise_route(route)
    target = normalise_target(target, route)
    if (route, target) in ROUTE_REFUSALS:
        return []
    known = list(known)
    found: list[IntentIssue] = []
    if route == ROUTE_SNAPPY:
        try:
            refusals = _snappy_refusals(db, prepared)
        except Exception:  # noqa: BLE001 - the run will say it itself
            refusals = []
        for refusal in refusals:
            found.append(IntentIssue(
                refusal.surface, refusal.reason, refusal.remedy, route,
                target, NOT_BUILDABLE,
                _REFUSAL_KINDS.get(refusal.subject, 'surface')))
    elif route == ROUTE_GMSH:
        from foammesh.core.gmsh import layer_sheets
        if document is None:
            document = document_of(db)
        for sheet in layer_sheets.standing_sheets(
                document, prepared, dimensionality=dimensionality):
            reason, remedy = layer_sheets.refusal_text(sheet['name'],
                                                       sheet['walls'])
            found.append(IntentIssue(sheet['name'], reason, remedy, route,
                                     target, NOT_BUILDABLE))
    said_reasons = {issue.reason.rstrip('. ') for issue in known}
    said_subjects = {issue.subject for issue in known if issue.blocking}
    issues: list[IntentIssue] = []
    for issue in found:
        names = {issue.subject, *issue.subject.split(', ')}
        if (issue.reason.rstrip('. ') in said_reasons
                or names & said_subjects or issue in issues):
            continue
        issues.append(issue)
    return issues


#: An interface whose sides do not name two regions of the mesh.
REGION_SIDES = 'region_sides'


def region_side_issues(document, route, target, *, case_path=None,
                       stage: str = 'run') -> list[IntentIssue]:
    """Region interfaces whose two sides do not name two mesh regions.

    The export couples a region interface by the regions on its sides
    (``handoff`` ``relation_ambiguous``). With three regions or more, a side
    that names no region of the mesh -- a body none of the regions holds, or
    ``surrounding`` beside a region that has more than one interface --
    leaves several pairs it could be, and that was found only after the
    mesh had been built: MEASURED on a hot fluid / pipe / cold fluid stack,
    whose cut interfaces both read ``pipe`` <-> ``surrounding``. This says it
    before the run. With two regions there is one pair and nothing to ask.
    """
    from foammesh.core.geometry.body_regions import body_regions, side_region

    regions = model.mesh_region_names(document)
    relations = [(key, relation) for key, relation in
                 model._sorted_items(document.get('surfaceRelations'))
                 if isinstance(relation, dict)
                 and model._text(relation.get('kind'))
                 in (model.CONFORMAL, model.NON_CONFORMAL)
                 and model._text(relation.get('state')) != model.UNRESOLVED]
    if len(regions) < 2 or not relations:
        return []
    try:
        bodies = body_regions(document, case_path)
    except Exception:  # noqa: BLE001 - judged by name alone
        bodies = {}
    blocking = stage != 'edit'
    resolved = []
    for key, relation in relations:
        sides = []
        for side in ('ownerRegion', 'neighbourRegion'):
            reference = model._text(relation.get(side))
            sides.append((reference, side_region(document, reference,
                                                 bodies)))
        resolved.append((key, relation, sides))
    interfaces_of: dict[str, int] = {}
    for _key, _relation, sides in resolved:
        for _reference, region in sides:
            if region:
                interfaces_of[region] = interfaces_of.get(region, 0) + 1
    remedy = ('Name the region on each side of the interface in the '
              'surface roles tab.')
    issues: list[IntentIssue] = []
    for key, relation, sides in resolved:
        label = _relation_label(relation, key)
        found = [region for _reference, region in sides]
        reason = ''
        if found[0] and found[0] == found[1]:
            reason = f'both of its sides are in the region {found[0]}'
        elif len(regions) > 2:
            for (reference, region), other in zip(sides, reversed(found)):
                if region:
                    continue
                if reference == model.SURROUNDING_REGION:
                    if other and interfaces_of.get(other, 0) > 1:
                        reason = (
                            f'one side is "surrounding", and {other} meets '
                            'more than one region, so the mesh cannot say '
                            'which one it is')
                elif reference:
                    reason = (f'its side {reference} is in none of the '
                              f'mesh regions ({", ".join(regions)})')
                else:
                    reason = 'one of its sides names no region'
                if reason:
                    break
        if reason:
            issue = IntentIssue(label, reason, remedy, route, target,
                                REGION_SIDES, 'relation', blocking)
            if issue not in issues:
                issues.append(issue)
    return issues


def preflight(db, case_path, route, target, *, stage: str, prepared=None,
              geometry: Iterable = (), dimensionality: str | None = None,
              stale: bool = False) -> list[IntentIssue]:
    """Every issue the run (or export) pre-flight judges, blocking or not.

    :func:`gate` raises on the blocking ones; the stage report lists them
    all (DP-6071: one function, so the two cannot disagree). *prepared* is
    the prepared geometry mapping (read from *case_path* when omitted). On
    a building stage the route's builder refusals are added
    (:func:`build_issues`); *stale* adds the issue of a mesh built from
    other intent than the project now holds (export only).
    """
    document = document_of(db)
    if not document:
        return []
    if prepared is None:
        prepared = prepared_mapping(case_path)
    document = measured_document(document, prepared)
    issues = validate(document, route, target, stage=stage, assessed=True,
                      geometry=geometry, dimensionality=dimensionality)
    if (normalise_route(route), normalise_target(target, route))             not in ROUTE_REFUSALS:
        issues.extend(issue for issue in region_side_issues(
            document, route, target, case_path=case_path, stage=stage)
            if issue not in issues)
    if stage in BUILD_STAGES:
        issues.extend(build_issues(
            db, route, target, document=document, prepared=prepared,
            dimensionality=dimensionality, known=issues))
    if stale and case_path is not None:
        found = stale_issue(case_path, document, route, target)
        if found is not None:
            issues.append(found)
    return issues


def gate(db, case_path, route, target, *, stage: str, prepared=None,
         geometry: Iterable = (), dimensionality: str | None = None,
         stale: bool = False) -> list[IntentIssue]:
    """The one check a run (``stage='run'``) or an export (``'export'``)
    makes before it starts. Raises :class:`SurfaceIntentRefused` naming
    every blocking issue; returns the non-blocking ones.

    The issues are :func:`preflight`'s, the same list the stage report
    shows.
    """
    issues = preflight(db, case_path, route, target, stage=stage,
                       prepared=prepared, geometry=geometry,
                       dimensionality=dimensionality, stale=stale)
    refused = blocking(issues)
    if refused:
        raise SurfaceIntentRefused(refused, stage=stage)
    return issues


def check(document, route, target, *, stage: str, **options) -> None:
    """Raise :class:`SurfaceIntentRefused` when anything blocks *stage*."""
    issues = blocking(validate(document, route, target, stage=stage,
                               **options))
    if issues:
        raise SurfaceIntentRefused(issues, stage=stage)


# --------------------------------------------------------------------------- #
# Requested / supported / validated / built / exported
# --------------------------------------------------------------------------- #

BUILT_RECORD = Path('foammesh') / 'surface_intent' / 'built.json'
EXPORTED_RECORD = Path('foammesh') / 'surface_intent' / 'exported.json'
RECORD_VERSION = 1


#: The transform numbers of a relation the digest holds (DP-6055).
DIGEST_NUMBERS = ('translationX', 'translationY', 'translationZ',
                  'rotationAxisX', 'rotationAxisY', 'rotationAxisZ',
                  'rotationCentreX', 'rotationCentreY', 'rotationCentreZ',
                  'rotationAngleDegrees')


def intent_digest(document, *, assessed: bool = False) -> str:
    """What a mesh was built from: roles, purposes, types and relations.

    Names are left out on purpose: a rename keeps the intent (plan 3.1), and
    the patch names a rename changes are the mesh's, not the intent's. So
    are the derived states and the measured verdicts: they follow from the
    intent and the geometry, and are checked again by every gate.
    """
    document = document if assessed else assessed_document(document)
    surfaces = []
    for key, row in model._sorted_items(document.get('geometry')):
        if not model._is_surface(row):
            continue
        surfaces.append([str(key), model._text(row.get('patchUuid')),
                         model._text(row.get('surfaceRole')),
                         model._text(row.get('boundaryCategory')),
                         model._text(row.get('meshPatchType'))])
    relations = []
    for _key, relation in model._sorted_items(
            document.get('surfaceRelations')):
        if not isinstance(relation, dict):
            continue
        row = {field: model._text(relation.get(field)) for field in (
            'relationId', 'kind', 'ownerPatchUuid', 'neighbourPatchUuid',
            'sharedSurface', 'ownerRegion', 'neighbourRegion', 'transform')}
        # DP-6055. Compared as numbers, an unset one as its default: a
        # relation made in this session has no transform numbers at all,
        # and the same relation read back from the saved project has the
        # schema's ('0.0', '1.0'), so a reopened case read as changed.
        for field in DIGEST_NUMBERS:
            row[field] = repr(model._number(
                relation.get(field), model._NUMBER_DEFAULTS.get(field, 0.0)))
        relations.append(row)
    affine = [[source, model._text(pair.get('masterScopeToken')),
               model._text(pair.get('slaveScopeToken'))]
              for source, pair in model.affine_gmsh_pairs(document)]
    encoded = json.dumps({'s': surfaces, 'r': relations, 'a': affine},
                         sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:32]


def _write(path: Path, payload: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True),
                        encoding='utf-8')
    except OSError:
        return False
    return True


def read_record(case_path, which=BUILT_RECORD) -> dict | None:
    try:
        payload = json.loads((Path(case_path) / which).read_text(
            encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_built_record(case_path, document, route, target, *,
                       digest: str = '') -> bool:
    """After a mesh was published: what it was built from.

    *digest* is the :func:`intent_digest` taken when the run started (the
    project may have been edited while it ran); without it, *document*'s.
    """
    return _write(Path(case_path) / BUILT_RECORD, {
        'version': RECORD_VERSION, 'route': normalise_route(route),
        'target': normalise_target(target),
        'digest': digest or intent_digest(document)})


def forget_built_record(case_path) -> None:
    """A run is replacing the mesh: its record no longer describes it."""
    if case_path is None:
        return
    for which in (BUILT_RECORD, EXPORTED_RECORD):
        try:
            (Path(case_path) / which).unlink(missing_ok=True)
        except OSError:
            pass


def write_exported_record(case_path, document, target, destination) -> bool:
    built = read_record(case_path) or {}
    return _write(Path(case_path) / EXPORTED_RECORD, {
        'version': RECORD_VERSION, 'target': normalise_target(target),
        'route': built.get('route', ''), 'digest': intent_digest(document),
        'built_digest': built.get('digest', ''),
        'destination': Path(str(destination)).name})


def built_staleness(case_path, document, route=None, target=None) -> str:
    """Why the case's mesh no longer matches the project's surface intent.

    ``''`` when it does, or when no record exists (a mesh from before SR7:
    nothing is claimed about it either way).
    """
    record = read_record(case_path)
    if not record:
        return ''
    if record.get('digest') != intent_digest(document):
        return ('the surface roles, types or pairs changed after this mesh '
                'was built')
    if route is not None and record.get('route') != normalise_route(route):
        return (f'this mesh was built on the '
                f'{ROUTE_NAMES.get(record.get("route"), record.get("route"))}'
                f' route, and the case is now set to '
                f'{ROUTE_NAMES.get(normalise_route(route), route)}')
    if target is not None and record.get('target') != normalise_target(
            target) and normalise_target(target) != TARGET_NATIVE:
        return (f'this mesh was built for '
                f'{TARGET_NAMES.get(record.get("target"), record.get("target"))}'
                f', and the case is now set to '
                f'{TARGET_NAMES.get(normalise_target(target), target)}')
    return ''


def stale_issue(case_path, document, route, target) -> IntentIssue | None:
    reason = built_staleness(case_path, document, route=route)
    if not reason:
        return None
    return IntentIssue('Mesh', reason, 'Mesh again, then export.',
                       normalise_route(route), normalise_target(target),
                       STALE, 'case')


def _supported(issues) -> bool:
    return not any(issue.code in (UNSUPPORTED, CONDITION) for issue in issues)


def stage_report(document, route, target, *, case_path=None,
                 issues: Iterable[IntentIssue] | None = None) -> list[dict]:
    """One row per surface and relation, with the stages kept apart.

    ``requested`` -- what the project holds (role, type, kind);
    ``supported`` -- the capability table's verdict for this route/target;
    ``validated`` -- the geometric check: ``verified``, ``failed``,
    ``unverified`` (periodic pairs are measured; contacts and interfaces are
    checked when the mesh is built, so they read ``at build``);
    ``built`` / ``exported`` -- ``True`` only when a record of this case says
    a mesh (an export) was made from this very intent on this route for this
    target; ``None`` when nothing was built, ``False`` when the record is
    stale, with ``stale`` saying why.

    *issues* are the pre-flight's (:func:`preflight`, which adds what the
    route's builder would refuse, DP-6071); without them the document is
    judged by :func:`validate` alone. An issue naming several surfaces
    (``a, b``) is shown on each of them.
    """
    route = normalise_route(route)
    target = normalise_target(target, route)
    document = assessed_document(document)
    if issues is None:
        issues = validate(document, route, target, stage='run',
                          assessed=True)
    refused = {}
    for issue in issues:
        names = [issue.subject]
        if ', ' in issue.subject:
            names += [name for name in issue.subject.split(', ') if name]
        for name in dict.fromkeys(names):
            refused.setdefault(name, []).append(issue)
    stale, built, exported = '', None, None
    if case_path is not None:
        record = read_record(case_path)
        if record:
            stale = built_staleness(case_path, document, route, target)
            built = not stale
        export_record = read_record(case_path, EXPORTED_RECORD)
        if export_record:
            exported = (built is True and export_record.get('digest')
                        == intent_digest(document, assessed=True)
                        and export_record.get('target') in (target,
                                                            TARGET_NATIVE))
    rows = []
    for key, row in model._sorted_items(document.get('geometry')):
        if not model._is_surface(row):
            continue
        name = _name(row, key)
        role = model._text(row.get('surfaceRole'))
        found = refused.get(name, [])
        rows.append({
            'subject': name, 'kind': 'surface', 'key': str(key),
            'requested': {'role': role or None,
                          'purpose': model._text(row.get('boundaryCategory'))
                          or None,
                          'mesh_patch_type':
                              model._text(row.get('meshPatchType')) or None,
                          'origin': model._text(row.get('intentOrigin'))
                          or None},
            'supported': _supported(found),
            'validated': ('at build' if role in model.INTERFACE_ROLES
                          else 'not needed'),
            'built': built if not found else None,
            'exported': exported if not found else None,
            'stale': stale, 'issues': [issue.text() for issue in found]})
    for key, relation in model._sorted_items(
            document.get('surfaceRelations')):
        if not isinstance(relation, dict):
            continue
        label = _relation_label(relation, key)
        kind = model._text(relation.get('kind'))
        found = refused.get(label, [])
        validated = (model._text(relation.get('verification')) or 'unverified'
                     ) if kind == model.PERIODIC else 'at build'
        rows.append({
            'subject': label, 'kind': 'relation', 'key': str(key),
            'requested': {'kind': kind,
                          'transform': model._text(relation.get('transform'))
                          or None},
            'supported': _supported(found),
            'validated': validated,
            'built': built if not found else None,
            'exported': exported if not found else None,
            'stale': stale, 'issues': [issue.text() for issue in found]})
    return rows


__all__ = (
    'AXISYMMETRIC', 'CAPABILITIES', 'Capability', 'IntentIssue',
    'ROUTES', 'ROUTE_GMSH', 'ROUTE_SNAPPY', 'SOLID_IMPORT', 'TARGETS',
    'TARGET_NATIVE', 'TARGET_OPENFOAM', 'TARGET_SU2', 'TWO_D',
    'ISSUE_CODES', 'NOT_BUILDABLE',
    'SurfaceIntentRefused', 'blocking', 'build_issues', 'built_staleness',
    'capability', 'capability_table', 'check', 'gate', 'intent_digest',
    'preflight', 'read_record', 'region_side_issues',
    'refusal_text', 'stage_report', 'stale_issue', 'validate',
    'write_built_record', 'write_exported_record')
