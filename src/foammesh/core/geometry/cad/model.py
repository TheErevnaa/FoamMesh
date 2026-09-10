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


@dataclass
class CadFace:
    id: str
    patch_uuid: str = field(default_factory=lambda: str(uuid4()))
    source_ref: dict = field(default_factory=dict)
    name: str = ''
    # patch name to assign to triangles originating from this face (defaults to name/id)
    patch: str = ''
    color: str = ''          # '#rrggbb' from the CAD file, if present (XDE)

    def __post_init__(self):
        if not self.patch:
            self.patch = self.name or self.id


@dataclass
class CadBody:
    id: str
    name: str = ''
    faces: list[CadFace] = field(default_factory=list)
    color: str = ''

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
