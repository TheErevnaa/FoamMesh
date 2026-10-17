"""Stage the prepared geometry as one STL per named surface for the tool.

Plan 41 section 12.2 S2. The shared pipeline is not changed: this reads what
Preparation published (``prepared-geometry.json`` and its ``sources/`` folder)
and writes copies under ``foammesh/sizing/staged/``. A multi-solid ASCII STL
is split at its ``solid`` blocks, one file per solid, named by the solid. A
source with a single solid is named by its group (patch) name when the source
has exactly one group, otherwise by the source's display name.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import struct

import numpy as np

_SAFE = re.compile(r'[^A-Za-z0-9_.-]+')


class StagingError(ValueError):
    pass


def _safe(name: str) -> str:
    return _SAFE.sub('_', str(name).strip()).strip('_') or 'surface'


def _is_binary(raw: bytes) -> bool:
    if len(raw) < 84:
        return False
    count = struct.unpack('<I', raw[80:84])[0]
    return 84 + 50 * count == len(raw)


def read_stl_solids(path) -> list[tuple[str, np.ndarray]]:
    """``[(solid name, triangles (n, 3, 3))]`` of an ASCII or binary STL."""
    raw = Path(path).read_bytes()
    if _is_binary(raw):
        count = struct.unpack('<I', raw[80:84])[0]
        record = np.dtype([('n', '<f4', 3), ('v', '<f4', (3, 3)),
                           ('a', '<u2')])
        data = np.frombuffer(raw, dtype=record, count=count, offset=84)
        return [(Path(path).stem, data['v'].astype(float))]
    solids: list[tuple[str, list]] = []
    current = None
    vertices: list = []
    for line in raw.decode('utf-8', 'replace').splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == 'solid':
            current = (' '.join(words[1:]) or Path(path).stem, [])
            solids.append(current)
        elif words[0] == 'vertex' and current is not None:
            vertices.append([float(item) for item in words[1:4]])
            if len(vertices) == 3:
                current[1].append(vertices)
                vertices = []
    result = [(name, np.array(tris, dtype=float).reshape(-1, 3, 3))
              for name, tris in solids if tris]
    if not result:
        raise StagingError(f'{path} holds no triangles')
    return result


def write_stl(path, name: str, triangles: np.ndarray) -> None:
    lines = [f'solid {name}']
    for tri in triangles:
        normal = np.cross(tri[1] - tri[0], tri[2] - tri[0])
        length = np.linalg.norm(normal)
        normal = normal / length if length > 0 else normal
        lines.append('facet normal %.9g %.9g %.9g' % tuple(normal))
        lines.append(' outer loop')
        for vertex in tri:
            lines.append('  vertex %.9g %.9g %.9g' % tuple(vertex))
        lines.append(' endloop')
        lines.append('endfacet')
    lines.append(f'endsolid {name}')
    Path(path).write_text('\n'.join(lines) + '\n', 'utf-8')


@dataclass(frozen=True)
class StagedSurface:
    name: str
    path: Path
    triangles: int

    def to_dict(self) -> dict:
        return {'name': self.name, 'path': str(self.path),
                'triangles': self.triangles}


def _unique(name: str, taken: set) -> str:
    candidate, number = name, 2
    while candidate in taken:
        candidate = f'{name}_{number}'
        number += 1
    taken.add(candidate)
    return candidate


def stage_sources(sources, out_dir) -> list[StagedSurface]:
    """Stage ``[(display name, stl path)]`` as one STL per named surface."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    taken: set = set()
    staged = []
    for display, path in sources:
        solids = read_stl_solids(path)
        for solid, triangles in solids:
            label = display if len(solids) == 1 else solid
            name = _unique(_safe(label), taken)
            target = out_dir / f'{name}.stl'
            write_stl(target, name, triangles)
            staged.append(StagedSurface(name, target, int(len(triangles))))
    if not staged:
        raise StagingError('there is no surface to size')
    return staged


def prepared_sources(case_path) -> tuple[list, str]:
    """``([(name, stl)], prepared fingerprint)`` of the published revision."""
    root = Path(case_path) / 'foammesh' / 'geometry' / 'prepared'
    try:
        current = json.loads((root / 'current.json').read_text('utf-8'))
        revision = root / current['revision_id']
        manifest = json.loads(
            (revision / 'prepared-geometry.json').read_text('utf-8'))
        groups = json.loads(
            (revision / 'group-manifest.json').read_text('utf-8'))
    except (OSError, KeyError, ValueError) as error:
        raise StagingError(
            'no prepared geometry is published to size') from error
    group_names: dict = {}
    for group in groups.get('groups', ()):
        group_names.setdefault(group.get('geometry_id'), []).append(
            group.get('display_name') or group.get('solver_name'))
    sources = []
    for record in manifest.get('sources', ()):
        prepared = record.get('surface_prepared_name') or record.get(
            'prepared_name')
        path = revision / 'sources' / str(prepared)
        if path.suffix.lower() != '.stl' or not path.is_file():
            continue
        names = group_names.get(record.get('geometry_id')) or []
        display = names[0] if len(names) == 1 else (
            record.get('display_name') or record.get('geometry_id'))
        sources.append((display, path))
    if not sources:
        raise StagingError('the prepared geometry has no STL surface to size')
    return sources, str(current.get('prepared_fingerprint')
                        or manifest.get('prepared_fingerprint') or '')


def geometry_digest(staged) -> str:
    """Digest of the staged surfaces: the Reuse key's geometry half."""
    digest = hashlib.sha256()
    for item in sorted(staged, key=lambda s: s.name):
        digest.update(item.name.encode())
        digest.update(Path(item.path).read_bytes())
    return digest.hexdigest()
