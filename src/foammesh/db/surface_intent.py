"""Plan 49 SR1. What each surface is for, and the relations between surfaces.

Four separate things used to share three overlapping stores. A surface row's
``cfdType`` said both "this is a boundary" and, through the volume it hangs
off, "this carves a cell zone"; its ``nonConformal`` and ``interRegion``
switches said something about coupling that neither engine built; and the
Geometry page's ``interfacePairs`` said it again, by prepared patch uuid, with
a coupling of its own. Purpose (inlet, wall) was not stored at all: it was
read out of the surface's *name* every time, so a rename could turn a wall
into an outlet.

The authoring model kept here separates them (plan section 3):

* ``surfaceRole`` on a surface row -- the topology: external boundary,
  internal face, conformal or non-conformal region interface, blocking
  baffle;
* ``boundaryCategory`` on a surface row -- the purpose, a hint for the solver
  set-up (wall, inlet, outlet, far field, unclassified and the legacy values
  migration needs). It is not a mesh type and not a field boundary condition;
* ``meshPatchType`` on a surface row -- the free mesh patch types (``patch``,
  ``wall``, ``symmetry``, ``symmetryPlane``, ``empty``, ``wedge``). Coupled
  types are never free: they are generated from a relation;
* one ``surfaceRelations`` record per region interface or periodic pair --
  the single coupling store. A surface's controls edit its relation; the
  surface itself holds no second copy of the coupling.

Validation state lives in the model (plan D7): a surface or relation that
cannot be built is saved as ``unresolved`` with a reason, never dropped.
Blocking a run or an export on that state is SR7's job, not this module's.

Until SR4/SR5 switch the writers over, both meshers still read the legacy
``cfdType``/``nonConformal``/``interRegion`` fields and ``interfacePairs``.
Migration therefore never rewrites or removes those: they stay exactly as
saved, which is also what keeps them recoverable.

Everything here works on the plain document (``db.data()`` or the YAML a
project file holds), so the load migration, the commit hook and the tests use
one implementation.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Iterable, Mapping

#: Bumped when the meaning of the stored fields changes. Stored in
#: ``surfaceIntent/modelVersion`` so a later reader can tell.
MODEL_VERSION = 1

#: The region on the other side of a surface that carves a zone, as the mesh
#: finds it. A split interface records only the body it encloses, not the
#: body around it, so the neighbour is known to exist but not by name until
#: the mesh is split (``splitMeshRegions``) -- which is exactly what the
#: legacy writers relied on.
SURROUNDING_REGION = 'surrounding'
_REGION_PREFIX = 'geometry:'
_MESH_REGION_PREFIX = 'region:'

EXTERNAL = 'externalBoundary'
INTERNAL = 'internalFace'
CONFORMAL = 'regionInterfaceConformal'
NON_CONFORMAL = 'regionInterfaceNonConformal'
BAFFLE = 'blockingBaffle'
PERIODIC = 'periodic'

ROLES = (EXTERNAL, INTERNAL, CONFORMAL, NON_CONFORMAL, BAFFLE)
RELATION_KINDS = (CONFORMAL, NON_CONFORMAL, PERIODIC)
INTERFACE_ROLES = (CONFORMAL, NON_CONFORMAL)
FREE_PATCH_TYPES = ('patch', 'wall', 'symmetry', 'symmetryPlane', 'empty',
                    'wedge')

VALID = 'valid'
UNRESOLVED = 'unresolved'
AUTHORED = 'authored'
MIGRATED = 'migrated'
INFERRED = 'inferred'

#: The legacy pair coupling each relation kind is converted from.
_KIND_FOR_COUPLING = {'conformal': CONFORMAL, 'non_conformal': NON_CONFORMAL,
                      'cyclic': PERIODIC}

#: The categories the name inference knows. Mirrors
#: ``configurations_schema.BoundaryCategory``; kept as a literal so this module
#: imports nothing heavier than the standard library.
_CATEGORIES = ('wall', 'inlet', 'outlet', 'symmetry', 'wedge', 'far_field',
               'interface', 'unclassified')

_TRANSFORM_FIELDS = (
    'transform', 'matchTolerance', 'translationX', 'translationY',
    'translationZ', 'rotationCentreX', 'rotationCentreY', 'rotationCentreZ',
    'rotationAxisX', 'rotationAxisY', 'rotationAxisZ', 'rotationAngleDegrees')


# --------------------------------------------------------------------------- #
# Small readers
# --------------------------------------------------------------------------- #

def _value(item, default=None):
    """An enum member, a raw string or ``None``, read as its stored string."""
    if item is None:
        return default
    return getattr(item, 'value', item)


def _text(item) -> str:
    value = _value(item, '')
    return '' if value is None else str(value).strip()


def _flag(item) -> bool:
    value = _value(item, False)
    if isinstance(value, str):
        return value.strip().lower() in ('true', 'yes', 'on', '1')
    return bool(value)


def _key_order(key):
    try:
        return 0, int(key), ''
    except (TypeError, ValueError):
        return 1, 0, str(key)


def _sorted_items(mapping) -> list:
    if not isinstance(mapping, dict):
        return []
    return sorted(mapping.items(), key=lambda item: _key_order(item[0]))


def _is_surface(row) -> bool:
    return isinstance(row, dict) and _text(row.get('gType')) == 'surface'


def region_ref(key) -> str:
    """The stable id of the region a geometry row's cell zone stands for."""
    return f'{_REGION_PREFIX}{key}'


def region_key(reference) -> str | None:
    text = _text(reference)
    if text.startswith(_REGION_PREFIX):
        return text[len(_REGION_PREFIX):]
    return None


def mesh_region_ref(name) -> str:
    """The stable id of a region of the mesh itself: a seeded region on
    snappy, a typed solid on Gmsh (DP-6062). Named, because that name is
    what the export splits the mesh into."""
    return f'{_MESH_REGION_PREFIX}{name}'


def region_name(reference) -> str | None:
    """The region name a :func:`mesh_region_ref` holds, else ``None``."""
    text = _text(reference)
    if text.startswith(_MESH_REGION_PREFIX):
        return text[len(_MESH_REGION_PREFIX):] or None
    return None


def mesh_region_names(document) -> list[str]:
    """The regions the export will split this case into, by name.

    Gmsh: the typed, included solids (``export_regions.solid_regions``);
    every other route: the ``region`` table. Read from the plain document,
    so the roles editor offers what the export makes.
    """
    document = document if isinstance(document, dict) else {}
    engine = _text((document.get('mesh') or {}).get('engine')).lower()
    names: list[str] = []
    if engine == 'gmsh':
        from foammesh.core.mesh.cad_solids import typing_of

        rows = (document.get('gmsh') or {}).get('volumeControls') or {}
        for item in typing_of(rows if isinstance(rows, dict) else {}).values():
            name = str(item.get('name') or '').strip()
            if item.get('type') and item.get('included') and name:
                names.append(name)
    else:
        for _key, row in _sorted_items(document.get('region') or {}):
            name = _text(row.get('name')) if isinstance(row, dict) else ''
            if name:
                names.append(name)
    return list(dict.fromkeys(names))


def new_relation_id() -> str:
    return f'rel-{uuid.uuid4().hex}'


# --------------------------------------------------------------------------- #
# Legacy readings, kept here so migration and the meshers agree
# --------------------------------------------------------------------------- #

def category_for_name(name: str, default: str) -> str:
    """The category a surface name implied before purpose was stored.

    The same reading ``domain_operations._category_for_name`` applies at
    preparation (R74): the whole name, then its leading two words, then its
    leading word, and otherwise the project's configured default.
    """
    name = str(name or '').strip().lower().replace(' ', '_')
    known = set(_CATEGORIES)
    if name in known:
        return name
    tokens = [token for token in re.split(r'[^a-z0-9]+', name) if token]
    for size in (2, 1):
        head = '_'.join(tokens[:size])
        if head and head in known:
            return head
    return default


def legacy_patch_type(category: str) -> str:
    """The mesh patch type the writers published for a legacy category.

    ``core/export/poly_mesh_writer._foam_patch_type``: ``wall`` is a wall,
    ``symmetry`` was published as ``symmetryPlane``, ``wedge`` as ``wedge``,
    and everything else -- inlet, outlet, far field, unclassified -- as a
    plain ``patch``.
    """
    return {'wall': 'wall', 'symmetry': 'symmetryPlane',
            'wedge': 'wedge'}.get(str(category or '').lower(), 'patch')


def default_category(document) -> str:
    preparation = document.get('geometryPreparation') \
        if isinstance(document, dict) else None
    value = _text((preparation or {}).get('defaultBoundaryCategory')) \
        if isinstance(preparation, dict) else ''
    value = value.split('.')[-1].lower()
    # The schema default is ``wall``; a document that holds none ran on it.
    return value if value in _CATEGORIES else 'wall'


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

def _legacy(row) -> dict:
    return {'cfdType': _text(row.get('cfdType')) or None,
            'nonConformal': _flag(row.get('nonConformal')),
            'interRegion': _flag(row.get('interRegion')),
            'volume': None if row.get('volume') in (None, '')
            else str(row.get('volume'))}


def _pair_legacy(pair) -> dict:
    snapshot = {'name': _text(pair.get('name')),
                'enabled': _flag(pair.get('enabled', True)),
                'coupling': _text(pair.get('coupling')) or 'conformal',
                'masterScopeToken': _text(pair.get('masterScopeToken')),
                'slaveScopeToken': _text(pair.get('slaveScopeToken'))}
    for field in _TRANSFORM_FIELDS:
        if field in pair:
            snapshot[field] = _value(pair.get(field))
    return snapshot


def _zone_region(geometry, row) -> str | None:
    """The region the cell zone this surface bounds stands for, if any."""
    volume = row.get('volume')
    if volume in (None, ''):
        return None
    owner = geometry.get(str(volume))
    if owner is None:
        owner = geometry.get(volume)
    if isinstance(owner, dict) and _text(owner.get('cfdType')) == 'cellZone':
        return region_ref(volume)
    return None


def _carved(geometry, volume) -> bool:
    """Does a surface typed ``interface`` hang off this volume (DP-421)?"""
    return any(
        _is_surface(row) and _text(row.get('cfdType')) == 'interface'
        and str(row.get('volume')) == str(volume)
        for row in geometry.values())


def _seeded(geometry, volume) -> bool:
    owner = geometry.get(str(volume))
    return isinstance(owner, dict) and _flag(owner.get('zoneInsidePointSet'))


def _item(kind, target, surface, reason, *, legacy=None, result=None,
          remedy='') -> dict:
    entry = {'kind': kind, 'target': target, 'surface': surface,
             'reason': reason}
    if remedy:
        entry['remedy'] = remedy
    if legacy is not None:
        entry['legacy'] = legacy
    if result is not None:
        entry['result'] = result
    return entry


def _next_key(mapping) -> str:
    keys = [int(key) for key in mapping if str(key).isdigit()]
    return str(max(keys) + 1 if keys else 1)


def _relation(kind, *, owner_key=None, owner_row=None, neighbour_key=None,
              neighbour_row=None, shared=False, owner_region='',
              neighbour_region='', name='', legacy_source='',
              owner_uuid='', neighbour_uuid='', owner_label='',
              neighbour_label='', transform=None) -> dict:
    def label(row, fallback):
        return _text(row.get('name')) if isinstance(row, dict) else fallback

    def patch(row, fallback):
        return (_text(row.get('patchUuid')) or fallback) \
            if isinstance(row, dict) else fallback

    record = {
        'relationId': new_relation_id(),
        'name': name,
        'kind': kind,
        'ownerSurface': None if owner_key is None else str(owner_key),
        'ownerPatchUuid': patch(owner_row, owner_uuid),
        'ownerLabel': label(owner_row, owner_label),
        'neighbourSurface': None if neighbour_key is None
        else str(neighbour_key),
        'neighbourPatchUuid': patch(neighbour_row, neighbour_uuid),
        'neighbourLabel': label(neighbour_row, neighbour_label),
        'sharedSurface': bool(shared),
        'ownerRegion': owner_region or '',
        'neighbourRegion': neighbour_region or '',
        'transform': 'coincident',
        'state': VALID,
        'stateReason': '',
        'verification': 'unverified',
        'origin': MIGRATED,
        'legacySource': legacy_source,
    }
    for field, value in (transform or {}).items():
        record[field] = value
    return record


_HAND_TICKED = (
    'An interface ticked by hand. Before this release it was written as a '
    'cyclic pair, which is not an impermeable wall, a region interface or a '
    'periodic connection. It is not converted to any of them.')
_HAND_TICKED_REMEDY = (
    'Choose what this surface is: a blocking baffle, a region interface '
    '(with its two regions) or one side of a periodic pair.')
_SWITCH_ONLY = (
    'An interface with the {switches} switch on. The switch never built a '
    'coupling on either mesher, so what it was meant to be cannot be read '
    'from it.')
_SWITCH_REMEDY = (
    'Choose a conformal or non-conformal region interface and its two '
    'regions, or another role.')


def migrate_surface_intent(document, notes=None) -> list[dict]:
    """Convert a saved project's legacy surface intent, in place.

    Reads ``cfdType``, both switches, the volume a surface bounds (the split
    interface's zone and seed), the name-implied category and the
    ``interfacePairs`` together, and writes ``surfaceRole``,
    ``boundaryCategory``, ``meshPatchType`` and ``surfaceRelations``. Only
    unambiguous cases are converted; everything else becomes an unresolved
    repair item that names the reason and the remedy. The legacy fields are
    left exactly as saved, so the meshers' output does not change and nothing
    is lost.

    Idempotent: a surface already assessed (role set, or marked unresolved)
    and a pair already converted (a relation names it as its
    ``legacySource``) are left alone, so opening a project twice converts
    nothing twice. Returns the items this run produced, and appends them to
    the persistent report in ``surfaceIntent/conversionReport``.
    """
    if not isinstance(document, dict):
        return []
    geometry = document.get('geometry')
    if not isinstance(geometry, dict):
        return []
    relations = document.get('surfaceRelations')
    if not isinstance(relations, dict):
        relations = document['surfaceRelations'] = {}
    default = default_category(document)
    items: list[dict] = []

    surfaces = {str(key): row for key, row in _sorted_items(geometry)
                if _is_surface(row)}
    pending = {key: row for key, row in surfaces.items()
               if _text(row.get('surfaceRole')) == ''
               and _text(row.get('intentState')) == ''}
    converted = {_text(item.get('legacySource'))
                 for item in relations.values() if isinstance(item, dict)}
    # DP-5815. A disabled pair gets no relation, so the set above never
    # names it; what the report already kept is not reported again.
    # A pair switched back on is converted as usual.
    kept = {_text(item.get('target'))
            for item in conversion_report(document)
            if isinstance(item, dict) and item.get('kind') == 'kept'}

    by_uuid: dict[str, list[str]] = {}
    for key, row in surfaces.items():
        token = _text(row.get('patchUuid'))
        if token:
            by_uuid.setdefault(token, []).append(key)

    pairs = []
    membership: dict[str, int] = {}
    for key, pair in _sorted_items(document.get('interfacePairs')):
        if not isinstance(pair, dict):
            continue
        source = f'interfacePairs/{key}'
        if source in converted:
            continue
        if not _flag(pair.get('enabled', True)):
            if source in kept:
                continue
            items.append(_item(
                'kept', source, _text(pair.get('name')),
                'A disabled interface pair. Neither mesher used it; it is '
                'kept as saved and not converted.',
                legacy=_pair_legacy(pair)))
            continue
        sides = []
        for token in (_text(pair.get('masterScopeToken')),
                      _text(pair.get('slaveScopeToken'))):
            keys = by_uuid.get(token, [])
            sides.append((token, keys))
            for found in keys:
                membership[found] = membership.get(found, 0) + 1
        pairs.append((key, pair, source, sides))

    def assess(key, row, role, *, category=None, patch_type=None,
               origin=MIGRATED):
        row['surfaceRole'] = role
        row['boundaryCategory'] = category
        row['meshPatchType'] = patch_type
        row['intentOrigin'] = origin
        row['intentState'] = VALID
        row['intentIssue'] = ''

    def unresolved(key, row, reason, remedy):
        row['surfaceRole'] = None
        row['boundaryCategory'] = None
        row['meshPatchType'] = None
        row['intentOrigin'] = MIGRATED
        row['intentState'] = UNRESOLVED
        row['intentIssue'] = f'{reason} {remedy}'.strip()
        items.append(_item('unresolved', f'geometry/{key}',
                           _text(row.get('name')), reason, remedy=remedy,
                           legacy=_legacy(row)))

    def zone_relation(key, row, owner_region):
        relation = _relation(
            CONFORMAL, owner_key=key, owner_row=row, neighbour_key=key,
            neighbour_row=row, shared=True, owner_region=owner_region,
            neighbour_region=SURROUNDING_REGION,
            name=_text(row.get('name')), legacy_source=f'geometry/{key}')
        relations[_next_key(relations)] = relation
        return relation

    for key, row in pending.items():
        legacy = _legacy(row)
        cfd = legacy['cfdType'] or 'boundary'
        name = _text(row.get('name'))
        switches = [label for flag, label in (
            (legacy['nonConformal'], 'non-conformal'),
            (legacy['interRegion'], 'inter-region')) if flag]
        paired = membership.get(key, 0) > 0
        if cfd == 'interface':
            if paired:
                unresolved(key, row,
                           'An interface surface is also a side of an '
                           'interface pair; the two say different things.',
                           'Keep one: the pair, or this surface\'s own role.')
                continue
            if switches:
                unresolved(key, row, _SWITCH_ONLY.format(
                    switches=' and '.join(switches)), _SWITCH_REMEDY)
                continue
            owner_region = _zone_region(geometry, row)
            volume = row.get('volume')
            if owner_region and _seeded(geometry, volume):
                # DP-421. The shape Split interfaces writes, and what the
                # writers build from it: the face zone that carves the cell
                # zone of the body it encloses, seeded by that body's point.
                assess(key, row, CONFORMAL)
                relation = zone_relation(key, row, owner_region)
                items.append(_item(
                    'converted', f'geometry/{key}', name,
                    'A split interface carving the zone of the body it '
                    'encloses becomes a conformal region interface. The '
                    'region on its other side is the one the mesh finds '
                    'there; confirm it once the regions are split.',
                    legacy=legacy, result={'surfaceRole': CONFORMAL,
                                           'relationId':
                                               relation['relationId']}))
                continue
            unresolved(key, row, _HAND_TICKED, _HAND_TICKED_REMEDY)
            continue
        if cfd == 'none':
            if paired:
                unresolved(key, row,
                           'An internal face is also a side of an interface '
                           'pair; an internal face has no sides to pair.',
                           'Keep one: the pair, or the internal face.')
                continue
            assess(key, row, INTERNAL)
            items.append(_item(
                'converted', f'geometry/{key}', name,
                'An internal surface (type none) becomes an internal face.',
                legacy=legacy, result={'surfaceRole': INTERNAL}))
        elif cfd in ('boundary', 'cellZone'):
            owner_region = (region_ref(key) if cfd == 'cellZone'
                            else _zone_region(geometry, row))
            carves_zone = owner_region is not None and (
                cfd == 'cellZone'
                or not _carved(geometry, row.get('volume')))
            # An explicit pair beats the zone reading, which is implied by
            # the volume's type: the pair conversion below decides the role.
            if carves_zone and not paired:
                # DP-387. A boundary of a volume typed cell zone was written
                # as that zone's internal face, and a zoned case is split
                # into regions on export: the surface is the conformal
                # interface between the zone and what surrounds it.
                assess(key, row, CONFORMAL)
                relation = zone_relation(key, row, owner_region)
                items.append(_item(
                    'converted', f'geometry/{key}', name,
                    'A surface that bounds a cell zone becomes a conformal '
                    'region interface between that zone and the region '
                    'around it.', legacy=legacy,
                    result={'surfaceRole': CONFORMAL,
                            'relationId': relation['relationId']}))
                continue
            category = category_for_name(name, default)
            patch_type = legacy_patch_type(category)
            assess(key, row, EXTERNAL, category=category,
                   patch_type=patch_type, origin=INFERRED)
            items.append(_item(
                'inferred', f'geometry/{key}', name,
                f'An external boundary. Its purpose "{category}" was read '
                f'from its name or the project default, as before, and is '
                f'now stored so that a rename no longer changes it; its mesh '
                f'type "{patch_type}" is the one the meshers published.',
                legacy=legacy, remedy='Review the purpose.',
                result={'surfaceRole': EXTERNAL, 'boundaryCategory': category,
                        'meshPatchType': patch_type}))
            if switches:
                items.append(_item(
                    'note', f'geometry/{key}', name,
                    f'The {" and ".join(switches)} switch was on a surface '
                    'that is not an interface; no mesher read it there. It '
                    'is kept as saved.', legacy=legacy))
        else:
            unresolved(key, row, f'An unknown surface type "{cfd}".',
                       'Choose the surface\'s role.')

    for key, pair, source, sides in pairs:
        items.extend(_convert_pair(document, geometry, relations, key, pair,
                                   source, sides, membership))

    # Plan 49 SR3 (DP-5831). The Gmsh Periodic page's pairs are periodic
    # relations too; an affine one has no cyclic transform and stays a Gmsh
    # control (DP-5832).
    before = {_text(item.get('legacySource')) for item in relations.values()
              if isinstance(item, dict)}
    sync_periodic_relations(document, origin=MIGRATED)
    for item in relations.values():
        if not isinstance(item, dict):
            continue
        source = _text(item.get('legacySource'))
        if source.startswith(f'{GMSH_PERIODIC}/') and source not in before:
            items.append(_item(
                'converted', source, _text(item.get('name')),
                'A Gmsh periodic pair becomes a periodic relation. Its '
                'geometry is not yet checked.',
                result={'relationId': item['relationId'],
                        'kind': PERIODIC}))
    reported = {entry.get('target') for entry in conversion_report(document)}
    for source, pair in affine_gmsh_pairs(document):
        if source in reported:
            continue
        items.append(_item(
            'kept', source, _text(pair.get('name')),
            'An affine Gmsh periodic pair. An OpenFOAM cyclic patch has no '
            'affine transform, so it is not a periodic relation; it stays a '
            'Gmsh control and is kept as saved.'))

    refresh_surface_intent(document)
    _record(document, items, notes)
    return items


def unassessed_names(document) -> dict:
    """``{row key: name}`` of every surface nobody has assessed yet."""
    geometry = (document or {}).get('geometry')
    if not isinstance(geometry, dict):
        return {}
    return {str(key): _text(row.get('name'))
            for key, row in geometry.items()
            if _is_surface(row) and not _text(row.get('surfaceRole'))
            and not _text(row.get('intentState'))}


def assess_before_rename(document, names_before: Mapping) -> list[str]:
    """Plan 49 SR7 (DP-5788). Pin the purpose of a surface being renamed.

    A surface nobody has assessed has its purpose read from its name when the
    project is opened (``inferred``, flagged for review). Renaming it before
    that would move the purpose with the name -- ``wall_bore`` renamed
    ``inlet_bore`` turning into an inlet. So, in the commit that renames such
    a surface, the project's surfaces are assessed under their names *before*
    the rename, as opening the project would have done, and the new name
    changes nothing else. Returns the renamed row keys.
    """
    geometry = (document or {}).get('geometry')
    if not isinstance(geometry, dict) or not names_before:
        return []
    renamed = {}
    for key, before in names_before.items():
        row = geometry.get(str(key))
        if row is None:
            try:
                row = geometry.get(int(key))
            except (TypeError, ValueError):
                row = None
        if not _is_surface(row) or _text(row.get('surfaceRole')) or \
                _text(row.get('intentState')):
            continue
        if _text(row.get('name')) != _text(before):
            renamed[str(key)] = (row, row.get('name'))
    if not renamed:
        return []
    for key, (row, _after) in renamed.items():
        row['name'] = names_before[key]
    try:
        migrate_surface_intent(document)
    finally:
        for row, after in renamed.values():
            row['name'] = after
    return list(renamed)


def _convert_pair(document, geometry, relations, key, pair, source, sides,
                  membership) -> list[dict]:
    coupling = _text(pair.get('coupling')) or 'conformal'
    kind = _KIND_FOR_COUPLING.get(coupling)
    name = _text(pair.get('name')) or f'pair {key}'
    legacy = _pair_legacy(pair)
    transform = {field: _value(pair.get(field)) for field in _TRANSFORM_FIELDS
                 if field in pair}
    (owner_token, owner_keys), (neighbour_token, neighbour_keys) = sides
    owner_key = owner_keys[0] if len(owner_keys) == 1 else None
    neighbour_key = neighbour_keys[0] if len(neighbour_keys) == 1 else None
    owner_row = geometry.get(owner_key) if owner_key else None
    neighbour_row = geometry.get(neighbour_key) if neighbour_key else None
    owner_region = _zone_region(geometry, owner_row) if owner_row else None
    neighbour_region = (_zone_region(geometry, neighbour_row)
                        if neighbour_row else None)
    relation = _relation(
        kind or NON_CONFORMAL, owner_key=owner_key, owner_row=owner_row,
        neighbour_key=neighbour_key, neighbour_row=neighbour_row,
        owner_region=owner_region or '',
        neighbour_region=neighbour_region or '', name=name,
        legacy_source=source, owner_uuid=owner_token,
        neighbour_uuid=neighbour_token, owner_label=owner_token,
        neighbour_label=neighbour_token, transform=transform)
    if kind is None:
        relation['state'] = UNRESOLVED
        relation['stateReason'] = f'unknown coupling "{coupling}"'
    relations[_next_key(relations)] = relation
    items = []
    # Interface pairs move their sides' roles; a periodic side stays the
    # external boundary it was (plan section 3: periodic sides remain
    # boundaries, the coupling comes from the relation).
    if kind in INTERFACE_ROLES:
        for side_key, row in ((owner_key, owner_row),
                              (neighbour_key, neighbour_row)):
            if (row is None or membership.get(side_key, 0) != 1
                    or _text(row.get('intentOrigin')) == AUTHORED
                    or _text(row.get('surfaceRole')) != EXTERNAL):
                continue
            row['surfaceRole'] = kind
            row['boundaryCategory'] = None
            row['meshPatchType'] = None
            row['intentOrigin'] = MIGRATED
    refresh_surface_intent(document)
    if relation['state'] == VALID:
        items.append(_item(
            'converted', source, name,
            f'An enabled {coupling} interface pair becomes a {kind} '
            'relation. Its geometry is not yet checked.', legacy=legacy,
            result={'relationId': relation['relationId'], 'kind': kind}))
    else:
        items.append(_item(
            'unresolved', source, name,
            f'An enabled {coupling} interface pair could not be converted: '
            f'{relation["stateReason"]}.', legacy=legacy,
            remedy='Repair the relation: its two surfaces, regions and '
                   'transform.',
            result={'relationId': relation['relationId'], 'kind': kind}))
    return items


def _record(document, items, notes) -> None:
    # A run that assessed nothing writes nothing: a project with no surfaces
    # (or none new) must reopen exactly as it was saved.
    if not items:
        return
    section = document.get('surfaceIntent')
    if not isinstance(section, dict):
        section = document['surfaceIntent'] = {}
    section['modelVersion'] = MODEL_VERSION
    report = conversion_report(document)
    report.extend(items)
    section['conversionReport'] = json.dumps(report, sort_keys=True)
    if notes is not None:
        counts = {}
        for item in items:
            counts[item['kind']] = counts.get(item['kind'], 0) + 1
        parts = [f'{counts[kind]} {label}' for kind, label in (
            ('converted', 'converted'), ('inferred', 'read from names'),
            ('unresolved', 'to repair'), ('kept', 'kept as saved'),
            ('note', 'noted')) if counts.get(kind)]
        notes.append(
            'Surfaces: roles, purposes and interfaces are stored per surface '
            'now (' + ', '.join(parts) + '). The legacy settings are kept '
            'and the meshers read them as before; see the conversion report.')
        for item in items:
            if item['kind'] == 'unresolved':
                notes.append(f'Surfaces: {item["surface"] or item["target"]} '
                             f'-- {item["reason"]} {item.get("remedy", "")}'
                             .strip())


def conversion_report(document) -> list[dict]:
    """Every conversion and repair item migration recorded, oldest first."""
    section = document.get('surfaceIntent') if isinstance(document, dict) \
        else None
    text = _text((section or {}).get('conversionReport')) \
        if isinstance(section, dict) else ''
    if not text:
        return []
    try:
        report = json.loads(text)
    except ValueError:
        return []
    return report if isinstance(report, list) else []


# --------------------------------------------------------------------------- #
# Validation: the state each surface and relation carries
# --------------------------------------------------------------------------- #

DELETED = 'its surface was deleted'
REPLACED = 'its surface was replaced'


def _resolve_side(surfaces, by_uuid, key, token):
    """``(key, problem)`` for one side of a relation."""
    key = None if key in (None, '') else str(key)
    if token:
        found = by_uuid.get(token, [])
        if len(found) > 1:
            return None, 'two surfaces carry its patch identity'
        if found:
            return found[0], None
        if key is not None and key in surfaces and \
                _text(surfaces[key].get('patchUuid')):
            return None, REPLACED
        if key is not None and key in surfaces:
            return key, None
        return None, (DELETED if key is not None
                      else 'no surface carries its patch identity')
    if key is not None and key in surfaces:
        return key, None
    return None, (DELETED if key is not None else 'it names no surface')


def _relation_problems(geometry, surfaces, relation, owner, neighbour,
                       count, mesh_names=()) -> list[str]:
    kind = _text(relation.get('kind'))
    problems = []
    shared = _flag(relation.get('sharedSurface'))
    if kind not in RELATION_KINDS:
        return [f'unknown relation kind "{kind}"']
    if owner and neighbour and owner == neighbour and not shared:
        problems.append('both sides are the same surface')
    if shared and kind != CONFORMAL:
        problems.append('only a conformal interface may use one shared '
                        'surface for both sides')
    for side in {owner, neighbour} - {None}:
        if count.get(side, 0) > 1:
            problems.append(f'surface "{_text(surfaces[side].get("name"))}" '
                            'is a side of more than one relation')
        role = _text(surfaces[side].get('surfaceRole'))
        wanted = EXTERNAL if kind == PERIODIC else kind
        if not role and kind == PERIODIC:
            # Plan 49 SR3. A surface nobody has assessed yet is the boundary
            # its legacy type says; only a legacy interface or internal type
            # contradicts a periodic side.
            legacy = _text(surfaces[side].get('cfdType')) or 'boundary'
            if legacy not in ('boundary', 'cellZone'):
                problems.append(
                    f'surface "{_text(surfaces[side].get("name"))}" is typed '
                    f'{legacy}, not a boundary')
        elif not role:
            problems.append(f'surface "{_text(surfaces[side].get("name"))}" '
                            'has no role yet')
        elif role != wanted:
            problems.append(f'surface "{_text(surfaces[side].get("name"))}" '
                            f'is a {role}, not a {wanted}')
    if kind == PERIODIC:
        if _text(relation.get('transform')) not in ('translational',
                                                    'rotational'):
            problems.append('a periodic pair needs a translation or a '
                            'rotation')
        if _text(relation.get('verification')) == 'failed':
            # Plan 49 SR3 (DP-5834). Measured on the prepared geometry.
            problems.append('its transform does not map one side onto the '
                            'other: '
                            + (_text(relation.get('verificationReason'))
                               or 'measured as failed'))
    else:
        owner_region = _text(relation.get('ownerRegion'))
        neighbour_region = _text(relation.get('neighbourRegion'))
        if not owner_region or not neighbour_region:
            problems.append('the regions on its two sides are not recorded')
        elif owner_region == neighbour_region:
            problems.append('both sides are in the same region')
        for region in (owner_region, neighbour_region):
            reference = region_key(region)
            if reference is not None and reference not in geometry:
                problems.append(f'region {region} no longer exists')
            name = region_name(region)
            # DP-6073: a mesh region (a seeded or typed one) the case has
            # let go. Judged only where the case lists any, so a case that
            # has not typed its regions yet is not flagged.
            if name is not None and mesh_names and name not in mesh_names:
                problems.append(f'region {name} no longer exists')
    return problems


def refresh_surface_intent(document) -> bool:
    """Recompute the validation state of every relation and surface.

    A relation whose surface was deleted or replaced is marked unresolved
    with the reason, and keeps the side's last name and patch identity so it
    can be pointed at a surface again; it is never removed. A side found by
    its patch identity under another row is followed there. Returns whether
    anything changed.
    """
    if not isinstance(document, dict):
        return False
    geometry = document.get('geometry')
    relations = document.get('surfaceRelations')
    if not isinstance(geometry, dict):
        geometry = {}
    if not isinstance(relations, dict) or not relations:
        if not any(_is_surface(row) and _text(row.get('surfaceRole'))
                   for row in geometry.values()):
            return False
        relations = relations if isinstance(relations, dict) else {}
    surfaces = {str(key): row for key, row in geometry.items()
                if _is_surface(row)}
    mesh_names = mesh_region_names(document)
    by_uuid: dict[str, list[str]] = {}
    for key, row in surfaces.items():
        token = _text(row.get('patchUuid'))
        if token:
            by_uuid.setdefault(token, []).append(key)
    changed = False

    def put(record, field, value):
        nonlocal changed
        if record.get(field) != value:
            record[field] = value
            changed = True

    resolved = {}
    count: dict[str, int] = {}
    for key, relation in _sorted_items(relations):
        if not isinstance(relation, dict):
            continue
        owner, owner_problem = _resolve_side(
            surfaces, by_uuid, relation.get('ownerSurface'),
            _text(relation.get('ownerPatchUuid')))
        if _flag(relation.get('sharedSurface')):
            neighbour, neighbour_problem = owner, owner_problem
        else:
            neighbour, neighbour_problem = _resolve_side(
                surfaces, by_uuid, relation.get('neighbourSurface'),
                _text(relation.get('neighbourPatchUuid')))
        resolved[key] = (owner, owner_problem, neighbour, neighbour_problem)
        for side in {owner, neighbour} - {None}:
            count[side] = count.get(side, 0) + 1

    member: dict[str, list[str]] = {}
    for key, relation in _sorted_items(relations):
        if key not in resolved:
            continue
        owner, owner_problem, neighbour, neighbour_problem = resolved[key]
        problems = []
        for side, problem, prefix in ((owner, owner_problem, 'owner'),
                                      (neighbour, neighbour_problem,
                                       'neighbour')):
            if problem:
                label = _text(relation.get(f'{prefix}Label')) or prefix
                problems.append(f'{prefix} side "{label}": {problem}')
                if problem in (DELETED, REPLACED):
                    # The row key is let go, so a later row that happens to
                    # take the same key is never mistaken for this side; the
                    # name and patch identity stay, so re-importing the
                    # surface (or undoing the delete) finds it again.
                    put(relation, f'{prefix}Surface', None)
                continue
            row = surfaces[side]
            stored = relation.get(f'{prefix}Surface')
            if stored is None or str(stored) != side:
                put(relation, f'{prefix}Surface', side)
            put(relation, f'{prefix}Label', _text(row.get('name')))
            token = _text(row.get('patchUuid'))
            if token:
                put(relation, f'{prefix}PatchUuid', token)
            member.setdefault(side, []).append(key)
        problems.extend(_relation_problems(
            geometry, surfaces, relation, owner, neighbour, count,
            mesh_names))
        reason = '; '.join(dict.fromkeys(problems))
        put(relation, 'state', UNRESOLVED if reason else VALID)
        put(relation, 'stateReason', reason)

    for key, row in surfaces.items():
        role = _text(row.get('surfaceRole'))
        if not role:
            continue  # unassessed, or an unresolved migration item
        problems = []
        patch_type = _text(row.get('meshPatchType'))
        mine = [relations[item] for item in member.get(key, ())]
        if role not in ROLES:
            problems.append(f'unknown role "{role}"')
        elif role == EXTERNAL:
            if not patch_type:
                problems.append('an external boundary needs a mesh type')
            elif patch_type not in FREE_PATCH_TYPES:
                problems.append(f'"{patch_type}" is not a type a boundary '
                                'may be given; coupled types come from a '
                                'relation')
        else:
            if patch_type:
                problems.append(f'a {role} has no free mesh type; its '
                                'patches are generated')
            if role in INTERFACE_ROLES:
                if not mine:
                    problems.append('a region interface needs its relation: '
                                    'the two regions it joins')
        if any(_text(item.get('state')) == UNRESOLVED for item in mine):
            problems.append('its relation is unresolved')
        reason = '; '.join(dict.fromkeys(problems))
        put(row, 'intentState', UNRESOLVED if reason else VALID)
        put(row, 'intentIssue', reason)
    return changed


# --------------------------------------------------------------------------- #
# Reading the model
# --------------------------------------------------------------------------- #

def relations_of(document, key) -> list[tuple[str, dict]]:
    """The relations naming geometry row *key* on either side."""
    key = str(key)
    found = []
    for relation_key, relation in _sorted_items(
            (document or {}).get('surfaceRelations')):
        if not isinstance(relation, dict):
            continue
        if key in {str(relation.get('ownerSurface')),
                   str(relation.get('neighbourSurface'))}:
            found.append((relation_key, relation))
    return found


def surface_intent(document, key) -> dict | None:
    """The authored intent of one surface row, or ``None`` if unassessed."""
    geometry = (document or {}).get('geometry') or {}
    row = geometry.get(str(key))
    if row is None:
        row = geometry.get(key)
    if not _is_surface(row):
        return None
    if not _text(row.get('surfaceRole')) and not _text(row.get('intentState')):
        return None
    relations = []
    for _relation_key, relation in relations_of(document, key):
        owner = str(relation.get('ownerSurface')) == str(key)
        shared = _flag(relation.get('sharedSurface'))
        side = 'shared' if shared else ('owner' if owner else 'neighbour')
        partner = 'neighbour' if owner and not shared else 'owner'
        entry = {
            'relation_id': _text(relation.get('relationId')),
            'kind': _text(relation.get('kind')),
            'side': side,
            'partner_patch_uuid': _text(relation.get(f'{partner}PatchUuid')),
            'owner_region': _text(relation.get('ownerRegion')),
            'neighbour_region': _text(relation.get('neighbourRegion')),
            'state': _text(relation.get('state')),
        }
        if _text(relation.get('kind')) == PERIODIC:
            entry['transform'] = {
                field: _value(relation.get(field))
                for field in _TRANSFORM_FIELDS if field in relation}
        relations.append(entry)
    return {
        'role': _text(row.get('surfaceRole')) or None,
        'boundary_category': _text(row.get('boundaryCategory')) or None,
        'mesh_patch_type': _text(row.get('meshPatchType')) or None,
        'origin': _text(row.get('intentOrigin')) or None,
        'state': _text(row.get('intentState')) or None,
        'issue': _text(row.get('intentIssue')),
        'relations': relations,
    }


def prepared_surface_intent(document, entries: Iterable[Mapping]) -> dict:
    """The authored intent of each prepared patch, by its patch uuid.

    Plan 49 section 2.3, the bridge from the project to prepared geometry:
    the prepared store enumerates patches from the geometry artifacts
    (``entries``), the project stores intent on its rows. A row is joined to
    a patch by the patch uuid it was imported from, and -- for a row saved
    before rows carried one -- by its artifact and name, then by a name only
    one row has. A patch with no assessed row has no entry, so a project
    with no authored intent prepares exactly what it prepared before.
    """
    geometry = (document or {}).get('geometry') or {}
    rows = {str(key): row for key, row in geometry.items()
            if _is_surface(row)}
    by_uuid = {}
    by_artifact_name = {}
    by_name: dict[str, list[str]] = {}
    for key, row in rows.items():
        token = _text(row.get('patchUuid'))
        if token:
            by_uuid.setdefault(token, key)
        name = _text(row.get('name'))
        by_artifact_name.setdefault((_text(row.get('geometryId')), name), key)
        by_name.setdefault(name, []).append(key)
    intent = {}
    for entry in entries or ():
        patches = entry.get('patches') or ({
            'patch_uuid': entry.get('patch_uuid'),
            'name': entry.get('name') or entry.get('geometry_id'),
        },)
        for patch in patches:
            token = str(patch.get('patch_uuid') or '').strip()
            if not token:
                continue
            name = str(patch.get('name') or entry.get('name') or '').strip()
            key = by_uuid.get(token)
            if key is None:
                key = by_artifact_name.get(
                    (str(entry.get('geometry_id') or ''), name))
            if key is None and len(by_name.get(name, ())) == 1:
                key = by_name[name][0]
            if key is None:
                continue
            found = surface_intent(document, key)
            if found is None:
                continue
            intent[token] = {**found, 'surface': key}
    return intent


def intent_digest(intent: Mapping) -> str:
    """A stable digest of a ``prepared_surface_intent`` mapping."""
    encoded = json.dumps(intent, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


# --------------------------------------------------------------------------- #
# Plan 49 SR3: periodic pairs are relations
# --------------------------------------------------------------------------- #
#
# A periodic (cyclic) pair is authored through two forms until SR8 gives the
# relation store controls of its own: a ``cyclic`` row of ``interfacePairs``
# on the Geometry page, and a row of ``gmsh/periodicPairs`` on the Gmsh
# Periodic page. Neither form is a coupling store any more: on every commit
# each enabled row is carried into the one periodic ``surfaceRelations``
# record that names it as its ``legacySource``, and a row removed, disabled
# or turned into another coupling takes its relation with it. The relation
# is what validation, verification and (from SR4/SR5) the meshers read.

INTERFACE_PAIRS = 'interfacePairs'
GMSH_PERIODIC = 'gmsh/periodicPairs'
PERIODIC_SOURCES = (INTERFACE_PAIRS, GMSH_PERIODIC)

#: The Gmsh Periodic page's transform words -> the relation's.
_GMSH_TRANSFORM = {'translation': 'translational', 'rotation': 'rotational'}
#: And back, for the read adapter.
_GMSH_WORD = {value: key for key, value in _GMSH_TRANSFORM.items()}

_NUMBERS = ('matchTolerance', 'translationX', 'translationY', 'translationZ',
            'rotationCentreX', 'rotationCentreY', 'rotationCentreZ',
            'rotationAxisX', 'rotationAxisY', 'rotationAxisZ',
            'rotationAngleDegrees')
_NUMBER_DEFAULTS = {'matchTolerance': 1e-6, 'rotationAxisZ': 1.0}


def _number(value, default=0.0) -> float:
    value = _value(value, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _vector_fields(row, name, prefix) -> dict:
    vector = row.get(name) if isinstance(row, dict) else None
    vector = vector if isinstance(vector, dict) else {}
    return {f'{prefix}{axis.upper()}':
            _number(vector.get(axis),
                    _NUMBER_DEFAULTS.get(f'{prefix}{axis.upper()}', 0.0))
            for axis in 'xyz'}


def _periodic_form_rows(document) -> dict:
    """``{source: values}`` for every enabled pair that is a periodic pair.

    ``values`` holds the relation fields the row states: ``name``, the two
    patch uuids and the transform. A ``cyclic`` interface pair with no
    transform (coincident) is included -- its relation is unresolved and says
    why. An affine Gmsh pair is not: a cyclic patch has no affine transform
    (DP-5832).
    """
    rows = {}
    for key, pair in _sorted_items(document.get(INTERFACE_PAIRS)):
        if not isinstance(pair, dict) or not _flag(pair.get('enabled', True)):
            continue
        if _text(pair.get('coupling')) != 'cyclic':
            continue
        values = {'name': _text(pair.get('name')),
                  'ownerPatchUuid': _text(pair.get('masterScopeToken')),
                  'neighbourPatchUuid': _text(pair.get('slaveScopeToken')),
                  'transform': _text(pair.get('transform')) or 'coincident'}
        for field in _NUMBERS:
            values[field] = _number(pair.get(field),
                                    _NUMBER_DEFAULTS.get(field, 0.0))
        rows[f'{INTERFACE_PAIRS}/{key}'] = values
    gmsh = document.get('gmsh')
    pairs = gmsh.get('periodicPairs') if isinstance(gmsh, dict) else None
    for key, pair in _sorted_items(pairs):
        if not isinstance(pair, dict) or not _flag(pair.get('enabled', True)):
            continue
        transform = _GMSH_TRANSFORM.get(_text(pair.get('transform')))
        if transform is None:
            continue
        values = {'name': _text(pair.get('name')),
                  'ownerPatchUuid': _text(pair.get('masterScopeToken')),
                  'neighbourPatchUuid': _text(pair.get('slaveScopeToken')),
                  'transform': transform,
                  'matchTolerance': _number(pair.get('matchTolerance'), 1e-6),
                  'rotationAngleDegrees':
                      _number(pair.get('rotationAngleDegrees'))}
        values.update(_vector_fields(pair, 'translation', 'translation'))
        values.update(_vector_fields(pair, 'rotationCentre', 'rotationCentre'))
        values.update(_vector_fields(pair, 'rotationAxis', 'rotationAxis'))
        rows[f'{GMSH_PERIODIC}/{key}'] = values
    return rows


def affine_gmsh_pairs(document) -> list[tuple[str, dict]]:
    """``(source, row)`` of each enabled affine Gmsh pair (no relation)."""
    gmsh = (document or {}).get('gmsh')
    pairs = gmsh.get('periodicPairs') if isinstance(gmsh, dict) else None
    return [(f'{GMSH_PERIODIC}/{key}', pair)
            for key, pair in _sorted_items(pairs)
            if isinstance(pair, dict) and _flag(pair.get('enabled', True))
            and _text(pair.get('transform')) == 'affine']


def is_periodic_form(source) -> bool:
    """Is *source* (a ``legacySource``) a periodic pair's form row?"""
    text = _text(source)
    return any(text.startswith(f'{root}/') for root in PERIODIC_SOURCES)


def _surfaces_by_uuid(geometry) -> dict:
    found: dict[str, list[str]] = {}
    for key, row in _sorted_items(geometry):
        if _is_surface(row) and _text(row.get('patchUuid')):
            found.setdefault(_text(row['patchUuid']), []).append(str(key))
    return found


def periodic_verification_key(document, relation) -> str:
    """What a periodic relation's measurement depends on, as a digest.

    The two sides' patch uuids and the geometry files they come from, the
    transform and the tolerance. Renaming a side changes none of these, so a
    rename keeps the verdict; a different side, a re-imported file or a new
    transform does not.
    """
    geometry = (document or {}).get('geometry') or {}
    files = []
    for prefix in ('owner', 'neighbour'):
        key = relation.get(f'{prefix}Surface')
        row = geometry.get(str(key)) if key not in (None, '') else None
        files.append(_text(row.get('geometryId')) if isinstance(row, dict)
                     else '')
    payload = {
        'owner': _text(relation.get('ownerPatchUuid')),
        'neighbour': _text(relation.get('neighbourPatchUuid')),
        'files': files,
        'transform': _text(relation.get('transform')),
        'numbers': [round(_number(relation.get(field),
                                  _NUMBER_DEFAULTS.get(field, 0.0)), 12)
                    for field in _NUMBERS],
    }
    encoded = json.dumps(payload, sort_keys=True).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()[:32]


def sync_periodic_relations(document, origin=AUTHORED) -> bool:
    """Carry every periodic pair form row into its relation. Idempotent.

    A new enabled row gains a relation (``origin``); an edited row updates
    its relation's name, sides and transform, keeping the relation id; a row
    removed, disabled, made affine or given another coupling loses its
    relation. A side whose patch uuid changed is looked up again; a side
    whose surface was deleted or replaced is left to
    :func:`refresh_surface_intent`, which marks the relation unresolved and
    keeps the side's name and uuid. A measurement whose
    :func:`periodic_verification_key` no longer matches is put back to
    ``unverified``. Returns whether anything changed.
    """
    if not isinstance(document, dict):
        return False
    relations = document.get('surfaceRelations')
    forms = _periodic_form_rows(document)
    owned = {}
    if isinstance(relations, dict):
        owned = {_text(item.get('legacySource')): key
                 for key, item in _sorted_items(relations)
                 if isinstance(item, dict)
                 and _text(item.get('kind')) == PERIODIC
                 and is_periodic_form(item.get('legacySource'))}
    if not forms and not owned:
        return False
    if not isinstance(relations, dict):
        relations = document['surfaceRelations'] = {}
    geometry = document.get('geometry')
    geometry = geometry if isinstance(geometry, dict) else {}
    by_uuid = _surfaces_by_uuid(geometry)
    changed = False

    def put(record, field, value):
        nonlocal changed
        if record.get(field) != value:
            record[field] = value
            changed = True

    for source, key in owned.items():
        if source not in forms:
            del relations[key]
            changed = True
    for source, values in forms.items():
        key = owned.get(source)
        if key is None:
            record = _relation(PERIODIC, name=values['name'],
                               legacy_source=source)
            record['origin'] = origin
            record['ownerPatchUuid'] = None
            record['neighbourPatchUuid'] = None
            relations[_next_key(relations)] = record
            changed = True
        else:
            record = relations[key]
        put(record, 'name', values['name'])
        for prefix in ('owner', 'neighbour'):
            token = values[f'{prefix}PatchUuid']
            if record.get(f'{prefix}PatchUuid') is not None and \
                    _text(record.get(f'{prefix}PatchUuid')) == token:
                continue
            found = by_uuid.get(token, [])
            side = found[0] if len(found) == 1 else None
            put(record, f'{prefix}PatchUuid', token)
            put(record, f'{prefix}Surface', side)
            put(record, f'{prefix}Label',
                _text(geometry[side].get('name')) if side else token)
        put(record, 'transform', values['transform'])
        for field in _NUMBERS:
            put(record, field, values[field])
        put(record, 'sharedSurface', False)
        if _text(record.get('verificationKey')) != \
                periodic_verification_key(document, record):
            put(record, 'verification', 'unverified')
            put(record, 'verificationReason', '')
            put(record, 'verificationKey', '')
    return changed


def relation_for_source(document, source):
    """``(key, relation)`` of the relation edited through row *source*."""
    for key, item in _sorted_items((document or {}).get('surfaceRelations')):
        if isinstance(item, dict) and \
                _text(item.get('legacySource')) == _text(source):
            return key, item
    return None, None


_ROLE_WORDS = {CONFORMAL: 'a conformal region interface',
               NON_CONFORMAL: 'a non-conformal region interface',
               BAFFLE: 'a blocking baffle',
               INTERNAL: 'an internal face, which has no sides to pair'}
_KIND_WORDS = {PERIODIC: 'periodic pair', CONFORMAL: 'region interface',
               NON_CONFORMAL: 'region interface'}


def _side_rows(document, relation):
    geometry = (document or {}).get('geometry') or {}
    by_uuid = _surfaces_by_uuid(geometry)
    sides = []
    for prefix in ('owner', 'neighbour'):
        token = _text(relation.get(f'{prefix}PatchUuid'))
        key = relation.get(f'{prefix}Surface')
        key = None if key in (None, '') else str(key)
        found = by_uuid.get(token, []) if token else []
        if len(found) == 1:
            key = found[0]
        row = geometry.get(key) if key is not None else None
        sides.append((key, token, row if _is_surface(row) else None))
    return sides


def _conflict(surface, pair, reason, remedy) -> dict:
    return {'surface': surface, 'pair': pair, 'reason': reason,
            'remedy': remedy,
            'message': f'Surface "{surface}" cannot be a side of periodic '
                       f'pair "{pair}": {reason}. {remedy}'}


def periodic_side_conflict(document, source) -> dict | None:
    """Why the periodic pair edited through *source* may not be saved.

    Plan 49 section 3: a side cannot be periodic, a region interface and a
    blocking baffle at once. Returns ``None`` when the pair's sides are
    free, else ``{surface, pair, reason, remedy, message}`` for the first
    side that is already something else: a region interface or baffle by
    its role (or an interface or internal surface by its legacy type), a
    side of another relation or of another enabled interface pair, or the
    pair's other side.
    """
    _key, relation = relation_for_source(document, source)
    if relation is None:
        return None
    pair = _text(relation.get('name')) or _text(source)
    relation_id = _text(relation.get('relationId'))
    sides = _side_rows(document, relation)
    (owner_key, owner_token, owner_row), (neighbour_key, neighbour_token,
                                          _neighbour_row) = sides
    if (owner_token and owner_token == neighbour_token) or (
            owner_key is not None and owner_key == neighbour_key):
        name = _text((owner_row or {}).get('name')) or owner_token
        return _conflict(name, pair, 'it is both sides of the pair',
                         'Choose two different surfaces.')
    for key, token, row in sides:
        if row is None:
            continue  # a missing side is reported by the relation's state
        name = _text(row.get('name')) or token
        role = _text(row.get('surfaceRole'))
        legacy = _text(row.get('cfdType')) or 'boundary'
        if role in _ROLE_WORDS:
            return _conflict(name, pair, f'it is {_ROLE_WORDS[role]}',
                             'Make it an external boundary first, or choose '
                             'another surface.')
        if not role and legacy in ('interface', 'none'):
            what = ('typed interface' if legacy == 'interface'
                    else 'an internal surface (type none)')
            return _conflict(name, pair, f'it is {what}',
                             'Set its type to boundary first, or choose '
                             'another surface.')
        for _other_key, other in _sorted_items(
                document.get('surfaceRelations')):
            if not isinstance(other, dict) or \
                    _text(other.get('relationId')) == relation_id:
                continue
            tokens = {_text(other.get('ownerPatchUuid')),
                      _text(other.get('neighbourPatchUuid'))} - {''}
            keys = {str(other.get('ownerSurface')),
                    str(other.get('neighbourSurface'))} - {'None', ''}
            if (token and token in tokens) or (key is not None
                                               and key in keys):
                kind = _text(other.get('kind'))
                other_name = (_text(other.get('name'))
                              or _text(other.get('relationId')))
                return _conflict(
                    name, pair,
                    f'it is already a side of '
                    f'{_KIND_WORDS.get(kind, "relation")} "{other_name}"',
                    f'Remove it from "{other_name}" first, or choose another '
                    'surface.')
        for pair_key, other in _sorted_items(document.get(INTERFACE_PAIRS)):
            if not isinstance(other, dict) or \
                    not _flag(other.get('enabled', True)) or \
                    _text(other.get('coupling')) == 'cyclic':
                continue
            if token and token in (_text(other.get('masterScopeToken')),
                                   _text(other.get('slaveScopeToken'))):
                other_name = _text(other.get('name')) or f'pair {pair_key}'
                return _conflict(
                    name, pair,
                    f'it is already a side of interface pair "{other_name}"',
                    f'Remove it from "{other_name}" first, or choose another '
                    'surface.')
        # Plan 49 SR7 (DP-5832). An affine Gmsh pair has no relation, but its
        # sides are sides all the same.
        found = _affine_pair_naming(document, token, exclude=_text(source))
        if found is not None:
            return _conflict(
                name, pair,
                f'it is already a side of affine periodic pair "{found}"',
                f'Remove it from "{found}" first, or choose another surface.')
    return None


def _affine_pair_naming(document, token, exclude='') -> str | None:
    """The name of an enabled affine Gmsh pair with side *token*."""
    if not token:
        return None
    for source, other in affine_gmsh_pairs(document):
        if source == exclude:
            continue
        if token in (_text(other.get('masterScopeToken')),
                     _text(other.get('slaveScopeToken'))):
            return _text(other.get('name')) or source
    return None


def affine_side_conflict(document, source) -> dict | None:
    """Why the affine Gmsh pair at *source* may not name its sides.

    Plan 49 SR7 (DP-5832). An affine pair stays a native Gmsh control
    (SR5), not a relation; the side rule of plan section 3 holds for it all
    the same: a side cannot be periodic and a region interface, a baffle, an
    internal face or a side of another pair at once.
    """
    pair = None
    for found_source, row in affine_gmsh_pairs(document):
        if found_source == _text(source):
            pair = row
            break
    if pair is None:
        return None
    label = _text(pair.get('name')) or _text(source)
    geometry = (document or {}).get('geometry') or {}
    by_uuid = _surfaces_by_uuid(geometry)
    tokens = [_text(pair.get('masterScopeToken')),
              _text(pair.get('slaveScopeToken'))]
    if tokens[0] and tokens[0] == tokens[1]:
        keys = by_uuid.get(tokens[0], [])
        name = (_text(geometry[keys[0]].get('name')) if keys else tokens[0])
        return _conflict(name, label, 'it is both sides of the pair',
                         'Choose two different surfaces.')
    for token in tokens:
        if not token:
            continue
        keys = by_uuid.get(token, [])
        row = geometry.get(keys[0]) if len(keys) == 1 else None
        name = _text((row or {}).get('name')) or token
        role = _text((row or {}).get('surfaceRole'))
        if role in _ROLE_WORDS:
            return _conflict(name, label, f'it is {_ROLE_WORDS[role]}',
                             'Make it an external boundary first, or choose '
                             'another surface.')
        for _key, other in _sorted_items(document.get('surfaceRelations')):
            if not isinstance(other, dict):
                continue
            if token in (_text(other.get('ownerPatchUuid')),
                         _text(other.get('neighbourPatchUuid'))):
                other_name = (_text(other.get('name'))
                              or _text(other.get('relationId')))
                kind = _KIND_WORDS.get(_text(other.get('kind')), 'relation')
                return _conflict(
                    name, label, f'it is already a side of {kind} '
                    f'"{other_name}"', f'Remove it from "{other_name}" '
                    'first, or choose another surface.')
        other_name = _affine_pair_naming(document, token,
                                         exclude=_text(source))
        if other_name is not None:
            return _conflict(
                name, label,
                f'it is already a side of affine periodic pair '
                f'"{other_name}"',
                f'Remove it from "{other_name}" first, or choose another '
                'surface.')
    return None


def surface_role_conflict(document, key) -> dict | None:
    """Why geometry row *key*'s role or type contradicts its periodic pair.

    The other direction of :func:`periodic_side_conflict`: a surface that is
    a side of a periodic relation may not become a region interface, a
    blocking baffle or an internal face (or, by its legacy type, an
    interface or internal surface) while the pair names it.
    """
    geometry = (document or {}).get('geometry') or {}
    row = geometry.get(str(key))
    if not _is_surface(row):
        return None
    name = _text(row.get('name'))
    role = _text(row.get('surfaceRole'))
    legacy = _text(row.get('cfdType')) or 'boundary'
    token = _text(row.get('patchUuid'))
    if role in _ROLE_WORDS:
        becomes = _ROLE_WORDS[role]
    elif not role and legacy in ('interface', 'none'):
        becomes = ('typed interface' if legacy == 'interface'
                   else 'an internal surface (type none)')
    else:
        return None
    pairs = []
    for _relation_key, relation in _sorted_items(
            document.get('surfaceRelations')):
        if not isinstance(relation, dict) or \
                _text(relation.get('kind')) != PERIODIC:
            continue
        keys = {str(relation.get('ownerSurface')),
                str(relation.get('neighbourSurface'))}
        tokens = {_text(relation.get('ownerPatchUuid')),
                  _text(relation.get('neighbourPatchUuid'))} - {''}
        if str(key) not in keys and token not in tokens:
            continue
        pairs.append(_text(relation.get('name'))
                     or _text(relation.get('relationId')))
    # Plan 49 SR7 (DP-5832): an affine Gmsh pair's sides too.
    affine = _affine_pair_naming(document, token)
    if affine is not None:
        pairs.append(affine)
    for pair in pairs:
        remedy = (f'Remove the periodic pair "{pair}" first, or keep the '
                  'surface an external boundary.')
        reason = f'it would become {becomes} while it is a side of this pair'
        conflict = _conflict(name, pair, reason, remedy)
        conflict['message'] = (f'Surface "{name}" is a side of periodic pair '
                               f'"{pair}" and cannot become {becomes}. '
                               f'{remedy}')
        return conflict
    return None


def periodic_pairs(document) -> list[dict]:
    """The read adapter: every periodic relation, in the Gmsh pair's shape.

    For the meshers' switch-over (SR5 Gmsh, SR4 snappy). One row per
    periodic relation, whatever form it was edited through, shaped like a
    ``gmsh/periodicPairs`` row (``masterScopeToken`` is the owner's patch
    uuid; the transform maps the owner onto the neighbour) plus the
    relation's identity and verdicts: ``relationId``, ``origin`` =
    ``surfaceRelations``, ``source`` (the form row), ``state``,
    ``stateReason``, ``verification`` and ``verificationReason``. A caller
    lowers only ``state == 'valid'`` rows and refuses the rest with their
    reason. Affine Gmsh pairs are not relations; see
    :func:`affine_gmsh_pairs`.
    """
    rows = []
    for _key, relation in _sorted_items(
            (document or {}).get('surfaceRelations')):
        if not isinstance(relation, dict) or \
                _text(relation.get('kind')) != PERIODIC:
            continue

        def vector(prefix, defaults=(0.0, 0.0, 0.0), relation=relation):
            return {axis: _number(relation.get(f'{prefix}{axis.upper()}'),
                                  fallback)
                    for axis, fallback in zip('xyz', defaults)}

        transform = _text(relation.get('transform'))
        rows.append({
            'name': _text(relation.get('name')),
            'enabled': True,
            'masterScopeToken': _text(relation.get('ownerPatchUuid')),
            'slaveScopeToken': _text(relation.get('neighbourPatchUuid')),
            'transform': _GMSH_WORD.get(transform, transform),
            'translation': vector('translation'),
            'rotationCentre': vector('rotationCentre'),
            'rotationAxis': vector('rotationAxis', (0.0, 0.0, 1.0)),
            'rotationAngleDegrees':
                _number(relation.get('rotationAngleDegrees')),
            'matchTolerance': _number(relation.get('matchTolerance'), 1e-6),
            'origin': 'surfaceRelations',
            'relationId': _text(relation.get('relationId')),
            'source': _text(relation.get('legacySource')),
            'state': _text(relation.get('state')) or VALID,
            'stateReason': _text(relation.get('stateReason')),
            'verification': _text(relation.get('verification'))
            or 'unverified',
            'verificationReason': _text(relation.get('verificationReason')),
        })
    return rows
