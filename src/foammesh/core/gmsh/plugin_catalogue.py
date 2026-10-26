"""The Gmsh mesh plugins FoamMesh offers, with their options (Plan 48 GM9).

Gmsh's Python API can run a plugin and set its options but cannot list them,
so the table is *generated*: ``scripts/gmsh_catalogue/gen_plugin_catalogue.py`` reads
the GM1 catalogue (``plans/evidence/plan48/catalogue.json``, itself read from
the Gmsh 4.15.2 source) and writes the block between the GENERATED markers
below.  Every number **and string** option of every mesh plugin is there, with
the default Gmsh declares in its source.  A test regenerates the block and
fails on any difference, so the table cannot drift from the catalogue.

What the catalogue cannot say, the hand-written part below adds: which plugins
need an input view, which change the mesh in the session, and what a plugin
makes.  The 30 post-processing plugins stay out (Q6, ``EXCLUDED_POST``);
CVTRemesh is added by GM16b through ``register_extra`` (see below) so this file
need not change for it.  ``Crop`` is not a plugin of Gmsh 4.15.2 and is not
listed.

No value here depends on the target solver or on OpenFOAM.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PluginOption:
    name: str
    kind: str                      # 'float' | 'int' | 'bool' | 'string'
    default: object                # float for numbers, str for 'string'
    help: str
    #: ``'m'`` for a length in model units, which the panel shows in the
    #: project display unit; '' for everything else.
    unit: str = ''

    @property
    def is_length(self) -> bool:
        return self.unit == 'm'

    @property
    def is_string(self) -> bool:
        return self.kind == 'string'


@dataclass(frozen=True)
class Plugin:
    name: str
    title: str
    summary: str
    options: tuple = ()
    #: A view is required as input; the run is refused without one.
    needs_view: bool = False
    #: Changes the mesh in the session (so the run is logged and replayed).
    changes_mesh: bool = False
    #: Why this build cannot run it ('' when it can).
    unavailable: str = ''
    #: Shown in the task row of the acceptance (Skin, MeshVolume, ...).
    shows: str = 'a view'
    #: Gmsh's own help text for the plugin.
    help: str = ''
    #: Build switches the plugin needs (``HAVE_MESH``, ``HAVE_KBIPACK`` ...).
    build_gate: tuple = ()
    #: Which package supplies the plugin ('GM9' for the generated ones).
    source: str = 'GM9'

    @property
    def takes_view(self) -> bool:
        """Has a ``View`` option (the panel fills it from the input view)."""
        return any(o.name == 'View' for o in self.options)


# BEGIN GENERATED (scripts/gmsh_catalogue/gen_plugin_catalogue.py)
CATALOGUE_FINGERPRINT = '1ede20dce2347342'
GMSH_VERSION = '4.15.2'

#: (name, title, summary, help, build gate, options); an option is
#: (name, kind, Gmsh default, unit, help).  kind: float | int |
#: bool | string.
_ROWS = (
    ('Skin', 'Skin',
     'Extracts the boundary (skin) of the current mesh (if FromMesh = 1), or from the the view View (in which case it creates a new view).',
     'Plugin(Skin) extracts the boundary (skin) of the current mesh (if FromMesh = 1), or from the the view View (in which case it creates a new view). If View < 0 and FromMesh = 0, the plugin is run on the current view.\nIf Visible is set, the plugin only extracts the skin of visible entities.',
     (), (
        ('Visible', 'bool', 1.0, '', 'If Visible is set, the plugin only extracts the skin of visible entities.'),
        ('FromMesh', 'bool', 0.0, '', 'Plugin(Skin) extracts the boundary (skin) of the current mesh (if FromMesh = 1), or from the the view View (in which case it creates a new view).'),
        ('View', 'int', -1.0, '', 'Plugin(Skin) extracts the boundary (skin) of the current mesh (if FromMesh = 1), or from the the view View (in which case it creates a new view).'),
     )),
    ('MeshVolume', 'Mesh volume',
     'Computes the volume of the mesh.',
     "Plugin(MeshVolume) computes the volume of the mesh.\n\nOnly the elements in the physical group PhysicalGroup of dimension Dimension are taken into account, unless 'PhysicalGroup' is negative, in which case all the elements of the given Dimension are considered. If Dimension is negative, all the elements are considered.\n\nPlugin(MeshVolume) creates one new list-based view.",
     (), (
        ('PhysicalGroup', 'int', -1.0, '', "Only the elements in the physical group PhysicalGroup of dimension Dimension are taken into account, unless 'PhysicalGroup' is negative, in which case all the elements of the given Dimension are considered."),
        ('Dimension', 'int', 3.0, '', "Only the elements in the physical group PhysicalGroup of dimension Dimension are taken into account, unless 'PhysicalGroup' is negative, in which case all the elements of the given Dimension are considered."),
     )),
    ('Distance', 'Distance',
     'Computes distances to entities in a mesh.',
     'Plugin(Distance) computes distances to entities in a mesh.\n\nIf PhysicalPoint, PhysicalLine and PhysicalSurface are 0, the distance is computed to all the boundaries. Otherwise the distance is computed to the given physical group.\n\nIf DistanceType is 0, the plugin computes the geometrical Euclidean distance using the naive O(N^2) algorithm. If DistanceType > 0, the plugin computes an approximate distance by solving a PDE with a diffusion constant equal to DistanceType time the maximum size of the bounding box of the mesh as in [Legrand et al. 2006].\n\nPositive MinScale and MaxScale scale the distance function.\n\nPlugin(Distance) creates one new list-based view.',
     ('HAVE_SOLVER',), (
        ('PhysicalPoint', 'int', 0.0, '', 'If PhysicalPoint, PhysicalLine and PhysicalSurface are 0, the distance is computed to all the boundaries.'),
        ('PhysicalLine', 'int', 0.0, '', 'If PhysicalPoint, PhysicalLine and PhysicalSurface are 0, the distance is computed to all the boundaries.'),
        ('PhysicalSurface', 'int', 0.0, '', 'If PhysicalPoint, PhysicalLine and PhysicalSurface are 0, the distance is computed to all the boundaries.'),
        ('DistanceType', 'float', 0.0, '', 'If DistanceType is 0, the plugin computes the geometrical Euclidean distance using the naive O(N^2) algorithm.'),
        ('MinScale', 'float', 0.0, '', 'Positive MinScale and MaxScale scale the distance function.'),
        ('MaxScale', 'float', 0.0, '', 'Positive MinScale and MaxScale scale the distance function.'),
     )),
    ('AnalyseMeshQuality', 'Analyse mesh quality',
     'Analyses the quality of the elements of a given dimension in the current model.',
     'Plugin(AnalyseMeshQuality) analyses the quality of the elements of a given dimension in the current model. Depending on the input parameters it computes the minimum of the Jacobian determinant (J), the IGE quality measure (Inverse Gradient Error) and/or the ICN quality measure (Condition Number). Statistics are printed and, if requested, a model-based post-processing view is created for each quality measure. The plugin can optionally hide elements by comparing the measure to a prescribed threshold.\n\nJ is faster to compute but gives information only on element validity while the other measures also give information on element quality. The IGE measure is related to the error on the gradient of the finite element solution. It is the scaled Jacobian for quads and hexes and a new measure for triangles and tetrahedra. The ICN measure is related to the condition number of the stiffness matrix. (See the article "Efficient computation of the minimum of shape quality measures on curvilinear finite elements" for details.)\n\nParameters:\n\n- JacobianDeterminant: compute J?\n\n- IGEMeasure: compute IGE?\n\n- ICNMeasure: compute ICN?\n\n- HidingThreshold: hide all elements for which min(mu) is strictly greater than (if ThresholdGreater == 1) or less than (if ThresholdGreater == 0) the threshold, where mu is ICN if ICNMeasure == 1, IGE if IGEMeasure == 1 or min(J)/max(J) if JacobianDeterminant == 1.\n\n- CreateView: create a model-based view of min(J)/max(J), min(IGE) and/or min(ICN)?\n\n- Recompute: force recomputation (set to 1 if the mesh has changed).\n\n- DimensionOfElements: analyse elements of the given dimension if equal to 1, 2 or 3; analyse 2D and 3D elements if equal to 4; or analyse elements of the highest dimension if equal to -1.',
     ('HAVE_MESH',), (
        ('JacobianDeterminant', 'bool', 0.0, '', '- JacobianDeterminant: compute J?'),
        ('IGEMeasure', 'bool', 0.0, '', '- IGEMeasure: compute IGE?'),
        ('ICNMeasure', 'bool', 0.0, '', '- ICNMeasure: compute ICN?'),
        ('HidingThreshold', 'float', 99.0, '', '- HidingThreshold: hide all elements for which min(mu) is strictly greater than (if ThresholdGreater == 1) or less than (if ThresholdGreater == 0) the threshold, where mu is ICN if ICNMeasure == 1, IGE if IGEMeasure == 1 or min(J)/max(J) if JacobianDeterminant == 1.'),
        ('ThresholdGreater', 'bool', 1.0, '', '- HidingThreshold: hide all elements for which min(mu) is strictly greater than (if ThresholdGreater == 1) or less than (if ThresholdGreater == 0) the threshold, where mu is ICN if ICNMeasure == 1, IGE if IGEMeasure == 1 or min(J)/max(J) if JacobianDeterminant == 1.'),
        ('CreateView', 'bool', 0.0, '', '- CreateView: create a model-based view of min(J)/max(J), min(IGE) and/or min(ICN)?'),
        ('Recompute', 'bool', 0.0, '', '- Recompute: force recomputation (set to 1 if the mesh has changed).'),
        ('DimensionOfElements', 'int', -1.0, '', '- DimensionOfElements: analyse elements of the given dimension if equal to 1, 2 or 3; analyse 2D and 3D elements if equal to 4; or analyse elements of the highest dimension if equal to -1.'),
     )),
    ('DiscretizationError', 'Discretization error',
     'Computes the error between the mesh and the geometry.',
     'Plugin(DiscretizationError) computes the error between the mesh and the geometry. It does so by supersampling the elements and computing the distance between the supersampled points dans their projection on the geometry.',
     (), (
        ('SuperSamplingNodes', 'int', 10.0, '', ''),
     )),
    ('ExtractEdges', 'Extract edges',
     'Extracts sharp edges from a triangular mesh.',
     'Plugin(ExtractEdges) extracts sharp edges from a triangular mesh.\n\nPlugin(ExtractEdges) creates one new view.',
     (), (
        ('Angle', 'float', 40.0, '', ''),
        ('IncludeBoundary', 'bool', 1.0, '', ''),
     )),
    ('BoundaryAngles', 'Boundary angles',
     'Computes the (interior) angles between the line elements on the boundary of all surfaces.',
     "Plugin(BoundaryAngles) computes the (interior) angles between the line elements on the boundary of all surfaces. The angles, computed modulo 2*Pi, are stored in a new post-processing view, one for each surface. The plugin currently only works for planar surfaces.Available options:- Visible (1=True, 0 = False, Default = 1): Visibility of the Views in the GUI - Save (1=True, 0 = False, Default = 0): Save the Views on disk ?- Remove (1=True, 0 = False, Default = 0): Remove the View from the memory after execution?- Filename (Default = 'Angles_Surface'): Root name for the Views (in case of save / Visibility)- Dir (Default = ''): Output directory (possibly nested)",
     (), (
        ('View', 'int', -1.0, '', 'The plugin currently only works for planar surfaces.Available options:- Visible (1=True, 0 = False, Default = 1): Visibility of the Views in the GUI - Save (1=True, 0 = False, Default = 0): Save the Views on disk ?- Remove (1=True, 0 = False, Default = 0): Remove the View from the memory after execu'),
        ('Save', 'bool', 0.0, '', 'The plugin currently only works for planar surfaces.Available options:- Visible (1=True, 0 = False, Default = 1): Visibility of the Views in the GUI - Save (1=True, 0 = False, Default = 0): Save the Views on disk ?- Remove (1=True, 0 = False, Default = 0): Remove the View from the memory after execu'),
        ('Visible', 'bool', 0.0, '', 'The plugin currently only works for planar surfaces.Available options:- Visible (1=True, 0 = False, Default = 1): Visibility of the Views in the GUI - Save (1=True, 0 = False, Default = 0): Save the Views on disk ?- Remove (1=True, 0 = False, Default = 0): Remove the View from the memory after execu'),
        ('Remove', 'bool', 0.0, '', 'The plugin currently only works for planar surfaces.Available options:- Visible (1=True, 0 = False, Default = 1): Visibility of the Views in the GUI - Save (1=True, 0 = False, Default = 0): Save the Views on disk ?- Remove (1=True, 0 = False, Default = 0): Remove the View from the memory after execu'),
        ('Filename', 'string', 'Angles_Surface', '', 'The plugin currently only works for planar surfaces.Available options:- Visible (1=True, 0 = False, Default = 1): Visibility of the Views in the GUI - Save (1=True, 0 = False, Default = 0): Save the Views on disk ?- Remove (1=True, 0 = False, Default = 0): Remove the View from the memory after execu'),
        ('Dir', 'string', '', '', 'The plugin currently only works for planar surfaces.Available options:- Visible (1=True, 0 = False, Default = 1): Visibility of the Views in the GUI - Save (1=True, 0 = False, Default = 0): Save the Views on disk ?- Remove (1=True, 0 = False, Default = 0): Remove the View from the memory after execu'),
     )),
    ('GaussPoints', 'Gauss points',
     'Given an input mesh, Plugin(GaussPoints) creates a list-based view containing the Gauss points for a given polynomial Order.',
     'Given an input mesh, Plugin(GaussPoints) creates a list-based view containing the Gauss points for a given polynomial Order.\n\nIf PhysicalGroup is nonzero, the plugin only creates points for the elements belonging to the group.',
     (), (
        ('Order', 'int', 0.0, '', 'Given an input mesh, Plugin(GaussPoints) creates a list-based view containing the Gauss points for a given polynomial Order.'),
        ('Dimension', 'int', 2.0, '', ''),
        ('PhysicalGroup', 'int', 0.0, '', 'If PhysicalGroup is nonzero, the plugin only creates points for the elements belonging to the group.'),
     )),
    ('CurvedBndDist', 'Curved bnd dist',
     '...',
     'Plugin(CurvedBndDist) ...',
     (), (
     )),
    ('MeshSizeFieldView', 'Mesh size field view',
     'Evaluates the mesh size field MeshSizeField on specified Component (0 for scalar) of the post-processing view View.',
     'Plugin(MeshSizeFieldView) evaluates the mesh size field MeshSizeField on specified Component (0 for scalar) of the post-processing view View.',
     ('HAVE_MESH',), (
        ('MeshSizeField', 'int', 0.0, '', 'Plugin(MeshSizeFieldView) evaluates the mesh size field MeshSizeField on specified Component (0 for scalar) of the post-processing view View.'),
        ('View', 'int', -1.0, '', 'Plugin(MeshSizeFieldView) evaluates the mesh size field MeshSizeField on specified Component (0 for scalar) of the post-processing view View.'),
        ('Component', 'int', 0.0, '', 'Plugin(MeshSizeFieldView) evaluates the mesh size field MeshSizeField on specified Component (0 for scalar) of the post-processing view View.'),
     )),
    ('CutPlane', 'Cut plane',
     'Cuts the view View with the plane A*X + B*Y + C*Z + D = 0.',
     'Plugin(CutPlane) cuts the view View with the plane A*X + B*Y + C*Z + D = 0.\n\nIf ExtractVolume is nonzero, the plugin extracts the elements on one side of the plane (depending on the sign of ExtractVolume).\n\nIf View < 0, the plugin is run on the current view. If Visible = 1, the plugin only cuts visible entities.\n\nPlugin(CutPlane) creates one new list-based view.',
     (), (
        ('A', 'float', 1.0, '', 'Plugin(CutPlane) cuts the view View with the plane A*X + B*Y + C*Z + D = 0.'),
        ('B', 'float', 0.0, '', 'Plugin(CutPlane) cuts the view View with the plane A*X + B*Y + C*Z + D = 0.'),
        ('C', 'float', 0.0, '', 'Plugin(CutPlane) cuts the view View with the plane A*X + B*Y + C*Z + D = 0.'),
        ('D', 'float', -0.01, 'm', 'Plugin(CutPlane) cuts the view View with the plane A*X + B*Y + C*Z + D = 0.'),
        ('ExtractVolume', 'int', 0.0, '', 'If ExtractVolume is nonzero, the plugin extracts the elements on one side of the plane (depending on the sign of ExtractVolume).'),
        ('RecurLevel', 'int', 3.0, '', ''),
        ('TargetError', 'float', 0.0001, '', ''),
        ('View', 'int', -1.0, '', 'Plugin(CutPlane) cuts the view View with the plane A*X + B*Y + C*Z + D = 0.'),
        ('Visible', 'bool', 1.0, '', 'If Visible = 1, the plugin only cuts visible entities.'),
     )),
    ('CutBox', 'Cut box',
     'Cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).',
     'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).\n\nThe number of points along U, V, W is set with the options NumPointsU, NumPointsV and NumPointsW.\n\nIf ConnectPoints is zero, the plugin creates points; otherwise, the plugin generates hexahedra, quadrangles, lines or points depending on the values of NumPointsU, NumPointsV and NumPointsW.\n\nIf Boundary is zero, the plugin interpolates the view inside the box; otherwise the plugin interpolates the view at its boundary.\n\nIf View < 0, the plugin is run on the current view.\n\nPlugin(CutBox) creates one new list-based view.',
     (), (
        ('X0', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Y0', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Z0', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('X1', 'float', 1.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Y1', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Z1', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('X2', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Y2', 'float', 1.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Z2', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('X3', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Y3', 'float', 0.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('Z3', 'float', 1.0, 'm', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
        ('NumPointsU', 'int', 20.0, '', 'The number of points along U, V, W is set with the options NumPointsU, NumPointsV and NumPointsW.'),
        ('NumPointsV', 'int', 20.0, '', 'The number of points along U, V, W is set with the options NumPointsU, NumPointsV and NumPointsW.'),
        ('NumPointsW', 'int', 20.0, '', 'The number of points along U, V, W is set with the options NumPointsU, NumPointsV and NumPointsW.'),
        ('ConnectPoints', 'bool', 1.0, '', 'If ConnectPoints is zero, the plugin creates points; otherwise, the plugin generates hexahedra, quadrangles, lines or points depending on the values of NumPointsU, NumPointsV and NumPointsW.'),
        ('Boundary', 'bool', 1.0, '', 'If Boundary is zero, the plugin interpolates the view inside the box; otherwise the plugin interpolates the view at its boundary.'),
        ('View', 'int', -1.0, '', 'Plugin(CutBox) cuts the view View with a rectangular box defined by the 4 points (X0,Y0,Z0) (origin), (X1,Y1,Z1) (axis of U), (X2,Y2,Z2) (axis of V) and (X3,Y3,Z3) (axis of W).'),
     )),
    ('CutSphere', 'Cut sphere',
     'Cuts the view View with the sphere (X-Xc)^2 + (Y-Yc)^2 + (Z-Zc)^2 = R^2.',
     'Plugin(CutSphere) cuts the view View with the sphere (X-Xc)^2 + (Y-Yc)^2 + (Z-Zc)^2 = R^2.\n\nIf ExtractVolume is nonzero, the plugin extracts the elements inside (if ExtractVolume < 0) or outside (if ExtractVolume > 0) the sphere.\n\nIf View < 0, the plugin is run on the current view. If Visible = 1, the plugin only cuts visible entities.\n\nPlugin(CutSphere) creates one new list-based view.',
     (), (
        ('Xc', 'float', 0.0, 'm', 'Plugin(CutSphere) cuts the view View with the sphere (X-Xc)^2 + (Y-Yc)^2 + (Z-Zc)^2 = R^2.'),
        ('Yc', 'float', 0.0, 'm', 'Plugin(CutSphere) cuts the view View with the sphere (X-Xc)^2 + (Y-Yc)^2 + (Z-Zc)^2 = R^2.'),
        ('Zc', 'float', 0.0, 'm', 'Plugin(CutSphere) cuts the view View with the sphere (X-Xc)^2 + (Y-Yc)^2 + (Z-Zc)^2 = R^2.'),
        ('R', 'float', 0.25, 'm', 'Plugin(CutSphere) cuts the view View with the sphere (X-Xc)^2 + (Y-Yc)^2 + (Z-Zc)^2 = R^2.'),
        ('ExtractVolume', 'int', 0.0, '', 'If ExtractVolume is nonzero, the plugin extracts the elements inside (if ExtractVolume < 0) or outside (if ExtractVolume > 0) the sphere.'),
        ('RecurLevel', 'int', 3.0, '', ''),
        ('TargetError', 'float', 0.0001, '', ''),
        ('View', 'int', -1.0, '', 'Plugin(CutSphere) cuts the view View with the sphere (X-Xc)^2 + (Y-Yc)^2 + (Z-Zc)^2 = R^2.'),
        ('Visible', 'bool', 1.0, '', 'If Visible = 1, the plugin only cuts visible entities.'),
     )),
    ('Isosurface', 'Isosurface',
     'Extracts the isosurface of value Value from the view View, and draws the OtherTimeStep-th step of the view OtherView on this isosurface.',
     'Plugin(Isosurface) extracts the isosurface of value Value from the view View, and draws the OtherTimeStep-th step of the view OtherView on this isosurface.\n\nIf ExtractVolume is nonzero, the plugin extracts the isovolume with values greater (if ExtractVolume > 0) or smaller (if ExtractVolume < 0) than the isosurface Value.\n\nIf OtherTimeStep < 0, the plugin uses, for each time step in View, the corresponding time step in OtherView. If OtherView < 0, the plugin uses View as the value source.\n\nIf View < 0, the plugin is run on the current view. If Visible = 1, the plugin only cuts visible entities.\n\nPlugin(Isosurface) creates as many list-based views as there are time steps in View.',
     (), (
        ('Value', 'float', 0.0, '', 'Plugin(Isosurface) extracts the isosurface of value Value from the view View, and draws the OtherTimeStep-th step of the view OtherView on this isosurface.'),
        ('ExtractVolume', 'int', 0.0, '', 'If ExtractVolume is nonzero, the plugin extracts the isovolume with values greater (if ExtractVolume > 0) or smaller (if ExtractVolume < 0) than the isosurface Value.'),
        ('RecurLevel', 'int', 3.0, '', ''),
        ('TargetError', 'float', 0.0001, '', ''),
        ('View', 'int', -1.0, '', 'Plugin(Isosurface) extracts the isosurface of value Value from the view View, and draws the OtherTimeStep-th step of the view OtherView on this isosurface.'),
        ('OtherTimeStep', 'int', -1.0, '', 'Plugin(Isosurface) extracts the isosurface of value Value from the view View, and draws the OtherTimeStep-th step of the view OtherView on this isosurface.'),
        ('OtherView', 'int', -1.0, '', 'Plugin(Isosurface) extracts the isosurface of value Value from the view View, and draws the OtherTimeStep-th step of the view OtherView on this isosurface.'),
        ('Visible', 'bool', 1.0, '', 'If Visible = 1, the plugin only cuts visible entities.'),
     )),
    ('ExtractElements', 'Extract elements',
     'Extracts some elements from the view View.',
     'Plugin(ExtractElements) extracts some elements from the view View. If MinVal != MaxVal, it extracts the elements whose TimeStep-th values (averaged by element) are comprised between MinVal and MaxVal. If Visible != 0, it extracts visible elements. \n\nIf View < 0, the plugin is run on the current view.\n\nPlugin(ExtractElements) creates one new list-based view.',
     (), (
        ('MinVal', 'float', 0.0, '', 'If MinVal != MaxVal, it extracts the elements whose TimeStep-th values (averaged by element) are comprised between MinVal and MaxVal.'),
        ('MaxVal', 'float', 0.0, '', 'If MinVal != MaxVal, it extracts the elements whose TimeStep-th values (averaged by element) are comprised between MinVal and MaxVal.'),
        ('TimeStep', 'int', 0.0, '', 'If MinVal != MaxVal, it extracts the elements whose TimeStep-th values (averaged by element) are comprised between MinVal and MaxVal.'),
        ('Visible', 'bool', 1.0, '', 'If Visible != 0, it extracts visible elements.'),
        ('Dimension', 'int', -1.0, '', ''),
        ('View', 'int', -1.0, '', 'Plugin(ExtractElements) extracts some elements from the view View.'),
     )),
    ('CutMesh', 'Cut mesh',
     "Cuts the mesh of the current GModel with the zero value of the levelset defined with the view 'View'.Sub-elements are created in the new model (polygons in 2D and polyhedra in 3D) and border elements are created on the zero-levelset.",
     "Plugin(CutMesh) cuts the mesh of the current GModel with the zero value of the levelset defined with the view 'View'.Sub-elements are created in the new model (polygons in 2D and polyhedra in 3D) and border elements are created on the zero-levelset.\n\nIf Split is nonzero, the plugin splits the meshalong the edges of the cut elements in the positive side.\n\nIf 'SaveTri' is nonzero, the sub-elements are saved as simplices.\n\nPlugin(CutMesh) creates one new GModel.",
     ('HAVE_DINTEGRATION',), (
        ('View', 'int', -1.0, '', "Plugin(CutMesh) cuts the mesh of the current GModel with the zero value of the levelset defined with the view 'View'.Sub-elements are created in the new model (polygons in 2D and polyhedra in 3D) and border elements are created on the zero-levelset."),
        ('Split', 'bool', 0.0, '', 'If Split is nonzero, the plugin splits the meshalong the edges of the cut elements in the positive side.'),
        ('SaveTri', 'bool', 0.0, '', "If 'SaveTri' is nonzero, the sub-elements are saved as simplices."),
     )),
    ('Triangulate', 'Triangulate',
     'Triangulates the points in the view View, assuming that all the points belong to a surface that can be projected one-to-one onto a plane.',
     'Plugin(Triangulate) triangulates the points in the view View, assuming that all the points belong to a surface that can be projected one-to-one onto a plane. \n\nIf View < 0, the plugin is run on the current view.\n\nPlugin(Triangulate) creates one new list-based view.',
     (), (
        ('View', 'int', -1.0, '', 'Plugin(Triangulate) triangulates the points in the view View, assuming that all the points belong to a surface that can be projected one-to-one onto a plane.'),
     )),
    ('Tetrahedralize', 'Tetrahedralize',
     'Tetrahedralizes the points in the view View.',
     'Plugin(Tetrahedralize) tetrahedralizes the points in the view View.\n\nIf View < 0, the plugin is run on the current view.\n\nPlugin(Tetrahedralize) creates one new list-based view.',
     ('HAVE_MESH',), (
        ('View', 'int', -1.0, '', 'Plugin(Tetrahedralize) tetrahedralizes the points in the view View.'),
     )),
    ('MakeSimplex', 'Make simplex',
     'Decomposes all non-simplectic elements (quadrangles, prisms, hexahedra, pyramids) in the view View into simplices (triangles, tetrahedra).',
     'Plugin(MakeSimplex) decomposes all non-simplectic elements (quadrangles, prisms, hexahedra, pyramids) in the view View into simplices (triangles, tetrahedra).\n\nIf View < 0, the plugin is run on the current view.\n\nPlugin(MakeSimplex) is executed in-place.',
     (), (
        ('View', 'int', -1.0, '', 'Plugin(MakeSimplex) decomposes all non-simplectic elements (quadrangles, prisms, hexahedra, pyramids) in the view View into simplices (triangles, tetrahedra).'),
     )),
    ('NearestNeighbor', 'Nearest neighbor',
     'Computes the distance from each point in View to its nearest neighbor.',
     'Plugin(NearestNeighbor) computes the distance from each point in View to its nearest neighbor.\n\nIf View < 0, the plugin is run on the current view.\n\nPlugin(NearestNeighbor) is executed in-place.',
     ('HAVE_ANN',), (
        ('View', 'int', -1.0, '', 'Plugin(NearestNeighbor) computes the distance from each point in View to its nearest neighbor.'),
     )),
    ('SimplePartition', 'Simple partition',
     'Partitions the current mesh into NumSlicesX, NumSlicesY and NumSlicesZ slices along the X-, Y- and Z-axis, respectively.',
     'Plugin(SimplePartition) partitions the current mesh into NumSlicesX, NumSlicesY and NumSlicesZ slices along the X-, Y- and Z-axis, respectively. The distribution of these slices is governed by MappingX, MappingY and MappingZ, where t is a normalized absissa along each direction. (Setting MappingX to t will thus lead to equidistant slices along the X-axis.)',
     (), (
        ('NumSlicesX', 'int', 4.0, '', 'Plugin(SimplePartition) partitions the current mesh into NumSlicesX, NumSlicesY and NumSlicesZ slices along the X-, Y- and Z-axis, respectively.'),
        ('NumSlicesY', 'int', 1.0, '', 'Plugin(SimplePartition) partitions the current mesh into NumSlicesX, NumSlicesY and NumSlicesZ slices along the X-, Y- and Z-axis, respectively.'),
        ('NumSlicesZ', 'int', 1.0, '', 'Plugin(SimplePartition) partitions the current mesh into NumSlicesX, NumSlicesY and NumSlicesZ slices along the X-, Y- and Z-axis, respectively.'),
        ('MappingX', 'string', 't', '', 'The distribution of these slices is governed by MappingX, MappingY and MappingZ, where t is a normalized absissa along each direction.'),
        ('MappingY', 'string', 't', '', 'The distribution of these slices is governed by MappingX, MappingY and MappingZ, where t is a normalized absissa along each direction.'),
        ('MappingZ', 'string', 't', '', 'The distribution of these slices is governed by MappingX, MappingY and MappingZ, where t is a normalized absissa along each direction.'),
     )),
    ('ShowNeighborElements', 'Show neighbor elements',
     'Sets visible some elements and a layer of elements around them, the other being set invisible.',
     'Plugin(ShowNeighborElements) sets visible some elements and a layer of elements around them, the other being set invisible.',
     (), (
        ('NumLayers', 'int', 1.0, '', ''),
        ('Element1', 'int', 0.0, '', ''),
        ('Element2', 'int', 0.0, '', ''),
        ('Element3', 'int', 0.0, '', ''),
        ('Element4', 'int', 0.0, '', ''),
        ('Element5', 'int', 0.0, '', ''),
     )),
    ('Invisible', 'Invisible',
     'Deletes (if DeleteElements is set) or reverses (if ReverseElements is set) all the invisible elements in the current model.',
     'Plugin(Invisible) deletes (if DeleteElements is set) or reverses (if ReverseElements is set) all the invisible elements in the current model. If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.',
     (), (
        ('DeleteElements', 'bool', 1.0, '', 'Plugin(Invisible) deletes (if DeleteElements is set) or reverses (if ReverseElements is set) all the invisible elements in the current model.'),
        ('ReverseElements', 'bool', 0.0, '', 'Plugin(Invisible) deletes (if DeleteElements is set) or reverses (if ReverseElements is set) all the invisible elements in the current model.'),
        ('XMin', 'float', 0.0, 'm', 'If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.'),
        ('YMin', 'float', 0.0, 'm', 'If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.'),
        ('ZMin', 'float', 0.0, 'm', 'If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.'),
        ('XMax', 'float', 0.0, 'm', 'If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.'),
        ('YMax', 'float', 0.0, 'm', 'If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.'),
        ('ZMax', 'float', 0.0, 'm', 'If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.'),
        ('Inside', 'bool', 0.0, '', 'If the bounding box defined by XMin < x < XMax, YMin < y < YMax and ZMin < z < ZMax is not empty, mark all elements outside or inside (if Inside is set) the bounding box as invisible prior to deleting or inverting the elements.'),
     )),
    ('MeshSubEntities', 'Mesh sub entities',
     'Creates mesh elements for the entities of dimension OutputDimension (0 for vertices, 1 for edges, 2 for faces) of the InputPhysicalGroup of dimension InputDimension.',
     'Plugin(MeshSubEntities) creates mesh elements for the entities of dimension OutputDimension (0 for vertices, 1 for edges, 2 for faces) of the InputPhysicalGroup of dimension InputDimension. The plugin creates new elements belonging to OutputPhysicalGroup.',
     (), (
        ('InputDimension', 'int', 1.0, '', 'Plugin(MeshSubEntities) creates mesh elements for the entities of dimension OutputDimension (0 for vertices, 1 for edges, 2 for faces) of the InputPhysicalGroup of dimension InputDimension.'),
        ('InputPhysicalGroup', 'int', 1.0, '', 'Plugin(MeshSubEntities) creates mesh elements for the entities of dimension OutputDimension (0 for vertices, 1 for edges, 2 for faces) of the InputPhysicalGroup of dimension InputDimension.'),
        ('OuputDimension', 'int', 0.0, '', ''),
        ('OuputPhysicalGroup', 'int', 2000.0, '', ''),
     )),
    ('Crack', 'Crack',
     'Creates a crack around the orientable, manifold physical group PhysicalGroup of dimension Dimension (1 or 2), embedded in a mesh of dimension Dimension + 1.The plugin duplicates the nodes and the elements on the crack and stores them in a new discrete curve (Dimension = 1) or surface (Dimension = 2).',
     'Plugin(Crack) creates a crack around the orientable, manifold physical group PhysicalGroup of dimension Dimension (1 or 2), embedded in a mesh of dimension Dimension + 1.The plugin duplicates the nodes and the elements on the crack and stores them in a new discrete curve (Dimension = 1) or surface (Dimension = 2). The elements touching the crack on the positive side are modified to use the newly generated nodes.If OpenBoundaryPhysicalGroup is given (> 0), its nodes are duplicated and the crack will be left open on that (part of the) boundary. Otherwise, the lips of the crack are sealed, i.e., its nodes are not duplicated. For 1D cracks, NormalX, NormalY and NormalZ provide the reference normal of the surface in which the crack is supposed to be embedded. If NewPhysicalGroup is positive, use it as the tag of the newly created curve or surface; otherwise use PhysicalGroup. If SwapOrientation is set to 1, the orientation of the duplicated elements on the crack is reversed.',
     (), (
        ('Dimension', 'int', 1.0, '', 'Plugin(Crack) creates a crack around the orientable, manifold physical group PhysicalGroup of dimension Dimension (1 or 2), embedded in a mesh of dimension Dimension + 1.The plugin duplicates the nodes and the elements on the crack and stores them in a new discrete curve (Dimension = 1) or surface ('),
        ('PhysicalGroup', 'int', 1.0, '', 'Plugin(Crack) creates a crack around the orientable, manifold physical group PhysicalGroup of dimension Dimension (1 or 2), embedded in a mesh of dimension Dimension + 1.The plugin duplicates the nodes and the elements on the crack and stores them in a new discrete curve (Dimension = 1) or surface ('),
        ('OpenBoundaryPhysicalGroup', 'int', 0.0, '', 'The elements touching the crack on the positive side are modified to use the newly generated nodes.If OpenBoundaryPhysicalGroup is given (> 0), its nodes are duplicated and the crack will be left open on that (part of the) boundary.'),
        ('NormalX', 'float', 0.0, '', 'For 1D cracks, NormalX, NormalY and NormalZ provide the reference normal of the surface in which the crack is supposed to be embedded.'),
        ('NormalY', 'float', 0.0, '', 'For 1D cracks, NormalX, NormalY and NormalZ provide the reference normal of the surface in which the crack is supposed to be embedded.'),
        ('NormalZ', 'float', 1.0, '', 'For 1D cracks, NormalX, NormalY and NormalZ provide the reference normal of the surface in which the crack is supposed to be embedded.'),
        ('NewPhysicalGroup', 'int', 0.0, '', 'If NewPhysicalGroup is positive, use it as the tag of the newly created curve or surface; otherwise use PhysicalGroup.'),
        ('DebugView', 'bool', 0.0, '', ''),
        ('SwapOrientation', 'bool', 0.0, '', 'If SwapOrientation is set to 1, the orientation of the duplicated elements on the crack is reversed.'),
     )),
    ('SpanningTree', 'Spanning tree',
     'Builds a tree spanning every vertex of a mesh and stores it directly in the model.',
     'Plugin(SpanningTree) builds a tree spanning every vertex of a mesh and stores it directly in the model.\nThe tree is constructed by starting first on the curves, then on the surfaces and finally on the volumes.\n\nParameters\n- PhysicalVolumes: list of the physical volumes upon which the tree must be built.\n- PhysicalSurfaces: list of the physical surfaces upon which the tree must be built.\n- PhysicalCurves: list of the physical curves upon which the tree must be built.\n- OutputPhysical: physical tag of the generated tree (-1 will select a new tag automatically).\n\nNote - Lists must be comma separated integers and spaces are ignored.\nRemark - This plugin does not overwrite a physical group.Therefore, if an existing physical tag is used in OutputPhysical, the edges of the tree will be /added/ to the specified group.',
     (), (
        ('OutputPhysical', 'int', -1.0, '', '- OutputPhysical: physical tag of the generated tree (-1 will select a new tag automatically).'),
        ('PhysicalVolumes', 'string', '', '', '- PhysicalVolumes: list of the physical volumes upon which the tree must be built.'),
        ('PhysicalSurfaces', 'string', '', '', '- PhysicalSurfaces: list of the physical surfaces upon which the tree must be built.'),
        ('PhysicalCurves', 'string', '', '', '- PhysicalCurves: list of the physical curves upon which the tree must be built.'),
     )),
    ('BoundaryLayer', 'Boundary layer',
     'Performs magic.',
     'Plugin(BoundaryLayer) performs magic.',
     (), (
        ('Thickness', 'float', 0.01, 'm', ''),
        ('Size', 'float', 0.001, 'm', ''),
        ('Ratio', 'float', 1.2, '', ''),
        ('Volumes', 'string', '', '', ''),
        ('Surfaces', 'string', '', '', ''),
        ('Curves', 'string', '', '', ''),
        ('Points', 'string', '', '', ''),
     )),
    ('Bubbles', 'Bubbles',
     'Constructs a geometry consisting of bubbles inscribed in the Voronoi of an input triangulation.',
     'Plugin(Bubbles) constructs a geometry consisting of bubbles inscribed in the Voronoi of an input triangulation. ShrinkFactor allows to change the size of the bubbles. The plugin expects a triangulation in the z = 0 plane to exist in the current model.\n\nPlugin(Bubbles) creates one .geo file.',
     (), (
        ('ShrinkFactor', 'float', 0.0, '', 'ShrinkFactor allows to change the size of the bubbles.'),
        ('OutputFile', 'string', 'bubbles.geo', '', ''),
     )),
    ('VoroMetal', 'Voro metal',
     'Creates microstructures using Voronoi diagrams.',
     'Plugin(VoroMetal) creates microstructures using Voronoi diagrams.',
     ('HAVE_MESH',), (
        ('ComputeBestSeeds', 'bool', 0.0, '', ''),
        ('ComputeMicrostructure', 'bool', 1.0, '', ''),
        ('SeedsFile', 'string', 'seeds.txt', '', ''),
     )),
    ('HomologyComputation', 'Homology computation',
     'Computes representative chains of basis elements of (relative) homology and cohomology spaces.',
     'Plugin(HomologyComputation) computes representative chains of basis elements of (relative) homology and cohomology spaces.\n\nDefine physical groups in order to specify the computation domain and the relative subdomain. Otherwise the whole mesh is the domain and the relative subdomain is empty. \n\nPlugin(HomologyComputation) creates new views, one for each basis element. If Filename is provided, the plugin saves the resulting basis chains of desired dimension along with the mesh.',
     ('HAVE_KBIPACK',), (
        ('ComputeHomology', 'bool', 1.0, '', ''),
        ('ComputeCohomology', 'bool', 0.0, '', ''),
        ('HomologyPhysicalGroupsBegin', 'int', -1.0, '', ''),
        ('CohomologyPhysicalGroupsBegin', 'int', -1.0, '', ''),
        ('CreatePostProcessingViews', 'bool', 1.0, '', ''),
        ('ReductionOmit', 'int', 1.0, '', ''),
        ('ReductionCombine', 'int', 3.0, '', ''),
        ('PostProcessSimplify', 'bool', 1.0, '', ''),
        ('ReductionHeuristic', 'int', 1.0, '', ''),
        ('DomainPhysicalGroups', 'string', '', '', ''),
        ('SubdomainPhysicalGroups', 'string', '', '', ''),
        ('ReductionImmunePhysicalGroups', 'string', '', '', ''),
        ('DimensionOfChainsToSave', 'string', '0, 1, 2, 3', '', ''),
        ('Filename', 'string', '', '', 'If Filename is provided, the plugin saves the resulting basis chains of desired dimension along with the mesh.'),
     )),
    ('HomologyPostProcessing', 'Homology post processing',
     'Operates on representative basis chains of homology and cohomology spaces.',
     "Plugin(HomologyPostProcessing) operates on representative basis chains of homology and cohomology spaces. Functionality:\n\n1. (co)homology basis transformation:\n 'TransformationMatrix': Integer matrix of the transformation.\n 'PhysicalGroupsOfOperatedChains': (Co)chains of a (co)homology space basis to be transformed.\n Results a new (co)chain basis that is an integer combination of the given basis.\n\n2. Make basis representations of a homology space and a cohomology space compatible:\n'PhysicalGroupsOfOperatedChains': Chains of a homology space basis.\n'PhysicalGroupsOfOperatedChains2': Cochains of a cohomology space basis.\nResults a new basis for the homology space such that the incidence matrix of the new basis and the basis of the cohomology space is the identity matrix.\n\nOptions:\n'PhysicalGroupsToTraceResults': Trace the resulting (co)chains to the given physical groups.\n'PhysicalGroupsToProjectResults': Project the resulting (co)chains to the complement of the given physical groups.\n'NameForResultChains': Post-processing view name prefix for the results.\n'ApplyBoundaryOperatorToResults': Apply boundary operator to the resulting chains.",
     ('HAVE_KBIPACK',), (
        ('ApplyBoundaryOperatorToResults', 'bool', 0.0, '', "'ApplyBoundaryOperatorToResults': Apply boundary operator to the resulting chains."),
        ('TransformationMatrix', 'string', '1, 0; 0, 1', '', "'TransformationMatrix': Integer matrix of the transformation."),
        ('PhysicalGroupsOfOperatedChains', 'string', '1, 2', '', "'PhysicalGroupsOfOperatedChains': (Co)chains of a (co)homology space basis to be transformed."),
        ('PhysicalGroupsOfOperatedChains2', 'string', '', '', "'PhysicalGroupsOfOperatedChains2': Cochains of a cohomology space basis."),
        ('PhysicalGroupsToTraceResults', 'string', '', '', "'PhysicalGroupsToTraceResults': Trace the resulting (co)chains to the given physical groups."),
        ('PhysicalGroupsToProjectResults', 'string', '', '', "'PhysicalGroupsToProjectResults': Project the resulting (co)chains to the complement of the given physical groups."),
        ('NameForResultChains', 'string', 'c', '', "'NameForResultChains': Post-processing view name prefix for the results."),
     )),
)

#: Post-processing plugins of the build, excluded by Q6 (plan section 5.2).
EXCLUDED_POST = (
    'Annotate',
    'Curl',
    'CutGrid',
    'CutParametric',
    'Divergence',
    'Eigenvalues',
    'Eigenvectors',
    'FieldFromAmplitudePhase',
    'Gradient',
    'HarmonicToTime',
    'Integrate',
    'Lambda2',
    'LongitudeLatitude',
    'MathEval',
    'MinMax',
    'ModifyComponents',
    'ModulusPhase',
    'NearToFarField',
    'NewView',
    'Particles',
    'Probe',
    'Remove',
    'Scal2Tens',
    'Scal2Vec',
    'Smooth',
    'SphericalRaise',
    'StreamLines',
    'Summation',
    'Transform',
    'Warp',
)

#: Plugins another package builds (CVTRemesh: GM16b).
DEFERRED = {'CVTRemesh': 'GM16b'}

#: Options the build lists that its API cannot set.
NOT_SETTABLE = ('AnalyseMeshQuality.Element to draw quality',)
# END GENERATED


#: Plugins that cannot run without an input view (``View`` is an index; Gmsh
#: falls back to the last view made, which a run from the panel must not rely
#: on).  Measured on 4.15.2 by ``tests/plan48/gm9/test_plugins_live.py``.
VIEW_REQUIRED = frozenset((
    'CutPlane', 'CutBox', 'CutSphere', 'Isosurface', 'ExtractElements',
    'Triangulate', 'Tetrahedralize', 'NearestNeighbor', 'MakeSimplex',
    'CutMesh', 'MeshSizeFieldView',
))
#: Plugins whose run changes the model or the mesh in the session, so the run
#: is logged and replays after a crash.  (``Skin`` is conditional, see
#: :func:`edits_mesh`.)
MESH_EDITORS = frozenset((
    'SimplePartition', 'Crack', 'Invisible', 'MeshSubEntities', 'CutMesh',
    'VoroMetal', 'HomologyComputation', 'BoundaryLayer',
))
#: What a plugin leaves on show, for the acceptance task row.
_SHOWS = {'Skin': 'a surface of the boundary', 'MeshVolume': 'a one-point view',
          'Crack': 'a duplicated crack curve or surface'}


def _build() -> tuple:
    out = []
    for name, title, summary, help_text, gate, options in _ROWS:
        out.append(Plugin(
            name, title, summary,
            tuple(PluginOption(o[0], o[1], o[2], o[4], o[3]) for o in options),
            needs_view=name in VIEW_REQUIRED,
            changes_mesh=name in MESH_EDITORS, unavailable='',
            shows=_SHOWS.get(name, 'a view'), help=help_text,
            build_gate=tuple(gate)))
    return tuple(out)


_BASE: tuple = _build()
_EXTRA: dict = {}

#: The mesh plugins in the panel's order; extra entries follow them.
PLUGINS: tuple = _BASE
BY_NAME: dict = {p.name: p for p in PLUGINS}

#: The four the plan names: they run and show in the viewport.
SHOWN_IN_VIEWPORT = ('Skin', 'MeshVolume', 'Distance', 'CutPlane')


def register_extra(entry: Plugin) -> Plugin:
    """Add a plugin another package builds (GM16b: CVTRemesh).

    The panel lists it after the generated ones, the local API accepts it, and
    ``numbers_for`` / ``strings_for`` / ``edits_mesh`` treat it like any other.
    Registered once per name; a second registration raises.  The extension
    point for the panel's form is ``view/gmsh_tools/registry.py``: see
    ``plugins_panel.PLUGIN_PANEL_AREA``.
    """
    global PLUGINS
    if entry.name in BY_NAME:
        raise ValueError('the Gmsh plugin %s is already listed' % entry.name)
    _EXTRA[entry.name] = entry
    PLUGINS = PLUGINS + (entry,)
    BY_NAME[entry.name] = entry
    return entry


def unregister_extra(name: str) -> None:
    """Take an extra plugin out again (tests)."""
    global PLUGINS
    if name in _EXTRA:
        del _EXTRA[name]
        PLUGINS = tuple(p for p in PLUGINS if p.name != name)
        BY_NAME.pop(name, None)


def plugin(name: str) -> Plugin:
    try:
        return BY_NAME[name]
    except KeyError:
        raise KeyError('FoamMesh has no entry for the Gmsh plugin %r' % name)


def runnable() -> tuple:
    return tuple(p for p in PLUGINS if not p.unavailable)


def defaults(name: str) -> dict:
    """Gmsh's own default of every option of the plugin."""
    return {o.name: o.default for o in plugin(name).options}


def _known(name: str) -> dict:
    return {option.name: option for option in plugin(name).options}


def numbers_for(name: str, values: dict) -> dict:
    """The number options to send: only those the plugin has, each a float."""
    known = _known(name)
    out = {}
    for key, value in (values or {}).items():
        if key not in known:
            raise ValueError('%s has no option %s' % (name, key))
        if known[key].is_string:
            continue
        out[key] = float(value)
    return out


def strings_for(name: str, values: dict) -> dict:
    """The string options to send: only those the plugin has, each a ``str``."""
    known = _known(name)
    out = {}
    for key, value in (values or {}).items():
        if key not in known:
            raise ValueError('%s has no option %s' % (name, key))
        if known[key].is_string:
            out[key] = '' if value is None else str(value)
    return out


def split_values(name: str, values: dict) -> tuple:
    """``(numbers, strings)`` for a mixed ``{option: value}`` dict."""
    return numbers_for(name, values), strings_for(name, values)


def edits_mesh(name: str, values: dict | None = None) -> bool:
    """Does this run change the mesh in the session (so it is logged)?

    ``Skin`` does when it takes its elements from the mesh: Gmsh then adds a
    discrete surface carrying the boundary triangles (measured on 4.15.2:
    surfaces 3 -> 4).  That is when ``FromMesh`` is set, and Gmsh's own default
    for it is 0, so it is read from the values given.
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


# --------------------------------------------------------------------------- #
# Running a plugin on a controller's resident session
# --------------------------------------------------------------------------- #

#: String options that name a file Gmsh writes; a relative name is placed in
#: the plugin output folder instead of the runner's working folder.
FILE_OUTPUTS = frozenset(('Bubbles.OutputFile', 'HomologyComputation.Filename'))


def full_values(name: str, values: dict | None) -> dict:
    """Gmsh's defaults overlaid with *values*.

    Plugin options are sticky inside a Gmsh process, so a run that sent only
    what the caller named would inherit the previous run's settings.  Every run
    sends every option, so the same call gives the same result.
    """
    merged = defaults(name)
    unknown = [k for k in (values or {}) if k not in merged]
    if unknown:
        raise ValueError('%s has no option %s' % (name, unknown[0]))
    merged.update(values or {})
    return merged


def run_on_controller(controller, name: str, values: dict | None = None, *,
                      view: int | None = None,
                      other_view: int | None = None,
                      time_limit: float | None = None) -> dict:
    """Run plugin *name* on the controller's resident session.

    The string options go with the numbers (``GenerateController.run_plugin``
    forwards numbers only).  Refuses, with the reason, what cannot run.
    """
    from . import generate_actions as actions

    try:
        entry = plugin(name)
    except KeyError as error:
        raise actions.ActionRefusal(str(error.args[0])) from None
    if entry.unavailable:
        raise actions.ActionRefusal(entry.unavailable)
    try:
        merged = full_values(name, values)
        numbers, strings = split_values(name, merged)
    except ValueError as error:
        raise actions.ActionRefusal(str(error)) from None
    if entry.needs_view and view is None:
        raise actions.ActionRefusal(
            '%s works on a view: run a plugin that makes one (Distance, '
            'Analyse mesh quality, Mesh volume) or pick a view of the '
            'session as the input view.' % entry.title)
    with controller._lock:
        session = controller.session
        if session is None or not session.alive:
            raise actions.ActionRefusal(
                'there is no mesh yet; generate one first')
        if not (session.state() or {}).get('nodes'):
            raise actions.ActionRefusal(
                'there is no mesh yet; generate one first')
        directory = controller.plugin_directory()
        Path(directory).mkdir(parents=True, exist_ok=True)
        for key in list(strings):
            if '%s.%s' % (name, key) in FILE_OUTPUTS and strings[key] \
                    and not strings[key].startswith(('/', '\\')) \
                    and ':' not in strings[key]:
                strings[key] = session.runtime_path(directory) + '/' \
                    + strings[key]
        kwargs = {'numbers': numbers, 'strings': strings,
                  'directory': session.runtime_path(directory)}
        if view is not None:
            kwargs['view'] = int(view)
        if other_view is not None:
            kwargs['other_view'] = int(other_view)
        edits = edits_mesh(name, merged)
        # Plan 48 TL: a plugin that does not return is stopped at its limit.
        limit = getattr(controller, '_limit', None)
        seconds = (limit('plugin', time_limit) if limit is not None
                   else float(time_limit or 0))
        answer = actions.call_with_limit(
            session, 'the %s plugin' % name, seconds,
            'session.plugin_edit' if edits else 'session.plugin_run', name,
            **kwargs)
        for item in answer.get('views') or ():
            if item.get('path'):
                item['host_path'] = str(Path(directory) / Path(
                    item['path']).name)
        answer['edits_mesh'] = bool(edits)
        return answer
