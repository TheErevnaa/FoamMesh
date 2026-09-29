"""CAD and export operations of the mesh worker (Plan 35 CR7).

Everything here loads OCCT or an export writer, and so runs only in a worker
process: :mod:`foammesh.core.geometry.cad.worker_client` is the window's side.
Each operation is the in-process body it replaced, so a worker answer is the
answer the window used to compute for itself.

``cad.import`` answers under the versioned ``cad_import/v1`` contract:

* ``schema`` -- the contract tag; a reader refuses a major it does not know;
* ``model`` -- the whole :class:`CadModel` (bodies, faces, ids, names,
  colours, source refs, planarity, areas, face order, adjacency, interface
  twins, units) as :meth:`CadModel.to_json` writes it;
* ``brep`` -- the shape as read (scaled to metres for a BREP in another
  unit), written with ``BRepTools`` after faceting, with its sha256;
* ``tessellation`` -- the facets as ``.vtp``, carrying ``cadFaceId`` as the
  importer always did plus ``face_id`` (the face's position in the flat walk)
  and ``body_id`` (its body's index), with a field-data table naming the
  model face of each position;
* ``diagnostics`` -- what the read measured about itself.
"""
from __future__ import annotations

import dataclasses
import time
from pathlib import Path

#: The per-cell arrays ``cad.import`` adds to the facets it sends back.
FACE_ID_ARRAY = 'face_id'
BODY_ID_ARRAY = 'body_id'
FACE_NAMES_ARRAY = 'face_ids'


def run(operation: str, args: dict) -> dict:
    parameters = dict(args.get('parameters') or {})
    handler = _OPERATIONS.get(operation)
    if handler is None:
        raise KeyError(f'the mesh worker does not run {operation!r}')
    return handler(parameters)


def _output(parameters: dict) -> Path:
    path = Path(parameters.get('output_dir') or '.')
    path.mkdir(parents=True, exist_ok=True)
    return path


# -- cad.import --------------------------------------------------------------- #

def import_stage(source, unit=None, tessellation=None) -> dict:
    """Read, scale and facet a CAD file: the OCCT half of an import.

    The same statements ``GeometryArtifactStore._import_cad`` ran before
    CR7, in the same order, so the facets and the model are the ones an
    in-process import produced.
    """
    from foammesh.core.geometry.cad import read_cad
    from foammesh.core.geometry.cad.formats import detect_format
    from foammesh.core.geometry.cad.tessellate import tessellate
    from foammesh.core.geometry.store import (
        GeometryArtifactStore, _scaled_shape, tessellation_params,
    )

    source = Path(source)
    # STEP and IGES say what they are written in; the unit given only
    # reaches a BREP, which does not.
    shape, model = read_cad(source, unit)
    # F-10. The deflection is the one thing that decides whether the
    # facets the mesher sees are the part or a caricature of it, and it
    # used to be a constant nobody could reach and nothing recorded.
    params = tessellation_params(tessellation)
    # F-11. A BREP declares no unit, so the Gmsh runner's
    # `Geometry.OCCTargetUnit` -- which converts what a STEP or IGES
    # header declares -- has nothing to act on, and a millimetre BREP
    # reached the mesher a thousand times too large. Scale the shape
    # itself and the artifact is in metres like everything else, with no
    # reader left to be told.
    cad_scale = 1.0
    if detect_format(source) == 'brep':
        factor = GeometryArtifactStore._unit_factor(model.unit)
        if factor != 1.0:
            shape = _scaled_shape(shape, factor)
            cad_scale = factor
    # DP-520. The deflection is in metres and the shape is in whatever the
    # reader handed back -- millimetres for STEP and IGES -- so it is
    # converted into the shape's units rather than applied as a raw
    # number. It was applied raw: the panel's `0.1 m` faceted a STEP at
    # 0.1 mm, and the same panel's Re-tessellate, which goes through the
    # repair route on a shape already in metres, at 0.1 m.
    shape_unit = 'm' if cad_scale != 1.0 else model.unit
    polydata = tessellate(shape, params, unit_factor=(
        GeometryArtifactStore._unit_factor(shape_unit)))
    return {'shape': shape, 'model': model, 'polydata': polydata,
            'cad_scale': float(cad_scale), 'shape_unit': shape_unit,
            'params': params}


def face_orders(polydata) -> list[int]:
    """The ``cadFaceId`` of every cell, as a list."""
    array = polydata.GetCellData().GetArray('cadFaceId')
    if array is None:
        return []
    return [int(array.GetTuple1(index))
            for index in range(polydata.GetNumberOfCells())]


def labelled(polydata, model):
    """A copy of ``polydata`` carrying ``face_id``/``body_id`` and a name table."""
    from vtkmodules.vtkCommonCore import vtkIntArray, vtkStringArray
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    by_order = {}
    for body_index, body in enumerate(model.bodies):
        for face in body.faces:
            by_order[int(face.face_order)] = (face.id, body_index)
    output = vtkPolyData()
    output.DeepCopy(polydata)
    faces, bodies = vtkIntArray(), vtkIntArray()
    faces.SetName(FACE_ID_ARRAY)
    bodies.SetName(BODY_ID_ARRAY)
    for order in face_orders(polydata):
        faces.InsertNextValue(order)
        bodies.InsertNextValue(by_order.get(order, ('', -1))[1])
    output.GetCellData().AddArray(faces)
    output.GetCellData().AddArray(bodies)
    names = vtkStringArray()
    names.SetName(FACE_NAMES_ARRAY)
    for order in range(max(by_order) + 1 if by_order else 0):
        names.InsertNextValue(by_order.get(order, ('', -1))[0])
    output.GetFieldData().AddArray(names)
    return output


def unlabelled(polydata):
    """``polydata`` without what :func:`labelled` added: the importer's own."""
    polydata.GetCellData().RemoveArray(FACE_ID_ARRAY)
    polydata.GetCellData().RemoveArray(BODY_ID_ARRAY)
    polydata.GetFieldData().RemoveArray(FACE_NAMES_ARRAY)
    return polydata


def write_brep(shape, destination: Path) -> dict:
    from foammesh.core.geometry.cad.worker_client import file_digest
    from foammesh.core.geometry.store import GeometryArtifactStore

    GeometryArtifactStore._write_brep(shape, destination)
    return {'path': str(destination), 'sha256': file_digest(destination),
            'bytes': destination.stat().st_size}


def _cad_import(parameters: dict) -> dict:
    from foammesh.core.geometry.cad.model import CAD_IMPORT_SCHEMA
    from foammesh.core.geometry.cad.worker_client import write_polydata

    started = time.monotonic()
    output = _output(parameters)
    stage = import_stage(parameters['source'], parameters.get('unit'),
                         parameters.get('tessellation'))
    model, polydata = stage['model'], stage['polydata']
    brep = write_brep(stage['shape'], output / 'shape.brep')
    surface = output / 'tessellation.vtp'
    digest = write_polydata(labelled(polydata, model), surface)
    walked = {int(face.face_order) for body in model.bodies
              for face in body.faces}
    faceted = set(face_orders(polydata))
    return {
        'schema': CAD_IMPORT_SCHEMA,
        'model': model.to_json(),
        'brep': brep,
        'tessellation': {'path': str(surface), 'sha256': digest,
                         'cells': int(polydata.GetNumberOfCells()),
                         'points': int(polydata.GetNumberOfPoints())},
        'cad_scale': stage['cad_scale'], 'shape_unit': stage['shape_unit'],
        'params': dataclasses.asdict(stage['params']),
        'diagnostics': {
            'faces': int(model.n_faces), 'bodies': int(model.n_bodies),
            'faces_without_facets': sorted(walked - faceted),
            'facets_without_face': sorted(faceted - walked),
            'seconds': round(time.monotonic() - started, 3)},
    }


# -- follow-on OCCT operations -------------------------------------------------- #

def finding_to_json(finding) -> dict:
    document = dataclasses.asdict(finding)
    document['severity'] = getattr(finding.severity, 'value', finding.severity)
    return document


def finding_from_json(document: dict):
    from foammesh.core.geometry.diagnostics.checks import Finding, Severity

    values = dict(document)
    values['severity'] = Severity(values['severity'])
    values['locations'] = tuple(tuple(item) for item in
                                values.get('locations') or ())
    values['repairable_by'] = tuple(values.get('repairable_by') or ())
    return Finding(**values)


def _cad_check(parameters: dict) -> dict:
    from foammesh.core.geometry.cad import read_cad
    from foammesh.core.geometry.diagnostics.cad_checks import check_cad

    shape, _model = read_cad(parameters['cad_artifact'])
    findings = check_cad(shape, unit_factor=float(
        parameters.get('unit_factor') or 1.0))
    return {'findings': [finding_to_json(item) for item in findings]}


def _cad_solids(parameters: dict) -> dict:
    from foammesh.core.mesh.cad_solids import occ_solids

    found = occ_solids(dict(parameters.get('entry') or {}))
    if found is None:
        return {'solids': None}
    return {'solids': [[volume, sorted(ids), list(bounds), area]
                       for volume, ids, bounds, area in found]}


def _cad_tessellate_file(parameters: dict) -> dict:
    from foammesh.core.geometry.cad.worker_client import write_polydata
    from foammesh.core.geometry.validation.surface import tessellate_cad

    polydata = tessellate_cad(
        parameters['cad_path'], float(parameters['deflection']),
        angular_deflection_deg=float(
            parameters.get('angular_deflection_deg', 5.0)))
    surface = _output(parameters) / 'tessellation.vtp'
    return {'tessellation': {'path': str(surface),
                             'sha256': write_polydata(polydata, surface)}}


def _cad_repair_preview(parameters: dict) -> dict:
    """The CAD repair preview, with the healed solid and facets as files."""
    from foammesh.core.geometry.cad.worker_client import write_polydata
    from foammesh.core.geometry.store import GeometryArtifactStore

    output = _output(parameters)
    store = GeometryArtifactStore(parameters['case_path'])
    preview = store._preview_cad_repair_plan_here(
        dict(parameters['plan']))
    surface = output / 'healed.vtp'
    public = {key: value for key, value in preview.items()
              if not key.startswith('_')}
    return {
        'preview': public,
        'patch_map': preview['_patch_map'],
        'params': dataclasses.asdict(preview['_params']),
        'unit_factor': float(preview['_unit_factor']),
        'brep': write_brep(preview['_shape'], output / 'healed.brep'),
        'tessellation': {'path': str(surface), 'sha256': write_polydata(
            preview['_polydata'], surface)},
    }


# -- exports ------------------------------------------------------------------ #

def _export_dataset(parameters: dict) -> dict:
    """One ``ImportExportService.export_<format>`` call, as the window made it."""
    from foammesh.core.import_export.service import ImportExportService

    runtime = parameters.get('gmsh_runtime')
    if runtime:
        from foammesh.core.gmsh.launch_profiles import use_runtime

        use_runtime(runtime.get('distribution'), runtime.get('user'))
    fmt = str(parameters['format'])
    method = getattr(ImportExportService(), f'export_{fmt}')
    result = method(Path(parameters['case_path']),
                    Path(parameters['destination']))
    from foammesh.core.facade.domain_operations import _to_payload

    return {'result': dict(_to_payload(result))}


_OPERATIONS = {
    'cad.import': _cad_import,
    'cad.check': _cad_check,
    'cad.solids': _cad_solids,
    'cad.tessellate_file': _cad_tessellate_file,
    'cad.repair_preview': _cad_repair_preview,
    'export.dataset': _export_dataset,
}
OPERATIONS = tuple(_OPERATIONS)
