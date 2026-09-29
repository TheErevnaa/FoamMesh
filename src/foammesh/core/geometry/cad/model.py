#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CAD assembly model (pure data; no OCCT needed).

A normalized view of an imported CAD shape — bodies (solids) and their faces with
preserved names — that the GUI tree, patch-naming, and tessellation use. The OCCT
importer (cad_importer.py) builds this from a TopoDS_Shape / XDE document.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import uuid4

#: Plan 35 CR7. What a CAD import answers when it is read in another process.
#: The major number is the contract: a reader refuses a major it does not
#: know rather than guess at fields it has never seen.
CAD_IMPORT_SCHEMA = 'cad_import/v1'
CAD_IMPORT_MAJOR = 1


class CadSchemaError(ValueError):
    """A CAD import answer written under a contract this build cannot read."""


def schema_major(schema) -> int:
    """The major number of a ``cad_import/v<major>[.<minor>]`` tag."""
    text = str(schema or '')
    name, _slash, version = text.partition('/v')
    if name != 'cad_import' or not version:
        raise CadSchemaError(f'not a CAD import answer: {text!r}')
    try:
        return int(version.split('.', 1)[0])
    except ValueError:
        raise CadSchemaError(f'not a CAD import answer: {text!r}') from None


@dataclass
class CadFace:
    id: str
    patch_uuid: str = field(default_factory=lambda: str(uuid4()))
    source_ref: dict = field(default_factory=dict)
    name: str = ''
    # patch name to assign to triangles originating from this face (defaults to name/id)
    patch: str = ''
    color: str = ''          # '#rrggbb' from the CAD file, if present (XDE)
    # DP-92. What the OCCT face answers about itself. The boundary-layer page
    # authors a layer selection, and whether a selection meshes turns on two
    # things nothing downstream of here used to carry: whether a patch left
    # uncovered is flat, because the rebuild closes that opening with a plane
    # (DP-90), and whether two patches that share an edge grow their layers in
    # opposite directions, because a shared edge cannot be extruded twice
    # (DP-91). `None` means nobody measured -- the STL route does not.
    planar: bool | None = None
    area: float | None = None
    #: Ids of the faces this one shares an edge with, anywhere in the model.
    adjacent_ids: tuple[str, ...] = ()
    #: The id of this face's twin in another solid, where the file carries an
    #: interface as two coincident faces. Healing merges the pair and keeps
    #: the one the walk saw first, so a selection has to name that one.
    interface_id: str = ''
    #: Position in the importer's flat face walk, which is the order the
    #: mesher reads the file in and so which twin survives the merge.
    face_order: int = -1

    def __post_init__(self):
        if not self.patch:
            self.patch = self.name or self.id


@dataclass
class CadBody:
    id: str
    name: str = ''
    faces: list[CadFace] = field(default_factory=list)
    color: str = ''
    #: DP-900. True for a closed solid, False for faces or an open shell
    #: that bound nothing, None where OCCT was not asked.
    solid: bool | None = None

    @property
    def n_faces(self) -> int:
        return len(self.faces)


@dataclass
class CadModel:
    source_format: str = ''
    #: The unit the coordinates are actually in. R193: for STEP and IGES
    #: that is the unit the OCCT reader emits, not the one the file declares.
    unit: str = 'mm'
    #: What the file said it was written in. Kept as provenance so a scale
    #: surprise can be traced to the source instead of guessed at.
    declared_unit: str = ''
    bodies: list[CadBody] = field(default_factory=list)
    # Plan 26 WP7.3 removed `healing_report`: it was read into the artifact
    # record on import and never written by anything, so every entry carried
    # an empty dict that read as "healing produced no findings". The healed
    # report now lives on the store entry instead, written where the healing
    # actually happens.

    @property
    def n_bodies(self) -> int:
        return len(self.bodies)

    @property
    def n_faces(self) -> int:
        return sum(b.n_faces for b in self.bodies)

    def patch_names(self) -> list[str]:
        return [f.patch for b in self.bodies for f in b.faces]

    # -- Plan 35 CR7: the model crosses a process boundary as JSON ---------- #

    def to_json(self) -> dict:
        """Every field of the model, as plain JSON, under the schema tag."""
        return {
            'schema': CAD_IMPORT_SCHEMA,
            'source_format': self.source_format, 'unit': self.unit,
            'declared_unit': self.declared_unit,
            'bodies': [{
                'id': body.id, 'name': body.name, 'color': body.color,
                'solid': body.solid,
                'faces': [{
                    'id': face.id, 'patch_uuid': face.patch_uuid,
                    'source_ref': dict(face.source_ref), 'name': face.name,
                    'patch': face.patch, 'color': face.color,
                    'planar': face.planar, 'area': face.area,
                    'adjacent_ids': list(face.adjacent_ids),
                    'interface_id': face.interface_id,
                    'face_order': face.face_order,
                } for face in body.faces],
            } for body in self.bodies],
        }

    @classmethod
    def from_json(cls, document: dict) -> 'CadModel':
        """The model :meth:`to_json` wrote; refuses an unknown major."""
        if not isinstance(document, dict):
            raise CadSchemaError('a CAD model answer must be an object')
        major = schema_major(document.get('schema'))
        if major != CAD_IMPORT_MAJOR:
            raise CadSchemaError(
                f'CAD import answer {document.get("schema")!r} is a version '
                f'this build cannot read (it reads v{CAD_IMPORT_MAJOR})')
        bodies = []
        for body in document.get('bodies') or ():
            faces = [CadFace(
                id=str(face['id']), patch_uuid=str(face['patch_uuid']),
                source_ref=dict(face.get('source_ref') or {}),
                name=str(face.get('name') or ''),
                patch=str(face.get('patch') or ''),
                color=str(face.get('color') or ''),
                planar=face.get('planar'), area=face.get('area'),
                adjacent_ids=tuple(str(item) for item in
                                   face.get('adjacent_ids') or ()),
                interface_id=str(face.get('interface_id') or ''),
                face_order=int(face.get('face_order', -1)),
            ) for face in body.get('faces') or ()]
            bodies.append(CadBody(
                id=str(body['id']), name=str(body.get('name') or ''),
                faces=faces, color=str(body.get('color') or ''),
                solid=body.get('solid')))
        return cls(source_format=str(document.get('source_format') or ''),
                   unit=str(document.get('unit') or 'mm'),
                   declared_unit=str(document.get('declared_unit') or ''),
                   bodies=bodies)
