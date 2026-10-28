"""Plan 49 SR2. One edit of a surface's role, purpose, mesh type and partner.

The surface tab edits the SR1 model (``foammesh.db.surface_intent``) and
nothing else: ``surfaceRole``, ``boundaryCategory``, ``meshPatchType`` on the
row, and the one ``surfaceRelations`` record that couples it to its partner.
This module decides what such an edit writes, refuses the edits that are
structurally impossible (plan section 3) and lets the dialog preview the
validation verdict before anything is committed.

It works on the plain document (``db.data()``) so the dialog's preview, the
dialog's commit and the tests share one implementation. :func:`plan_edit`
returns an :class:`EditPlan`; :func:`apply_plan` applies it to a plain
document (the preview), and the dialog writes the same plan through its
working copy.

Legacy fields. Until SR4/SR5 switch the writers to the role model, the
meshers, the export and a few pages still read ``cfdType`` and the
``nonConformal``/``interRegion`` switches. They are no longer something a
person sets: :func:`legacy_fields` derives them from the role, in the same
transaction, so the readers that have not moved yet see the surface the
person described. Nothing reads them back into the role.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from . import surface_intent as model
from .surface_intent import (BAFFLE, CONFORMAL, EXTERNAL, INTERFACE_ROLES,
                             INTERNAL, NON_CONFORMAL, PERIODIC, ROLES,
                             SURROUNDING_REGION, UNRESOLVED)

#: "Leave this as it is" -- the value of a control that shows a mixed
#: multi-selection and was not touched.
KEEP = object()

#: The partner of a conformal interface made of one surface: the mesh splits
#: it into its two region patches.
SHARED = 'shared'

#: The purposes the surface tab offers, in the order it lists them. ``interface``
#: is a legacy category migration may have stored; it is shown when stored,
#: never offered.
PURPOSES = ('wall', 'inlet', 'outlet', 'far_field', 'symmetry', 'wedge',
            'unclassified')

#: Mesh types that need geometry no edit-time check can confirm: ``empty``
#: needs a one-cell-thick planar pair, ``wedge`` a one-cell revolved sector.
#: Gmsh checks them at publication, snappy cannot build them (SR0 probe 5),
#: so the surface tab marks them as not checked.
UNCHECKED_PATCH_TYPES = ('empty', 'wedge')


def default_patch_type(category) -> str:
    """The mesh type a purpose normally takes (plan section 3.1).

    Wall is a ``wall``; inlet, outlet, far field and unclassified are a plain
    ``patch``. ``symmetry`` is the OpenFOAM ``symmetry`` type -- not
    ``symmetryPlane``, which is a different type a person may still choose.
    """
    return {'wall': 'wall', 'symmetry': 'symmetry',
            'wedge': 'wedge'}.get(str(category or ''), 'patch')


class EditRefused(ValueError):
    """An edit the model cannot hold: nothing is written.

    Plan 49 SR8. ``code`` names the rule (the local API returns it beside
    the sentence the dialog shows); ``conflict`` carries the surface, pair,
    reason and remedy when the rule is the periodic side rule
    (``surface_intent.surface_role_conflict``).
    """

    #: The codes, one per rule :func:`plan_edit` enforces.
    CODES = ('not_a_surface', 'unknown_role', 'no_role',
             'surface_role_conflict', 'partner_for_many', 'own_partner',
             'partner_not_surface', 'partner_periodic', 'partner_taken',
             'sides_differ')

    def __init__(self, message, *, code: str = 'refused', conflict=None):
        super().__init__(message)
        self.code = code
        self.conflict = dict(conflict) if conflict else None


@dataclass
class SurfaceEdit:
    """What the surface tab asks for. ``KEEP`` leaves a value as it is."""
    role: object = KEEP
    category: object = KEEP
    patch_type: object = KEEP
    #: A geometry row key, :data:`SHARED`, ``None`` (no partner) or KEEP.
    partner: object = KEEP
    #: Region references (``geometry:<key>`` or ``surrounding``) on this
    #: surface's side and on the partner's side, or KEEP.
    own_region: object = KEEP
    partner_region: object = KEEP


@dataclass
class EditPlan:
    #: ``{row key: {field: value}}`` for geometry rows.
    rows: dict = field(default_factory=dict)
    #: ``{relation key: {field: value}}`` for existing relations.
    relation_updates: dict = field(default_factory=dict)
    #: Whole new relation records, in order.
    relation_new: list = field(default_factory=list)
    #: Relation keys to remove.
    relation_removed: list = field(default_factory=list)
    #: ``interfacePairs`` keys to switch off (the legacy pair a removed
    #: relation was converted from, which the meshers still read).
    pairs_disabled: list = field(default_factory=list)
    #: Every row the edit touched or left without its relation, for the
    #: preview to report on.
    affected: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def _geometry(document) -> dict:
    geometry = (document or {}).get('geometry')
    return geometry if isinstance(geometry, dict) else {}


def _row(document, key):
    geometry = _geometry(document)
    row = geometry.get(str(key))
    if row is None:
        try:
            row = geometry.get(int(key))
        except (TypeError, ValueError):
            row = None
    return row


def _is_surface(row) -> bool:
    return isinstance(row, dict) and model._text(row.get('gType')) == 'surface'


def _name(document, key) -> str:
    row = _row(document, key)
    return model._text(row.get('name')) if isinstance(row, dict) else str(key)


def region_choices(document) -> list[tuple[str, str]]:
    """The regions a side of an interface can be in, as ``(ref, label)``.

    A region is the cell zone of a volume typed cell zone, or the region
    around a zone-carving surface (``surrounding``), as SR1 records them.

    DP-6062. And the regions of the mesh itself -- the seeded regions on
    snappy, the typed solids on Gmsh -- which are what the export splits
    into. With three of them (the CHT tube: two fluids and the steel
    between) a side has to be able to name each; offering only cell-zone
    volumes left the person ``wall`` and Surrounding for both interfaces.
    A mesh region whose name a cell-zone volume already offers is not
    offered twice.

    A body Split interfaces cut is offered too, zone or not: it records a
    point inside each body an interface joins, and names both as the
    interface's sides. So is any body a relation already names, so the side
    a relation holds is always one the editor can show.
    """
    named = set()
    for relation in ((document or {}).get('surfaceRelations') or {}).values():
        if isinstance(relation, dict):
            for side in ('ownerRegion', 'neighbourRegion'):
                key = model.region_key(relation.get(side))
                if key is not None:
                    named.add(str(key))
    choices = []
    for key, row in model._sorted_items(_geometry(document)):
        if (isinstance(row, dict)
                and model._text(row.get('gType')) == 'volume'
                and (model._text(row.get('cfdType')) == 'cellZone'
                     or model._flag(row.get('zoneInsidePointSet'))
                     or str(key) in named)):
            choices.append((model.region_ref(key),
                            model._text(row.get('name')) or str(key)))
    offered = {label for _ref, label in choices}
    for name in model.mesh_region_names(document):
        if name not in offered:
            choices.append((model.mesh_region_ref(name), name))
    choices.append((SURROUNDING_REGION, 'Surrounding'))
    return choices


def zone_region(document, key) -> str:
    """The region the cell zone a surface bounds stands for, or ``''``."""
    row = _row(document, key)
    if not isinstance(row, dict):
        return ''
    return model._zone_region(_geometry(document), row) or ''


def interface_relation(document, key):
    """``(relation key, relation)`` of a surface's region interface, or
    ``(None, None)``."""
    for relation_key, relation in model.relations_of(document, key):
        if model._text(relation.get('kind')) in INTERFACE_ROLES:
            return relation_key, relation
    return None, None


def periodic_relation(document, key):
    for relation_key, relation in model.relations_of(document, key):
        if model._text(relation.get('kind')) == PERIODIC:
            return relation_key, relation
    return None, None


def side_of(relation, key) -> str:
    """``'owner'`` or ``'neighbour'``: which side of *relation* row *key* is."""
    return ('owner' if str(relation.get('ownerSurface')) == str(key)
            else 'neighbour')


def partner_of(relation, key):
    """The partner a relation gives row *key*: a row key, SHARED or None."""
    if model._flag(relation.get('sharedSurface')):
        return SHARED
    other = 'neighbour' if side_of(relation, key) == 'owner' else 'owner'
    value = relation.get(f'{other}Surface')
    return None if value in (None, '') else str(value)


def regions_of(relation, key) -> tuple[str, str]:
    """``(own region, partner region)`` of row *key* in *relation*."""
    own = side_of(relation, key)
    other = 'neighbour' if own == 'owner' else 'owner'
    return (model._text(relation.get(f'{own}Region')),
            model._text(relation.get(f'{other}Region')))


def _relation_label(relation) -> str:
    return (model._text(relation.get('name'))
            or model._text(relation.get('relationId')) or 'a relation')


# --------------------------------------------------------------------------- #
# Legacy fields, derived from the role
# --------------------------------------------------------------------------- #

def legacy_fields(document, key, role) -> dict:
    """The ``cfdType`` and switches the not-yet-moved readers need for *role*.

    External boundary is ``boundary``, internal face ``none``. A conformal
    interface keeps a surface that bounds a cell zone as it was written
    (``boundary`` of a zoned volume, DP-387, or the split interface,
    DP-421), and is otherwise ``interface``. A non-conformal interface is
    ``interface`` with both switches. A blocking baffle is ``interface`` with
    the non-conformal switch only: the snappy writer then makes two
    uncoupled sides (``faceType boundary``) instead of the cyclic pair a
    bare ``interface`` turns into at export (DP-5784); the wall pair itself
    is SR4's.
    """
    row = _row(document, key) or {}
    current = model._text(row.get('cfdType')) or 'boundary'
    if role == EXTERNAL:
        cfd, switches = 'boundary', (False, False)
    elif role == INTERNAL:
        cfd, switches = 'none', (False, False)
    elif role == CONFORMAL:
        zoned = bool(zone_region(document, key))
        cfd = current if zoned and current in ('boundary', 'interface') \
            else 'interface'
        switches = (False, False)
    elif role == NON_CONFORMAL:
        cfd, switches = 'interface', (True, True)
    elif role == BAFFLE:
        cfd, switches = 'interface', (True, False)
    else:
        return {}
    values = {'cfdType': cfd, 'nonConformal': switches[0],
              'interRegion': switches[1]}
    if cfd != current:
        # What the old radios cleared with the type: a layer group belongs to
        # a boundary, the slave group to an interface's second side.
        if cfd != 'interface':
            values['slaveLayerGroup'] = None
        if cfd != 'boundary':
            values['layerGroup'] = None
    return values


# --------------------------------------------------------------------------- #
# Planning an edit
# --------------------------------------------------------------------------- #

def _role_fields(document, key, role, category, patch_type) -> dict:
    row = _row(document, key) or {}
    values = {'surfaceRole': role, 'intentOrigin': model.AUTHORED}
    if role == EXTERNAL:
        stored = model._text(row.get('boundaryCategory')) or None
        if category is KEEP:
            category = stored or model.category_for_name(
                model._text(row.get('name')), model.default_category(document))
        if patch_type is KEEP:
            stored_type = model._text(row.get('meshPatchType')) or None
            patch_type = stored_type or default_patch_type(category)
        values['boundaryCategory'] = category
        values['meshPatchType'] = patch_type
    else:
        values['boundaryCategory'] = None
        values['meshPatchType'] = None
    values.update(legacy_fields(document, key, role))
    return values


def plan_edit(document, keys, edit: SurfaceEdit) -> EditPlan:
    """What an edit of the surfaces *keys* writes, or :class:`EditRefused`.

    Refused, because the model cannot hold them (plan section 3):

    * a periodic side made anything but an external boundary -- a side is
      periodic, a region interface or a blocking baffle, never two of them;
    * a partner for more than one surface at once;
    * a non-conformal interface whose partner is the surface itself;
    * a partner that is not a surface, is periodic, or is already a side of
      another interface (the sides of an interface name each other, one
      relation each).

    Everything else is accepted, including an interface with no partner or
    no regions yet: that is a draft the project saves, marked unresolved
    with its reason (plan D7).
    """
    keys = [str(key) for key in keys]
    plan = EditPlan()
    affected = list(keys)
    for key in keys:
        if not _is_surface(_row(document, key)):
            raise EditRefused(f'geometry {key} is not a surface',
                              code='not_a_surface')

    untouched = all(value is KEEP for value in (
        edit.role, edit.category, edit.patch_type, edit.partner,
        edit.own_region, edit.partner_region))
    for key in keys:
        if untouched:
            break
        row = _row(document, key)
        role = edit.role
        if role is KEEP:
            role = model._text(row.get('surfaceRole')) or None
        if role is not None and role not in ROLES:
            raise EditRefused(f'"{role}" is not a surface role',
                              code='unknown_role')
        if role is None:
            if any(value is not KEEP for value in (
                    edit.category, edit.patch_type, edit.partner)):
                raise EditRefused(
                    f'{_name(document, key)} has no role: choose one first',
                    code='no_role')
            continue
        _periodic_key, periodic = periodic_relation(document, key)
        if periodic is not None and role != EXTERNAL:
            pair = _relation_label(periodic)
            raise EditRefused(
                f'{_name(document, key)} is one side of the periodic pair '
                f'"{pair}". A periodic side stays an '
                'external boundary; remove it from the pair first.',
                code='surface_role_conflict', conflict={
                    'surface': _name(document, key), 'pair': pair,
                    'reason': f'it would become {role} while it is a side '
                              'of this pair',
                    'remedy': f'Remove the periodic pair "{pair}" first, or '
                              'keep the surface an external boundary.'})
        category = edit.category
        patch_type = edit.patch_type
        if role == EXTERNAL and category is not KEEP and patch_type is KEEP:
            stored_category = model._text(row.get('boundaryCategory')) or None
            stored_type = model._text(row.get('meshPatchType')) or None
            # A purpose change carries the mesh type with it, unless the type
            # had been chosen apart from the old purpose's normal one.
            if stored_type in (None, default_patch_type(stored_category)):
                patch_type = default_patch_type(category)
        plan.rows[key] = _role_fields(document, key, role, category,
                                      patch_type)

        relation_key, relation = interface_relation(document, key)
        if role not in INTERFACE_ROLES:
            if relation is not None:
                _remove_relation(document, plan, relation_key, relation, key,
                                 affected)
            continue
        _plan_interface(document, plan, keys, key, role, edit, relation_key,
                        relation, affected)

    plan.affected = list(dict.fromkeys(affected))
    if not untouched:
        # Plan 49 SR7 (SR2 note, DP-5832). The other direction of the
        # periodic side rule, asked on the edited copy before anything is
        # written: a side of a periodic pair -- a relation or an affine Gmsh
        # pair -- stays an external boundary.
        trial = copy.deepcopy(document)
        apply_plan(trial, plan)
        for key in keys:
            conflict = model.surface_role_conflict(trial, key)
            if conflict is not None:
                raise EditRefused(conflict['message'],
                                  code='surface_role_conflict',
                                  conflict=conflict)
    return plan


def _remove_relation(document, plan, relation_key, relation, key, affected):
    if relation_key in plan.relation_removed:
        return
    plan.relation_updates.pop(relation_key, None)
    plan.relation_removed.append(relation_key)
    source = model._text(relation.get('legacySource'))
    if source.startswith('interfacePairs/'):
        plan.pairs_disabled.append(source.split('/', 1)[1])
    partner = partner_of(relation, key)
    if partner not in (None, SHARED):
        affected.append(partner)


def _plan_interface(document, plan, keys, key, role, edit, relation_key,
                    relation, affected):
    if relation_key in plan.relation_removed:
        # Its other side, edited in the same selection, already let it go.
        relation_key, relation = None, None
    current_partner = partner_of(relation, key) if relation else None
    partner = current_partner if edit.partner is KEEP else edit.partner
    if partner not in (None, SHARED):
        partner = str(partner)
    if edit.partner is not KEEP and len(keys) > 1:
        raise EditRefused('a partner pairs one surface: choose it for one '
                          'surface at a time', code='partner_for_many')
    if partner == key or (partner == SHARED and role == NON_CONFORMAL):
        if role == NON_CONFORMAL:
            raise EditRefused(
                f'{_name(document, key)} cannot be its own partner: a '
                'non-conformal interface joins two different surfaces',
                code='own_partner')
        partner = SHARED
    if partner not in (None, SHARED):
        other = _row(document, partner)
        if not _is_surface(other):
            raise EditRefused(f'the partner {partner} is not a surface',
                              code='partner_not_surface')
        _key, periodic = periodic_relation(document, partner)
        if periodic is not None:
            raise EditRefused(
                f'{_name(document, partner)} is one side of the periodic '
                f'pair "{_relation_label(periodic)}" and cannot also be a '
                'side of an interface', code='partner_periodic')
        other_key, other_relation = interface_relation(document, partner)
        if other_relation is not None and other_key != relation_key:
            raise EditRefused(
                f'{_name(document, partner)} is already one side of the '
                f'interface "{_relation_label(other_relation)}". Each side of '
                'an interface names the other; change that interface first.',
                code='partner_taken')
        if partner in plan.rows and plan.rows[partner].get(
                'surfaceRole') != role:
            raise EditRefused('the two sides of an interface must have the '
                              'same role', code='sides_differ')

    if partner is None:
        # A role with no partner yet: a draft. A relation it had loses its
        # partner and goes with it.
        if relation is not None:
            _remove_relation(document, plan, relation_key, relation, key,
                             affected)
        return

    own_default, partner_default = (regions_of(relation, key) if relation
                                    else ('', ''))
    if not own_default:
        own_default = zone_region(document, key)
    if partner != current_partner or not partner_default:
        partner_default = (SURROUNDING_REGION if partner == SHARED
                           else zone_region(document, partner)
                           or partner_default)
    own_region = own_default if edit.own_region is KEEP else (
        edit.own_region or '')
    partner_region = partner_default if edit.partner_region is KEEP else (
        edit.partner_region or '')

    if current_partner not in (None, SHARED, partner):
        affected.append(current_partner)  # left with a role and no relation
    if partner != SHARED:
        affected.append(partner)
        if partner not in plan.rows:
            plan.rows[partner] = _role_fields(document, partner, role, KEEP,
                                              KEEP)

    row = _row(document, key)
    shared = partner == SHARED
    if relation is not None:
        own = side_of(relation, key)
        other = 'neighbour' if own == 'owner' else 'owner'
        partner_row = row if shared else _row(document, partner)
        partner_key = key if shared else partner
        plan.relation_updates[relation_key] = {
            'kind': role,
            'sharedSurface': shared,
            f'{other}Surface': int(partner_key),
            f'{other}PatchUuid': model._text(partner_row.get('patchUuid')),
            f'{other}Label': model._text(partner_row.get('name')),
            f'{own}Region': own_region,
            f'{other}Region': partner_region,
            'origin': model.AUTHORED,
        }
        return
    partner_key = key if shared else partner
    partner_row = row if shared else _row(document, partner)
    record = model._relation(
        role, owner_key=key, owner_row=row, neighbour_key=partner_key,
        neighbour_row=partner_row, shared=shared, owner_region=own_region,
        neighbour_region=partner_region, name=model._text(row.get('name')))
    record['ownerSurface'] = int(key)
    record['neighbourSurface'] = int(partner_key)
    record['origin'] = model.AUTHORED
    plan.relation_new.append(record)


# --------------------------------------------------------------------------- #
# Applying and previewing
# --------------------------------------------------------------------------- #

def apply_plan(document, plan: EditPlan) -> None:
    """Apply *plan* to a plain document, in place, and refresh validation."""
    for key, values in plan.rows.items():
        row = _row(document, key)
        row.update(values)
    relations = document.get('surfaceRelations')
    if not isinstance(relations, dict):
        relations = document['surfaceRelations'] = {}
    for relation_key in plan.relation_removed:
        relations.pop(relation_key, None)
    for relation_key, values in plan.relation_updates.items():
        relations[relation_key].update(values)
    for record in plan.relation_new:
        relations[model._next_key(relations)] = dict(record)
    pairs = document.get('interfacePairs')
    for pair_key in plan.pairs_disabled:
        if isinstance(pairs, dict):
            pair = pairs.get(pair_key)
            if pair is None:
                try:
                    pair = pairs.get(int(pair_key))
                except ValueError:
                    pair = None
            if isinstance(pair, dict):
                pair['enabled'] = False
    model.refresh_surface_intent(document)


def write_plan(db, plan: EditPlan) -> None:
    """Write *plan* through an editable ``SimpleDB`` working copy.

    The same plan :func:`apply_plan` previews; the commit hook
    (``Configurations.commit``) recomputes the validation state.
    """
    for key, values in plan.rows.items():
        element = db.checkout(f'geometry/{key}')
        for name, value in values.items():
            element.setValue(name, value)
        db.commit(element)
    for relation_key in plan.relation_removed:
        db.removeElement('surfaceRelations', relation_key)
    for relation_key, values in plan.relation_updates.items():
        for name, value in values.items():
            db.setValue(f'surfaceRelations/{relation_key}/{name}', value)
    for record in plan.relation_new:
        element = db.newElement('surfaceRelations')
        for name, value in record.items():
            element.setValue(name, value)
        db.addElement('surfaceRelations', element)
    for pair_key in plan.pairs_disabled:
        db.setValue(f'interfacePairs/{pair_key}/enabled', False)


@dataclass
class Issue:
    surface: str
    reason: str
    remedy: str

    def text(self) -> str:
        return f'{self.surface}: {self.reason}. {self.remedy}'.strip()


_REMEDIES = (
    ('needs a mesh type', 'Choose a mesh type.'),
    ('needs its relation', 'Choose the partner and the region on each side.'),
    ('regions on its two sides are not recorded',
     'Choose the region on each side.'),
    ('both sides are in the same region', 'Choose two different regions.'),
    ('no longer exists', 'Choose the region on each side again.'),
    ('was deleted', 'Choose the partner again.'),
    ('was replaced', 'Choose the partner again.'),
    ('more than one relation', 'Give each side one partner.'),
    ('has no role yet', 'Give the partner a role.'),
)


def remedy_for(reason: str) -> str:
    for fragment, remedy in _REMEDIES:
        if fragment in reason:
            return remedy
    return 'Change the role or the partner.'


def issues(document, keys) -> list[Issue]:
    """The unresolved state of surfaces *keys*, each with its remedy.

    A surface whose relation is unresolved reports the relation's reason, so
    the person reads what is wrong rather than "its relation is unresolved".
    """
    found = []
    for key in keys:
        row = _row(document, key)
        if not _is_surface(row):
            continue
        if model._text(row.get('intentState')) != UNRESOLVED:
            continue
        issue = model._text(row.get('intentIssue'))
        if not model._text(row.get('surfaceRole')):
            # A migration repair item: its text carries reason and remedy.
            found.append(Issue(_name(document, key), issue.rstrip('.'), ''))
            continue
        reasons = [part for part in issue.split('; ')
                   if part and part != 'its relation is unresolved']
        for _relation_key, relation in model.relations_of(document, key):
            if model._text(relation.get('state')) == UNRESOLVED:
                reasons.extend(
                    model._text(relation.get('stateReason')).split('; '))
        reasons = [part for part in dict.fromkeys(reasons) if part]
        if not reasons:
            reasons = [issue]
        reason = '; '.join(reasons)
        found.append(Issue(_name(document, key), reason, remedy_for(reason)))
    return found


def capability_issues(document, keys) -> list[Issue]:
    """Plan 49 SR7. What the configured route and target cannot build of
    surfaces *keys* -- the verdict the run and the export give, word for
    word (``core/engine/surface_capabilities``), shown while editing. The
    draft is still saved (plan D7)."""
    from foammesh.core.engine import surface_capabilities as capabilities
    route, target = capabilities.route_target_of(document)
    if route not in capabilities.ROUTES:
        return []
    found = []
    for issue in capabilities.validate(document, route, target, stage='edit',
                                       keys=[str(key) for key in keys]):
        if issue.code == capabilities.UNRESOLVED:
            continue        # :func:`issues` says those, with their remedy
        found.append(Issue(issue.subject, issue.reason.rstrip('. '),
                           issue.remedy))
    return found


def all_issues(document, keys) -> list[Issue]:
    """The unresolved state and the capability verdict of *keys*."""
    return issues(document, keys) + capability_issues(document, keys)


def preview(document, keys, edit: SurfaceEdit) -> list[Issue]:
    """The issues the surfaces would carry after *edit*, without writing.

    Raises :class:`EditRefused` for an edit the model cannot hold.
    """
    trial = copy.deepcopy(document)
    plan = plan_edit(trial, keys, edit)
    apply_plan(trial, plan)
    return all_issues(trial, plan.affected)
