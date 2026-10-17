"""A Gmsh plugin's result drawn in the viewport (Plan 41 GN20).

The runner writes every view a plugin made as ``plugin_view_<tag>.vtu`` with one
array, ``value``, on the points or on the cells.  :class:`GmshPluginView` reads
one, colours it by ``value`` between its own minimum and maximum, and puts it in
the scene through the viewport widget only (``addActor``, ``removeActor``,
``refresh``), like :class:`GmshEntityView` and :class:`GmshMeshView`.
"""
from __future__ import annotations


class GmshPluginView:
    """The plugin results on show, by view tag (or ``dim:tag`` for entities)."""

    def __init__(self, view):
        self._view = view
        self._actors: dict = {}
        self.ranges: dict = {}

    def keys(self) -> list:
        return sorted(self._actors, key=str)

    def actor(self, key):
        return self._actors.get(key)

    def show(self, key, vtu_path, name: str = '') -> dict:
        """Draw *vtu_path* under *key*; replaces what *key* showed before."""
        from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter
        from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper

        self.hide(key, refresh=False)
        reader = vtkXMLUnstructuredGridReader()
        reader.SetFileName(str(vtu_path))
        reader.Update()
        grid = reader.GetOutput()
        surface = vtkDataSetSurfaceFilter()
        surface.SetInputData(grid)
        surface.Update()
        data = surface.GetOutput()
        point_values = grid.GetPointData().GetArray('value')
        cell_values = grid.GetCellData().GetArray('value')
        array = point_values if point_values is not None else cell_values
        low, high = (array.GetRange() if array is not None else (0.0, 1.0))
        if high <= low:
            high = low + 1.0
        mapper = vtkPolyDataMapper()
        mapper.SetInputData(data)
        if array is not None:
            mapper.SetScalarModeToUsePointFieldData() \
                if point_values is not None else \
                mapper.SetScalarModeToUseCellFieldData()
            mapper.SelectColorArray('value')
            from foammesh.support import colour_standard
            table = colour_standard.lookup_table('plugin', low, high)
            mapper.SetLookupTable(table)
            mapper.SetScalarRange(low, high)
            mapper.ScalarVisibilityOn()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('gmshPluginResult_%s' % key)
        actor.GetProperty().SetLineWidth(2.0)
        actor.GetProperty().SetPointSize(8.0)
        actor.GetProperty().SetRenderPointsAsSpheres(True)
        self._view.addActor(actor)
        self._actors[key] = actor
        self.ranges[key] = (float(low), float(high))
        self._view.refresh()
        return {'key': key, 'name': name, 'cells': data.GetNumberOfCells(),
                'range': self.ranges[key]}

    def hide(self, key, *, refresh: bool = True) -> bool:
        actor = self._actors.pop(key, None)
        self.ranges.pop(key, None)
        if actor is None:
            return False
        self._view.removeActor(actor)
        if refresh:
            self._view.refresh()
        return True

    def clear(self) -> None:
        for key in list(self._actors):
            self.hide(key, refresh=False)
        self._view.refresh()
