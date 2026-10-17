"""Open, merge and reclassify on the resident Gmsh model (Plan 41 GN5).

Gmsh's ``File > Open`` and ``File > Merge``, and ``Mesh > Reclassify 2D``,
``Create topology`` and ``Create geometry``, as functions on the session's
model.  Runs inside the session process, next to ``session_v1``.

Everything here was measured on Gmsh 4.15.2 (2026-10-10):

* ``gmsh.merge`` picks the reader from the extension.  ``.inp`` has no reader
  in this build: the file falls into the ``.geo`` parser, which fails with a
  syntax error.  The extension is therefore refused here, before the parser
  sees it.
* ``Geometry.OCCTargetUnit`` exists (a string option, empty by default) and
  works on STEP and IGES: they carry their unit and the importer converts to
  the target (empty is millimetre; ``M`` makes a 10 mm edge 0.01).  Setting it
  back to the empty string does NOT undo an earlier unit -- the Open CASCADE
  parameter sticks -- so ``MM`` is set explicitly.  BREP
  carries no unit, so the option does nothing there and ``Geometry.OCCScaling``
  is the only handle.
* A mesh file imports as discrete entities with elements; the surface mesh of
  a closed shell is turned back into something meshable by dropping the
  volumes, ``classifySurfaces`` + ``createGeometry``, and a surface loop.
"""
from __future__ import annotations

import math
import os

#: extension -> kind.  ``cad`` reads through Open CASCADE, ``geo`` through the
#: script parser, ``tessellation`` is a triangulated surface, ``mesh`` a mesh
#: with elements (and often physical groups).
KINDS = {
    '.step': 'cad', '.stp': 'cad', '.iges': 'cad', '.igs': 'cad',
    '.brep': 'cad', '.xao': 'cad',
    '.geo': 'geo', '.geo_unrolled': 'geo',
    '.stl': 'tessellation', '.obj': 'tessellation', '.ply': 'tessellation',
    '.ply2': 'tessellation', '.off': 'tessellation',
    '.msh': 'mesh', '.med': 'mesh', '.cgns': 'mesh', '.unv': 'mesh',
    '.bdf': 'mesh', '.mesh': 'mesh', '.vtk': 'mesh', '.nii': 'mesh',
}
REFUSED = {
    '.inp': 'this Gmsh build (4.15.2) writes Abaqus INP but has no reader for '
            'it, so the file would be handed to the .geo parser and fail',
}
DEFAULT_UNIT = 'MM'    # Open CASCADE's own default; '' means this
UNITS = ('', 'UM', 'MM', 'CM', 'M', 'KM', 'FT')


class ImportError_(ValueError):
    """A refusal that says why."""


def extension(path: str) -> str:
    name = os.path.basename(path).lower()
    if name.endswith('.nii.gz'):
        return '.nii'
    return os.path.splitext(name)[1]


def kind_of(path: str) -> str:
    ext = extension(path)
    if ext in REFUSED:
        raise ImportError_(f'{ext} cannot be opened: {REFUSED[ext]}')
    if ext not in KINDS:
        raise ImportError_(
            f'{ext or "this file"} is not a format Gmsh reads: '
            + ', '.join(sorted(KINDS)))
    return KINDS[ext]


def counts(gmsh) -> dict:
    return {str(d): len(gmsh.model.getEntities(d)) for d in range(4)}


def _mesh_summary(gmsh) -> dict:
    nodes = len(gmsh.model.mesh.getNodes()[0])
    elements = 0
    for dim in range(4):
        for d, t in gmsh.model.getEntities(dim):
            for tags in gmsh.model.mesh.getElements(d, t)[1]:
                elements += len(tags)
    return {'nodes': nodes, 'elements': elements}


def _bbox(gmsh):
    try:
        return [float(v) for v in gmsh.model.getBoundingBox(-1, -1)]
    except Exception:                                     # noqa: BLE001
        return []


def _groups(gmsh) -> list:
    return [{'dim': int(d), 'tag': int(t),
             'name': gmsh.model.getPhysicalName(d, t).strip()}
            for d, t in gmsh.model.getPhysicalGroups()]


def classify_arguments(angle=40.0, boundary=True, reparametrize=True,
                       curve_angle=180.0, export_discrete=True) -> tuple:
    """Degrees to radians; ``curve_angle`` of 180 or more means "no split"."""
    angle = float(angle)
    curve_angle = float(curve_angle)
    if not 0.0 < angle <= 180.0:
        raise ImportError_('the classification angle must be between 0 and '
                           '180 degrees')
    if not 0.0 < curve_angle <= 180.0:
        raise ImportError_('the curve angle must be between 0 and 180 degrees')
    return (angle * math.pi / 180.0, bool(boundary), bool(reparametrize),
            curve_angle * math.pi / 180.0, bool(export_discrete))


def prime_unit(gmsh) -> None:
    """Construct the Open CASCADE IGES reader without reading anything.

    MEASURED (DP-60 in the runner, repeated here on a fresh process): with
    ``Geometry.OCCTargetUnit`` set, an IGES import raises "Could not set
    OpenCASCADE target unit" unless a STEP or IGES reader was constructed
    first -- Gmsh's STEP path builds its reader before applying the unit, its
    IGES path does not.  Reading one empty ``.iges`` builds the reader and
    nothing else.
    """
    import tempfile
    current = gmsh.model.getCurrent()
    gmsh.model.add('__foammesh_occ_unit_prime__')
    try:
        with tempfile.TemporaryDirectory() as directory:
            empty = os.path.join(directory, 'prime.iges')
            open(empty, 'wb').close()
            try:
                gmsh.model.occ.importShapes(empty)
            except Exception:                             # noqa: BLE001
                pass
    finally:
        gmsh.model.remove()
        gmsh.model.setCurrent(current)


def set_target_unit(gmsh, unit: str) -> None:
    """``Geometry.OCCTargetUnit``, priming the reader first when it refuses."""
    try:
        gmsh.option.setString('Geometry.OCCTargetUnit', unit)
        return
    except Exception:                                     # noqa: BLE001
        prime_unit(gmsh)
    gmsh.option.setString('Geometry.OCCTargetUnit', unit)


def open_file(gmsh, path: str, mode: str = 'open', *, unit: str = '',
              scaling: float = 1.0, classify=None) -> dict:
    """File > Open (replace the model) or File > Merge (add to it).

    *classify*: for a tessellation, an optional dict of ``reclassify``
    arguments run straight after the read (the STL classification of
    ``Mesh > Reclassify 2D`` at import time).
    """
    if mode not in ('open', 'merge'):
        raise ImportError_('mode must be open or merge')
    kind = kind_of(path)
    if unit not in UNITS:
        raise ImportError_(
            'unit %r is not accepted by Open CASCADE here; use one of %s'
            % (unit, ', '.join(repr(u) for u in UNITS)))
    if not os.path.isfile(path):
        raise ImportError_(f'{path} does not exist')
    scaling = float(scaling)
    if scaling <= 0.0:
        raise ImportError_('the scale factor must be positive')
    if mode == 'open':
        gmsh.clear()
        stem = os.path.splitext(os.path.basename(path))[0] or 'foammesh'
        gmsh.model.add(stem)
    before = counts(gmsh)
    applied = {'unit': None, 'scaling': None}
    if kind == 'cad' and extension(path) != '.xao':
        # MEASURED: an empty string does not restore the default once a unit
        # was set -- the Open CASCADE parameter sticks.  Say MM explicitly,
        # and put it back after the read so a later import is not surprised.
        # BREP carries no unit, so the option is left alone for it.
        if extension(path) in ('.iges', '.igs'):
            prime_unit(gmsh)
        if extension(path) != '.brep':
            set_target_unit(gmsh, unit or DEFAULT_UNIT)
        gmsh.option.setNumber('Geometry.OCCScaling', scaling)
        applied = {'unit': gmsh.option.getString('Geometry.OCCTargetUnit'),
                   'scaling': gmsh.option.getNumber('Geometry.OCCScaling')}
        try:
            created = gmsh.model.occ.importShapes(path)
        finally:
            gmsh.option.setNumber('Geometry.OCCScaling', 1.0)
            if extension(path) != '.brep':
                set_target_unit(gmsh, DEFAULT_UNIT)
        gmsh.model.occ.synchronize()
    else:
        gmsh.merge(path)
        if kind in ('geo', 'cad'):
            # the script / XAO builds in the geometry kernels; both need the
            # model synchronised (geo first: occ.synchronize would drop a
            # built-in kernel model that has not been)
            gmsh.model.geo.synchronize()
            gmsh.model.occ.synchronize()
    result = {'kind': kind, 'mode': mode, 'path': path,
              'entities': counts(gmsh), 'added': {
                  d: counts(gmsh)[d] - before[d] for d in before},
              'groups': _groups(gmsh), 'bbox': _bbox(gmsh),
              'applied': applied}
    result.update(_mesh_summary(gmsh))
    result['has_mesh'] = result['elements'] > 0
    if kind == 'tessellation' and classify:
        result['reclassified'] = reclassify(gmsh, **classify)
    return result


def reclassify(gmsh, angle=40.0, boundary=True, reparametrize=True,
               curve_angle=180.0, export_discrete=True,
               create_geometry=True, dim_tags=None) -> dict:
    """Mesh > Reclassify 2D with every argument of ``classifySurfaces``."""
    args = classify_arguments(angle, boundary, reparametrize, curve_angle,
                              export_discrete)
    if not gmsh.model.getEntities(2):
        raise ImportError_('there are no surfaces to reclassify; open a '
                           'surface mesh first')
    before = counts(gmsh)
    gmsh.model.mesh.classifySurfaces(*args)
    if create_geometry:
        gmsh.model.mesh.createGeometry(
            [tuple(item) for item in dim_tags] if dim_tags else [])
    return {'before': before, 'after': counts(gmsh),
            'args': {'angle': float(angle), 'boundary': bool(boundary),
                     'reparametrize': bool(reparametrize),
                     'curve_angle': float(curve_angle),
                     'export_discrete': bool(export_discrete),
                     'create_geometry': bool(create_geometry)}}


def create_topology(gmsh, make_simply_connected=True,
                    export_discrete=True) -> dict:
    """Mesh > Create topology."""
    before = counts(gmsh)
    gmsh.model.mesh.createTopology(bool(make_simply_connected),
                                   bool(export_discrete))
    return {'before': before, 'after': counts(gmsh),
            'args': {'make_simply_connected': bool(make_simply_connected),
                     'export_discrete': bool(export_discrete)}}


def create_geometry(gmsh, dim_tags=None) -> dict:
    """Mesh > Create geometry (a parametrisation for discrete entities)."""
    before = counts(gmsh)
    gmsh.model.mesh.createGeometry(
        [tuple(item) for item in dim_tags] if dim_tags else [])
    return {'before': before, 'after': counts(gmsh),
            'args': {'dim_tags': [list(i) for i in dim_tags or []]}}


def remesh_surfaces(gmsh, angle=40.0, curve_angle=180.0,
                    size: float = 0.0) -> dict:
    """Turn the closed surface mesh of the model into a meshable volume.

    The recipe the native GUI user follows by hand: drop the volume entities
    (their elements go with them), classify the surfaces and parametrise them,
    close them with one surface loop and one volume.  The caller then
    generates the 3D mesh.
    """
    volumes = [tag for _d, tag in gmsh.model.getEntities(3)]
    for tag in volumes:
        gmsh.model.mesh.clear([(3, tag)])
    if volumes:
        gmsh.model.removeEntities([(3, tag) for tag in volumes])
    reclassify(gmsh, angle=angle, curve_angle=curve_angle)
    surfaces = [tag for _d, tag in gmsh.model.getEntities(2)]
    if not surfaces:
        raise ImportError_('there are no surfaces to close into a volume')
    loop = gmsh.model.geo.addSurfaceLoop(surfaces)
    volume = gmsh.model.geo.addVolume([loop])
    gmsh.model.geo.synchronize()
    if size and float(size) > 0.0:
        gmsh.option.setNumber('Mesh.MeshSizeMax', float(size))
    return {'surfaces': len(surfaces), 'volume': int(volume),
            'entities': counts(gmsh)}
