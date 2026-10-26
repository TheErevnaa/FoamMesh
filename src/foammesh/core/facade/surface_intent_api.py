"""Plan 49 SR8. The surface tab's role controls through the local API.

The surface tab (``view/geometry/surface_dialog.py``) edits a surface's role,
purpose, mesh type, partner and the region on each side through one planner,
``db/surface_authoring.py``: :func:`~foammesh.db.surface_authoring.plan_edit`
refuses what the model cannot hold, :func:`write_plan` writes the row, the
derived legacy fields, ``intentOrigin`` and the one relation that couples the
surface to its partner, and :func:`all_issues` says what is still unresolved
and what the configured route cannot build. This module is the local API's
half of the same thing, so an outside program gets the dialog's refusals and
writes, word for word, and its edit lands in the same history (undo).

It also holds the rules the local API adds for fields a person can no longer
set (Plan 49 SR8):

* read-only fields -- computed ones (``intent_state``, ``intent_issue``) and
  the origin every role edit stamps (``intent_origin``) -- are refused by name
  (``field_read_only``);
* retired fields -- ``non_conformal`` and ``inter_region``, and the
  ``cfd_type`` values ``interface`` (any row) and ``none`` (a surface) -- are
  refused by name (``field_retired``) with the field that replaced them. The
  writers read the role model since SR4/SR5 (``openfoam/surface_roles.py``,
  ``core/gmsh/surface_roles.py``); these fields are derived from the role and
  a write of them alone was silently overwritten or ignored;
* ``geometry.surface_relations`` is read-only (``collection_read_only``).
"""
from __future__ import annotations

import re

from foammesh.db import surface_authoring as authoring
from foammesh.db import surface_intent as model

from .errors import ValidationFailedError

#: Storage names of the role fields the planner writes, and the
#: :class:`~foammesh.db.surface_authoring.SurfaceEdit` attribute each feeds.
INTENT_FIELDS = {'surfaceRole': 'role', 'boundaryCategory': 'category',
                 'meshPatchType': 'patch_type'}

#: Storage name -> (API id, why it cannot be written, what writes it).
READ_ONLY_FIELDS = {
    'intentState': ('intent_state',
                    'it is computed from the role and its relation when the '
                    'case is saved'),
    'intentIssue': ('intent_issue',
                    'it is computed from the role and its relation when the '
                    'case is saved'),
    'intentOrigin': ('intent_origin',
                     'every role edit stamps it "authored"; a migrated or '
                     'inferred role says so until it is edited'),
}

#: Storage name -> (API id, what replaced it).
RETIRED_FIELDS = {
    'nonConformal': (
        'non_conformal',
        'surface_role regionInterfaceNonConformal (or blockingBaffle), with '
        'geometry.surface_intent.edit for the partner and regions'),
    'interRegion': (
        'inter_region',
        'surface_role and the region on each side '
        '(geometry.surface_intent.edit)'),
}

#: The operations that write what ``geometry.surface_relations`` holds.
RELATION_WRITERS = ('geometry.surface_intent.edit', 'geometry.interface_pairs',
                    'gmsh.periodic_pairs.controls')

READ_ONLY_COLLECTIONS = frozenset({'geometry.surface_relations'})

#: Mesh patch types the surface tab offers (the schema's enum).
MESH_PATCH_TYPES = ('patch', 'wall', 'symmetry', 'symmetryPlane', 'empty',
                    'wedge')

_EDIT_KEYS = ('surface_role', 'boundary_category', 'mesh_patch_type',
              'partner', 'own_region', 'partner_region')


# --------------------------------------------------------------------------- #
# Named refusals
# --------------------------------------------------------------------------- #

def collection_read_only(operation: str) -> ValidationFailedError:
    collection = operation.rsplit('.', 1)[0]
    return ValidationFailedError(
        f'{collection} is read-only: a relation is written by the surface '
        'tab (geometry.surface_intent.edit) and the two periodic pair tables '
        '(geometry.interface_pairs, gmsh.periodic_pairs.controls), which '
        'keep both sides consistent.',
        details={'error': 'collection_read_only', 'collection': collection,
                 'operation': operation,
                 'replacement_operations': list(RELATION_WRITERS)})


def read_only_operation(operation: str) -> ValidationFailedError | None:
    """The named refusal for a create/patch/remove of a read-only
    collection, or ``None`` for any other operation."""
    collection, _, verb = operation.rpartition('.')
    if collection in READ_ONLY_COLLECTIONS and verb in (
            'create', 'patch', 'remove'):
        return collection_read_only(operation)
    return None


def _field_read_only(name: str, entity_id) -> ValidationFailedError:
    api_id, why = READ_ONLY_FIELDS[name]
    return ValidationFailedError(
        f'geometry.items {api_id} is read-only: {why}.',
        details={'error': 'field_read_only', 'collection': 'geometry.items',
                 'entity_id': entity_id, 'field': api_id, 'reason': why})


def _field_retired(api_id: str, value, replacement: str,
                   entity_id) -> ValidationFailedError:
    return ValidationFailedError(
        f'geometry.items {api_id}{"" if value is None else f" = {value!r}"} '
        f'is retired: the surface role model replaced it. Write '
        f'{replacement} instead.',
        details={'error': 'field_retired', 'collection': 'geometry.items',
                 'entity_id': entity_id, 'field': api_id, 'value': value,
                 'replacement': replacement,
                 'replacement_operation': 'geometry.surface_intent.edit'})


def refused(error: authoring.EditRefused, keys) -> ValidationFailedError:
    """The local API's form of a refusal the surface tab shows inline."""
    keys = [str(key) for key in keys]
    if error.code == 'surface_role_conflict' and error.conflict:
        conflict = error.conflict
        return ValidationFailedError(str(error), details={
            'collection': 'geometry.items', 'error': 'periodic_side_conflict',
            'geometry_ids': keys,
            'surface': conflict.get('surface'), 'pair': conflict.get('pair'),
            'reason': conflict.get('reason'),
            'remedy': conflict.get('remedy')})
    return ValidationFailedError(str(error), details={
        'collection': 'geometry.items', 'error': 'surface_edit_refused',
        'code': error.code, 'geometry_ids': keys, 'reason': str(error)})


def _edit_refused(code: str, message: str, keys) -> ValidationFailedError:
    return refused(authoring.EditRefused(message, code=code), keys)


# --------------------------------------------------------------------------- #
# geometry.items create / patch
# --------------------------------------------------------------------------- #

def pop_intent_fields(normalized: dict, entity_id=None) -> authoring.SurfaceEdit | None:
    """Take the role fields out of a ``geometry.items`` patch.

    Refuses the read-only and retired fields by name. Returns the
    :class:`SurfaceEdit` the role fields ask for (``None`` when there are
    none); *normalized* keeps everything else.
    """
    for name in READ_ONLY_FIELDS:
        if name in normalized:
            raise _field_read_only(name, entity_id)
    for name, (api_id, replacement) in RETIRED_FIELDS.items():
        if name in normalized:
            raise _field_retired(api_id, normalized[name], replacement,
                                 entity_id)
    found = {name: normalized.pop(name) for name in list(normalized)
             if name in INTENT_FIELDS}
    if not found:
        return None
    edit = authoring.SurfaceEdit()
    for name, value in found.items():
        setattr(edit, INTENT_FIELDS[name], value)
    if edit.role is None:
        raise _edit_refused('no_role', 'a surface role cannot be cleared; '
                            'choose one of ' + ', '.join(model.ROLES),
                            [entity_id])
    return edit


def _text(value) -> str:
    return str(getattr(value, 'value', value) or '')


#: DP-6008 (user decision 2026-10-25). The Interface pairs table makes
#: periodic (cyclic) pairs only; a conformal or non-conformal interface is a
#: surface's role and partner, held in one relation.
PAIR_COUPLING_KEPT = 'cyclic'
RETIRED_PAIR_COUPLINGS = ('conformal', 'non_conformal')
_PAIR_COUPLING_REPLACEMENT = (
    'surface_role regionInterfaceConformal or regionInterfaceNonConformal '
    'with its partner (geometry.surface_intent.edit), set on either surface')


def screen_pair_coupling(collection_id: str, data, storage_path: str, key,
                         normalized: dict, *, created: bool) -> None:
    """DP-6008. Refuse a non-cyclic ``geometry.interface_pairs`` row.

    A new row with no coupling is a cyclic pair (the table offers nothing
    else). A create or patch that leaves a row conformal or non-conformal is
    refused by name: the interface is authored on its surfaces, so a second
    store cannot say something else about it. A row saved before this is
    left exactly as it was (opening the project converted it into a
    relation); it can still be removed.
    """
    if collection_id != 'geometry.interface_pairs':
        return
    if created and 'coupling' not in normalized:
        normalized['coupling'] = PAIR_COUPLING_KEPT
    if 'coupling' in normalized:
        coupling = _text(getattr(normalized['coupling'], 'value',
                                 normalized['coupling']))
    else:
        try:
            stored = data.getValue(f'{storage_path}/{key}/coupling')
        except Exception:  # noqa: BLE001 - no coupling saved: the default
            stored = 'conformal'
        coupling = _text(getattr(stored, 'value', stored)) or 'conformal'
    if coupling == PAIR_COUPLING_KEPT:
        return
    entity_id = None if created else str(key)
    raise ValidationFailedError(
        f'geometry.interface_pairs coupling {coupling!r} is retired: a '
        'conformal or non-conformal interface is set on its surfaces (Role '
        'and Partner). Write '
        f'{_PAIR_COUPLING_REPLACEMENT} instead. The Interface pairs table '
        'makes periodic (cyclic) pairs only.',
        details={'error': 'field_retired',
                 'collection': 'geometry.interface_pairs',
                 'entity_id': entity_id, 'field': 'coupling',
                 'value': coupling, 'kept': [PAIR_COUPLING_KEPT],
                 'replacement': _PAIR_COUPLING_REPLACEMENT,
                 'replacement_operation': 'geometry.surface_intent.edit'})


def screen_cfd_type(data, key, normalized: dict, edit, *, created: bool):
    """Judge a ``cfd_type`` write against the row it lands on.

    ``interface`` is retired on every row and ``none`` on a surface: a
    surface's type is its role now. ``boundary`` on an existing surface is
    what the old type meant by it -- an external boundary -- and is written
    as that role through the planner (a new surface keeps the import's
    ``boundary`` and is assessed like any imported surface). Returns the
    edit to plan, possibly *edit* itself or a new one.
    """
    if 'cfdType' not in normalized:
        return edit
    value = _text(normalized['cfdType'])
    try:
        surface = _text(data.getValue(f'geometry/{key}/gType')) == 'surface'
    except Exception:  # noqa: BLE001 - a row without a type is not a surface
        surface = False
    if value == 'interface':
        raise _field_retired(
            'cfd_type', value,
            'surface_role regionInterfaceConformal, '
            'regionInterfaceNonConformal or blockingBaffle', key)
    if not surface:
        return edit
    if value == 'none':
        raise _field_retired('cfd_type', value, 'surface_role internalFace',
                             key)
    if value == 'boundary' and not created:
        normalized.pop('cfdType')
        if edit is None:
            edit = authoring.SurfaceEdit()
        if edit.role is authoring.KEEP:
            edit.role = model.EXTERNAL
        elif edit.role != model.EXTERNAL:
            raise _edit_refused(
                'conflicting_type',
                f'cfd_type boundary and surface_role {edit.role} disagree; '
                'write surface_role alone', [key])
    elif value == 'cellZone':
        raise _edit_refused('not_a_volume',
                            'cell_zone is a volume type; a surface takes a '
                            'surface_role', [key])
    return edit


def _check_edit_shape(document, keys, edit) -> None:
    """What the surface tab never lets a person ask for, refused by name.

    The tab shows purpose and mesh type only for an external boundary, and
    partner and regions only for a region interface of one surface.
    """
    for key in keys:
        row = authoring._row(document, key)
        if not authoring._is_surface(row):
            raise _edit_refused('not_a_surface',
                                f'geometry {key} is not a surface', keys)
        role = edit.role
        if role is authoring.KEEP:
            role = model._text(row.get('surfaceRole')) or None
        if role is not None and role not in model.ROLES:
            raise _edit_refused('unknown_role',
                                f'"{role}" is not a surface role; choose one '
                                f'of {", ".join(model.ROLES)}', keys)
        if role is not None and role != model.EXTERNAL and any(
                value is not authoring.KEEP
                for value in (edit.category, edit.patch_type)):
            raise _edit_refused(
                'needs_external_boundary',
                f'{authoring._name(document, key)} is {role}: purpose and '
                'mesh type belong to an external boundary', keys)
        if role is not None and role not in model.INTERFACE_ROLES and any(
                value is not authoring.KEEP for value in (
                    edit.partner, edit.own_region, edit.partner_region)):
            raise _edit_refused(
                'needs_interface',
                f'{authoring._name(document, key)} is {role}: partner and '
                'regions belong to a region interface', keys)
    if edit.category not in (authoring.KEEP, None) \
            and edit.category not in authoring.PURPOSES:
        raise _edit_refused('unknown_purpose',
                            f'"{edit.category}" is not a purpose; choose one '
                            f'of {", ".join(authoring.PURPOSES)}', keys)
    if edit.patch_type not in (authoring.KEEP, None) \
            and edit.patch_type not in MESH_PATCH_TYPES:
        raise _edit_refused('unknown_patch_type',
                            f'"{edit.patch_type}" is not a mesh type; choose '
                            f'one of {", ".join(MESH_PATCH_TYPES)}', keys)
    if len(keys) > 1 and any(value is not authoring.KEEP for value in (
            edit.own_region, edit.partner_region)):
        raise _edit_refused('partner_for_many',
                            'the regions of an interface are chosen for one '
                            'surface at a time', keys)
    refs = {ref for ref, _label in authoring.region_choices(document)}
    for value in (edit.own_region, edit.partner_region):
        if value not in (authoring.KEEP, None) and value not in refs:
            raise _edit_refused('unknown_region',
                                f'"{value}" is not a region; choose one of '
                                f'{", ".join(sorted(refs))}', keys)
    if edit.partner not in (authoring.KEEP, None, authoring.SHARED):
        edit.partner = str(edit.partner)


def plan(data, keys, edit) -> authoring.EditPlan:
    """The planner's verdict on *edit* of *keys*, as the tab asks it."""
    keys = [str(key) for key in keys]
    document = data.data()
    _check_edit_shape(document, keys, edit)
    try:
        return authoring.plan_edit(document, keys, edit)
    except authoring.EditRefused as error:
        raise refused(error, keys) from error


def write(data, keys, edit) -> authoring.EditPlan:
    """Plan *edit* and write it through the working copy *data*."""
    found = plan(data, keys, edit)
    authoring.write_plan(data, found)
    return found


def issue_dicts(found) -> list[dict]:
    return [{'surface': issue.surface, 'reason': issue.reason,
             'remedy': issue.remedy, 'text': issue.text()}
            for issue in found]


def edit_from_parameters(parameters: dict) -> tuple[list[str], authoring.SurfaceEdit]:
    """``geometry.surface_intent.edit`` parameters as the tab's edit."""
    parameters = dict(parameters or {})
    keys = parameters.get('geometry_ids')
    if isinstance(keys, (str, int)):
        keys = [keys]
    keys = [str(key) for key in (keys or [])]
    if not keys:
        raise ValidationFailedError('geometry_ids names no surface', details={
            'error': 'surface_edit_refused', 'code': 'no_surface',
            'geometry_ids': []})
    unknown = sorted(set(parameters) - set(_EDIT_KEYS) - {'geometry_ids'})
    if unknown:
        raise ValidationFailedError(
            f'unknown parameters: {", ".join(unknown)}', details={
                'error': 'unknown_parameters', 'parameters': unknown})
    edit = authoring.SurfaceEdit()
    if 'surface_role' in parameters:
        if parameters['surface_role'] in (None, ''):
            raise _edit_refused('no_role', 'a surface role cannot be '
                                'cleared; choose one of '
                                + ', '.join(model.ROLES), keys)
        edit.role = str(parameters['surface_role'])
    for name, attribute in (('boundary_category', 'category'),
                            ('mesh_patch_type', 'patch_type'),
                            ('partner', 'partner'),
                            ('own_region', 'own_region'),
                            ('partner_region', 'partner_region')):
        if name in parameters:
            value = parameters[name]
            setattr(edit, attribute, None if value in (None, '') else value)
    return keys, edit


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _snake(name: str) -> str:
    return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


_RELATION_FIELDS = None


def _relation_fields() -> dict:
    global _RELATION_FIELDS
    if _RELATION_FIELDS is None:
        from .field_adapters import EntityAdapter
        from .fields import REGISTRY
        adapter = EntityAdapter(REGISTRY.collections['geometry.surface_relations'])
        _RELATION_FIELDS = {field.relative_path: field.descriptor
                            for field in adapter.fields.values()}
    return _RELATION_FIELDS


def relation_payload(key, relation: dict) -> dict:
    """A relation as ``geometry.surface_relations`` publishes it: snake-case
    ids, values typed as the descriptors say (SimpleDB keeps text)."""
    from .field_adapters import typed_value
    descriptors = _relation_fields()
    row = {'entity_id': str(key)}
    for name, value in (relation or {}).items():
        value = getattr(value, 'value', value)
        descriptor = descriptors.get(str(name))
        if descriptor is not None:
            value = typed_value(descriptor, value)
            row[descriptor.id.rsplit('/', 1)[-1]] = value
        else:
            row[_snake(str(name))] = value
    return row


def surface_payload(document, key) -> dict:
    row = authoring._row(document, key) or {}
    payload = {
        'geometry_id': str(key), 'name': model._text(row.get('name')),
        'surface_role': model._text(row.get('surfaceRole')) or None,
        'boundary_category':
            model._text(row.get('boundaryCategory')) or None,
        'mesh_patch_type': model._text(row.get('meshPatchType')) or None,
        'intent_origin': model._text(row.get('intentOrigin')) or None,
        'intent_state': model._text(row.get('intentState')) or None,
        'intent_issue': model._text(row.get('intentIssue')) or None,
        'partner': None, 'own_region': None, 'partner_region': None,
        'interface_relation': None, 'periodic_relation': None,
        'unchecked_patch_type': model._text(row.get('meshPatchType'))
        in authoring.UNCHECKED_PATCH_TYPES,
    }
    relation_key, relation = authoring.interface_relation(document, key)
    if relation is not None:
        own, other = authoring.regions_of(relation, key)
        payload.update(partner=authoring.partner_of(relation, key),
                       own_region=own or None, partner_region=other or None,
                       interface_relation=str(relation_key))
    periodic_key, _periodic = authoring.periodic_relation(document, key)
    if periodic_key is not None:
        payload['periodic_relation'] = str(periodic_key)
    return payload


def read(document, keys=None) -> dict:
    """What the surface tab and the pair tables show of the role model."""
    geometry = authoring._geometry(document)
    if keys:
        keys = [str(key) for key in keys]
        missing = [key for key in keys
                   if not authoring._is_surface(geometry.get(key))]
        if missing:
            raise ValidationFailedError(
                f'not a surface: {", ".join(missing)}', details={
                    'error': 'surface_edit_refused', 'code': 'not_a_surface',
                    'geometry_ids': missing})
    else:
        keys = [str(key) for key, row in model._sorted_items(geometry)
                if authoring._is_surface(row)]
    section = document.get('surfaceIntent')
    section = section if isinstance(section, dict) else {}
    surfaces = []
    for key in keys:
        payload = surface_payload(document, key)
        payload['issues'] = issue_dicts(authoring.all_issues(document, [key]))
        surfaces.append(payload)
    relations = [relation_payload(key, relation) for key, relation
                 in model._sorted_items(document.get('surfaceRelations'))
                 if isinstance(relation, dict)]
    return {
        'model_version': section.get('modelVersion'),
        'conversion_report': model.conversion_report(document),
        'surfaces': surfaces,
        'relations': relations,
        'choices': {
            'surface_roles': list(model.ROLES),
            'purposes': list(authoring.PURPOSES),
            'mesh_patch_types': list(MESH_PATCH_TYPES),
            'unchecked_patch_types': list(authoring.UNCHECKED_PATCH_TYPES),
            'default_patch_types': {purpose: authoring.default_patch_type(
                purpose) for purpose in authoring.PURPOSES},
            'regions': [{'ref': ref, 'label': label} for ref, label
                        in authoring.region_choices(document)],
            'shared_partner': authoring.SHARED,
        },
    }


def interface_rows(document, route, target, built=None) -> list[dict]:
    """``intent_report`` for the region interfaces the project holds.

    One row per interface relation: ``intent`` (conformal or
    non_conformal), whether this route and target can honour it, whether
    the build will couple it (``will_couple``), ``coupled`` (``None``
    until *built* says) and ``state``.
    """
    from foammesh.core.engine.interface_intent import (
        CONFORMAL, NON_CONFORMAL, intent_report)
    pairs = []
    for key, relation in model._sorted_items(document.get('surfaceRelations')):
        if not isinstance(relation, dict):
            continue
        kind = model._text(relation.get('kind'))
        if kind not in model.INTERFACE_ROLES:
            continue
        pairs.append({
            'name': (model._text(relation.get('name'))
                     or model._text(relation.get('relationId')) or str(key)),
            'coupling': (NON_CONFORMAL if kind == model.NON_CONFORMAL
                         else CONFORMAL)})
    # The engine registry's ids are the route names (snappy, gmsh).
    return intent_report(route, target, pairs, built)
