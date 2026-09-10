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


def read_set_file(path) -> list[int]:
    path = Path(path)
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


def _read_label_list(path) -> list[int]:
    """The label list of a polyMesh file, without a regex over millions of ids.

    ``owner`` is the largest ASCII file in a case; the id regex the set reader
    uses walks it token by token and is measurably slower than splitting the
    one parenthesised block, which is all this needs.
    """
    path = Path(path)
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8', errors='ignore') as source:
            text = source.read()
    else:
        text = path.read_text(encoding='utf-8', errors='ignore')
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    text = re.sub(r'//[^\n]*', ' ', text)
    text = re.sub(r'FoamFile\s*\{.*?\}', ' ', text, flags=re.S)
    if '(' not in text or ')' not in text:
        return []
    body = text.split('(', 1)[1].rsplit(')', 1)[0]
    labels = []
    for token in body.split():
        try:
            labels.append(int(token))
        except ValueError:
            return []
    return labels


def face_owner_cells(case_path) -> list[int]:
    """Face id to owning cell id, read from ``constant/polyMesh/owner``.

    Plan 31 (``checkmesh.write_surfaces``). Returns an empty list when the
    mesh is absent or is written in a form this cannot read -- binary, or a
    non-numeric body -- because a partial map would mis-highlight cells, which
    is worse than highlighting none.
    """
    directory = Path(case_path) / 'constant' / 'polyMesh'
    for name in ('owner', 'owner.gz'):
        candidate = directory / name
        if candidate.is_file():
            return _read_label_list(candidate)
    return []


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
    owner = face_owner_cells(case_path)
    if not owner:
        return discovered
    limit = len(owner)
    for path in face_sets:
        name = path.name.removesuffix('.gz')
        if name in discovered:
            # A cellSet of the same name is already the answer; a faceSet
            # cannot improve on it and must not silently replace it.
            continue
        cells = {owner[face] for face in read_set_file(path)
                 if 0 <= face < limit}
        if cells:
            discovered[name] = sorted(cells)
    return discovered


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
