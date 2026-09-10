"""VTK actor bundle and stable-ID quality interactions for canonical meshes."""
from __future__ import annotations

from dataclasses import dataclass
import csv
import json
from pathlib import Path


from foammesh.core.mesh.model import CanonicalMesh
from foammesh.core.mesh.vtk_adapter import CanonicalVtkData, to_vtk


CANONICAL_CELL_ID = 'foammesh_canonical_cell_id'
CANONICAL_FACE_ID = 'foammesh_canonical_face_id'


@dataclass(frozen=True)
class CanonicalActorBundle:
    """Datasets and actors kept together so tables and the viewer share IDs."""
    data: CanonicalVtkData
    volume_actor: object
    boundary_actor: object


def build_canonical_actors(mesh: CanonicalMesh, *, quality_report=None
                           ) -> CanonicalActorBundle:
    import vtk

    data = to_vtk(mesh, quality_report=quality_report)
    volume_mapper = vtk.vtkDataSetMapper()
    volume_mapper.SetInputData(data.volume)
    boundary_mapper = vtk.vtkPolyDataMapper()
    boundary_mapper.SetInputData(data.boundary)
    volume_actor = vtk.vtkActor()
    volume_actor.SetMapper(volume_mapper)
    boundary_actor = vtk.vtkActor()
    boundary_actor.SetMapper(boundary_mapper)
    volume_actor.SetObjectName('foammesh-canonical-volume')
    boundary_actor.SetObjectName('foammesh-canonical-boundary')
    return CanonicalActorBundle(data, volume_actor, boundary_actor)


def isolate_stable_ids(dataset, ids, *, entity_kind: str = 'volume_cell'):
    """Extract table-selected canonical entities in the caller-provided order."""
    import vtk

    name = CANONICAL_CELL_ID if entity_kind == 'volume_cell' else CANONICAL_FACE_ID
    array = dataset.GetCellData().GetArray(name)
    if array is None:
        raise ValueError(f'dataset has no stable ID array {name}')
    wanted = tuple(dict.fromkeys(int(value) for value in ids))
    available = {int(array.GetTuple1(index)): index
                 for index in range(array.GetNumberOfTuples())}
    missing = [value for value in wanted if value not in available]
    if missing:
        raise ValueError(f'canonical IDs are not present: {missing}')
    selection_ids = vtk.vtkIdTypeArray()
    for value in wanted:
        selection_ids.InsertNextValue(available[value])
    node = vtk.vtkSelectionNode()
    node.SetFieldType(vtk.vtkSelectionNode.CELL)
    node.SetContentType(vtk.vtkSelectionNode.INDICES)
    node.SetSelectionList(selection_ids)
    selection = vtk.vtkSelection()
    selection.AddNode(node)
    extract = vtk.vtkExtractSelection()
    extract.SetInputData(0, dataset)
    extract.SetInputData(1, selection)
    extract.Update()
    output = extract.GetOutput()
    result = output.NewInstance()
    result.ShallowCopy(output)
    return result


def export_failed_entities(mesh: CanonicalMesh, failed_set, destination: str | Path) -> Path:
    """Export one canonical failed set as CSV, JSON, or diagnostic VTU."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    suffix = destination.suffix.lower()
    rows = [
        {'entity_id': int(entity_id), 'metric': failed_set.metric,
         'value': float(value), 'threshold': float(failed_set.threshold),
         'comparison': failed_set.comparison, 'entity_kind': failed_set.entity_kind}
        for entity_id, value in zip(failed_set.entity_ids, failed_set.values)
    ]
    if suffix == '.json':
        destination.write_text(json.dumps({
            'schema_version': 1, 'failed_set': failed_set.to_dict(), 'rows': rows,
        }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    elif suffix == '.csv':
        with destination.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=(
                'entity_id', 'entity_kind', 'metric', 'value', 'comparison', 'threshold'))
            writer.writeheader()
            writer.writerows(rows)
    elif suffix == '.vtu':
        if failed_set.entity_kind != 'volume_cell':
            raise ValueError('VTU failed-set export currently requires volume cells')
        data = to_vtk(mesh, quality_report=_SingleFailedReport(failed_set))
        selected = isolate_stable_ids(data.volume, failed_set.entity_ids,
                                      entity_kind='volume_cell')
        import vtk
        writer = vtk.vtkXMLUnstructuredGridWriter()
        writer.SetFileName(str(destination))
        writer.SetInputData(selected)
        if writer.Write() != 1:
            raise OSError(f'failed to write diagnostic VTU: {destination}')
    else:
        raise ValueError('failed-set destination must end with .csv, .json, or .vtu')
    return destination


class _SingleFailedReport:
    def __init__(self, failed_set):
        self.failed_sets = (failed_set,)

