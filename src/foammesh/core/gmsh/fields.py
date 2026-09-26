"""The control register: every ``gmsh/*`` field, its native target, its consumer.

This is the single place that answers "what does this control actually do?".
The pipeline this replaces shipped five selectable values that changed nothing
and a setter that was reported effective while the mesh ignored it, because no
such register existed and nothing could be checked against one.

Three rules hold here, and are enforced by tests:

* every schema path under ``gmsh/`` appears exactly once,
* every entry names the consumer that reads it, and
* an entry whose consumer is :data:`GATE` names no Gmsh option, because it is
  a FoamMesh-side check rather than something handed to the mesher.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


#: Consumers. A control is only worth shipping if one of these reads it.
RUNNER = 'runner'          #: handed to Gmsh as an option or API call
DERIVATION = 'derivation'  #: shapes the job before the runner sees it
GATE = 'gate'              #: judged by FoamMesh, never sent to Gmsh
PUBLISH = 'publish'        #: consumed while writing constant/polyMesh


#: What kind of thing ``native_name`` names.
OPTION = 'option'   #: a ``gmsh.option.set*`` name, verifiable against Gmsh
API = 'api'         #: a ``gmsh.model.*`` call, not an option
NONE = 'none'       #: gates target nothing native


@dataclass(frozen=True)
class GmshControl:
    """One control, and exactly how it reaches the mesh."""

    path: str
    consumer: str
    #: The Gmsh option or API entry point. Empty only for GATE controls.
    native_name: str = ''
    #: Bumped when the derivation changes, so recorded values stay traceable.
    calculation_version: str = ''
    note: str = ''
    #: Whether ``native_name`` is an option name or an API call. Options are
    #: checked against a live Gmsh, which is how a control claiming
    #: ``Mesh.RemoveDuplicateNodes`` -- an API call, not an option -- was found.
    native_kind: str = OPTION

    def __post_init__(self) -> None:
        if self.consumer not in {RUNNER, DERIVATION, GATE, PUBLISH}:
            raise ValueError(f'{self.path}: unknown consumer {self.consumer!r}')
        if self.consumer is GATE and self.native_name:
            raise ValueError(
                f'{self.path}: a gate is judged by FoamMesh and must not claim '
                'a Gmsh option')
        if self.consumer != GATE and not self.native_name:
            raise ValueError(
                f'{self.path}: name the Gmsh option this reaches, or make it a '
                'gate; a control that reaches nothing must not ship')
        if self.native_kind not in {OPTION, API, NONE}:
            raise ValueError(f'{self.path}: unknown native kind {self.native_kind!r}')

    @property
    def field_id(self) -> str:
        return self.path.replace('/', '.')


def _register(*controls: GmshControl):
    seen: dict[str, GmshControl] = {}
    for control in controls:
        if control.path in seen:
            raise ValueError(f'duplicate control: {control.path}')
        seen[control.path] = control
    return MappingProxyType(seen)


CONTROLS = _register(
    # -- dimensionality --------------------------------------------------- #
    # FC-E. The mode is the only control here that reaches Gmsh: it chooses
    # the argument to `gmsh.model.mesh.generate`. The other five are read by
    # the publisher, which builds the second layer of nodes Gmsh never made,
    # so they are PUBLISH controls rather than options nobody could verify
    # against a live Gmsh.
    GmshControl('gmsh/dimensionality/mode', RUNNER, 'mesh.generate(dim)',
                'gmsh.dimensionality.v1', native_kind=API,
                note='two_d and axisymmetric mesh the section with '
                     'generate(2); the import check reads the same value '
                     'when it decides whether a model with no volumes is a '
                     'failure or exactly what was asked for'),
    GmshControl('gmsh/dimensionality/thickness', PUBLISH,
                'polyMesh.extrusion.thickness', 'gmsh.dimensionality.v1',
                native_kind=API,
                note='how far the section is translated to make the one cell '
                     'of thickness OpenFOAM needs between two empty patches'),
    GmshControl('gmsh/dimensionality/wedgeAngle', PUBLISH,
                'polyMesh.wedge.angle', 'gmsh.dimensionality.v1',
                native_kind=API,
                note='the included angle of an axisymmetric wedge, half each '
                     'way about the centre plane; refused outside 0-10 and '
                     'warned above 5'),
    GmshControl('gmsh/dimensionality/wedgeAxis', PUBLISH,
                'polyMesh.wedge.axis', 'gmsh.dimensionality.v1',
                native_kind=API,
                note='the coordinate axis the section is revolved about; '
                     'the wedge planes are symmetric about the plane it '
                     'lies in, which is what wedgePolyPatch checks'),
    GmshControl('gmsh/dimensionality/frontPatch', PUBLISH,
                'polyMesh.boundary.front', native_kind=API,
                note='named so the case can refer to it; typed empty or '
                     'wedge by the publisher, never by the user'),
    GmshControl('gmsh/dimensionality/backPatch', PUBLISH,
                'polyMesh.boundary.back', native_kind=API,
                note='the other of the two faces the extrusion makes'),
    # DP-675. The runner names the section's boundary curves with these
    # instead of `edge_<tag>`; the physical group is what becomes the SU2
    # marker and the polyMesh patch.
    GmshControl('gmsh/dimensionality/edgeNames', RUNNER,
                'model.addPhysicalGroup(1)', 'gmsh.dimensionality.v1',
                native_kind=API,
                note='names the section boundary curves by tag; a curve not '
                     'listed keeps edge_<tag>'),

    # -- global sizing -------------------------------------------------- #
    GmshControl('gmsh/globalSizing/targetSize', DERIVATION,
                'Mesh.MeshSizeMax', 'gmsh.sizing.v1',
                'derived from the bounding-box diagonal when unset'),
    GmshControl('gmsh/globalSizing/minimumSize', DERIVATION,
                'Mesh.MeshSizeMin', 'gmsh.sizing.v1',
                'defaults to one tenth of the target'),
    GmshControl('gmsh/globalSizing/sizeFactor', RUNNER, 'Mesh.MeshSizeFactor'),
    GmshControl('gmsh/globalSizing/fromCurvature', RUNNER,
                'Mesh.MeshSizeFromCurvature'),
    GmshControl('gmsh/globalSizing/fromPoints', RUNNER, 'Mesh.MeshSizeFromPoints'),
    GmshControl('gmsh/globalSizing/extendFromBoundary', RUNNER,
                'Mesh.MeshSizeExtendFromBoundary'),
    # Plan 30 WP12. Gmsh keeps one background field, so every size source is
    # folded into a single Min or Max field. This is that choice, and it is
    # how the Max field is reachable at all.
    GmshControl('gmsh/globalSizing/fieldCombiner', RUNNER,
                'field.Min+field.Max', native_kind=API),
    # Plan 31 FC-B. The curve-discretisation floors. Sizing decided how big an
    # element should be; these decide how few a curve may have whatever that
    # says, which is what stops a small hole becoming a triangle. Only
    # Mesh.MeshSizeFromCurvature reached the 1D pass before.
    GmshControl('gmsh/globalSizing/minimumCirclePoints', RUNNER,
                'Mesh.MinimumCirclePoints'),
    GmshControl('gmsh/globalSizing/minimumCurvePoints', RUNNER,
                'Mesh.MinimumCurvePoints'),
    GmshControl('gmsh/globalSizing/minimumElementsPerTwoPi', RUNNER,
                'Mesh.MinimumElementsPerTwoPi',
                note='read through the curvature pass, so it does nothing '
                     'with fromCurvature at zero; the derivation warns'),
    # Mesh.MeshSizeFromParametricPoints is deliberately absent. Plan 31
    # FC-B built it, then measured it on seven geometries and could not make
    # it move a mesh: +0.2, -0.6, -0.2, 0.0, +0.5, 0.0 and +2.1 per cent, a
    # band centred on nothing and inside this machine's own four-thread
    # run-to-run spread. The one path by which it could have acted -- a curve
    # control in `localSize` mode, the only thing in this runner that calls
    # `setSize` on a point -- was not exercised by that sweep, so the honest
    # statement is that it did nothing to the sizing this application
    # actually asks for. See `fcb-algorithms/curve.json` and `curve-wide.json`.
    # Plan 31 FC-C. The same option the cell shape writes, asked for a
    # different reason: value 3 splits every tetrahedron into four and leaves
    # them tetrahedra. It is a separate control because it is a separate
    # question -- how fine, not what shape -- and because the two cannot both
    # be written, which is a refusal the run has to be able to name.
    GmshControl('gmsh/globalSizing/barycentricRefinement', RUNNER,
                'Mesh.SubdivisionAlgorithm',
                note='value 3; refused when the cell shape already claims '
                     'this option'),

    # -- algorithms ------------------------------------------------------ #
    GmshControl('gmsh/algorithms/surface', RUNNER, 'Mesh.Algorithm'),
    GmshControl('gmsh/algorithms/volume', RUNNER, 'Mesh.Algorithm3D'),
    GmshControl('gmsh/algorithms/cellShape', RUNNER,
                'Mesh.SubdivisionAlgorithm'),
    # Plan 30 WP12. Recombination is exposed for the SU2 route only. The
    # derivation refuses it for every other target, for the reason the old
    # comment here recorded: 3D recombination makes tet/pyramid meshes that
    # Gmsh calls sound and OpenFOAM rejects outright. The runner sets the
    # global option and recombines each scoped surface explicitly, because
    # RecombineAll alone leaves a surface Gmsh chose not to touch as
    # triangles.
    GmshControl('gmsh/algorithms/recombine', RUNNER, 'Mesh.RecombineAll',
                note='su2 targets only; cleared with a warning otherwise'),
    # Plan 31 CP-08 item 3. Which recombiner, split out of the row above --
    # the two option names were recorded as one control, so nothing could
    # assert the value that reached Mesh.RecombinationAlgorithm and nothing
    # could choose it. MEASURED: the four qualified values produce four
    # different meshes, 4 meshes nothing while reading back as 4, and 9 is
    # clamped to 0 in silence.
    GmshControl('gmsh/algorithms/recombinationAlgorithm', RUNNER,
                'Mesh.RecombinationAlgorithm',
                note='only read when recombination is on; values outside the '
                     'qualified four are refused with the reason'),
    # Plan 31 FC-B. One control, two options. Gmsh defaults both on, so what
    # this buys is not the retry -- that was always happening -- but the
    # ability to turn it off, and `measure_algorithms` in the runner, which
    # reads the algorithm each surface was finally meshed with out of Gmsh's
    # log and records it. The option cannot be read back for this: it still
    # holds the algorithm that failed.
    GmshControl('gmsh/algorithms/algorithmFallback', RUNNER,
                'Mesh.AlgorithmSwitchOnFailure+Mesh.MaxRetries',
                note='on by default in Gmsh itself; the run manifest names '
                     'the algorithm that actually meshed each surface'),
    # Plan 31 FC-C. Not an option: it is a call that rewrites the surface mesh
    # between the 2D and the 3D pass, which is the only place it works --
    # after the 3D pass the pyramids recombination made are already there and
    # splitting the quadrangles does not remove them.
    GmshControl('gmsh/algorithms/splitQuadrangles', RUNNER,
                'mesh.splitQuadrangles', 'gmsh.splitQuadrangles.v1',
                native_kind=API,
                note='only read when recombination is on; lets a target that '
                     'reads tetrahedra keep the recombination instead of '
                     'having it cleared'),

    # -- structuring ----------------------------------------------------- #
    # Plan 31 FC-C. Not an option: `setTransfiniteAutomatic` walks the model
    # and puts transfinite constraints on every volume whose faces it can
    # interpolate, so what it reaches is the model, not a setting. It
    # recombines as it goes, which is why it carries the same SU2-only guard
    # recombination does, and it succeeds per volume -- so the run reports how
    # many volumes came back structured rather than that it was switched on.
    GmshControl('gmsh/structuring/automatic', RUNNER,
                'mesh.setTransfiniteAutomatic', 'gmsh.structuring.v1',
                native_kind=API,
                note='su2 targets only; measured per volume, never from the '
                     'request'),
    GmshControl('gmsh/structuring/transfiniteTri', RUNNER,
                'Mesh.TransfiniteTri',
                note='structures the inside of a three-sided transfinite '
                     'face: measured 120 triangles down to 64, which is 8×8'),

    # -- farfield -------------------------------------------------------- #
    # Plan 30 WP12. Built with the OCC kernel, not set as an option: the box
    # is added and the solids are cut out of it, so what the control produces
    # is geometry rather than a setting.
    GmshControl('gmsh/farfield/enabled', RUNNER, 'occ.addBox+occ.cut',
                native_kind=API,
                note='CAD only; a tessellated import has no solid to cut'),
    # Plan 31 CP-08 item 7. Not an option but a decision: which of the
    # volumes the cut leaves is meshed. It reaches occ.remove, or does not.
    GmshControl('gmsh/farfield/sealedCavities', RUNNER, 'occ.remove',
                note='discard meshes the external domain alone, keep meshes '
                     'the pockets as separate regions, refuse stops'),
    GmshControl('gmsh/farfield/padding', RUNNER, 'occ.addBox',
                native_kind=API,
                note='multiples of the bounding-box diagonal, per side'),

    # -- parallel -------------------------------------------------------- #
    GmshControl('gmsh/parallel/threads', RUNNER,
                'General.NumThreads+Mesh.MaxNumThreads3D'
                '+Mesh.MaxNumThreads2D',
                note='one count, three stages, and only two of them here: '
                     'the surface limit takes the request whole and the '
                     'volume limit takes what the chosen volume algorithm '
                     'can actually use. Mesh.MaxNumThreads1D exists in this '
                     'build and is deliberately not set — see the '
                     'curve-stage row of the capability ledger'),

    # -- healing --------------------------------------------------------- #
    GmshControl('gmsh/healing/importTolerance', RUNNER, 'Geometry.Tolerance'),
    GmshControl('gmsh/healing/sewFaces', RUNNER, 'Geometry.OCCSewFaces',
                note='measured: enabling this drops the solid to a face set'),
    GmshControl('gmsh/healing/fixDegenerated', RUNNER,
                'Geometry.OCCFixDegenerated',
                note='measured: enabling this makes a sphere unmeshable'),
    # Not an option: Gmsh exposes this only as a call, which is why the
    # runner invokes it after meshing instead of setting a number.
    GmshControl('gmsh/healing/makeSolids', RUNNER, 'Geometry.OCCMakeSolids',
                note='measured: recovers a solid from a sewn shell, 0 volumes '
                     'to 1'),
    GmshControl('gmsh/healing/removeDuplicateNodes', RUNNER,
                'mesh.removeDuplicateNodes', native_kind=API),
    # Also a call rather than an option. Measured: two solids sharing a face
    # import as two copies of it and the volume mesher fails outright with
    # "Could not recover boundary mesh"; fusing them meshes 234,648 cells.
    GmshControl('gmsh/healing/removeDuplicateFaces', RUNNER,
                'occ.removeAllDuplicates', native_kind=API,
                note='measured: makes a shared interface conformal'),
    # Read only for tessellated input, which Gmsh meshes as a discrete
    # geometry rather than through the CAD importer.
    GmshControl('gmsh/healing/classificationAngle', RUNNER,
                'mesh.classifySurfaces', native_kind=API,
                note='dihedral angle for recovering patches from STL/OBJ'),

    # -- healing: the explicit repair pass and the OCC fix family --------- #
    # Plan 31, "Geometry kernels and healing". An earlier audit of this
    # repository recorded "heal_shape() uncalled" as a standing defect and it
    # stayed uncalled: the page offered the four import flags and no pass.
    # Not an option -- Gmsh exposes healing only as a call.
    GmshControl('gmsh/healing/healShapes', RUNNER, 'occ.healShapes',
                native_kind=API,
                note='explicit repair pass; performs exactly the repairs '
                     'ticked on this page, and reports what it changed'),
    GmshControl('gmsh/healing/fixSmallEdges', RUNNER,
                'Geometry.OCCFixSmallEdges',
                note='probed default 0; also selects that repair in the '
                     'healShapes pass'),
    GmshControl('gmsh/healing/fixSmallFaces', RUNNER,
                'Geometry.OCCFixSmallFaces',
                note='probed default 0; also selects that repair in the '
                     'healShapes pass'),
    GmshControl('gmsh/healing/autoFix', RUNNER, 'Geometry.OCCAutoFix',
                note='probed default 1; off is how a repaired import is '
                     'compared against the raw CAD'),
    GmshControl('gmsh/healing/unionUnify', RUNNER, 'Geometry.OCCUnionUnify',
                note='probed default 1; off keeps the fragments a boolean '
                     'left behind'),
    GmshControl('gmsh/healing/booleanTolerance', RUNNER,
                'Geometry.ToleranceBoolean',
                note='probed default 0, meaning the kernel default; read by '
                     'the farfield cut and the duplicate-face fuse'),
    GmshControl('gmsh/healing/importScaling', RUNNER, 'Geometry.OCCScaling',
                note='probed default 1; for CAD that declares no unit'),
    GmshControl('gmsh/healing/importLabels', RUNNER,
                'Geometry.OCCImportLabels',
                note='probed default 1; patch identity is built from the '
                     'labels, so off is a deliberate act'),
    GmshControl('gmsh/healing/occParallel', RUNNER, 'Geometry.OCCParallel',
                note='probed default 0; import speed only, not the result'),

    # -- optimisation ---------------------------------------------------- #
    GmshControl('gmsh/optimization/optimize', RUNNER, 'Mesh.Optimize'),
    # Plan 30 WP12. The boolean and the pass count are two controls, and the
    # runner used to hand the pass count to the boolean option.
    GmshControl('gmsh/optimization/netgen', RUNNER, 'Mesh.OptimizeNetgen'),
    GmshControl('gmsh/optimization/netgenPasses', RUNNER,
                'mesh.optimize.Netgen', native_kind=API,
                note='the explicit optimise loop; the option above is the '
                     'boolean Gmsh reads at the end of generate()'),
    GmshControl('gmsh/optimization/smoothing', RUNNER, 'Mesh.Smoothing'),
    GmshControl('gmsh/optimization/optimizeThreshold', RUNNER,
                'Mesh.OptimizeThreshold'),
    # Plan 31 FC-D. Two `optimize()` names rather than an option, because
    # there is no option for them: this is the only route the product has to
    # rescue a mesh with inverted elements, which the gate otherwise simply
    # refuses. The two run in that order -- untangle first, then relocate --
    # and each is judged by the mesh it left, never by the call being
    # accepted: this build accepts `optimize("Bogus")` without complaint.
    GmshControl('gmsh/optimization/repairPoorElements', RUNNER,
                'mesh.optimize.UntangleMeshGeometry+Relocate3D',
                native_kind=API,
                note='runs only when the mesh has already failed the quality '
                     'limit, and only on straight-sided elements; the run '
                     'records the worst element either side of it'),
    GmshControl('gmsh/optimization/highOrderOptimize', RUNNER,
                'Mesh.HighOrderOptimize',
                note='read by generate(), and only when Mesh.ElementOrder '
                     'is already 2; measured on a tangled torus as 26 '
                     'inverted elements at 0 against 0 at 1, and the run '
                     'records the worst element of the mesh it produced'),
    GmshControl('gmsh/optimization/qualityType', RUNNER, 'Mesh.QualityType'),
    GmshControl('gmsh/optimization/minQuality', GATE,
                calculation_version='gmsh.quality.v2',
                note='compared against the achieved element qualities'),
    # Plan 26 WP1.1. The gate used to compare the single worst element against
    # minQuality, so three bad elements read exactly like thirty thousand.
    # These say how much of the distribution may miss the limit. Both default
    # to zero, which reproduces the old behaviour exactly.
    GmshControl('gmsh/optimization/allowedFraction', GATE,
                calculation_version='gmsh.quality.v2',
                note='share of elements permitted below minQuality'),
    GmshControl('gmsh/optimization/allowedCount', GATE,
                calculation_version='gmsh.quality.v2',
                note='absolute count permitted below minQuality'),
    # Not a stricter minQuality: the point below which no allowance applies
    # and no human may accept the mesh, because an inverted or zero-volume
    # cell is not something a solver can integrate over.
    GmshControl('gmsh/optimization/hardFloor', GATE,
                calculation_version='gmsh.quality.v2',
                note='never acceptable below this, whatever the allowance'),

    # -- boundary layers -------------------------------------------------- #
    # R118. Which patches grow layers. MEASURED on the venturi: prisms sat on
    # the inlet plane (z=0.004999) and on the outlet plane (z=0.5937), which is
    # wrong for every flow case. Empty now means empty, not everything; the
    # runner rebuilds each un-extruded patch from the extrusion's inner rim,
    # so the core volume still closes.
    GmshControl('gmsh/boundaryLayers/patches', DERIVATION,
                'geo.extrudeBoundaryLayer', 'gmsh.layers.v1', native_kind=API),
    # Plan 33 section 1.1. Whether the list above is the answer or whether
    # the run is to read every eligible wall off the geometry it imports.
    # The rule that decides what a wall is lives in one file the page and the
    # runner both read, so a ticked list and an extruded surface agree.
    GmshControl('gmsh/boundaryLayers/patchMode', DERIVATION,
                'geo.extrudeBoundaryLayer', 'gmsh.layers.v1', native_kind=API),
    GmshControl('gmsh/boundaryLayers/enabled', DERIVATION,
                'geo.extrudeBoundaryLayer', 'gmsh.layers.v1', native_kind=API),
    GmshControl('gmsh/boundaryLayers/mode', DERIVATION,
                'geo.extrudeBoundaryLayer', 'gmsh.layers.v1', native_kind=API),
    GmshControl('gmsh/boundaryLayers/firstHeight', DERIVATION,
                'geo.extrudeBoundaryLayer.heights', 'gmsh.layers.v1', native_kind=API),
    GmshControl('gmsh/boundaryLayers/ratio', DERIVATION,
                'geo.extrudeBoundaryLayer.heights', 'gmsh.layers.v1', native_kind=API),
    GmshControl('gmsh/boundaryLayers/layerCount', DERIVATION,
                'geo.extrudeBoundaryLayer.numLayers', 'gmsh.layers.v1', native_kind=API),
    GmshControl('gmsh/boundaryLayers/totalThickness', DERIVATION,
                'geo.extrudeBoundaryLayer.heights', 'gmsh.layers.v1', native_kind=API),
    GmshControl('gmsh/boundaryLayers/quads', RUNNER,
                'geo.extrudeBoundaryLayer.recombine', native_kind=API),

    # -- size fields ------------------------------------------------------ #
    GmshControl('gmsh/sizeFields/{id}/name', DERIVATION, 'field.name',
                'gmsh.size_fields.v1', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/enabled', DERIVATION, 'field.enabled',
                'gmsh.size_fields.v1', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/priority', DERIVATION, 'field.order',
                'gmsh.size_fields.v1', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/fieldType', RUNNER, 'mesh.field.add', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/scopeToken', DERIVATION,
                'Distance.SurfacesList', 'gmsh.size_fields.v1', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/sizeInside', RUNNER,
                'Threshold.SizeMin+Box.VIn', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/sizeOutside', RUNNER,
                'Threshold.SizeMax+Box.VOut', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/distanceMin', RUNNER, 'Threshold.DistMin', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/distanceMax', RUNNER, 'Threshold.DistMax', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/centre/x', RUNNER,
                'Ball.XCenter+Cylinder.XCenter+Frustum.X1', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/centre/y', RUNNER,
                'Ball.YCenter+Cylinder.YCenter+Frustum.Y1', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/centre/z', RUNNER,
                'Ball.ZCenter+Cylinder.ZCenter+Frustum.Z1', native_kind=API),
    # WP-01 F-16. Two opposite corners, not a corner and a size.
    GmshControl('gmsh/sizeFields/{id}/boxMin/x', RUNNER, 'Box.XMin', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/boxMin/y', RUNNER, 'Box.YMin', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/boxMin/z', RUNNER, 'Box.ZMin', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/boxMax/x', RUNNER, 'Box.XMax', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/boxMax/y', RUNNER, 'Box.YMax', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/boxMax/z', RUNNER, 'Box.ZMax', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/radius', RUNNER,
                'Ball.Radius+Cylinder.Radius+Frustum.R1_outer', native_kind=API),
    # WP-01 F-16. The far end's radius; a frustum needs both or it is a tube.
    GmshControl('gmsh/sizeFields/{id}/radiusEnd', RUNNER, 'Frustum.R2_outer', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/axis/x', RUNNER, 'Cylinder.XAxis', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/axis/y', RUNNER, 'Cylinder.YAxis', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/axis/z', RUNNER, 'Cylinder.ZAxis', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/thickness', RUNNER, 'Ball.Thickness', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/sampling', RUNNER, 'Distance.Sampling',
                native_kind=API,
                note='points sampled per scoped entity when the distance '
                     'field is built'),
    GmshControl('gmsh/sizeFields/{id}/curvatureDelta', RUNNER,
                'Curvature.Delta', native_kind=API),
    GmshControl('gmsh/sizeFields/{id}/curvatureMin', RUNNER,
                'Threshold.DistMin', native_kind=API,
                note='curvature rows only: the Threshold ramps over curvature '
                     'rather than over distance'),
    GmshControl('gmsh/sizeFields/{id}/curvatureMax', RUNNER,
                'Threshold.DistMax', native_kind=API,
                note='curvature rows only'),
    GmshControl('gmsh/sizeFields/{id}/expression', DERIVATION, 'MathEval.F',
                'gmsh.size_fields.v1', native_kind=API,
                note='validated by name and costed across the domain before '
                     'a job is accepted'),

    # -- per-surface sizes -------------------------------------------------- #
    # Plan 29 WP8. Each row is compiled into a Distance+Threshold pair by
    # `size_fields.derive_surface_sizes`, so the fields it names are the
    # Threshold's, not a per-row Gmsh option.
    GmshControl('gmsh/surfaceSizes/{id}/name', DERIVATION, 'field.name',
                'gmsh.size_fields.v1', native_kind=API),
    GmshControl('gmsh/surfaceSizes/{id}/enabled', DERIVATION, 'field.enabled',
                'gmsh.size_fields.v1', native_kind=API),
    GmshControl('gmsh/surfaceSizes/{id}/surfaceId', DERIVATION,
                'Distance.SurfacesList', 'gmsh.size_fields.v1', native_kind=API,
                note='the Gmsh surface tag the row last resolved to; the '
                     'reference beside it is what the run resolves'),
    GmshControl('gmsh/surfaceSizes/{id}/surfaceRef', DERIVATION,
                'Distance.SurfacesList', 'gmsh.size_fields.v1', native_kind=API,
                note='Plan 33 W-G1: the prepared boundary the row refines. A '
                     'tag is a position in import order, so the reference is '
                     'the identity and the tag is derived from it'),
    GmshControl('gmsh/surfaceSizes/{id}/targetSize', RUNNER,
                'Threshold.SizeMin', native_kind=API),
    GmshControl('gmsh/surfaceSizes/{id}/blendDistance', DERIVATION,
                'Threshold.DistMax', 'gmsh.size_fields.v1', native_kind=API,
                note='zero derives one global cell, because a Threshold with '
                     'no ramp is a step change Gmsh refuses'),
    GmshControl('gmsh/surfaceSizes/{id}/priority', DERIVATION, 'field.order',
                'gmsh.size_fields.v1', native_kind=API),

    # -- output ------------------------------------------------------------- #
    # Plan 29 WP8. Offered on the SU2 route only: `gmshToFoam` reads
    # first-order MSH 2.2, so second order cannot reach OpenFOAM at all.
    GmshControl('gmsh/output/elementOrder', DERIVATION, 'Mesh.ElementOrder',
                'gmsh.export.v1',
                note='clamped to 1 unless the target solver is SU2'),
    GmshControl('gmsh/output/secondOrderIncomplete', DERIVATION,
                'Mesh.SecondOrderIncomplete', 'gmsh.export.v1',
                note='serendipity elements; read only when the order is 2'),
    # Plan 31 FC-D. The one high-order knob that moves this mesher's output:
    # straight midside nodes cannot invert an element, curved ones can and do.
    GmshControl('gmsh/output/secondOrderLinear', DERIVATION,
                'Mesh.SecondOrderLinear', 'gmsh.export.v1',
                note='midside nodes on the straight edge rather than on the '
                     'CAD; read only when the order is 2'),
    # Plan 31 FC-D. `Mesh.Renumber` is an option generate() reads, and this
    # runner has already generated by the time the mesh is final, so the
    # renumbering is done by the API call of the same effect.
    GmshControl('gmsh/output/renumber', RUNNER,
                'mesh.computeRenumbering+mesh.renumberNodes', native_kind=API,
                note='reverse Cuthill-McKee over the nodes, after the order '
                     'is raised and the duplicates merged'),

    # -- curve controls ---------------------------------------------------- #
    GmshControl('gmsh/curveControls/{id}/name', DERIVATION, 'curve.name',
                'gmsh.curves.v1', native_kind=API),
    GmshControl('gmsh/curveControls/{id}/enabled', DERIVATION, 'curve.enabled',
                'gmsh.curves.v1', native_kind=API),
    GmshControl('gmsh/curveControls/{id}/scopeToken', DERIVATION,
                'mesh.setTransfiniteCurve.tags', 'gmsh.curves.v1', native_kind=API),
    GmshControl('gmsh/curveControls/{id}/mode', RUNNER,
                'mesh.setTransfiniteCurve+mesh.setSize', native_kind=API),
    GmshControl('gmsh/curveControls/{id}/segments', DERIVATION,
                'mesh.setTransfiniteCurve.numNodes', 'gmsh.curves.v2',
                native_kind=API,
                note='elements, not nodes; the derivation adds one because '
                     'Gmsh counts nodes'),
    GmshControl('gmsh/curveControls/{id}/transfiniteSurface', RUNNER,
                'mesh.setTransfiniteSurface', native_kind=API,
                note='CAD only: a tessellated surface has no corner points '
                     'for Gmsh to interpolate between'),
    GmshControl('gmsh/curveControls/{id}/cornerPoints', DERIVATION,
                'mesh.setTransfiniteSurface.cornerTags', 'gmsh.curves.v3',
                native_kind=API,
                note='which corners to interpolate between on a face with '
                     'more than four; Gmsh does not check they are on the '
                     'face and meshes something else, so the runner does'),
    GmshControl('gmsh/curveControls/{id}/law', RUNNER,
                'mesh.setTransfiniteCurve.meshType', native_kind=API),
    GmshControl('gmsh/curveControls/{id}/reverseGrading', RUNNER,
                'mesh.setTransfiniteCurve.coef', native_kind=API,
                note='Gmsh reads the grading direction as the sign of the '
                     'coefficient; a Bump law is symmetric and ignores it'),
    GmshControl('gmsh/curveControls/{id}/coefficient', RUNNER,
                'mesh.setTransfiniteCurve.coef', native_kind=API),
    GmshControl('gmsh/curveControls/{id}/localSize', RUNNER, 'mesh.setSize', native_kind=API),
    GmshControl('gmsh/curveControls/{id}/priority', DERIVATION, 'curve.order',
                'gmsh.curves.v1', native_kind=API),

    # -- volume controls ---------------------------------------------------- #
    GmshControl('gmsh/volumeControls/{id}/name', DERIVATION, 'volume.name',
                'gmsh.volumes.v1', native_kind=API),
    GmshControl('gmsh/volumeControls/{id}/enabled', DERIVATION, 'volume.enabled',
                'gmsh.volumes.v1', native_kind=API),
    GmshControl('gmsh/volumeControls/{id}/scopeToken', DERIVATION,
                'model.addPhysicalGroup.tags', 'gmsh.volumes.v1', native_kind=API),
    GmshControl('gmsh/volumeControls/{id}/included', DERIVATION,
                'occ.remove', 'gmsh.volumes.v1', native_kind=API),
    # A Restrict-style Constant field, not mesh.setSize: setSize acts on
    # points, and a point shared with the neighbouring volume would carry the
    # size across the boundary.
    GmshControl('gmsh/volumeControls/{id}/targetSize', RUNNER,
                'field.Constant.VolumesList', native_kind=API),
    GmshControl('gmsh/volumeControls/{id}/transfinite', RUNNER,
                'mesh.setTransfiniteVolume', native_kind=API),
    GmshControl('gmsh/volumeControls/{id}/priority', DERIVATION, 'volume.order',
                'gmsh.volumes.v1', native_kind=API),

    # -- periodic pairs ------------------------------------------------------ #
    GmshControl('gmsh/periodicPairs/{id}/name', DERIVATION, 'periodic.name',
                'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/enabled', DERIVATION, 'periodic.enabled',
                'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/masterScopeToken', DERIVATION,
                'mesh.setPeriodic.master', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/slaveScopeToken', DERIVATION,
                'mesh.setPeriodic.slave', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/transform', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/translation/x', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/translation/y', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/translation/z', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/rotationCentre/x', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/rotationCentre/y', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/rotationCentre/z', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/rotationAxis/x', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/rotationAxis/y', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/rotationAxis/z', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/rotationAngleDegrees', DERIVATION,
                'mesh.setPeriodic.affineTransform', 'gmsh.periodic.v1', native_kind=API),
    GmshControl('gmsh/periodicPairs/{id}/matchTolerance', PUBLISH,
                'cyclic.matchTolerance', native_kind=API),
)


#: Plan 30 WP12 (F-43). Which parameters of a size-field row the runner reads
#: for each field type.
#:
#: The editor rendered every parameter for every type, so a Ball row offered a
#: distance ramp and a Distance row offered a radius, and the page docstring
#: argued that hiding them was "the register's job, not the page's". It was:
#: the register simply did not say. It does now, and the editor reads it, so
#: the form shows the parameters that reach Gmsh and nothing else.
#:
#: The lists are pinned against ``apply_size_fields`` in ``runner_v1.py`` by
#: ``test_gmsh_size_field_applicability.py``, which reads the runner's own
#: branches rather than trusting this table, so a parameter added to the
#: runner and not to this table fails a test instead of vanishing from a form.
FIELD_PARAMETERS_ALWAYS = frozenset({
    # The row's identity and bookkeeping. Read by the derivation and by the
    # ledger rather than inside one branch, so every type shows them.
    'name', 'fieldType', 'enabled', 'priority',
})

#: Parameters that are points or directions. The editor spells each component
#: as its own field (``centre.x``), so a mapping to editor keys has to expand
#: them; the runner reads the whole triple.
FIELD_VECTOR_PARAMETERS = frozenset({'centre', 'axis', 'boxMin', 'boxMax'})

FIELD_PARAMETERS: dict = MappingProxyType({
    'distance_threshold': frozenset({
        'scopeToken', 'surfaces', 'sampling',
        'sizeInside', 'sizeOutside', 'distanceMin', 'distanceMax'}),
    'restrict': frozenset({'scopeToken', 'sizeInside'}),
    'curvature': frozenset({
        'scopeToken', 'surfaces', 'sampling', 'sizeInside', 'sizeOutside',
        'curvatureDelta', 'curvatureMin', 'curvatureMax'}),
    'box': frozenset({'boxMin', 'boxMax', 'sizeInside', 'sizeOutside'}),
    'ball': frozenset({
        'centre', 'radius', 'thickness', 'sizeInside', 'sizeOutside'}),
    'cylinder': frozenset({
        'centre', 'axis', 'radius', 'sizeInside', 'sizeOutside'}),
    'frustum': frozenset({
        'centre', 'axis', 'radius', 'radiusEnd', 'sizeInside', 'sizeOutside'}),
    'math_eval': frozenset({'expression'}),
})

#: Parameters the runner computes rather than the user authoring them. They
#: belong in :data:`FIELD_PARAMETERS` because the runner reads them, and in no
#: form, because there is no control to show.
FIELD_PARAMETERS_DERIVED = frozenset({'surfaces'})


def _editor_key(parameter: str) -> str:
    """``sizeInside`` -> ``size_inside``: the spelling the editor uses."""
    out = []
    for character in str(parameter):
        if character.isupper():
            out.append('_')
            out.append(character.lower())
        else:
            out.append(character)
    return ''.join(out)


def editor_keys_for_field_type(field_type) -> frozenset:
    """The editor fields one size-field type actually uses.

    Vectors expand to their components, because the form draws ``centre`` as
    three boxes named ``centre.x``, ``centre.y`` and ``centre.z``. Derived
    parameters are dropped: the runner reads them and nobody authors them.
    """
    token = str(getattr(field_type, 'value', field_type) or '').split('.')[-1]
    keys = set()
    for parameter in FIELD_PARAMETERS.get(token, frozenset()):
        if parameter in FIELD_PARAMETERS_DERIVED:
            continue
        key = _editor_key(parameter)
        if parameter in FIELD_VECTOR_PARAMETERS:
            keys.update(f'{key}.{axis}' for axis in 'xyz')
        else:
            keys.add(key)
    return frozenset(keys)


def control(path: str) -> GmshControl:
    try:
        return CONTROLS[path]
    except KeyError as error:
        raise KeyError(f'no Gmsh control is registered for {path!r}') from error


def native_name(path: str) -> str:
    return CONTROLS[path].native_name


def paths_for(consumer: str) -> tuple[str, ...]:
    return tuple(sorted(
        path for path, item in CONTROLS.items() if item.consumer == consumer))


def schema_paths() -> frozenset[str]:
    """Every registered path, for comparison against the schema itself."""
    return frozenset(CONTROLS)
