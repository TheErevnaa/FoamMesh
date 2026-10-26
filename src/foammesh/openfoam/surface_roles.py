"""Plan 49 SR4: the surface roles the snappyHexMesh route lowers.

The SR1 model (``db/surface_intent.py``) is the one store of what each
surface is for: its role, its purpose and mesh type when it is an external
boundary, and the relations that join two sides. This module reads that store
for the snappy writer and the authored export, so neither reads the legacy
``cfdType`` and the two interface switches any more.

A project opened in the application is migrated when it is loaded; a
configuration built in memory, or saved before Plan 49 and read without the
application, may not be. The reader migrates a *copy* of the document, so a
project that was never assessed meshes from the same conversion it would be
given when opened, and nothing is written back.

Everything here is engine-agnostic and imports no Qt; the Gmsh route can use
the same reader.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from foammesh.db import surface_intent as model

ROUTE = 'snappyHexMesh'


class SurfaceRoleRefusal(ValueError):
    """A construction this route cannot build, named and explained.

    Plan 49 D7: execution is blocked with the surface (or relation), the
    route, the reason and the remedy. Raised as a ``ValueError`` so every
    caller that already reports a dictionary refusal reports this one.
    """

    def __init__(self, surface: str, reason: str, remedy: str, *,
                 route: str = ROUTE, subject: str = 'Surface'):
        self.surface = str(surface)
        self.reason = str(reason).rstrip('.')
        self.remedy = str(remedy).strip()
        self.route = route
        self.subject = subject
        super().__init__(
            f'{subject} {self.surface}: {self.reason} ({route} route). '
            f'{self.remedy}'.strip())


def plain_document(db) -> dict:
    """The configuration as plain data, copied; ``{}`` when there is none."""
    document = None
    try:
        document = db.data()
    except Exception:  # noqa: BLE001 - a db with no document
        document = None
    if not isinstance(document, dict):
        try:
            import yaml
            document = yaml.safe_load(db.toYaml())
        except Exception:  # noqa: BLE001
            document = None
    return copy.deepcopy(document) if isinstance(document, dict) else {}


@dataclass
class EffectiveIntent:
    """The model as the meshers must read it, and which rows it assessed."""

    document: dict
    #: Row keys this reader assessed in memory (the stored row had no role
    #: and no state). Their purpose is the one the preparation recorded.
    assessed_here: set = field(default_factory=set)

    @property
    def geometry(self) -> dict:
        value = self.document.get('geometry')
        return value if isinstance(value, dict) else {}

    @property
    def relations(self) -> dict:
        value = self.document.get('surfaceRelations')
        return value if isinstance(value, dict) else {}

    def row(self, key):
        if key is None:
            return None
        geometry = self.geometry
        row = geometry.get(str(key))
        if row is None:
            row = geometry.get(key)
        return row if isinstance(row, dict) else None

    def relations_of(self, key) -> list[dict]:
        return [relation for _key, relation
                in model.relations_of(self.document, key)]

    def role(self, key) -> str | None:
        row = self.row(key)
        if row is None:
            return None
        return model._text(row.get('surfaceRole')) or None

    def state(self, key) -> str:
        row = self.row(key)
        return model._text((row or {}).get('intentState'))

    def issue(self, key) -> str:
        row = self.row(key)
        return model._text((row or {}).get('intentIssue'))

    def origin(self, key) -> str:
        row = self.row(key)
        return model._text((row or {}).get('intentOrigin'))


def effective_intent(db) -> EffectiveIntent:
    document = plain_document(db)
    geometry = document.get('geometry')
    before = set()
    if isinstance(geometry, dict):
        before = {
            str(key) for key, row in geometry.items()
            if model._is_surface(row)
            and not model._text(row.get('surfaceRole'))
            and not model._text(row.get('intentState'))}
    if document:
        model.migrate_surface_intent(document)
        model.refresh_surface_intent(document)
    return EffectiveIntent(document, before)


#: The mesh types a purpose maps to when the purpose was inferred rather than
#: authored. ``symmetry`` is OpenFOAM's ``symmetry``: a snapped surface is
#: never exactly planar, and ``symmetryPlane`` refuses it (DP-5764, SR0
#: probe 5).
_TYPE_FOR_CATEGORY = {'wall': 'wall', 'symmetry': 'symmetry',
                      'wedge': 'wedge', 'empty': 'empty'}


def type_for_category(category) -> str:
    return _TYPE_FOR_CATEGORY.get(str(category or '').lower(), 'patch')


def relation_label(relation: dict, fallback: str = '') -> str:
    return (model._text(relation.get('name'))
            or model._text(relation.get('relationId')) or fallback)


def relation_transform(relation: dict) -> dict:
    """The transform of a periodic relation, in the createNonConformalCouples
    vocabulary. The SR1 convention (as the Gmsh publisher reads it) is the
    owner-to-neighbour transform: owner + translation = neighbour."""
    kind = model._text(relation.get('transform')) or 'coincident'

    def number(name, default=0.0):
        try:
            return float(model._value(relation.get(name, default)))
        except (TypeError, ValueError):
            return float(default)

    if kind == 'translational':
        return {'transform': 'translational',
                'separation': [number('translationX'), number('translationY'),
                               number('translationZ')]}
    if kind == 'rotational':
        return {'transform': 'rotational',
                'rotationAxis': [number('rotationAxisX'),
                                 number('rotationAxisY'),
                                 number('rotationAxisZ', 1.0)],
                'rotationCentre': [number('rotationCentreX'),
                                   number('rotationCentreY'),
                                   number('rotationCentreZ')],
                'rotationAngle': number('rotationAngleDegrees')}
    return {'transform': kind}
