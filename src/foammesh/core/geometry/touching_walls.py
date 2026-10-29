"""Which surfaces of different bodies touch, and whether their triangles match.

The surface roles editor's Automatic choice (DP-6087). A person choosing
"Region interface" no longer has to know which wall of the other body lies
against this one: FoamMesh measures it.

Two answers per touching pair, because the two interface kinds need different
things:

* **shared triangles** -- the same three corners drawn by both bodies (the
  test :mod:`interface_split` cuts on). A conformal interface on snappy is one
  shared surface, so only these can be cut into one;
* **touching faces** -- the centroids of the smaller surface's triangles lie on
  the other surface within a tolerance. A non-conformal interface couples two
  separate walls whose triangles need not match, so this is what pairs them.

Two walls that touch but share no triangles are reported as such, so the
editor can say a conformal cut is not possible there and offer non-conformal,
instead of doing nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Share of the smaller surface's triangles that must lie on the other for the
#: two to count as touching. A cap that only meets another along an edge has
#: a thin ring of triangles near it, never a fifth of its faces.
TOUCH_SHARE = 0.2

#: The tolerance as a share of the smaller surface's median triangle edge:
#: wide enough for the chord sag of two separately faceted coaxial cylinders,
#: far below any gap between two walls a mesh would resolve.
EDGE_SHARE = 0.05

#: And never below this share of the model's size (floating-point noise).
SIZE_SHARE = 1e-6


@dataclass
class Wall:
    """One surface of one body: ``triangles`` is an ``(n, 3, 3)`` array."""
    key: str
    name: str
    body: str
    triangles: object
    low: object = field(init=False)
    high: object = field(init=False)

    def __post_init__(self):
        import numpy as np

        self.triangles = np.asarray(self.triangles, dtype=float).reshape(-1, 3, 3)
        if len(self.triangles):
            points = self.triangles.reshape(-1, 3)
            self.low, self.high = points.min(0), points.max(0)
        else:
            self.low = self.high = np.zeros(3)


@dataclass
class Contact:
    a: str
    b: str
    a_name: str
    b_name: str
    a_body: str
    b_body: str
    shared: int        # triangles both bodies draw at the same corners
    touching: float    # share of the smaller surface lying on the other
    tolerance: float

    @property
    def conformal(self) -> bool:
        """Can the two be cut into one shared surface?"""
        return self.shared > 0

    def to_dict(self) -> dict:
        return {'surfaces': [self.a, self.b],
                'names': [self.a_name, self.b_name],
                'bodies': [self.a_body, self.b_body],
                'shared_triangles': self.shared,
                'touching_share': round(self.touching, 4),
                'tolerance': self.tolerance,
                'conformal_possible': self.conformal}


def triangles_of(polydata):
    """The triangles of a VTK polydata as an ``(n, 3, 3)`` array."""
    import numpy as np
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkFiltersCore import vtkTriangleFilter

    if polydata is None or polydata.GetNumberOfCells() == 0:
        return np.zeros((0, 3, 3))
    triangulate = vtkTriangleFilter()
    triangulate.SetInputData(polydata)
    triangulate.PassVertsOff()
    triangulate.PassLinesOff()
    triangulate.Update()
    surface = triangulate.GetOutput()
    points = vtk_to_numpy(surface.GetPoints().GetData()).astype(float)
    cells = vtk_to_numpy(surface.GetPolys().GetData()).reshape(-1, 4)[:, 1:]
    return points[cells]


def _corner_keys(triangles) -> set:
    """Each triangle as its three corners, rounded as the cut welds them."""
    import numpy as np

    rounded = np.round(triangles, 9) + 0.0
    keys = set()
    for triangle in rounded:
        keys.add(tuple(sorted(tuple(corner) for corner in triangle)))
    return keys


def _median_edge(triangles) -> float:
    import numpy as np

    if not len(triangles):
        return 0.0
    edges = np.linalg.norm(triangles - np.roll(triangles, 1, axis=1), axis=-1)
    return float(np.median(edges))


def touching(walls: list[Wall]) -> list[Contact]:
    """Every pair of surfaces of different bodies that touch."""
    import numpy as np

    from .interface_contact import _distances

    walls = [wall for wall in walls if len(wall.triangles)]
    if len(walls) < 2:
        return []
    low = np.min([wall.low for wall in walls], axis=0)
    high = np.max([wall.high for wall in walls], axis=0)
    floor = SIZE_SHARE * float(np.linalg.norm(high - low))
    keys: dict = {}
    contacts = []
    for index, first in enumerate(walls):
        for second in walls[index + 1:]:
            if first.body == second.body:
                continue
            small, large = ((first, second)
                            if len(first.triangles) <= len(second.triangles)
                            else (second, first))
            tolerance = max(floor, EDGE_SHARE * _median_edge(small.triangles))
            if np.any(small.low > large.high + tolerance) \
                    or np.any(large.low > small.high + tolerance):
                continue
            distances = _distances(small.triangles.mean(axis=1),
                                   large.triangles)
            share = float((distances <= tolerance).mean())
            if share < TOUCH_SHARE:
                continue
            for wall in (first, second):
                if wall.key not in keys:
                    keys[wall.key] = _corner_keys(wall.triangles)
            shared = len(keys[first.key] & keys[second.key])
            contacts.append(Contact(
                first.key, second.key, first.name, second.name, first.body,
                second.body, shared, share, tolerance))
    return contacts
