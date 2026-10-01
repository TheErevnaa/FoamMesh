"""On Gmsh, a region is a solid: Plan 36 RP11.

snappyHexMesh keeps the connected space a seed sits in, so its regions are
found by labelling the domain (RP5) and placed by a point. Gmsh meshes the
volumes the geometry already has, so there is nothing to seed and nothing to
label: each solid *is* a region, and "how many fluid regions?" is answered by
listing the solids.

What a solid is here:

* **CAD (STEP/IGES/BREP).** Each OCC solid of the stored CAD artifact,
  matched to the import's region record by the faces it owns (the patch
  ``face_order`` is the same flat-walk index the tessellation carries as
  ``cadFaceId``). Its volume is OCC's mass property, in cubic metres. A
  region record that is no OCC solid -- the "loose faces" part an assembly
  with stray faces imports as -- is not a solid and is not listed.
* **Tessellated (STL/OBJ).** Each region record whose own triangles bound a
  closed shell (every edge used by exactly two of them). Its volume is
  ``vtkMassProperties`` of that shell. An open shell is not a solid; it is
  listed in ``open_regions`` so the caller can say why nothing was found.

Every solid is keyed by the ``region_uuid`` the import minted. That is the
identity the prepared geometry, the Gmsh volume controls (``scopeToken``) and
the geometry tree's volume rows (``regionUuid``) all use, and it survives
re-detection, reordering and renaming -- so typing a solid is keyed by it,
never by the solid's position in a list.

Nothing here touches Qt; the shells are built only when asked for
(``with_surfaces``), for the viewport.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: What the detect result says it was answered from.
SOURCE = 'solids'

#: How each solid's volume was measured.
MEASURED_OCC = 'occ'
MEASURED_SHELL = 'shell'

#: Why a region is listed in ``not_solids`` (RP13).
NO_OCC_SOLID = 'not_a_solid'
DEGENERATE = 'degenerate'

#: A CAD solid is degenerate -- a sliver an exporter left, not a volume to
#: mesh -- when its volume is below this fraction of the model's bounding
#: box volume, or when its mean thickness (volume / surface area) is below
#: this fraction of the model's bounding-box diagonal. The thickness test is
#: what catches a sheet: ``assembly_mixed.step``'s second solid is 1 mm x
#: 1 mm x 1.1 nm, 1e-6 of the model box by volume but 3e-7 of its diagonal
#: thick. A real thin part -- a 1 mm plate 10 cm square in a 1 m model -- is
#: 3e-4 of the diagonal thick and is kept.
DEGENERATE_VOLUME_FRACTION = 1e-9
DEGENERATE_THICKNESS_FRACTION = 1e-6


@dataclass(frozen=True)
class Solid:
    """One solid of the case, as a region."""

    region_uuid: str
    name: str
    geometry_id: str
    volume: float
    bounds: tuple
    measured: str

    def to_dict(self) -> dict:
        return {'region_uuid': self.region_uuid, 'name': self.name,
                'geometry_id': self.geometry_id, 'volume': self.volume,
                'bounds': list(self.bounds), 'measured': self.measured}


@dataclass
class CaseSolids:
    """Every solid of the case, and the regions that are not solids."""

    solids: list = field(default_factory=list)
    #: Region names whose shell does not close (tessellated sources).
    open_regions: list = field(default_factory=list)
    #: Region names that are no OCC solid (CAD "loose faces"), or whose
    #: solid is degenerate (RP13).
    not_solids: list = field(default_factory=list)
    #: Region name -> why it is in ``not_solids``: ``not_a_solid`` or
    #: ``degenerate``.
    not_solid_reasons: dict = field(default_factory=dict)
    #: ``region_uuid`` -> the solid's shell, in metres (``with_surfaces``).
    surfaces: dict = field(default_factory=dict)
    elapsed: float = 0.0

    def by_uuid(self) -> dict:
        return {solid.region_uuid: solid for solid in self.solids}


# -- the region records ----------------------------------------------------- #

def entry_regions(entry: dict) -> list[dict]:
    """The entry's region records, as the prepared geometry reads them.

    An entry without any (a plain STL) is one region, under the same
    synthesised ``region_uuid`` ``PreparedGeometryStore._regions`` gives it,
    so the identity here is the one the Gmsh volume controls scope to.
    """
    regions = list(entry.get('regions') or ())
    if regions:
        return regions
    return [{
        'region_uuid': 'region-' + hashlib.sha256(
            str(entry['geometry_id']).encode()).hexdigest()[:24],
        'name': entry.get('name') or entry['geometry_id'],
        'boundary_patch_uuids': [
            str(patch.get('patch_uuid') or '')
            for patch in entry.get('patches', ()) if patch.get('patch_uuid')],
    }]


def _region_face_ids(entry: dict, region: dict) -> set[int]:
    """The ``cadFaceId`` values of the faces that bound *region*."""
    wanted = {str(value) for value in region.get('boundary_patch_uuids') or ()}
    ids: set[int] = set()
    for patch in entry.get('patches') or ():
        if str(patch.get('patch_uuid') or '') not in wanted:
            continue
        for member in [patch, *(patch.get('members') or ())]:
            order = member.get('face_order')
            if isinstance(order, int) and order >= 0:
                ids.add(order)
                continue
            ref = member.get('source_ref') or {}
            index = ref.get('face_index')
            if isinstance(index, int) and index >= 0 and not entry.get(
                    'cad_artifact'):
                ids.add(index)
    return ids


# -- measuring -------------------------------------------------------------- #

def _surface(entry: dict):
    from foammesh.core.geometry.importers.base import import_surface

    artifact = Path(str(entry.get('artifact') or ''))
    if not artifact.is_file():
        return None
    try:
        return import_surface(artifact).surfaces[0].polydata
    except Exception as error:  # noqa: BLE001 - an unreadable artifact
        logger.debug('solids: cannot read %s: %s', artifact, error)
        return None


def _region_cells(entry: dict, regions: list, polydata) -> dict:
    """``region_uuid`` -> the ids of the surface cells bounding it."""
    import numpy as np
    from vtkmodules.util.numpy_support import vtk_to_numpy

    from foammesh.core.geometry.cad.surface_split import FACE_ID_ARRAY

    total = int(polydata.GetNumberOfCells())
    if len(regions) == 1:
        return {str(regions[0]['region_uuid']): np.arange(total)}
    array = polydata.GetCellData().GetArray(FACE_ID_ARRAY)
    if array is None:
        return {}
    face_ids = vtk_to_numpy(array).reshape(-1)
    out = {}
    for region in regions:
        ids = _region_face_ids(entry, region)
        if ids:
            out[str(region['region_uuid'])] = np.flatnonzero(
                np.isin(face_ids, sorted(ids)))
    return out


def _closed(polydata, cell_ids) -> bool:
    from foammesh.core.geometry.diagnostics.checks import _edge_use_census

    uses = _edge_use_census(polydata, [int(value) for value in cell_ids])
    return bool(uses) and all(count == 2 for count in uses.values())


def _shell(polydata, cell_ids):
    """The cells as their own surface (unused points dropped, triangles)."""
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
    from vtkmodules.vtkFiltersCore import vtkCleanPolyData, vtkTriangleFilter

    polys = vtkCellArray()
    for value in cell_ids:
        cell = polydata.GetCell(int(value))
        ids = cell.GetPointIds()
        polys.InsertNextCell(ids)
    picked = vtkPolyData()
    picked.SetPoints(polydata.GetPoints())
    picked.SetPolys(polys)
    triangles = vtkTriangleFilter()
    triangles.SetInputData(picked)
    clean = vtkCleanPolyData()
    clean.PointMergingOff()
    clean.SetInputConnection(triangles.GetOutputPort())
    clean.Update()
    out = vtkPolyData()
    out.DeepCopy(clean.GetOutput())
    return out


def shell_volume(polydata) -> float:
    """``vtkMassProperties`` volume of a closed triangulated shell."""
    from vtkmodules.vtkFiltersCore import vtkMassProperties

    if polydata is None or not polydata.GetNumberOfCells():
        return 0.0
    mass = vtkMassProperties()
    mass.SetInputData(polydata)
    mass.Update()
    return abs(float(mass.GetVolume()))


def occ_solids(entry: dict) -> list[tuple[float, set, tuple, float]] | None:
    """``[(volume m^3, cadFaceIds, bounds m, area m^2), ...]`` per OCC solid.

    None when there is no CAD artifact or OCC cannot read it; the caller
    then measures the tessellated shells instead. The faces are numbered by
    the same flat walk the importer numbered them by, which is what the
    tessellation's ``cadFaceId`` and each patch's ``face_order`` hold.
    """
    cad = Path(str(entry.get('cad_artifact') or ''))
    if not str(entry.get('cad_artifact') or '') or not cad.is_file():
        return None
    from foammesh.core.geometry.cad import worker_client

    if worker_client.in_worker():
        return _occ_solids_here(entry, cad)
    # Plan 35 CR7: OCCT measures the solids in a worker. A worker that
    # crashed or could not start is "OCC cannot read it": the tessellated
    # shells are measured instead, as they always were.
    try:
        with worker_client.call('cad.solids', {'entry': dict(entry)},
                                label='CAD solids', source=cad) as answer:
            found = answer.get('solids')
    except Exception as error:  # noqa: BLE001 - the fallback is the answer
        logger.warning('solids: OCC could not measure %s: %s', cad, error)
        return None
    if found is None:
        return None
    return [(float(volume), set(ids), tuple(bounds), float(area))
            for volume, ids, bounds, area in found]


def _occ_solids_here(entry: dict, cad: Path):
    """:func:`occ_solids` with OCCT in this process: a worker's body."""
    try:
        from OCC.Core.Bnd import Bnd_Box
        from OCC.Core.BRepBndLib import brepbndlib
        from OCC.Core.BRepGProp import brepgprop
        from OCC.Core.GProp import GProp_GProps
        from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_SOLID
        from OCC.Core.TopExp import TopExp_Explorer, topexp
        from OCC.Core.TopTools import TopTools_IndexedMapOfShape

        from foammesh.core.geometry.cad import read_cad
        from foammesh.core.geometry.store import (
            GeometryArtifactStore, read_back_unit,
        )
    except Exception as error:  # noqa: BLE001 - OCC is an optional extra
        logger.debug('solids: OCC is not available: %s', error)
        return None
    try:
        shape, _model = read_cad(cad)
        linear = float(GeometryArtifactStore._unit_factor(
            read_back_unit(entry)))
        faces = []
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        while explorer.More():
            faces.append(explorer.Current())
            explorer.Next()
        found = []
        explorer = TopExp_Explorer(shape, TopAbs_SOLID)
        while explorer.More():
            solid = explorer.Current()
            owned = TopTools_IndexedMapOfShape()
            topexp.MapShapes(solid, TopAbs_FACE, owned)
            ids = {index for index, face in enumerate(faces)
                   if owned.Contains(face)}
            props = GProp_GProps()
            brepgprop.VolumeProperties(solid, props)
            surface = GProp_GProps()
            brepgprop.SurfaceProperties(solid, surface)
            box = Bnd_Box()
            brepbndlib.Add(solid, box)
            xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
            bounds = tuple(float(value) * linear
                           for value in (xmin, xmax, ymin, ymax, zmin, zmax))
            found.append((abs(float(props.Mass())) * linear ** 3, ids, bounds,
                          abs(float(surface.Mass())) * linear ** 2))
            explorer.Next()
        return found
    except Exception as error:  # noqa: BLE001 - OCCT raises many kinds
        logger.warning('solids: OCC could not measure %s: %s', cad, error)
        return None


# -- the case --------------------------------------------------------------- #

_cache: dict = {}
_cache_lock = threading.Lock()


def _entry_key(entry: dict, with_surfaces: bool) -> tuple:
    def stamp(path):
        try:
            item = Path(str(path))
            return (str(item), item.stat().st_mtime_ns, item.stat().st_size)
        except (OSError, ValueError):
            return (str(path), None, None)
    regions = tuple((str(region.get('region_uuid')), str(region.get('name')))
                    for region in entry_regions(entry))
    return (str(entry.get('geometry_id')), stamp(entry.get('artifact')),
            stamp(entry.get('cad_artifact')) if entry.get('cad_artifact')
            else None, regions, bool(with_surfaces))


def forget_cached() -> None:
    with _cache_lock:
        _cache.clear()


def _union_bounds(boxes) -> tuple | None:
    boxes = [box for box in boxes if box]
    if not boxes:
        return None
    return (min(box[0] for box in boxes), max(box[1] for box in boxes),
            min(box[2] for box in boxes), max(box[3] for box in boxes),
            min(box[4] for box in boxes), max(box[5] for box in boxes))


def degenerate(volume, area, model_bounds) -> bool:
    """Whether a solid of *volume* and surface *area* is a sliver (RP13).

    Judged against the model's bounding box, so the test does not depend
    on the unit or the size of the model: see `DEGENERATE_VOLUME_FRACTION`.
    """
    if model_bounds is None:
        return False
    sides = [max(0.0, float(model_bounds[2 * axis + 1])
                 - float(model_bounds[2 * axis])) for axis in range(3)]
    diagonal = sum(side * side for side in sides) ** 0.5
    if diagonal <= 0.0:
        return False
    box = sides[0] * sides[1] * sides[2]
    if box > 0.0 and volume < DEGENERATE_VOLUME_FRACTION * box:
        return True
    if area and area > 0.0:
        return volume / area < DEGENERATE_THICKNESS_FRACTION * diagonal
    return volume <= 0.0


def _entry_solids(entry: dict, with_surfaces: bool):
    polydata = _surface(entry)
    regions = entry_regions(entry)
    cells = _region_cells(entry, regions, polydata) if polydata is not None \
        else {}
    occ = occ_solids(entry)
    model = _union_bounds(item[2] for item in occ) if occ else None
    solids, open_regions, not_solids, surfaces = [], [], [], {}
    reasons = {}
    for region in regions:
        uuid = str(region.get('region_uuid') or '').strip()
        name = str(region.get('name') or uuid)
        if not uuid:
            continue
        region_cells = cells.get(uuid)
        shell = None
        if region_cells is not None and len(region_cells):
            shell = _shell(polydata, region_cells)
        if occ is not None:
            faces = _region_face_ids(entry, region)
            match = next((item for item in occ
                          if faces and faces <= item[1]), None)
            if match is None:
                not_solids.append(name)
                reasons[name] = NO_OCC_SOLID
                continue
            volume, _ids, bounds, area = match
            if degenerate(volume, area, model):
                # RP13: a sliver is not a region to type or mesh.
                not_solids.append(name)
                reasons[name] = DEGENERATE
                continue
            measured = MEASURED_OCC
        else:
            if (shell is None or not shell.GetNumberOfCells()
                    or not _closed(polydata, region_cells)):
                open_regions.append(name)
                continue
            volume = shell_volume(shell)
            if volume <= 0.0:
                open_regions.append(name)
                continue
            bounds = tuple(float(value) for value in shell.GetBounds())
            measured = MEASURED_SHELL
        solids.append(Solid(uuid, name, str(entry.get('geometry_id')),
                            float(volume), tuple(bounds), measured))
        if with_surfaces and shell is not None:
            surfaces[uuid] = shell
    return solids, open_regions, not_solids, surfaces, reasons


def case_solids(case_path=None, *, entries=None,
                with_surfaces: bool = False) -> CaseSolids:
    """Every solid of the case's staged geometry, in import order.

    Cached per entry by its artifact files and region records, so asking
    again after a type change costs nothing; any re-import, repair or split
    writes a new artifact and is measured afresh.
    """
    began = time.perf_counter()
    if entries is None:
        from foammesh.core.geometry import GeometryArtifactStore

        entries = GeometryArtifactStore(case_path).entries()
    result = CaseSolids()
    for entry in entries or ():
        key = _entry_key(entry, with_surfaces)
        with _cache_lock:
            answer = _cache.get(key)
        if answer is None:
            answer = _entry_solids(entry, with_surfaces)
            with _cache_lock:
                _cache[key] = answer
        solids, open_regions, not_solids, surfaces, reasons = answer
        result.solids.extend(solids)
        result.open_regions.extend(open_regions)
        result.not_solids.extend(not_solids)
        result.not_solid_reasons.update(reasons)
        result.surfaces.update(surfaces)
    result.elapsed = time.perf_counter() - began
    return result


# -- the typing ------------------------------------------------------------- #

#: The words a solid's type is shown and applied in.
FLUID = 'fluid'
SOLID = 'solid'
EXCLUDED = 'excluded'
TYPES = (FLUID, SOLID, EXCLUDED)


def typing_of(rows) -> dict:
    """``region_uuid`` -> ``{'type', 'included', 'control', 'name'}``.

    *rows* are the Gmsh volume-control rows (``{key: row}``, each a mapping
    or a db element). The first enabled-or-not row scoping a solid holds its
    typing; ``type`` is ``None`` for a solid nobody typed.
    """
    def value(row, key):
        if isinstance(row, dict):
            found = row.get(key)
        else:
            try:
                found = row.value(key)
            except Exception:  # noqa: BLE001 - a leaf the row lacks
                return None
        return getattr(found, 'value', found)

    out: dict = {}
    for key, row in sorted((rows or {}).items(),
                           key=lambda item: _numeric(item[0])):
        scope = str(value(row, 'scopeToken') or '').strip()
        if not scope or scope in out:
            continue
        kind = value(row, 'volumeType')
        included = value(row, 'included')
        out[scope] = {
            'type': str(kind) if kind else None,
            'included': True if included is None else _truthy(included),
            'control': str(key),
            'name': str(value(row, 'name') or ''),
        }
    return out


def shown_type(typing: dict | None) -> str | None:
    """Fluid, Solid or Excluded, as the viewport and detect say it."""
    if not typing:
        return None
    if not typing.get('included', True):
        return EXCLUDED
    return typing.get('type')


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ('true', '1', 'yes', 'on')
    return bool(value)


def _numeric(key):
    text = str(key)
    return (0, int(text), '') if text.isdigit() else (1, 0, text)


# -- DP-915: the far field ---------------------------------------------------- #

#: Plan 37 UF13. The far field is one of these; unset or unknown is the box,
#: the shape the runner builds by default.
FARFIELD_SHAPES = ('box', 'sphere', 'cylinder')


def _shape_word(shape) -> str:
    shape = str(shape or '').strip().lower()
    return shape if shape in FARFIELD_SHAPES else 'box'


def farfield_shape(db) -> str:
    """``box``, ``sphere`` or ``cylinder``: the shape of the case's far field."""
    if db is None:
        return 'box'
    try:
        value = db.getValue('gmsh/farfield/shape')
    except Exception:  # noqa: BLE001 - a case without the leaf
        value = None
    return _shape_word(getattr(value, 'value', value))


class FarfieldState(str):
    """The far field's shape when it is on, else ``''`` -- with ``.shape``.

    A string so every reader that asks "is it on" reads it as before; the
    ``shape`` attribute is the chosen shape whether the far field is on or
    off, so a note about a far field that is off can name the one chosen.
    """

    shape: str

    def __new__(cls, on: bool, shape: str = 'box'):
        shape = _shape_word(shape)
        state = super().__new__(cls, shape if on else '')
        state.shape = shape
        return state


def shape_of(farfield) -> str:
    """The shape *farfield* names: a `FarfieldState`, a shape string, or a
    bare flag (the box)."""
    shape = getattr(farfield, 'shape', None)
    if shape:
        return _shape_word(shape)
    return _shape_word(farfield if isinstance(farfield, str) else 'box')


def farfield_enabled(db) -> str:
    """The far field's shape when the case asks Gmsh for one, else ``''``.

    Truthy exactly when the far field is on, so every reader that asks
    "is it on" reads it as before; Plan 37 UF13 made it the shape so that
    `propose`, handed this value, names a sphere or a cylinder as one. It is
    a `FarfieldState`, whose ``shape`` is the chosen shape even when off.
    """
    if db is None:
        return FarfieldState(False)
    try:
        value = db.getValue('gmsh/farfield/enabled')
    except Exception:  # noqa: BLE001 - a case without the leaf
        return FarfieldState(False)
    return FarfieldState(_truthy(getattr(value, 'value', value)),
                         farfield_shape(db))


def farfield_cuts(found: CaseSolids, enabled) -> bool:
    """Whether the far field subtracts every solid of *found*.

    DP-915. With the box on, the runner cuts it against every imported
    volume (``occ.cut`` with ``removeTool``) and every imported volume is
    consumed: what remains is the fluid around the bodies, which no solid
    owns. The cut needs OCC solids -- a tessellated import builds no box and
    meshes its shells as they are -- so it happens only when every solid is
    a CAD solid.
    """
    return bool(enabled) and bool(found.solids) and all(
        solid.measured == MEASURED_OCC for solid in found.solids)


def farfield_note(names, shape: str = 'box') -> str:
    """What detect says when the far field is the fluid (DP-915).

    Plan 37 UF13: *shape* is the far field's, so a sphere is not called a
    box. The box sentence is unchanged.
    """
    shape = _shape_word(shape)
    names = [str(name) for name in names]
    bodies = (f"'{names[0]}'" if len(names) == 1
              else ', '.join(f"'{name}'" for name in names))
    noun = 'body' if len(names) == 1 else 'bodies'
    return (f'The far-field {shape} is on: the run cuts {bodies} out of the '
            f'{shape}, '
            f'so the {noun} {"is" if len(names) == 1 else "are"} the '
            'obstacle and the fluid is the space around '
            f'{"it" if len(names) == 1 else "them"}. That one fluid region '
            'is built by the run itself; there is nothing to apply.')


def farfield_off_note(shape: str = 'box') -> str:
    """What detect says in external flow with the far field off (DP-915).

    Plan 37 UF13: it names the shape chosen for the far field; the box
    sentence is `FARFIELD_OFF_NOTE`, unchanged.
    """
    shape = _shape_word(shape)
    return (f'External flow on Gmsh: the far-field {shape} is off, so the '
            f'run meshes the solids themselves. Turn the far-field {shape} '
            'on (describe the geometry) to mesh the space around the bodies, '
            'or accept a solid only if it is the flow domain around them.')


FARFIELD_OFF_NOTE = farfield_off_note('box')


def farfield_tessellated_note(shape: str = 'box') -> str:
    """Why a far field on a tessellated import is not built (DP-915).

    Plan 37 UF13: it names the far field's own shape; the box sentence is
    `FARFIELD_TESSELLATED_NOTE`, unchanged.
    """
    shape = _shape_word(shape)
    return (f'The far-field {shape} is on, but it is cut by the CAD kernel '
            f'and this geometry is tessellated, so the run builds no {shape} '
            'and meshes the solids themselves. Import the geometry as STEP '
            f'to have the {shape} built.')


FARFIELD_TESSELLATED_NOTE = farfield_tessellated_note('box')


def propose(found: CaseSolids, count: int, typing: dict | None = None, *,
            external: bool = False, farfield=False,
            shape: str | None = None) -> dict:
    """The RP7 detect payload, answered from the solids.

    ``spaces`` are the solids, largest first, each with RP7's keys (``seed``
    and ``depth`` are ``None``: a solid is not placed by a point) plus its
    ``region_uuid`` -- the key `apply` takes -- ``name``, ``type``,
    ``included`` and how its volume was ``measured``. ``proposed`` is the
    ``count`` largest solids not already typed Solid or Excluded.

    DP-915. With the far-field box on (*farfield*) and CAD solids, the cut
    subtracts every solid: each row is ``cut_away``, nothing is proposed,
    ``fluid`` is ``'farfield'`` and ``note`` says the outside is the fluid.
    The far-field domain is the one fluid region, so asking for one is no
    mismatch; asking for more is ``farfield_is_the_fluid``. In external flow
    otherwise, as on the voxel path (DP-864), the solids past the count are
    not a surplus, and ``note`` says the box is off.

    Plan 37 UF13. *farfield* may be the far field's shape, as
    `farfield_enabled` returns it (whose ``shape`` survives the far field
    being off), or *shape* names it; every note then says sphere or cylinder
    rather than box. Neither given is the box.
    """
    from foammesh.core.mesh import fluid_regions

    count = int(count)
    typing = dict(typing or {})
    cut = farfield_cuts(found, farfield)
    shape = _shape_word(shape or shape_of(farfield))
    ordered = sorted(found.solids, key=lambda solid: -solid.volume)
    rows = []
    for index, solid in enumerate(ordered, start=1):
        typed = typing.get(solid.region_uuid) or {}
        rows.append({
            'id': index, 'volume': solid.volume, 'depth': None, 'seed': None,
            'outside': False, 'too_thin': False, 'resolution_warning': False,
            'bounds': [float(value) for value in solid.bounds],
            'region_uuid': solid.region_uuid, 'name': solid.name,
            'geometry_id': solid.geometry_id,
            'type': shown_type(typed) if typed else None,
            'included': bool(typed.get('included', True)),
            'measured': solid.measured,
            'cut_away': cut,
        })
    note = None
    if cut:
        proposed, found_count = [], 0
        reason = (None if count == 1
                  else fluid_regions.FARFIELD_IS_THE_FLUID)
        mismatch = (None if reason is None else
                    {'asked': count, 'found': 1, 'reason': reason})
        note = farfield_note((row['name'] for row in rows), shape)
        typed = [row['name'] for row in rows
                 if typing.get(row['region_uuid'])]
        if typed:
            note += (' A volume control already scopes '
                     + ', '.join(f"'{name}'" for name in typed)
                     + '; the run refuses a control on a solid the cut '
                       'consumed, so turn it off.')
    else:
        candidates = [row for row in rows if row['included']
                      and row['type'] not in (SOLID, EXCLUDED)]
        proposed = [row['id'] for row in candidates[:count]]
        found_count = len(candidates)
        reason = None
        if found_count < count:
            reason = (fluid_regions.FEWER_SPACES if rows
                      else fluid_regions.NO_CLOSED_SURFACE)
        elif found_count > count and not external:
            # DP-864 / DP-915: in external flow the solids past the count
            # are bodies, not a surplus region.
            reason = fluid_regions.MORE_SPACES
        mismatch = (None if reason is None else
                    {'asked': count, 'found': found_count, 'reason': reason})
        if farfield and rows:
            note = farfield_tessellated_note(shape)
        elif external and rows:
            note = farfield_off_note(shape)
    return {'spaces': rows, 'proposed': proposed,
            'found_enclosed': found_count, 'mismatch': mismatch,
            'h': None, 'voxels': 0, 'elapsed': float(found.elapsed),
            'source': SOURCE,
            'fluid': 'farfield' if cut else 'solids',
            'note': note,
            'open_regions': list(found.open_regions),
            'not_solids': list(found.not_solids),
            'not_solid_reasons': dict(found.not_solid_reasons)}
