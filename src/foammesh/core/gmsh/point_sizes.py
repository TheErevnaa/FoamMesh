"""Size at points, as rows of picked points (Plan 41 GN7, M-01).

Gmsh's Mesh > Define > Size at points sets the element size at chosen points
(``gmsh.model.mesh.setSize``).  Until now that was reachable only through a
curve control and only while "From points" was ticked on Global sizing.  A
row here is one picked point and the size asked there::

    {"name": "corner", "tag": 7, "x": 1.0, "y": 0.0, "z": 0.0,
     "size": 0.02, "enabled": True}

``tag`` is the point entity of the Gmsh model; ``x, y, z`` are its position
when it was picked.  The runner finds the point by tag and, if the model was
renumbered, by position, so a row survives a re-import.  The rows are stored
as JSON text in ``gmsh.point_sizes.rows``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

#: Where the rows live in the project.
FIELD_ID = 'gmsh.point_sizes.rows'
#: The stored name, as the registry spells it.
STORED_ID = 'gmsh/pointSizes/rows'

CALCULATION_VERSION = 'gmsh.point_sizes.v1'


class PointSizeRefusal(ValueError):
    """A set of rows Gmsh cannot be given, with the reason."""


@dataclass(frozen=True)
class PointSizes:
    rows: tuple = ()
    warnings: tuple = ()
    calculation_version: str = CALCULATION_VERSION

    def to_list(self) -> list:
        return [dict(row) for row in self.rows]

    def __bool__(self) -> bool:
        return bool(self.rows)


def rows_from_text(text) -> list[dict]:
    text = str(getattr(text, 'value', text) or '').strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError as error:
        raise PointSizeRefusal(
            f'the stored point sizes are not valid JSON: {error}') from None
    if isinstance(data, dict):
        data = data.get('rows', [])
    if not isinstance(data, list):
        raise PointSizeRefusal('the stored point sizes are not a list of rows')
    return [dict(item) for item in data if isinstance(item, dict)]


def rows_to_text(rows) -> str:
    rows = [dict(item) for item in (rows or ())]
    return json.dumps(rows, separators=(',', ':'), sort_keys=True) \
        if rows else ''


def _number(value, label: str, owner: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PointSizeRefusal(
            f'{owner}: {label} needs a number, not {value!r}') from None
    if not math.isfinite(number):
        raise PointSizeRefusal(f'{owner}: {label} must be finite')
    return number


def validate(rows) -> PointSizes:
    """Check *rows* and return them in the form the job carries."""
    out: list[dict] = []
    seen_names: set[str] = set()
    seen_tags: dict[int, str] = {}
    for index, row in enumerate(rows or (), start=1):
        name = str(row.get('name') or '').strip() or f'point{index}'
        owner = f'point size {name!r}'
        if name in seen_names:
            raise PointSizeRefusal(f'{owner}: two rows share that name')
        seen_names.add(name)
        try:
            tag = int(row.get('tag'))
        except (TypeError, ValueError):
            raise PointSizeRefusal(
                f'{owner}: pick a point (it has no point tag)') from None
        if tag < 1:
            raise PointSizeRefusal(f'{owner}: a point tag is 1 or more')
        size = _number(row.get('size'), 'the size', owner)
        if size <= 0.0:
            raise PointSizeRefusal(f'{owner}: the size must be above zero')
        item = {'name': name, 'tag': tag, 'size': size,
                'enabled': bool(row.get('enabled', True))}
        present = [k for k in ('x', 'y', 'z') if row.get(k) is not None]
        if present:
            if len(present) != 3:
                raise PointSizeRefusal(
                    f'{owner}: give all of x, y and z or none of them')
            for key in ('x', 'y', 'z'):
                item[key] = _number(row[key], key, owner)
        if item['enabled']:
            if tag in seen_tags:
                raise PointSizeRefusal(
                    f'{owner}: point {tag} already has a size in '
                    f'{seen_tags[tag]!r}; Gmsh keeps one size per point')
            seen_tags[tag] = name
        out.append(item)
    return PointSizes(rows=tuple(out))


def enabled_rows(rows) -> list[dict]:
    return [dict(r) for r in rows or () if r.get('enabled', True)]
