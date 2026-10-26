"""Plan 49 SR5. The surface roles and relations, lowered for the Gmsh route.

SR1 stores what each surface is for (``surfaceRole``, ``meshPatchType``,
``boundaryCategory``) and one ``surfaceRelations`` record per coupling
(``foammesh.db.surface_intent``). Before SR5 the Gmsh route read none of it:
the job a prepared surface wrote was the same whatever role the surface had
(DP-5783). This module is the bridge from that store to the job.

* :func:`lowered_surface_intent` reads the project's intent for the prepared
  patches of this revision and returns the ``surfaceIntent`` block the job
  intent carries (so the job digest covers it). A project whose every surface
  is an external boundary typed as its prepared category, with no relation,
  lowers to nothing, and its job stays byte-identical to the one it wrote
  before.
* :func:`relation_interface_pairs` turns the region-interface relations into
  the job's ``interfacePairs`` rows the runner already builds from
  (pair-local fusion, ``stages/geometry.py``), leaving out the legacy rows a
  relation was migrated from so no pair is built twice.
* :func:`relation_periodic_pairs` is DP-5805's adapter: the periodic
  relations as Gmsh periodic pairs.
* :func:`surface_intent_refusal` refuses, before a job is written, a role the
  Gmsh route cannot build for the chosen target.

Nothing here imports Qt or Gmsh: the job is plain data.
"""
from __future__ import annotations

import copy
from typing import Iterable, Mapping

from foammesh.db import surface_intent as si

#: Written into ``intent['surfaceIntent']['version']``.
SURFACE_INTENT_VERSION = 1

#: Gmsh periodic transform names for the relation's (legacy pair) ones.
_PERIODIC_TRANSFORMS = {'translational': 'translation',
                        'translation': 'translation',
                        'rotational': 'rotation', 'rotation': 'rotation'}

_TESSELLATED = ('.stl', '.obj', '.ply')


def _text(value) -> str:
    value = getattr(value, 'value', value)
    return '' if value is None else str(value).strip()


def _float(value, default=0.0) -> float:
    try:
        return float(getattr(value, 'value', value))
    except (TypeError, ValueError):
        return float(default)


def _document(db) -> dict:
    """The project document with every surface row assessed (in memory).

    A saved project is migrated on load; a row created since -- or a test
    database built row by row -- may still carry only ``cfdType`` and the
    switches. The migration is run on a copy so the job reads the same
    assessment the load would make, and the project is not touched.
    """
    content = getattr(db, 'data', None)
    if not callable(content):
        return {}
    try:
        document = copy.deepcopy(content() or {})
    except Exception:  # noqa: BLE001 - a db with no document has no intent
        return {}
    if not isinstance(document, dict):
        return {}
    si.migrate_surface_intent(document, [])
    return document


def _groups(prepared_geometry) -> list[dict]:
    from foammesh.core.gmsh.execution import _group_manifest
    if prepared_geometry is None:
        return []
    return [group for group in
            _group_manifest(prepared_geometry).get('groups') or ()
            if isinstance(group, dict)
            and _text(group.get('patch_uuid'))]


def _entries(groups) -> list[dict]:
    return [{'geometry_id': _text(group.get('geometry_id')),
             'patches': [{'patch_uuid': _text(group.get('patch_uuid')),
                          'name': _text(group.get('display_name')
                                        or group.get('name'))}]}
            for group in groups]


def _solver_name(group) -> str:
    return _text(group.get('solver_name') or group.get('name')
                 or group.get('display_name'))


def _transform(relation) -> dict:
    return {
        'transform': _text(relation.get('transform')) or 'coincident',
        'matchTolerance': _float(relation.get('matchTolerance'), 1e-6)
        or 1e-6,
        'translation': [_float(relation.get(f'translation{axis}'))
                        for axis in 'XYZ'],
        'rotationCentre': [_float(relation.get(f'rotationCentre{axis}'))
                           for axis in 'XYZ'],
        'rotationAxis': [_float(relation.get(f'rotationAxis{axis}'),
                                1.0 if axis == 'Z' else 0.0)
                         for axis in 'XYZ'],
        'rotationAngleDegrees': _float(
            relation.get('rotationAngleDegrees')),
    }


def _relation_entries(document, names, tokens=None) -> list[dict]:
    """The relations naming a patch in *tokens* (every relation if None)."""
    relations = []
    for key, relation in sorted(
            (document.get('surfaceRelations') or {}).items(),
            key=lambda item: str(item[0])):
        if not isinstance(relation, dict):
            continue
        owner = _text(relation.get('ownerPatchUuid'))
        neighbour = _text(relation.get('neighbourPatchUuid'))
        if tokens is not None and owner not in tokens \
                and neighbour not in tokens:
            continue
        kind = _text(relation.get('kind'))
        entry = {
            'relationId': _text(relation.get('relationId')) or str(key),
            'name': _text(relation.get('name')) or str(key),
            'kind': kind,
            'ownerScope': owner,
            'neighbourScope': neighbour,
            'ownerName': names.get(owner, _text(relation.get('ownerLabel'))),
            'neighbourName': names.get(neighbour,
                                       _text(relation.get('neighbourLabel'))),
            'shared': bool(getattr(relation.get('sharedSurface'), 'value',
                                   relation.get('sharedSurface'))),
            'ownerRegion': _text(relation.get('ownerRegion')),
            'neighbourRegion': _text(relation.get('neighbourRegion')),
            'state': _text(relation.get('state')) or si.VALID,
            'stateReason': _text(relation.get('stateReason')),
            'legacySource': _text(relation.get('legacySource')),
        }
        if kind == si.PERIODIC:
            entry.update(_transform(relation))
        relations.append(entry)
    return relations


def project_relations(db) -> dict:
    """Every relation of the project, as a ``surfaceIntent``-shaped block.

    DP-5805. The periodic adapter needs no prepared revision: a relation
    names its two patches by the prepared patch uuid already.
    """
    document = _document(db)
    if not document:
        return {}
    relations = _relation_entries(document, {})
    return {'relations': relations} if relations else {}


def lowered_surface_intent(db, prepared_geometry) -> dict:
    """The ``surfaceIntent`` block for this revision's job, or ``{}``.

    ``surfaces`` lists every prepared patch whose role is not a plain
    external boundary typed as its prepared category (or that is
    unresolved); ``relations`` lists every relation naming a patch of this
    revision. Each entry names its patch by the scope token (the prepared
    patch uuid) the runner resolves to Gmsh tags, and by the solver patch
    name it publishes under.
    """
    groups = _groups(prepared_geometry)
    if not groups:
        return {}
    document = _document(db)
    if not document:
        return {}
    intent = si.prepared_surface_intent(document, _entries(groups))
    by_token = {_text(group.get('patch_uuid')): group for group in groups}
    surfaces = []
    for token, group in by_token.items():
        found = intent.get(token)
        if not found:
            continue
        role = _text(found.get('role'))
        state = _text(found.get('state')) or si.VALID
        mesh_type = _text(found.get('mesh_patch_type'))
        category = _text(group.get('category')) or 'wall'
        # An external boundary nobody typed (migrated or inferred) is typed
        # by its prepared category exactly as before; one the user typed is
        # lowered only when the type differs from what that category writes.
        plain = (role == si.EXTERNAL and state != si.UNRESOLVED
                 and (_text(found.get('origin')) != si.AUTHORED
                      or not mesh_type
                      or mesh_type == si.legacy_patch_type(category)))
        if plain:
            continue
        surfaces.append({
            'scope': token,
            'name': _solver_name(group),
            'role': role or si.EXTERNAL,
            'meshPatchType': mesh_type,
            'boundaryCategory': _text(found.get('boundary_category')),
            'preparedCategory': category,
            'state': state,
            'issue': _text(found.get('issue')),
            'origin': _text(found.get('origin')),
        })
    names = {token: _solver_name(group) for token, group in by_token.items()}
    relations = _relation_entries(document, names, set(by_token))
    if not surfaces and not relations:
        return {}
    return {'version': SURFACE_INTENT_VERSION, 'surfaces': surfaces,
            'relations': relations}


def _valid_pair_relations(block, kind) -> list[dict]:
    return [relation for relation in (block or {}).get('relations') or ()
            if relation.get('kind') == kind
            and relation.get('state') == si.VALID
            and not relation.get('shared')
            and relation.get('ownerScope') and relation.get('neighbourScope')]


def _legacy_key(relation) -> str:
    source = _text(relation.get('legacySource'))
    prefix = 'interfacePairs/'
    return source[len(prefix):] if source.startswith(prefix) else ''


def relation_interface_pairs(block, legacy_rows: Iterable[Mapping]) -> list:
    """The job's ``interfacePairs``: the legacy rows plus the relations.

    A region-interface relation (conformal or non-conformal, two surfaces,
    valid) becomes a coincident pair row the runner fuses or keeps apart.
    A legacy row the relation was migrated from is left out, so one coupling
    is one pair. A relation that is shared (one surface between two regions)
    needs no pair: the surface already lies between the two volumes.
    """
    legacy_rows = [dict(row) for row in legacy_rows or ()]
    rows_by_side = {}
    for row in legacy_rows:
        rows_by_side.setdefault(frozenset((
            _text(row.get('masterScope')), _text(row.get('slaveScope')))),
            []).append(row)
    extra = []
    replaced: set[int] = set()
    for kind, coupling in ((si.CONFORMAL, 'conformal'),
                           (si.NON_CONFORMAL, 'non_conformal')):
        for relation in _valid_pair_relations(block, kind):
            sides = frozenset((relation['ownerScope'],
                               relation['neighbourScope']))
            legacy = _legacy_key(relation)
            for row in rows_by_side.get(sides, ()):
                if (not legacy or legacy in (_text(row.get('pairId')),
                                             _text(row.get('name')))
                        or _text(row.get('name')) == relation['name']):
                    replaced.add(id(row))
            extra.append({
                'pairId': relation['relationId'],
                'name': relation['name'],
                'coupling': coupling,
                'transform': 'coincident',
                'masterScope': relation['ownerScope'],
                'slaveScope': relation['neighbourScope'],
                'matchTolerance': 1e-6,
                'relationId': relation['relationId'],
                'origin': 'surfaceRelations',
            })
    kept = [row for row in legacy_rows if id(row) not in replaced]
    return kept + extra


#: The Gmsh periodic transforms a periodic relation lowers to.
_LOWERED_PERIODIC = ('translation', 'rotation')


def _prepared_mapping(prepared):
    if prepared is None or isinstance(prepared, dict):
        return prepared
    to_dict = getattr(prepared, 'to_dict', None)
    try:
        return to_dict() if callable(to_dict) else None
    except (OSError, ValueError, TypeError):
        return None


def periodic_relation_rows(db, prepared=None) -> tuple[list, list]:
    """DP-5805. ``(lowered, refused)``: the periodic relations, for Gmsh.

    The relation store is the one source (SR3: every translation or rotation
    pair, edited on the Gmsh Periodic page or as a cyclic pair on the
    Geometry page, is a periodic relation). ``surface_intent.periodic_pairs``
    gives each in the Gmsh pair's shape. With *prepared*, the relations whose
    measurement is stale or missing are measured again first (the document is
    a copy: the project is not written). A ``valid`` relation with a
    translation or rotation is lowered; every other one is refused, by name,
    with its reason -- never dropped.
    """
    document = _document(db)
    if not document:
        return [], []
    prepared = _prepared_mapping(prepared)
    if prepared is not None:
        from foammesh.core.geometry import periodic_relations
        periodic_relations.verify_periodic_relations(document, prepared)
        si.refresh_surface_intent(document)
    lowered, refused = [], []
    for row in si.periodic_pairs(document):
        name = row.get('name') or row.get('relationId') or 'unnamed'
        if row.get('state') != si.VALID:
            reason = row.get('stateReason') or row.get(
                'verificationReason') or 'its relation is not resolved'
            refused.append(f'the periodic pair {name!r} is not meshed: '
                           f'{reason}')
            continue
        if row.get('transform') not in _LOWERED_PERIODIC:
            refused.append(
                f'the periodic pair {name!r} is not meshed: it has no '
                'translation or rotation to map one side onto the other. '
                'Give the pair its transform.')
            continue
        lowered.append(row)
    return lowered, refused


def affine_periodic_refusal(section, *, publishes_poly_mesh: bool,
                            writes_su2: bool) -> str:
    """DP-5832 (the Gmsh half). Why an affine Gmsh pair cannot be built here.

    An affine pair (Plan 41 GN11) is a native Gmsh control: ``setPeriodic``
    takes the matrix and the ``.msh`` keeps it. An OpenFOAM cyclic patch
    takes only a translation or a rotation, and SU2's periodic markers the
    same, so a run that publishes a polyMesh or writes SU2 is refused with
    the pair's name; a native Gmsh output keeps it.
    """
    if not (publishes_poly_mesh or writes_su2):
        return ''
    pairs = (section or {}).get('periodicPairs') or {}
    rows = pairs.values() if isinstance(pairs, dict) else pairs
    names = [str(row.get('name') or 'unnamed') for row in rows
             if isinstance(row, dict) and row.get('enabled', True)
             and str(row.get('transform') or '') == 'affine']
    if not names:
        return ''
    target = ' and '.join(item for item, wanted in (
        ('an OpenFOAM polyMesh', publishes_poly_mesh),
        ('an SU2 mesh', writes_su2)) if wanted)
    return (f'the periodic pair{"s" if len(names) > 1 else ""} '
            + ', '.join(repr(name) for name in names)
            + f' {"are" if len(names) > 1 else "is"} affine, and this run '
            f'writes {target}, whose periodic patches take only a translation '
            'or a rotation. Give the pair a translation or a rotation, or '
            'export only the native Gmsh mesh.')


def surface_intent_refusal(block, *, target_solver, geometry=(),
                           dimensionality_mode='three_d') -> str:
    """Why the Gmsh route cannot build these roles for this target, or ''.

    * ``empty`` is a one-cell-thick extrusion's front and back, and ``wedge``
      a revolved sector's: offered only with that construction (the section
      route), refused on a three-dimensional job.
    * SU2's native file is single-zone (SR0 probe 6, DP-5765): a region
      interface, conformal or not, loses the volume identity it is between,
      and a non-conformal pair has nothing to couple it. Refused rather than
      exported as plain markers.
    * A non-conformal relation on a tessellated import has no two CAD faces
      to keep apart (the pair check in ``check_job`` says the same for the
      legacy rows).
    """
    if not block:
        return ''
    reasons = []
    mode = str(dimensionality_mode or 'three_d')
    for surface in block.get('surfaces') or ():
        mesh_type = surface.get('meshPatchType')
        name = surface.get('name') or surface.get('scope')
        if mesh_type == 'empty' and mode != 'two_d':
            reasons.append(
                f'{name} is typed empty, and an empty patch is the front or '
                'back of a mesh one cell thick: this job meshes '
                + ('an axisymmetric wedge' if mode == 'axisymmetric'
                   else 'a three-dimensional volume')
                + '. Choose the two-dimensional section (Dimension) or give '
                  'the surface another type.')
        if mesh_type == 'wedge' and mode != 'axisymmetric':
            reasons.append(
                f'{name} is typed wedge, and a wedge patch is a side of a '
                'revolved sector one cell thick: this job meshes '
                + ('a planar section' if mode == 'two_d'
                   else 'a three-dimensional volume')
                + '. Choose the axisymmetric section (Dimension) or give '
                  'the surface another type.')
    target = _text(target_solver).lower()
    if target == 'su2':
        coupled = [relation for relation in block.get('relations') or ()
                   if relation.get('kind') in si.INTERFACE_ROLES
                   and relation.get('state') == si.VALID]
        if coupled:
            reasons.append(
                'SU2 is written by Gmsh as one zone, so the region '
                'interface' + ('s ' if len(coupled) > 1 else ' ')
                + ', '.join(repr(relation['name']) for relation in coupled)
                + ' would reach it as plain markers with the volumes on '
                  'either side merged and no coupling (DP-5765). Export '
                  'OpenFOAM for a multi-region case, or make the surfaces '
                  'external boundaries.')
        # MEASURED (SR5 live probe): Gmsh writes an embedded sheet's group to
        # the .su2 file as a marker, a boundary marker inside the domain.
        internal = [surface.get('name') or surface.get('scope')
                    for surface in block.get('surfaces') or ()
                    if surface.get('role') == si.INTERNAL
                    and surface.get('state', si.VALID) == si.VALID]
        if internal:
            reasons.append(
                ', '.join(repr(name) for name in internal)
                + (' are internal faces' if len(internal) > 1
                   else ' is an internal face')
                + ', and SU2 has no face zone: Gmsh would write it as a '
                  'boundary marker inside the domain. Export OpenFOAM to '
                  'keep it as a face zone, or make it a blocking baffle or '
                  'an external boundary.')
    tessellated = [str(item) for item in geometry or ()
                   if str(item).lower().endswith(_TESSELLATED)]
    if tessellated and _valid_pair_relations(block, si.NON_CONFORMAL):
        reasons.append(
            'a non-conformal region interface is met on the Gmsh route by '
            'meshing its two solids apart, and a surface-mesh import has no '
            'solids to keep apart. Make the interface conformal, or import '
            'the solid model.')
    return ' '.join(reasons)


def unresolved_warnings(block) -> list[str]:
    """One warning per unresolved surface or relation the job carries.

    Blocking a run on an unresolved role is SR7's (plan D7); until then the
    Gmsh route meshes such a surface as the external boundary it published
    as before, and says so.
    """
    warnings = []
    for surface in (block or {}).get('surfaces') or ():
        if surface.get('state') == si.UNRESOLVED:
            warnings.append(
                f'surface {surface.get("name")!r} has an unresolved role'
                + (f' ({surface["issue"]})' if surface.get('issue') else '')
                + '; it is meshed as an external boundary until it is '
                  'repaired')
    for relation in (block or {}).get('relations') or ():
        if relation.get('state') == si.UNRESOLVED:
            warnings.append(
                f'relation {relation.get("name")!r} is unresolved'
                + (f' ({relation["stateReason"]})'
                   if relation.get('stateReason') else '')
                + ' and is not built')
    return warnings


__all__ = ('SURFACE_INTENT_VERSION', 'lowered_surface_intent',
           'project_relations',
           'relation_interface_pairs', 'periodic_relation_rows',
           'affine_periodic_refusal',
           'surface_intent_refusal', 'unresolved_warnings')
