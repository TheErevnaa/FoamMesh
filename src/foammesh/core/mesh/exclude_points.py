"""Exclude points (snappyHexMesh ``outsidePoints``): what they will remove.

Plan 37 UF16. Foundation 13 reads ``outsidePoints`` in
castellatedMeshControls and removes every connected space that holds one --
but it never says when a point does nothing. Measured live
(``plans/evidence/plan37/uf16-v13-exclude-points.md``):

* a point in the same connected space as a region seed removes nothing: the
  seed's space is re-selected after the exclusion (``meshRefinement.C``
  ``findRegions``) -- the seed wins;
* a point off the background mesh, on its boundary or on a surface finds no
  cell region and is skipped silently, with the run still switched to "keep
  every space but the excluded ones";
* with seeds present, that switch keeps every space nobody seeded and leaves
  the cut-off remnants of the excluded space as separate mesh regions
  (208 to 4415 regions measured).

So an exclusion is a statement about a space, and this module checks it
before the run. It follows the Plan 36 confidence rule: what is exact (a
point off the domain, on its wall, on a seed or on a surface) is an error;
what rests on the voxel labelling (two points in one space) is an error only
when the h/2 field confirms it, and a warning otherwise.

Nothing here touches Qt; the voxel field is passed in by a caller that
labelled it off the GUI thread (``fluid_regions.run_detection``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field as dataclass_field

import numpy as np

ERROR = 'error'
WARNING = 'warning'

#: Finding codes.
OUTSIDE_DOMAIN = 'exclude.outside_domain'
ON_DOMAIN_WALL = 'exclude.on_domain_wall'
AT_SEED = 'exclude.at_seed'
ON_SURFACE = 'exclude.on_surface'
NEAR_SURFACE = 'exclude.near_surface'
SAME_SPACE_AS_SEED = 'exclude.same_space_as_seed'
UNRESOLVED_POINT = 'exclude.unresolved_point'
KEEPS_UNSEEDED = 'exclude.keeps_unseeded_spaces'

# Plan 37 UF16 (user-approved 2026-10-01): seeds and exclude points together
# are warned about, never refused. The why is the measured v13 behaviour: a
# seed always keeps its own space (inside wins), so the exclude points can
# never remove more than the seeds alone would; what they add is the switch to
# "keep every space without an exclude point", which keeps unseeded closed
# spaces and leaves cut-off pieces of the excluded space as separate regions
# (208 to 4,415 regions in the live runs).
SEEDS_AND_EXCLUDES_TEXT = (
    'region seeds and exclude points are both set. A seed always keeps its '
    'own space, so the exclude points cannot remove more than the seeds '
    'alone would; what they add is that OpenFOAM 13 then keeps every space '
    'that holds no exclude point, including closed spaces nobody seeded, '
    'and leaves cut-off pieces of the excluded space as separate mesh '
    'regions (up to 4,415 in the measured runs). Seed only the spaces to '
    'keep and delete the exclude points. The run still launches, and the '
    'region count of the result is checked afterwards')

#: snappyHexMesh's own default ``mergeTolerance``: the relative distance it
#: perturbs a point by before it gives up on finding its cell.
MERGE_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    point: str = ''
    #: 'exact', or a Plan 36 confidence ('confirmed', 'approximate',
    #: 'unresolved') for a finding the voxel labelling produced.
    confidence: str = 'exact'

    def to_dict(self) -> dict:
        return {'code': self.code, 'severity': self.severity,
                'message': self.message, 'point': self.point,
                'confidence': self.confidence}


@dataclass
class Report:
    """What the exclude points will remove, and what stops them."""

    findings: list = dataclass_field(default_factory=list)
    #: One row per point the labelling placed in a space:
    #: ``{'point', 'space', 'volume', 'outside'}``.
    removals: list = dataclass_field(default_factory=list)

    @property
    def errors(self) -> list:
        return [one for one in self.findings if one.severity == ERROR]

    @property
    def warnings(self) -> list:
        return [one for one in self.findings if one.severity == WARNING]

    def codes(self, severity: str | None = None) -> list[str]:
        return [one.code for one in self.findings
                if severity is None or one.severity == severity]

    def to_dict(self) -> dict:
        return {'findings': [one.to_dict() for one in self.findings],
                'removals': [dict(row) for row in self.removals]}


# -- inputs ----------------------------------------------------------------- #

def _xyz(point) -> tuple[float, float, float]:
    x, y, z = (float(value) for value in point)
    return x, y, z


def _bounds(domain):
    """``(xmin, xmax, ...)`` in metres, from a `DomainBox` or six numbers."""
    if domain is None:
        return None, None
    if hasattr(domain, 'metres'):
        return tuple(domain.metres()), domain
    values = tuple(float(value) for value in domain)
    return (values if len(values) == 6 else None), None


def tolerance(bounds) -> float:
    """The distance below which two points, or a point and a wall, coincide.

    snappyHexMesh's ``mergeDistance``: ``mergeTolerance`` times the size of
    the mesh bounding box.
    """
    if not bounds:
        return 1e-9
    diagonal = math.sqrt(sum((bounds[2 * axis + 1] - bounds[2 * axis]) ** 2
                             for axis in range(3)))
    return max(MERGE_TOLERANCE * diagonal, 1e-12)


def _named(points) -> list[tuple[str, tuple]]:
    rows = []
    for index, entry in enumerate(points):
        if (isinstance(entry, (tuple, list)) and len(entry) == 2
                and isinstance(entry[0], str)):
            rows.append((entry[0] or f'exclude {index + 1}', _xyz(entry[1])))
        else:
            rows.append((f'exclude {index + 1}', _xyz(entry)))
    return rows


def _seeds(seeds) -> list[tuple[str, str, tuple]]:
    rows = []
    for index, entry in enumerate(seeds or ()):
        if isinstance(entry, (tuple, list)) and len(entry) == 3 \
                and not isinstance(entry[0], (int, float)):
            rows.append((str(entry[0]), str(entry[1]), _xyz(entry[2])))
        else:
            rows.append((f'region {index + 1}', 'fluid', _xyz(entry)))
    return rows


# -- surfaces --------------------------------------------------------------- #

def triangles(surface) -> np.ndarray:
    """``(N, 3, 3)`` triangle corners of a vtkPolyData, or of an array."""
    if surface is None:
        return np.zeros((0, 3, 3))
    if isinstance(surface, np.ndarray):
        return np.asarray(surface, dtype=float).reshape(-1, 3, 3)
    if not hasattr(surface, 'GetNumberOfCells'):
        return np.asarray(surface, dtype=float).reshape(-1, 3, 3)
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkFiltersCore import vtkTriangleFilter

    triangle_filter = vtkTriangleFilter()
    triangle_filter.SetInputData(surface)
    triangle_filter.PassLinesOff()
    triangle_filter.PassVertsOff()
    triangle_filter.Update()
    output = triangle_filter.GetOutput()
    if not output.GetNumberOfCells() or output.GetPoints() is None:
        return np.zeros((0, 3, 3))
    points = vtk_to_numpy(output.GetPoints().GetData()).astype(float)
    cells = vtk_to_numpy(output.GetPolys().GetConnectivityArray())
    return points[cells.reshape(-1, 3)]


def surface_distance(point, tris: np.ndarray) -> float:
    """The exact distance from *point* to the nearest of *tris*.

    Ericson's closest-point-on-triangle, vectorised over the triangles.
    """
    if tris is None or not len(tris):
        return math.inf
    p = np.asarray(point, dtype=float)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum('ij,ij->i', ab, ap)
    d2 = np.einsum('ij,ij->i', ac, ap)
    bp = p - b
    d3 = np.einsum('ij,ij->i', ab, bp)
    d4 = np.einsum('ij,ij->i', ac, bp)
    cp = p - c
    d5 = np.einsum('ij,ij->i', ab, cp)
    d6 = np.einsum('ij,ij->i', ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    with np.errstate(divide='ignore', invalid='ignore'):
        denom = va + vb + vc
        v = np.where(denom != 0, vb / denom, 0.0)
        w = np.where(denom != 0, vc / denom, 0.0)
        closest = a + ab * v[:, None] + ac * w[:, None]
        # vertex regions
        closest = np.where(((d1 <= 0) & (d2 <= 0))[:, None], a, closest)
        closest = np.where(((d3 >= 0) & (d4 <= d3))[:, None], b, closest)
        closest = np.where(((d6 >= 0) & (d5 <= d6))[:, None], c, closest)
        # edge regions
        t_ab = np.where(d1 - d3 != 0, d1 / (d1 - d3), 0.0)
        edge_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        closest = np.where(edge_ab[:, None], a + ab * t_ab[:, None], closest)
        t_ac = np.where(d2 - d6 != 0, d2 / (d2 - d6), 0.0)
        edge_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        closest = np.where(edge_ac[:, None], a + ac * t_ac[:, None], closest)
        bc = c - b
        num = d4 - d3
        t_bc = np.where(num + (d5 - d6) != 0, num / (num + (d5 - d6)), 0.0)
        edge_bc = (va <= 0) & (num >= 0) & ((d5 - d6) >= 0)
        closest = np.where(edge_bc[:, None], b + bc * t_bc[:, None], closest)
    return float(np.sqrt(np.min(np.einsum('ij,ij->i', closest - p,
                                          closest - p))))


# -- the checks ------------------------------------------------------------- #

def _wall_distance(point, bounds) -> float:
    return min(min(point[axis] - bounds[2 * axis],
                   bounds[2 * axis + 1] - point[axis]) for axis in range(3))


def exact_findings(points, *, seeds=(), domain=None, surfaces=None,
                   cell_size=None) -> list[Finding]:
    """The checks that need no voxels: each one either holds or it does not.

    *points* are ``(name, xyz)`` pairs or bare points, in metres; *seeds* are
    the region seeds, ``(label, type, xyz)`` or bare points; *domain* is a
    `DomainBox` or six bounds in metres; *surfaces* vtkPolyData or triangle
    arrays; *cell_size* the base cell, for the near-a-surface warning.
    """
    bounds, box = _bounds(domain)
    tol = tolerance(bounds)
    tris = None
    if surfaces:
        parts = [triangles(surface) for surface in surfaces]
        parts = [part for part in parts if len(part)]
        tris = np.concatenate(parts) if parts else None
    seed_rows = _seeds(seeds)
    findings: list[Finding] = []
    for name, point in _named(points):
        if bounds is not None:
            # `DomainBox.contains` answers in block vertex units.
            inside = (box.contains(tuple(value / (box.scale or 1.0)
                                         for value in point))
                      if box is not None else all(
                bounds[2 * axis] - tol <= point[axis]
                <= bounds[2 * axis + 1] + tol for axis in range(3)))
            if not inside:
                findings.append(Finding(
                    OUTSIDE_DOMAIN, ERROR,
                    f'exclude point {name} {_fmt(point)} is outside the '
                    f'background domain; OpenFOAM 13 would skip it and keep '
                    f'every space', name))
                continue
            if _wall_distance(point, bounds) <= tol:
                findings.append(Finding(
                    ON_DOMAIN_WALL, ERROR,
                    f'exclude point {name} {_fmt(point)} is on the boundary '
                    f'of the background domain; OpenFOAM 13 finds no cell '
                    f'for it and removes nothing', name))
                continue
        at_seed = next((label for label, _kind, seed in seed_rows
                        if math.dist(seed, point) <= tol), None)
        if at_seed is not None:
            findings.append(Finding(
                AT_SEED, ERROR,
                f'exclude point {name} {_fmt(point)} is on the region seed '
                f'{at_seed}; the seed wins in OpenFOAM 13, so nothing would be '
                f'removed', name))
            continue
        if tris is not None:
            distance = surface_distance(point, tris)
            if distance <= tol:
                findings.append(Finding(
                    ON_SURFACE, ERROR,
                    f'exclude point {name} {_fmt(point)} is on a surface; '
                    f'OpenFOAM 13 cannot tell which side is meant and removes '
                    f'nothing', name))
                continue
            if cell_size and distance < 0.5 * float(cell_size):
                findings.append(Finding(
                    NEAR_SURFACE, WARNING,
                    f'exclude point {name} {_fmt(point)} is {distance:.3g} m '
                    f'from a surface, closer than half a background cell; '
                    f'the cell it lands in may be cut by the surface. Move '
                    f'it into the middle of the space it should remove', name))
    return findings


def preflight(points, *, seeds=(), domain=None, surfaces=None, field=None,
              finer=None, cell_size=None) -> Report:
    """Every check, and the space each exclude point will remove.

    *field* is the labelled domain (`fluid_spaces.FluidSpaces`) at h and
    *finer* the same at h/2, or ``None``; see :func:`exact_findings` for the
    rest. Nothing is labelled here.
    """
    report = Report(exact_findings(points, seeds=seeds, domain=domain,
                                   surfaces=surfaces, cell_size=cell_size))
    named = _named(points)
    refused = {one.point for one in report.errors}
    seed_rows = _seeds(seeds)
    if named and seed_rows:
        report.findings.append(Finding(
            KEEPS_UNSEEDED, WARNING,
            SEEDS_AND_EXCLUDES_TEXT))
    if field is None:
        return report
    from .fluid_regions import APPROXIMATE, CONFIRMED, graded_conflicts

    for name, point in named:
        if name in refused:
            continue
        found = field.space_at(point)
        if found is None or found.label == 0:
            report.findings.append(Finding(
                UNRESOLVED_POINT, WARNING,
                f'exclude point {name} {_fmt(point)} is on a wall of the '
                f'labelled domain, so the space it removes is not known; move '
                f'it away from the surface', name, APPROXIMATE))
            continue
        report.removals.append({
            'point': name, 'space': int(found.label),
            'volume': float(found.volume), 'outside': bool(found.outside)})
        rows = [(label, kind, seed) for label, kind, seed in seed_rows]
        rows.append((f'\0{name}', 'exclude', point))
        for group in graded_conflicts(field, rows, finer):
            if f'\0{name}' not in group['regions']:
                continue
            others = [label for label, kind in
                      zip(group['regions'], group['types'])
                      if kind != 'exclude']
            if not others:
                continue
            confirmed = group['confidence'] == CONFIRMED
            report.findings.append(Finding(
                SAME_SPACE_AS_SEED, ERROR if confirmed else WARNING,
                f'exclude point {name} {_fmt(point)} is in the same space as '
                f'region {", ".join(others)}; the seed wins in OpenFOAM 13 and '
                f'nothing is removed'
                + ('' if confirmed else
                   ' (from the voxel labelling, which may miss a thin wall)'),
                name, group['confidence']))
    return report


def _fmt(point) -> str:
    return '(' + ' '.join(f'{value:.6g}' for value in point) + ')'
