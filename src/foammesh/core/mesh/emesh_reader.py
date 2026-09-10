"""Read the ``.eMesh`` files ``surfaceFeatures`` writes.

Plan 26 WP5.2 and WP6.2. ``surfaceFeatures`` is a ``run_gated`` task that ran
implicitly inside another stage, its output was never listed anywhere, and
``includedAngle`` -- the one control that governs the extraction -- had no
visible effect at all. Nothing could show the edges because nothing could read
them.

The format is an OpenFOAM dictionary with two unkeyed list entries: a vector
list of points, then an edge list of point-index pairs::

    FoamFile { ... class featureEdgeMesh; ... }
    3
    (
    (0 0 0)
    (1 0 0)
    (1 1 0)
    )
    2
    (
    (0 1)
    (1 2)
    )

Both ASCII and binary are written by OpenFOAM. Only ASCII is parsed here;
a binary file reports its edge count as unknown rather than guessing, because
a wrong count drawn on screen is worse than an absent one.
"""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass
from pathlib import Path

#: ``(1.5 0 -2)`` -- one point.
_VECTOR = re.compile(r'\(\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)')
#: ``(0 1)`` -- one edge, as a pair of point indices.
_EDGE = re.compile(r'\(\s*(\d+)\s+(\d+)\s*\)')
_FORMAT = re.compile(r'\bformat\s+(\w+)\s*;')


@dataclass(frozen=True)
class FeatureEdges:
    """One surface's extracted feature edges."""

    name: str
    path: Path
    points: tuple[tuple[float, float, float], ...] = ()
    edges: tuple[tuple[int, int], ...] = ()
    binary: bool = False

    @property
    def edge_count(self) -> int | None:
        """``None`` for a binary file, whose edges were not parsed."""
        return None if self.binary else len(self.edges)

    def segments(self) -> tuple[tuple[tuple[float, float, float],
                                      tuple[float, float, float]], ...]:
        """Edges as coordinate pairs, ready to draw.

        Silently drops an edge whose indices fall outside the point list rather
        than raising: a truncated file should render what it has.
        """
        count = len(self.points)
        return tuple(
            (self.points[first], self.points[second])
            for first, second in self.edges
            if 0 <= first < count and 0 <= second < count)

    def to_dict(self) -> dict:
        return {'name': self.name, 'path': str(self.path),
                'points': len(self.points), 'edges': self.edge_count,
                'binary': self.binary}


def _read_text(path: Path) -> str:
    if path.suffix == '.gz':
        with gzip.open(path, 'rt', encoding='utf-8', errors='ignore') as source:
            return source.read()
    return path.read_text(encoding='utf-8', errors='ignore')


def read_emesh(path) -> FeatureEdges:
    """Parse one ``.eMesh``. Never raises on malformed content."""
    path = Path(path)
    name = path.name.removesuffix('.gz').removesuffix('.eMesh')
    try:
        text = _read_text(path)
    except OSError:
        return FeatureEdges(name=name, path=path)

    header = _FORMAT.search(text)
    if header and header.group(1).lower() == 'binary':
        return FeatureEdges(name=name, path=path, binary=True)

    # Strip the FoamFile header so its own braces cannot be mistaken for data.
    body = text.split('}', 1)[1] if '}' in text else text
    blocks = _list_blocks(body)
    points = tuple(
        (float(x), float(y), float(z))
        for x, y, z in _VECTOR.findall(blocks[0])) if blocks else ()
    edges = tuple(
        (int(first), int(second))
        for first, second in _EDGE.findall(blocks[1])) if len(blocks) > 1 else ()
    return FeatureEdges(name=name, path=path, points=points, edges=edges)


def _list_blocks(text: str) -> list[str]:
    """The top-level ``( ... )`` blocks, in order.

    Written as a depth scan rather than a regex because the point list contains
    nested parentheses and a non-greedy match would stop at the first one.
    """
    blocks, depth, start = [], 0, -1
    for index, character in enumerate(text):
        if character == '(':
            if depth == 0:
                start = index
            depth += 1
        elif character == ')':
            depth -= 1
            if depth == 0 and start >= 0:
                blocks.append(text[start:index + 1])
                start = -1
            elif depth < 0:
                depth = 0
    return blocks


def discover_feature_edges(case_path) -> tuple[FeatureEdges, ...]:
    """Every ``.eMesh`` the case has, wherever ``surfaceFeatures`` put it.

    Both locations are searched because the two run paths disagree: the staged
    route writes beside ``triSurface`` and the pipeline route into
    ``constant/polyMesh``. Looking in one place would report "no features
    extracted" for a case that has them.
    """
    root = Path(case_path)
    found: dict[str, FeatureEdges] = {}
    for directory in (root / 'constant' / 'triSurface',
                      root / 'constant' / 'polyMesh',
                      root / 'constant' / 'extendedFeatureEdgeMesh'):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if not (path.name.endswith('.eMesh')
                    or path.name.endswith('.eMesh.gz')):
                continue
            edges = read_emesh(path)
            found.setdefault(edges.name, edges)
    return tuple(found[name] for name in sorted(found))
