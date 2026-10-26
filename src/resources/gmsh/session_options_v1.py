"""Session operations for the options area (Plan 48 GM2).

``dispatch()`` finds a public method by name (``session.<name>``), so the
methods below are the operations ``session.option_set``, ``session.option_get``
and ``session.options_apply``.  The mixin runs inside the Gmsh process and must
not import ``foammesh`` or ``session_v1``; it uses ``self.gmsh`` (the recording
wrapper) from the session.

Gmsh keeps a few deprecated option names in ``DefaultOptions.h`` that forward
to a live option.  They are not written here: the row comes back with
``applied: False`` and the name of the option to use.  The table below is a
copy of ``foammesh.core.gmsh.option_catalogue.ALIASES`` (generated from the
same file); ``tests/plan48/gm2`` checks the two are equal.
"""
from __future__ import annotations

#: deprecated name -> the option it forwards to
_ALIASES = {
    'Geometry.Color.Lines': 'Geometry.Color.Curves',
    'Mesh.Color.Points': 'Mesh.Color.Nodes',
    'Mesh.Color.PointsSup': 'Mesh.Color.NodesSup',
    'Geometry.CurveNumbers': 'Geometry.CurveLabels',
    'Geometry.DoubleClickedLineCommand': 'Geometry.DoubleClickedCurveCommand',
    'Geometry.LineNumbers': 'Geometry.CurveLabels',
    'Geometry.LineSelectWidth': 'Geometry.CurveSelectWidth',
    'Geometry.LineType': 'Geometry.CurveType',
    'Geometry.LineWidth': 'Geometry.CurveWidth',
    'Geometry.Lines': 'Geometry.Curves',
    'Geometry.OCCUnionUnify': 'Geometry.OCCBooleanSimplify',
    'Geometry.PointNumbers': 'Geometry.PointLabels',
    'Geometry.SurfaceNumbers': 'Geometry.SurfaceLabels',
    'Geometry.VolumeNumbers': 'Geometry.VolumeLabels',
    'Mesh.BoundaryLayerFanPoints': 'Mesh.BoundaryLayerFanElements',
    'Mesh.CharacteristicLengthExtendFromBoundary': 'Mesh.MeshSizeExtendFromBoundary',
    'Mesh.CharacteristicLengthFactor': 'Mesh.MeshSizeFactor',
    'Mesh.CharacteristicLengthFromCurvature': 'Mesh.MeshSizeFromCurvature',
    'Mesh.CharacteristicLengthFromParametricPoints': 'Mesh.MeshSizeFromParametricPoints',
    'Mesh.CharacteristicLengthFromPoints': 'Mesh.MeshSizeFromPoints',
    'Mesh.CharacteristicLengthMax': 'Mesh.MeshSizeMax',
    'Mesh.CharacteristicLengthMin': 'Mesh.MeshSizeMin',
    'Mesh.CompoundCharacteristicLengthFactor': 'Mesh.CompoundMeshSizeFactor',
    'Mesh.LineNumbers': 'Mesh.LineLabels',
    'Mesh.MinimumCircleNodes': 'Mesh.MinCircleNodes',
    'Mesh.MinimumCirclePoints': 'Mesh.MinCircleNodes',
    'Mesh.MinimumCurveNodes': 'Mesh.MinCurveNodes',
    'Mesh.MinimumCurvePoints': 'Mesh.MinCurveNodes',
    'Mesh.MinimumElementsPerTwoPi': 'Mesh.MeshSizeFromCurvature',
    'Mesh.MinimumLineNodes': 'Mesh.MinLineNodes',
    'Mesh.PointNumbers': 'Mesh.NodeLabels',
    'Mesh.PointSize': 'Mesh.NodeSize',
    'Mesh.PointType': 'Mesh.NodeType',
    'Mesh.Points': 'Mesh.Nodes',
    'Mesh.PointsSup': 'Mesh.NodesSup',
    'Mesh.StlRemoveDuplicateTriangles': 'Mesh.StlRemoveBadTriangles',
    'Mesh.SurfaceNumbers': 'Mesh.SurfaceLabels',
    'Mesh.VolumeNumbers': 'Mesh.VolumeLabels',
    'View.Format': 'View.NumberFormat',
}


def _alias_note(name):
    return (f'{name} is a deprecated name in Gmsh 4.15.2; set '
            f'{_ALIASES[name]} instead')


class SessionOptionsMixin:
    """Operations of the options area: set, read and apply Gmsh options."""

    __slots__ = ()

    def option_set(self, name, value, storage='number', **_):
        """Set one option and return what Gmsh reads back."""
        name = str(name)
        if name in _ALIASES:
            raise ValueError(_alias_note(name))
        option = self.gmsh.option
        if storage == 'string':
            option.setString(name, str(value))
            effective = option.getString(name)
        elif storage == 'color':
            option.setColor(name, *[int(part) for part in value])
            effective = list(option.getColor(name))
        else:
            option.setNumber(name, float(value))
            effective = option.getNumber(name)
        return {'name': name, 'requested': value, 'effective': effective,
                'storage': storage}

    def option_get(self, name, storage='number', **_):
        """The value Gmsh holds for one option."""
        name = str(name)
        option = self.gmsh.option
        if storage == 'string':
            value = option.getString(name)
        elif storage == 'color':
            value = list(option.getColor(name))
        else:
            value = option.getNumber(name)
        return {'name': name, 'value': value, 'storage': storage}

    def options_apply(self, rows, **_):
        """Apply the job's option rows; ``Geometry.*`` rows first.

        The order is the batch runner's: the geometry importers read their
        options when a shape is imported.  Applying the same rows again changes
        nothing.  A deprecated alias is not written.
        """
        ordered = sorted(
            enumerate(rows or ()),
            key=lambda pair: (not str(pair[1].get('name', '')).startswith(
                'Geometry.'), pair[0]))
        results = []
        for _index, row in ordered:
            name = str(row.get('name'))
            storage = row.get('storage', 'number')
            value = row.get('value')
            if name in _ALIASES:
                results.append({'name': name, 'requested': value,
                                'effective': None, 'applied': False,
                                'note': _alias_note(name)})
                continue
            try:
                done = self.option_set(name, value, storage)
            except Exception as error:                   # noqa: BLE001
                results.append({'name': name, 'requested': value,
                                'effective': None, 'applied': False,
                                'note': str(error)})
                continue
            results.append({'name': name, 'requested': value,
                            'effective': done['effective'], 'applied': True,
                            'note': ''})
        return results
