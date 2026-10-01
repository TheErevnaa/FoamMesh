#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Refinement levels and cell zones for a locally loaded volume (Plan 37 UF9).

The section worker reads ``cellLevel`` and ``cellZones`` with the cells it
cuts. A volume the window loaded itself (the full volume, or a small mesh
loaded whole) came from the VTK reader, which carries neither. This module
reads the two from the case on disk so the local cut can be coloured by them
too -- and only when they are the same mesh as the cells on screen:

* the values are bound to a **mesh revision**: the stat lease of the
  polyMesh's own members (``points``, ``faces``, ``owner``, ``neighbour``,
  ``boundary``) taken around the read (`mesh_revision`). A read during which
  the mesh or the field was rewritten is refused, never half-used;
* the volume on screen is bound to the revision first seen for it
  (`bindVolume`, stamped on the dataset itself so it goes when the dataset
  does). A mesh rewritten on disk after the volume was loaded refuses the
  colour with a reason rather than painting the old cells with new numbers;
* the values are one per cell of the mesh: they are only put on a volume
  dataset with exactly that many cells (the same count test
  ``MeshActor.attachQualityField`` relies on -- the reader keeps the
  polyMesh's cell order). A cell zone shown on its own, or another region,
  is a different set of cells and stays uncoloured;
* a field the case does not have is a reason, never zeros.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from foammesh.core.section import section_colour

#: The colour choices read from the case, and the polyMesh member each reads.
MEMBERS = {section_colour.CELL_LEVEL: 'cellLevel',
           section_colour.CELL_ZONE: 'cellZones'}

#: Field data on a volume dataset: the mesh revision it was first seen at,
#: and, per array, the revision the attached values were read at.
SEEN_REVISION = 'foammeshMeshRevision'
ARRAY_REVISION = 'foammeshRevision.'

READ = 'read'
REFUSED = 'refused'


@dataclass(frozen=True)
class CaseCellField:
    """One per-cell field read from the case, or why not."""

    key: str
    status: str                        # READ or REFUSED
    revision: str = ''                 # the mesh revision it belongs to
    values: np.ndarray | None = None   # one per cell (int32)
    names: tuple = ()                  # zone names, by code
    reason: str = ''

    @property
    def cells(self) -> int:
        return 0 if self.values is None else int(self.values.shape[0])


def is_case_field(key) -> bool:
    return key in MEMBERS


def mesh_directory(case):
    """``(constant/polyMesh, None)`` the case's cells are read from, or
    ``(None, why not)``."""
    from foammesh.core.mesh.poly_mesh_topology import probe_layout
    try:
        layout = probe_layout(case)
    except OSError as error:
        return None, str(error)
    if not layout.supported:
        return None, _sentence(layout.message)
    return Path(layout.mesh_dir), None


def mesh_revision(meshDir) -> str:
    """The revision of the mesh itself (stat only, nothing is read)."""
    from foammesh.core.mesh.poly_mesh_lease import ReadLease
    from foammesh.core.mesh.poly_mesh_topology import REQUIRED
    return ReadLease.take(Path(meshDir), list(REQUIRED)).revision


def has_member(meshDir, key) -> bool:
    from foammesh.core.mesh.poly_mesh_boundary import _member
    return _member(Path(meshDir), MEMBERS[key]) is not None


def absent_reason(key) -> str:
    if key == section_colour.CELL_LEVEL:
        return ('This mesh has no cellLevel: it was not refined by '
                'snappyHexMesh, so there are no refinement levels to show.')
    return 'This mesh has no cell zones.'


def _sentence(text) -> str:
    text = str(text or '').strip()
    if not text:
        return ''
    text = text[:1].upper() + text[1:]
    return text if text.endswith(('.', '…')) else text + '.'


def _cell_count(meshDir: Path, widths) -> int:
    """The mesh's cells: the owner header's ``nCells`` note, else counted."""
    from foammesh.core.mesh import poly_mesh_topology as topology
    head = topology._head(meshDir, 'owner')
    note = dict(topology._NOTE_COUNT.findall(head.header.get('note', '')))
    if 'nCells' in note:
        return int(note['nCells'])
    owner = topology._labels(meshDir, 'owner', head, widths, {})
    neighbour = topology._labels(meshDir, 'neighbour',
                                 topology._head(meshDir, 'neighbour'),
                                 widths, {})
    top = max(int(owner.max()) if owner.size else -1,
              int(neighbour.max()) if neighbour.size else -1)
    return top + 1


def read_case_field(meshDir, key) -> CaseCellField:
    """Read *key*'s field (`MEMBERS`) from *meshDir*, under a lease.

    Runs off the GUI thread. Never raises for a case problem: a refusal is
    a `CaseCellField` with ``status=REFUSED`` and the reason in a sentence.
    """
    from foammesh.core.mesh import poly_mesh_topology as topology
    from foammesh.core.mesh.poly_mesh_boundary import PolyMeshReadError
    from foammesh.core.mesh.poly_mesh_lease import ReadLease

    meshDir = Path(meshDir)
    member = MEMBERS[key]
    mesh = ReadLease.take(meshDir, list(topology.REQUIRED))
    field = ReadLease.take(meshDir, [member])
    revision = mesh.revision
    if member in field.absent:
        return CaseCellField(key, REFUSED, revision,
                             reason=absent_reason(key))
    widths = topology._Widths()
    try:
        cells = _cell_count(meshDir, widths)
        names = ()
        if key == section_colour.CELL_LEVEL:
            head = topology._head(meshDir, member)
            values = np.asarray(topology._labels(meshDir, member, head,
                                                 widths, {}), np.int64)
            if values.size != cells:
                return CaseCellField(key, REFUSED, revision, reason=(
                    'cellLevel has {0:,} entries and the mesh {1:,} cells: '
                    'it was left by an earlier run, so it is not this '
                    'mesh\'s refinement.').format(values.size, cells))
            if values.size and int(values.min()) < 0:
                return CaseCellField(key, REFUSED, revision, reason=(
                    'cellLevel holds a negative level; it cannot be shown.'))
            values = values.astype(np.int32)
        else:
            head = topology._head(meshDir, member)
            if head.binary:
                zones = topology._binary.read_binary_zones(
                    head.path, member, label_width=widths.label)
            else:
                zones = topology._read_zones(meshDir, member)
            values = np.full(cells, -1, dtype=np.int32)
            for code, zone in enumerate(zones):
                labels = np.asarray(zone.labels, dtype=np.int64)
                if labels.size and (int(labels.min()) < 0
                                    or int(labels.max()) >= cells):
                    return CaseCellField(key, REFUSED, revision, reason=(
                        'The cell zone {0} names a cell the mesh does not '
                        'have; the zones are not this mesh\'s.').format(
                            zone.name))
                values[labels] = code
            names = tuple(zone.name for zone in zones)
            if not names:
                return CaseCellField(key, REFUSED, revision,
                                     reason=absent_reason(key))
        mesh.verify()
        field.verify()
    except PolyMeshReadError as error:
        if getattr(error, 'reason', '') == 'stale_input':
            return CaseCellField(key, REFUSED, revision, reason=(
                'The mesh was being rewritten while {0} was read; it is '
                'read again once the mesh settles.').format(member))
        return CaseCellField(key, REFUSED, revision,
                             reason=_sentence(str(error)))
    except (OSError, ValueError) as error:
        return CaseCellField(key, REFUSED, revision,
                             reason=_sentence(str(error)))
    return CaseCellField(key, READ, revision, values=values, names=names)


# -- the volume datasets ------------------------------------------------------- #

def _fieldString(dataSet, name):
    array = dataSet.GetFieldData().GetAbstractArray(name)
    if array is None or array.GetNumberOfValues() < 1:
        return None
    return str(array.GetValue(0))


def _setFieldString(dataSet, name, value):
    from vtkmodules.vtkCommonCore import vtkStringArray
    fieldData = dataSet.GetFieldData()
    if fieldData.GetAbstractArray(name) is not None:
        fieldData.RemoveArray(name)
    array = vtkStringArray()
    array.SetName(name)
    array.InsertNextValue(str(value))
    fieldData.AddArray(array)


def bindVolume(dataSet, revision) -> str:
    """The mesh revision *dataSet* is of: the one stamped on it when first
    seen, stamping *revision* now if it has none."""
    seen = _fieldString(dataSet, SEEN_REVISION)
    if seen is None:
        _setFieldString(dataSet, SEEN_REVISION, revision)
        seen = revision
    return seen


def attach(dataSet, field: CaseCellField) -> bool:
    """Put *field*'s values on *dataSet* as its ``section_colour`` array.

    Only on a dataset of exactly the field's cell count (the whole volume);
    anything else is declined. Values already attached at the same revision
    are left as they are, so a re-cut does not re-upload them.
    """
    from vtkmodules.util.numpy_support import numpy_to_vtk

    if field.status != READ or field.values is None or dataSet is None:
        return False
    if dataSet.GetNumberOfCells() != field.cells:
        return False
    arrayName = section_colour.choice(field.key).array
    marker = ARRAY_REVISION + arrayName
    cellData = dataSet.GetCellData()
    if (cellData.GetArray(arrayName) is not None
            and _fieldString(dataSet, marker) == field.revision):
        return True
    array = numpy_to_vtk(np.ascontiguousarray(field.values, np.int32),
                         deep=True)
    array.SetName(arrayName)
    if cellData.GetArray(arrayName) is not None:
        cellData.RemoveArray(arrayName)
    cellData.AddArray(array)
    _setFieldString(dataSet, marker, field.revision)
    dataSet.Modified()
    return True


def detach(dataSet, key) -> None:
    """Take *key*'s attached values off *dataSet* (a stale revision)."""
    arrayName = section_colour.choice(key).array
    cellData = dataSet.GetCellData()
    if cellData.GetArray(arrayName) is not None \
            and _fieldString(dataSet, ARRAY_REVISION + arrayName) is not None:
        cellData.RemoveArray(arrayName)
        dataSet.GetFieldData().RemoveArray(ARRAY_REVISION + arrayName)
        dataSet.Modified()
