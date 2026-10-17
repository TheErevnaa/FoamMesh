"""The Gmsh plugins FoamMesh offers, with their options (Plan 41 GN20).

Gmsh's Python API can run a plugin and set its options but cannot list them, so
the table is written out.  Every option name here was accepted by
``gmsh.plugin.setNumber`` on the 4.15.2 build in ``OpenFOAM13Runtime`` (probed
2026-10-10; ``tests/unit/test_gn20_plugins_dock.py`` repeats the probe live).
A plugin the build does not have is listed with the reason, not hidden.

No value here depends on the target solver or on OpenFOAM.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PluginOption:
    name: str
    kind: str                      # 'float' | 'int' | 'bool'
    default: float
    help: str
    #: UXB3c. ``'m'`` for a length in model units, which the panel shows in
    #: the project display unit; '' for everything else.
    unit: str = ''

    @property
    def is_length(self) -> bool:
        return self.unit == 'm'


@dataclass(frozen=True)
class Plugin:
    name: str
    title: str
    summary: str
    options: tuple = ()
    #: Takes a view as its input (the ``View`` option); needs one to exist.
    needs_view: bool = False
    #: Changes the mesh in the session (so the run is logged and replayed).
    changes_mesh: bool = False
    #: Why this build cannot run it ('' when it can).
    unavailable: str = ''
    #: Shown in the task row of the acceptance (Skin, MeshVolume, ...).
    shows: str = 'a view'


def _f(name, default, help, unit=''):
    return PluginOption(name, 'float', float(default), help, unit)


def _i(name, default, help):
    return PluginOption(name, 'int', float(default), help)


def _b(name, default, help):
    return PluginOption(name, 'bool', float(bool(default)), help)


_UNKNOWN = ('Gmsh 4.15.2 in this runtime reports "Unknown plugin or plugin '
            'action" for %s.')

_VIEW = _i('View', -1, 'Index of the input view (the panel fills it from the '
           'input view chosen above).')

PLUGINS: tuple = (
    Plugin('Skin', 'Skin',
           'The boundary (skin) of the mesh elements, as a view.',
           (_b('Visible', False, 'Only the elements that are visible.'),
            _b('FromMesh', True, 'Take the elements from the mesh, not from a '
               'view.'), _VIEW)),
    Plugin('MeshVolume', 'Mesh volume',
           'The total size of the mesh elements of a dimension (length, area '
           'or volume), as a one-point view and in the log.',
           (_i('Dimension', 3, 'Dimension of the elements measured '
               '(0 = the highest present).'),
            _i('PhysicalGroup', -1, 'Physical group measured (-1 = all).'))),
    Plugin('Distance', 'Distance',
           'The distance from every node to the nodes or surfaces of a '
           'physical group, as a view.',
           (_i('PhysicalPoint', -1, 'Physical group of points to measure '
               'from (-1 = none).'),
            _i('PhysicalSurface', -1, 'Physical group of surfaces to measure '
               'from (-1 = all surfaces).'),
            _f('MinScale', 1.0, 'Smallest scale of the distance.'),
            _f('MaxScale', 1.0, 'Largest scale of the distance.'))),
    Plugin('CutPlane', 'Cut plane',
           'Cuts a view with the plane A x + B y + C z + D = 0.',
           (_f('A', 1, 'Plane coefficient A.'), _f('B', 0, 'B.'),
            _f('C', 0, 'C.'), _f('D', 0, 'D (a distance along the normal when A, B, C is a unit vector).', 'm'),
            _b('ExtractVolume', False, 'Keep the part of the volume on one '
               'side instead of the cut itself.'),
            _i('RecurLevel', 4, 'Recursion level of the adaptive cut.'),
            _f('TargetError', 0.0, 'Target error of the adaptive cut.'),
            _VIEW), needs_view=True),
    Plugin('AnalyseMeshQuality', 'Analyse mesh quality',
           "The Jacobian ratio, the inverse gradient error and the inverse "
           'condition number of every element, as views.',
           (_b('JacobianDeterminant', True, 'Compute the Jacobian.'),
            _b('IGEMeasure', True, 'Compute the inverse gradient error.'),
            _b('ICNMeasure', True, 'Compute the inverse condition number.'),
            _f('HidingThreshold', 0.0, 'Hide elements above this value.'),
            _b('ThresholdGreater', False, 'Hide the elements above the '
               'threshold instead of below.'),
            _b('CreateView', True, 'Create the views.'),
            _b('Recompute', True, 'Recompute the measures.'),
            _i('DimensionOfElements', 3, 'Dimension of the elements '
               'measured.'))),
    Plugin('CutBox', 'Cut box',
           'Cuts a view with a parallelepiped (origin and three edges).',
           (_f('X0', 0, 'Origin x.', 'm'), _f('Y0', 0, 'Origin y.', 'm'),
            _f('Z0', 0, 'Origin z.', 'm'), _f('X1', 1, 'Edge 1 end x.', 'm'),
            _f('Y1', 0, 'Edge 1 end y.', 'm'), _f('Z1', 0, 'Edge 1 end z.', 'm'),
            _f('X2', 0, 'Edge 2 end x.', 'm'), _f('Y2', 1, 'Edge 2 end y.', 'm'),
            _f('Z2', 0, 'Edge 2 end z.', 'm'), _f('X3', 0, 'Edge 3 end x.', 'm'),
            _f('Y3', 0, 'Edge 3 end y.', 'm'), _f('Z3', 1, 'Edge 3 end z.', 'm'),
            _i('NumPointsU', 20, 'Points along edge 1.'), _VIEW),
           needs_view=True),
    Plugin('Isosurface', 'Isosurface',
           'The surface where a view equals a value.',
           (_f('Value', 0, 'Iso value.'),
            _b('ExtractVolume', False, 'Keep the volume on one side.'),
            _f('TargetError', 0.0, 'Target error.'),
            _i('OtherView', -1, 'View the iso value is read from '
               '(-1 = the input view).'),
            _i('OtherTimeStep', -1, 'Time step of that view.'), _VIEW),
           needs_view=True),
    Plugin('ExtractElements', 'Extract elements',
           'Keeps the elements of a view whose value lies in a range.',
           (_f('MinVal', 0, 'Lowest value kept.'),
            _f('MaxVal', 1, 'Highest value kept.'),
            _i('TimeStep', -1, 'Time step read.'),
            _b('Visible', False, 'Only the visible elements.'),
            _i('Dimension', -1, 'Dimension of the elements kept (-1 = all).'),
            _VIEW), needs_view=True),
    Plugin('Triangulate', 'Triangulate',
           'Triangulates the quadrangles of a view.', (_VIEW,),
           needs_view=True, unavailable=_UNKNOWN % 'Triangulate'),
    Plugin('Tetrahedralize', 'Tetrahedralize',
           'Tetrahedralizes the points of a view.', (_VIEW,),
           needs_view=True, unavailable=_UNKNOWN % 'Tetrahedralize'),
    Plugin('Crop', 'Crop',
           'Crops a view to a box.', (), needs_view=True,
           unavailable=_UNKNOWN % 'Crop'),
    Plugin('DiscretizationError', 'Discretization error',
           'Estimates the discretization error of the last view made (the '
           'plugin has no input-view option in Gmsh 4.15.2).', (),
           needs_view=True),
    Plugin('SpanningTree', 'Spanning tree',
           'A spanning tree of the mesh edges, as a view.', ()),
    Plugin('NearestNeighbor', 'Nearest neighbour',
           'The nearest neighbour of every point of a view.', (_VIEW,),
           needs_view=True),
    Plugin('SimplePartition', 'Simple partition',
           'Splits the mesh into slabs along x, y and z.',
           (_i('NumSlicesX', 1, 'Slices along x.'),
            _i('NumSlicesY', 1, 'Slices along y.'),
            _i('NumSlicesZ', 1, 'Slices along z.')), changes_mesh=True),
    Plugin('ShowNeighborElements', 'Show neighbour elements',
           'Marks the elements around the selected ones.', ()),
    Plugin('MeshSubEntities', 'Mesh sub-entities',
           'Meshes the sub-entities of the model elements.', (),
           unavailable=_UNKNOWN % 'MeshSubEntities'),
)

BY_NAME = {plugin.name: plugin for plugin in PLUGINS}

#: The four the plan names: they run and show in the viewport.
SHOWN_IN_VIEWPORT = ('Skin', 'MeshVolume', 'Distance', 'CutPlane')


def plugin(name: str) -> Plugin:
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError('FoamMesh has no entry for the Gmsh plugin %r' % name)


def runnable() -> tuple:
    return tuple(p for p in PLUGINS if not p.unavailable)


def numbers_for(name: str, values: dict) -> dict:
    """The options to send: only those the plugin has, each as a number."""
    entry = plugin(name)
    known = {option.name: option for option in entry.options}
    out = {}
    for key, value in (values or {}).items():
        if key not in known:
            raise ValueError('%s has no option %s' % (name, key))
        out[key] = float(value)
    return out


def edits_mesh(name: str, values: dict | None = None) -> bool:
    """Does this run change the mesh in the session (so it is logged)?

    ``SimplePartition`` always does.  ``Skin`` does when it takes its elements
    from the mesh: Gmsh then adds a discrete surface carrying the boundary
    triangles (measured on 4.15.2: surfaces 3 -> 4).
    """
    entry = plugin(name)
    if entry.changes_mesh:
        return True
    if name == 'Skin':
        given = (values or {}).get('FromMesh')
        if given is None:
            given = next(o.default for o in entry.options
                         if o.name == 'FromMesh')
        return bool(float(given))
    return False
