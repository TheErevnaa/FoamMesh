#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Read OpenFOAM cell/face sets written by ``checkMesh -writeSets`` / topoSet.

A set file (constant/polyMesh/sets/<name>) is a FoamFile whose body is a labelled
list of ids::

    12
    (
    3 7 11 ... )

This parses the id list so the GUI can highlight failed cells/faces in the
viewport. Building the actual vtk selection needs the loaded mesh (GUI), but the
id extraction is headless and tested.
"""
from __future__ import annotations

import re
import gzip
from pathlib import Path


def parse_set_ids(text: str) -> list[int]:
    """Extract the integer id list from an OpenFOAM set file body."""
    # strip C/C++ style comments and the FoamFile header block
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'//[^\n]*', ' ', text)
    text = re.sub(r'FoamFile\s*\{.*?\}', ' ', text, flags=re.S)

    m = re.search(r'\((.*)\)', text, flags=re.S)
    if not m:
        return []
    body = m.group(1)
    return [int(tok) for tok in re.findall(r'-?\d+', body)]


def _binary_labels(path: Path) -> list[int] | None:
    """The labels of a member written ``format binary``, else None.

    Plan 50 IP15 (DP-6421). A set or ``owner`` in a binary working case is
    ``N`` then ``(`` then raw little-endian labels: the text reads below took
    the ASCII digits that happen to sit among those bytes as ids and
    highlighted cells nothing named. A binary member is decoded here, and one
    that does not decode as a plain label list gives ``[]`` -- no highlight
    rather than a wrong one.
    """
    from foammesh.core.mesh import poly_mesh_binary as binary
    from foammesh.core.mesh.poly_mesh_boundary import (
        PolyMeshReadError, _FOAMFILE, _parse_header, _read_bytes)
    try:
        raw = _read_bytes(path)
        if _FOAMFILE.search(raw[:65536]) is None:
            return None
        header = _parse_header(raw[:65536], path)
    except (OSError, EOFError, PolyMeshReadError):
        return None
    if str(header.get('format', 'ascii')).strip() != 'binary':
        return None
    # DP-6486. OpenFOAM 13 writes a set under a ``format binary`` header with
    # its labels as text, one a line (read off a live binary case). Decoding
    # that as raw bytes failed -- every set read empty -- or, for a one-label
    # set, "succeeded" with a cell id made of its digits. A body that is a
    # whole text list of exactly the count it declares is read as text; raw
    # labels can look like that only when every byte of every label is a
    # digit or a space, which no label below 151 million is.
    text = _text_labels(raw, path)
    if text is not None:
        return text
    try:
        return binary.read_binary_labels(path, path.name)[0].tolist()
    except (OSError, PolyMeshReadError):
        return []


#: DP-6486. What lies between the parentheses of a label list written as text.
_TEXT_BODY = re.compile(rb'[0-9\s]*')


def _text_labels(raw: bytes, path: Path) -> list[int] | None:
    """The labels of a list written as text, whatever its header says; None
    when the body is not a whole text list of the count it declares."""
    from foammesh.core.mesh.poly_mesh_boundary import (
        PolyMeshReadError, _FOAMFILE, _leading_count, _numbers, _payload)
    import numpy as np

    found = _FOAMFILE.search(raw)
    if found is None:
        return None
    payload = _payload(raw, found.end())
    try:
        count, open_at = _leading_count(payload, path.name)
    except PolyMeshReadError:
        return None
    close = payload.rfind(b')')
    if close < open_at or payload[close + 1:].strip():
        return None
    body = payload[open_at + 1:close]
    if not _TEXT_BODY.fullmatch(body):
        return None
    values = _numbers(body, np.int64)
    return values.tolist() if values.size == count else None


def read_set_file(path) -> list[int]:
    path = Path(path)
    decoded = _binary_labels(path)
    if decoded is not None:
        return decoded
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8', errors='ignore') as source:
            return parse_set_ids(source.read())
    return parse_set_ids(path.read_text(encoding='utf-8', errors='ignore'))


def set_entity_kind(path) -> str:
    """Return the OpenFOAM set class, or ``cellSet`` for old/headerless files.

    ``vtkUnstructuredGrid`` cell ids correspond to OpenFOAM cell ids.  Face and
    point sets need a different source dataset, so callers must not accidentally
    render their ids as cells.
    """
    path = Path(path)
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8', errors='ignore') as source:
            text = source.read()
    else:
        text = path.read_text(encoding='utf-8', errors='ignore')
    match = re.search(r'\bclass\s+([A-Za-z][A-Za-z0-9_]*)\s*;', text)
    return match.group(1) if match else 'cellSet'


def face_owner_cells(case_path) -> list[int]:
    """Face id to owning cell id, read from ``constant/polyMesh/owner``.

    Plan 31 (``checkmesh.write_surfaces``). Returns an empty list when the
    mesh is absent or its owner does not read whole, because a partial map
    would mis-highlight cells, which is worse than highlighting none. The
    face-set lookup itself uses :func:`owner_array` (DP-6486).
    """
    return owner_array(case_path).tolist()


def owner_array(case_path):
    """Face id to owning cell id as an int64 numpy array; empty when absent.

    DP-6486. Read by the polyMesh reader (ASCII, ``.gz``, binary at either
    label width, ``N{v}``), so a 150 M-face owner is never a Python list. A
    list that does not read whole is empty: a partial map would highlight
    the wrong cells.
    """
    import numpy as np

    from foammesh.core.mesh.poly_mesh_boundary import _member, _read_labels

    directory = Path(case_path) / 'constant' / 'polyMesh'
    if _member(directory, 'owner') is None:
        return np.empty(0, dtype=np.int64)
    try:
        return np.asarray(_read_labels(directory, 'owner'), dtype=np.int64)
    except (OSError, EOFError, ValueError):
        return np.empty(0, dtype=np.int64)


def discover_cell_set_files(case_path) -> dict[str, list[int]]:
    """The failed cells the viewport can highlight, whatever set names them.

    Plan 31 (``checkmesh.write_surfaces``). This kept only files whose class
    is ``cellSet`` -- and OpenFOAM 13's checkMesh writes its problem entities
    as *faceSets*: ``nonOrthoFaces``, ``skewFaces``, ``wrongOrientedFaces``,
    ``meshQualityFaces`` (MEASURED on build ``13-58ed5c2046ef``). So the
    toolbar's Failed Cells button was disabled with "no mesh check has written
    a failed-cell set yet" immediately after a mesh check had written four of
    them, and stayed disabled on every mesh this product has ever made.

    A face id is not a cell id, and the earlier docstring was right to refuse
    to render one as the other. The fix is to translate rather than discard:
    each face's owner cell is in ``constant/polyMesh/owner``, so a faceSet
    becomes the set of cells that own its faces -- exactly the cells a user
    means when they ask to see where the mesh failed. Without a readable
    ``owner`` the faceSets are skipped, as before.
    """
    directory = Path(case_path) / 'constant' / 'polyMesh' / 'sets'
    if not directory.is_dir():
        return {}
    files = [path for path in sorted(directory.iterdir()) if path.is_file()]
    kinds = {path: set_entity_kind(path) for path in files}
    discovered = {
        path.name.removesuffix('.gz'): read_set_file(path)
        for path in files if kinds[path] == 'cellSet'
    }
    face_sets = [path for path in files if kinds[path] == 'faceSet']
    if not face_sets:
        return discovered
    import numpy as np

    # DP-6486. In numpy: the owner of a large mesh is not walked in Python.
    owner = owner_array(case_path)
    if not owner.size:
        return discovered
    limit = owner.size
    for path in face_sets:
        name = path.name.removesuffix('.gz')
        if name in discovered:
            # A cellSet of the same name is already the answer; a faceSet
            # cannot improve on it and must not silently replace it.
            continue
        faces = np.asarray(read_set_file(path), dtype=np.int64)
        faces = faces[(faces >= 0) & (faces < limit)]
        cells = np.unique(owner[faces]).tolist()
        if cells:
            discovered[name] = cells
    return discovered


def sets_of_this_mesh(case_path) -> dict[str, list[int]]:
    """DP-2722. The sets already on disk, when they were written of the mesh
    that is there now: every set file no older than ``polyMesh/owner``.

    What a check run in this session wrote is recorded as it finishes; a case
    opened later, its sets on disk and its stored check on screen, had the
    failed-cells button disabled with "no mesh check has written a
    failed-cell set yet". A mesh written after the check (a re-mesh leaves
    the old ``sets`` directory in place) makes the sets name other cells, so
    any set file older than the mesh refuses the lot.
    """
    polymesh = Path(case_path) / 'constant' / 'polyMesh'
    owner = next((polymesh / name for name in ('owner', 'owner.gz')
                  if (polymesh / name).is_file()), None)
    directory = polymesh / 'sets'
    if owner is None or not directory.is_dir():
        return {}
    meshed = owner.stat().st_mtime
    files = [path for path in directory.iterdir() if path.is_file()]
    if not files or any(path.stat().st_mtime < meshed for path in files):
        return {}
    return discover_cell_set_files(case_path)


def discover_check_surfaces(case_path) -> list[dict]:
    """The surfaces ``checkMesh -writeSurfaces`` wrote, newest instance first.

    Plan 31 (``checkmesh.write_surfaces``). MEASURED: the flag writes only
    ``postProcessing/checkMesh/<instance>/<name>.<ext>`` and leaves
    ``constant/polyMesh/sets`` empty, so nothing that reads the sets directory
    can see these at all. Reported alongside the sets so a QA report can say
    the files exist and where they are.
    """
    root = Path(case_path) / 'postProcessing' / 'checkMesh'
    if not root.is_dir():
        return []
    found = []
    for instance in sorted(root.iterdir(), reverse=True):
        if not instance.is_dir():
            continue
        for path in sorted(instance.iterdir()):
            if not path.is_file():
                continue
            found.append({
                'name': path.stem,
                'instance': instance.name,
                'format': path.suffix.lstrip('.').lower(),
                'path': str(path),
                'bytes': path.stat().st_size,
            })
    return found


def discover_set_details(case_path) -> list[dict]:
    """Serializable inventory for the QA dashboard and persisted report."""
    directory = Path(case_path) / 'constant' / 'polyMesh' / 'sets'
    if not directory.is_dir():
        return []
    details = []
    for path in sorted(directory.iterdir()):
        if not path.is_file():
            continue
        ids = read_set_file(path)
        details.append({
            'name': path.name.removesuffix('.gz'),
            'kind': set_entity_kind(path),
            'count': len(ids),
            'path': str(path),
        })
    return details


def build_vtk_selection(grid, ids: list[int], field_type: str = 'CELL'):
    """Build a vtkSelection of the given ids on *grid* (for viewport highlight)."""
    from vtkmodules.vtkCommonDataModel import (
        vtkSelection, vtkSelectionNode)
    from vtkmodules.vtkCommonCore import vtkIdTypeArray

    arr = vtkIdTypeArray()
    arr.SetNumberOfComponents(1)
    for i in ids:
        arr.InsertNextValue(i)

    node = vtkSelectionNode()
    node.SetFieldType(getattr(vtkSelectionNode, field_type))
    node.SetContentType(vtkSelectionNode.INDICES)
    node.SetSelectionList(arr)

    selection = vtkSelection()
    selection.AddNode(node)
    return selection


def extract_selected_cells(grid, ids: list[int]):
    """Return an unstructured-grid overlay containing exactly the selected cell IDs."""
    from vtkmodules.vtkFiltersExtraction import vtkExtractSelection
    extraction = vtkExtractSelection()
    extraction.SetInputData(0, grid)
    extraction.SetInputData(1, build_vtk_selection(grid, ids))
    extraction.Update()
    return extraction.GetOutput()
