"""What a finished step left behind, in the mesh's own counts (Plan 40 UXA5).

Every step that writes the mesh records the counts of the mesh it wrote:
cells, faces, points and boundary faces, plus the time the run took. The page
header and the outline row read them back. The numbers are read from the
mesh itself (the header OpenFOAM writes into ``owner``, or the files when a
writer left no header), so they are the counts the viewport and checkMesh
see, not a second tally.

The record lives beside the task state, per engine, so a reopened case still
shows the strip of the run it last made.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

__all__ = ['mesh_counts', 'native_counts', 'record', 'load', 'headline', 'detail', 'change',
           'tooltip', 'short_cells']

_NOTE = re.compile(r'(nPoints|nCells|nFaces|nInternalFaces)\s*:\s*(\d+)')
_PATCH_FACES = re.compile(r'\bnFaces\s+(\d+)\s*;')


def _path(case_path, engine_id: str) -> Path:
    return (Path(case_path) / 'foammesh' / 'workflow'
            / f'{engine_id}-stage-strips.json')


def _head(path: Path, size: int = 4096) -> str:
    try:
        with path.open('rb') as handle:
            return handle.read(size).decode('utf-8', errors='replace')
    except OSError:
        return ''


def mesh_counts(poly_mesh) -> dict | None:
    """``{cells, faces, points, boundary_faces}`` of a polyMesh folder.

    ``None`` when the folder holds no mesh whose counts can be read.
    """
    root = Path(poly_mesh)
    found = {name: int(value)
             for name, value in _NOTE.findall(_head(root / 'owner'))}
    cells, faces = found.get('nCells'), found.get('nFaces')
    points, internal = found.get('nPoints'), found.get('nInternalFaces')
    if None in (cells, faces, points):
        try:
            from foammesh.core.mesh.poly_mesh_boundary import read_poly_mesh

            mesh = read_poly_mesh(root.parents[1], layout_expectation='any')
        except Exception:                                    # noqa: BLE001
            return None
        cells, faces = int(mesh.cell_count), int(mesh.face_count)
        points = int(mesh.points.shape[0])
        internal = None
    if internal is None:
        try:
            text = (root / 'boundary').read_text(
                encoding='utf-8', errors='replace')
        except OSError:
            return None
        boundary = sum(int(value) for value in _PATCH_FACES.findall(text))
    else:
        boundary = faces - internal
    return {'cells': int(cells), 'faces': int(faces), 'points': int(points),
            'boundary_faces': int(boundary)}


#: Faces each linear volume family has; a face shared by two cells counts once.
_FACES_PER_CELL = {'tetrahedron': 4, 'hexahedron': 6, 'prism': 5,
                   'pyramid': 5}


def native_counts(path) -> dict | None:
    """The same four counts from a native mesh file (``.su2`` or ``.msh``).

    For a run that published no polyMesh (the SU2 route, a second-order
    mesh). Cells, points and boundary faces are counted from the file; the
    faces are every cell's faces plus the boundary faces, each shared face
    once. ``None`` for a planar section or any family not listed, because a
    number the file does not support is worse than none.
    """
    from foammesh.core.mesh.census import element_census

    try:
        census = element_census(path)
    except Exception:                                        # noqa: BLE001
        return None
    if census.read_error or census.dimension != 3 or not census.volume_count:
        return None
    if any(name not in _FACES_PER_CELL and count
           for name, count in census.volume_by_family.items()):
        return None
    corners = sum(_FACES_PER_CELL[name] * int(count)
                  for name, count in census.volume_by_family.items())
    boundary = int(census.surface_count)
    if (corners + boundary) % 2:
        return None
    return {'cells': int(census.volume_count),
            'faces': (corners + boundary) // 2,
            'points': int(census.point_count), 'boundary_faces': boundary}


def load(case_path, engine_id: str) -> dict:
    """``{task_id: record}``; empty when nothing was recorded or readable."""
    try:
        document = json.loads(
            _path(case_path, engine_id).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    records = document.get('steps') if isinstance(document, dict) else None
    return {str(task): dict(item) for task, item in (records or {}).items()
            if isinstance(item, dict)}


def record(case_path, engine_id: str, task_id: str, counts: dict,
           seconds: float | None = None) -> None:
    """Keep *counts* (and the run's seconds) for *task_id*.

    Never raises: a strip that could not be kept is a strip not drawn.
    """
    if not counts or not task_id:
        return
    path = _path(case_path, engine_id)
    try:
        steps = load(case_path, engine_id)
        entry = dict(counts)
        if seconds is not None:
            entry['seconds'] = round(float(seconds), 2)
        steps[task_id] = entry
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f'.{os.getpid()}.tmp')
        temporary.write_text(
            json.dumps({'schema_version': 1, 'steps': steps}, indent=2,
                       sort_keys=True) + '\n', encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        return


# -- words -------------------------------------------------------------- #

def _plural(count: int, one: str, many: str) -> str:
    return f'{count:,} {one if count == 1 else many}'


def short_cells(cells: int) -> str:
    """A cell count short enough for an outline row: 66,544 -> ``66.5k``."""
    cells = int(cells)
    if cells >= 1_000_000:
        return f'{cells / 1_000_000:.2f}M'.replace('.00M', 'M')
    if cells >= 10_000:
        return f'{cells / 1000:.1f}k'.replace('.0k', 'k')
    return f'{cells:,}'


def headline(entry: dict) -> str:
    """The key number, large: ``66,544 cells``."""
    return _plural(int(entry.get('cells') or 0), 'cell', 'cells')


def _time(seconds) -> str:
    seconds = float(seconds)
    if seconds < 60:
        return f'{seconds:.1f} s' if seconds < 10 else f'{seconds:.0f} s'
    minutes, rest = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f'{minutes} min {rest:02d} s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours} h {minutes:02d} min'


def detail(entry: dict) -> str:
    """Faces, points, boundary faces and time, in the normal weight."""
    parts = [_plural(int(entry.get('faces') or 0), 'face', 'faces'),
             _plural(int(entry.get('points') or 0), 'point', 'points'),
             _plural(int(entry.get('boundary_faces') or 0),
                     'boundary face', 'boundary faces')]
    if entry.get('seconds') is not None:
        parts.append(_time(entry['seconds']))
    return ' · '.join(parts)


def change(entry: dict, previous: dict | None, previous_title: str) -> str:
    """``+41,200 cells since Castellation``; empty with nothing to compare."""
    if not previous or not previous_title:
        return ''
    delta = int(entry.get('cells') or 0) - int(previous.get('cells') or 0)
    return f'{delta:+,} cells since {previous_title}'


def tooltip(entry: dict, previous: dict | None = None,
            previous_title: str = '') -> str:
    lines = [headline(entry), detail(entry)]
    moved = change(entry, previous, previous_title)
    if moved:
        lines.append(moved)
    return '\n'.join(lines)
