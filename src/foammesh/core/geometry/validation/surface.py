"""Generating the independent surface, not merely sizing it.

Plan 23 §5.2. Recording the deflection a reference *should* have is not a
reference; the surface has to exist, and for a CAD source it has to be a third
surface — distinct from both the mesh and the tessellation handed to the
mesher — or a defect present in the mesher's input would certify itself.

Two classes, two strategies:

* **CAD-backed** — re-tessellate the prepared CAD at §16.1's deflection, which
  is materially tighter than the tessellation the mesher received. The result
  is written as its own file and fingerprinted.
* **Discrete** — the imported surface *is* the truth available to FoamMesh, so
  there is nothing to generate and nothing to be tighter than. §5.2 accepts
  this and requires the report to say the check proves conformance only to that
  discrete source. No second copy is written: copying a file to call it
  independent would be theatre.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
from pathlib import Path

import numpy as np

SURFACE_SUFFIX = '.validation.vtp'


class ValidationSurfaceError(ValueError):
    pass


@dataclass(frozen=True)
class SurfaceRecord:
    """One generated or adopted reference surface."""

    geometry_id: str
    strategy: str                 # occt_tessellation | imported_surface
    path: str
    sha256: str
    triangles: int = 0
    points: int = 0
    #: The deflection actually requested of the tessellator, for provenance.
    linear_deflection: float = 0.0

    def to_dict(self) -> dict:
        return {
            'geometry_id': self.geometry_id, 'strategy': self.strategy,
            'path': self.path, 'sha256': self.sha256,
            'triangles': self.triangles, 'points': self.points,
            'linear_deflection': self.linear_deflection,
        }


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def write_polydata(polydata, destination: Path) -> tuple[int, int]:
    """Persist a surface, returning ``(points, triangles)``."""
    from vtkmodules.vtkIOXML import vtkXMLPolyDataWriter

    destination.parent.mkdir(parents=True, exist_ok=True)
    writer = vtkXMLPolyDataWriter()
    writer.SetFileName(str(destination))
    writer.SetInputData(polydata)
    writer.SetDataModeToBinary()
    if not writer.Write():
        raise ValidationSurfaceError(f'could not write {destination}')
    return polydata.GetNumberOfPoints(), polydata.GetNumberOfCells()


def read_polydata(path: str | Path):
    """Read a reference surface, whatever form it was recorded in (R177).

    This used the XML reader unconditionally. A CAD source is tessellated to
    ``.vtp`` and read back fine; a **discrete** source is adopted in place --
    `build()` records the imported ``.stl`` itself as the reference, because
    that surface *is* the truth available. Handed an STL, the XML reader does
    not raise: it logs and returns a polydata whose ``GetPoints()`` is None,
    which the caller treats as a corrupt file and skips. So every STL-imported
    case lost its reference silently, and that is nearly every case.
    """
    suffix = Path(path).suffix.lower()
    if suffix == '.stl':
        from vtkmodules.vtkIOGeometry import vtkSTLReader

        reader = vtkSTLReader()
        # R179. An STL written by the feature-angle split carries one named
        # solid per prepared face, and that is the only record of which
        # triangles belong to which boundary. Asking for the tags costs one
        # cell array and is what makes a per-boundary reference possible.
        reader.SetScalarTags(True)
    elif suffix == '.obj':
        from vtkmodules.vtkIOGeometry import vtkOBJReader

        reader = vtkOBJReader()
    elif suffix == '.vtk':
        from vtkmodules.vtkIOLegacy import vtkPolyDataReader

        reader = vtkPolyDataReader()
    else:
        from vtkmodules.vtkIOXML import vtkXMLPolyDataReader

        reader = vtkXMLPolyDataReader()
    reader.SetFileName(str(path))
    reader.Update()
    return reader.GetOutput()


#: Cell-data array `vtkSTLReader` writes when asked to keep solid tags.
SOLID_LABEL_ARRAY = 'STLSolidLabeling'

#: Cell-data array `cad.tessellate.tessellate` writes: the index, in
#: `TopExp_Explorer` face order, of the CAD face each triangle came from.
#: R206. The two producers of a reference surface tag their triangles the
#: same way and name the array differently, and only the STL name was ever
#: looked for. So a CAD reference read as untagged, `as_arrays_by_solid`
#: returned {}, and R179's rule -- a body-wide reference cannot say which
#: of several boundaries it is, so stay unrated -- fired on every CAD case
#: with more than one patch per body, which is every CAD case. MEASURED on
#: tee_gmsh_r2: the reference on disk carries cadFaceId 0..4 with exactly
#: the face indices the group manifest names (wall 0 and 3, inlet 1,
#: outlet_top 2, outlet_branch 4), and all four sections still read "no
#: reference for section".
CAD_FACE_ARRAY = 'cadFaceId'


def as_arrays_by_solid(polydata) -> dict:
    """``solid index -> (vertices, triangles)``, or ``{}`` when untagged.

    R179. A boundary is one face of a body, not the body. The reference is
    recorded per *body*, so a section measured against it is compared with
    every other face of the same part -- and because the comparison is a
    bidirectional Hausdorff, the reference-to-mesh direction then reports the
    distance from the far end of the body to the section. MEASURED on the live
    tee: `wall` scored 0.53 mm against its 0.68 mm tolerance while `inlet`,
    `outlet_top` and `outlet_branch` scored 0.6 m, 0.6 m and 0.4 m -- exactly
    the body's z-extent and the branch offset, on a mesh that follows the
    geometry. Every section but the largest was reported as three orders of
    magnitude out.

    The split that names the boundaries also writes them as named solids, so
    the subset a section should be measured against is already on disk.
    """
    labels = solid_labels(polydata)
    if labels is None:
        return {}
    vertices, triangles = as_arrays(polydata)
    if len(labels) != len(triangles):
        # The tags do not describe this triangle set, so nothing here can be
        # attributed to a face. Better no subset than a mislabelled one.
        return {}
    subsets = {}
    for label in np.unique(labels):
        selected = triangles[labels == label]
        if not len(selected):
            continue
        subsets[int(label)] = (vertices, selected)
    return subsets


def solid_labels(polydata):
    """Per-triangle face index, or ``None`` when the surface records none.

    Both reference strategies record it; they disagree only on the name.
    An adopted STL carries `STLSolidLabeling`, one solid per prepared
    face; a re-tessellated CAD body carries `cadFaceId`, one value per
    OCCT face. Either is the answer to the same question, so either is
    read. The STL name is preferred where both exist, because a surface
    that was adopted rather than generated is the one the mesher saw.
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy

    cells = polydata.GetCellData() if polydata is not None else None
    if cells is None:
        return None
    for name in (SOLID_LABEL_ARRAY, CAD_FACE_ARRAY):
        array = cells.GetArray(name)
        if array is not None:
            return vtk_to_numpy(array).astype(np.int64)
    return None


def as_arrays(polydata) -> tuple[np.ndarray, np.ndarray]:
    """``(vertices, triangles)`` from a surface, for the sampler."""
    from vtkmodules.util.numpy_support import vtk_to_numpy

    points = vtk_to_numpy(polydata.GetPoints().GetData()).astype(np.float64)
    raw = vtk_to_numpy(polydata.GetPolys().GetData())
    triangles = []
    index = 0
    while index < len(raw):
        count = int(raw[index])
        if count == 3:
            triangles.append(raw[index + 1:index + 4])
        index += count + 1
    return points, (np.asarray(triangles, dtype=np.int64) if triangles
                    else np.empty((0, 3), dtype=np.int64))


def tessellate_cad(cad_path: str | Path, deflection: float, *,
                   angular_deflection_deg: float = 5.0):
    """Re-tessellate a CAD source at the reference deflection.

    Kept behind its own function so a caller can substitute one in a test: the
    OCCT dependency is optional, and the surrounding budget and provenance
    logic must remain testable without it.
    """
    # R199. This read `CadImporter().read_shape(...)`. There is no CadImporter
    # in core -- the only class by that name is the GUI's file-dialog importer
    # in foammesh.view.geometry. Reading a CAD shape here has always been the
    # module function below. The ImportError was caught by the caller and
    # written into the reference as a reason string, so every CAD-backed case
    # reported "no reference for section" instead of failing loudly, and no
    # test caught it because every test substitutes `tessellator`.
    from foammesh.core.geometry.cad import worker_client

    if not worker_client.in_worker():
        # Plan 35 CR7: OCCT re-facets the reference in a worker.
        with worker_client.call(
                'cad.tessellate_file',
                {'cad_path': str(Path(cad_path).resolve()),
                 'deflection': float(deflection),
                 'angular_deflection_deg': float(angular_deflection_deg)},
                label='CAD reference tessellation',
                source=cad_path) as answer:
            surface = answer['tessellation']
            return worker_client.read_polydata(
                surface['path'], surface['sha256'])
    from foammesh.core.geometry.cad.cad_importer import read_shape
    from foammesh.core.geometry.cad.tessellate import (
        TessellationParams, tessellate,
    )

    shape = read_shape(str(cad_path))
    return tessellate(shape, TessellationParams(
        linear_deflection=float(deflection),
        angular_deflection_deg=float(angular_deflection_deg)))


def _diagonal(low_high) -> float:
    """Bounding-box diagonal from a VTK-order ``(xmin, xmax, ymin, ...)``."""
    values = [float(value) for value in low_high]
    if len(values) != 6:
        return 0.0
    return float(np.linalg.norm(
        np.asarray(values[1::2]) - np.asarray(values[0::2])))


def _known_unit_factor(ratio: float) -> float | None:
    """``ratio`` as a length-unit conversion, if it is one.

    Snapping rather than accepting the raw ratio keeps a repaired body -- whose
    prepared bounding box legitimately differs from the CAD by a fraction of a
    percent -- from being scaled by that fraction.
    """
    from foammesh.core.geometry.units import UNIT_TO_M

    if not ratio or ratio != ratio:
        return None
    for factor in sorted({1.0, *UNIT_TO_M.values()}):
        if abs(ratio - factor) <= 0.005 * factor:
            return factor
    return None


def cad_scale_to_metres(record: Mapping, polydata=None) -> float:
    """Metres per unit of the prepared CAD copy for ``record`` (R207).

    The prepared manifest declares `units: 'm'`, and the surface handed to the
    mesher is converted on import. The CAD file beside it is copied byte for
    byte, so it is still in whatever the reader emitted -- millimetres for
    every STEP this application has seen. Re-tessellating it and calling the
    result a reference put a 600 mm tee's reference at 600 m, and the fidelity
    report then read "worst deviation 601.5 m against a 0.0006782 m tolerance"
    on a mesh that follows its geometry exactly.

    Two answers to the same question, in order of trust:

    * the ``bbox`` the manifest recorded for this source, in metres, measured
      against the surface just tessellated -- available for every prepared
      revision ever written, and a measurement rather than a claim;
    * ``cad_unit``, recorded from this revision onwards.

    Neither available, or a ratio that is not a unit conversion: 1.0, which
    leaves the surface as tessellated.
    """
    if polydata is not None:
        measured = _diagonal(polydata.GetBounds())
        target = _diagonal(record.get('bbox') or ())
        if measured > 0 and target > 0:
            factor = _known_unit_factor(target / measured)
            if factor is not None:
                return factor

    unit = str(record.get('cad_unit') or '')
    if unit:
        from foammesh.core.geometry.units import si_factor

        try:
            return float(si_factor(unit))
        except (KeyError, ValueError):
            return 1.0
    return 1.0


def build(record: dict, *, destination_dir: Path, deflection: float,
          prepared_root: Path, tessellator=None) -> SurfaceRecord:
    """Produce the reference surface for one prepared source."""
    geometry_id = str(record.get('geometry_id') or '')
    fmt = str(record.get('source_format') or '').lower()
    discrete = fmt in {'stl', 'obj', 'vtk', 'vtp', ''}

    if discrete:
        # The imported surface is the reference. Adopted in place, with its own
        # checksum recorded, rather than duplicated.
        name = record.get('surface_prepared_name') or record.get('prepared_name')
        source = Path(prepared_root) / 'sources' / str(name)
        if not source.is_file():
            raise ValidationSurfaceError(
                f'prepared surface is missing for {geometry_id}: {source}')
        return SurfaceRecord(
            geometry_id, 'imported_surface', str(source), _digest(source))

    primary = Path(prepared_root) / 'sources' / str(
        record.get('cad_prepared_name') or record.get('prepared_name'))
    if not primary.is_file():
        raise ValidationSurfaceError(
            f'prepared CAD is missing for {geometry_id}: {primary}')

    make = tessellator or tessellate_cad
    # R207. `deflection` is a length in metres, and the CAD copy is not in
    # metres. Tessellating first at the requested number tells us the scale
    # (and, when the source really is in metres, costs nothing more); the
    # deflection is then re-expressed in the file's own unit so the reference
    # is as fine as it was asked to be and no finer.
    polydata = make(primary, deflection)
    factor = cad_scale_to_metres(record, polydata)
    if factor != 1.0:
        from foammesh.core.geometry.units import scale_polydata

        polydata = scale_polydata(make(primary, deflection / factor), factor)
    destination = Path(destination_dir) / f'{geometry_id}{SURFACE_SUFFIX}'
    points, triangles = write_polydata(polydata, destination)
    return SurfaceRecord(
        geometry_id, 'occt_tessellation', str(destination),
        _digest(destination), triangles, points, float(deflection))
