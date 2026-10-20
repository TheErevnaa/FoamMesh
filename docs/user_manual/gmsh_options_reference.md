# Gmsh options reference

Generated from the option catalogue by `scripts/generate_gmsh_help_docs.py`; do not edit by hand. Every option of Gmsh 4.15.2 that FoamMesh sets is listed on the page whose Advanced section shows it, with the default, the unit and Gmsh's own help text. A value you type is sent to Gmsh as typed, and the run ledger shows the value Gmsh applied.

| Page | Options |
|---|---|
| Geometry | 67 |
| Global sizing | 39 |
| Structure and volume controls | 18 |
| Layers | 2 |
| Periodic | 3 |
| Generate mesh | 50 |
| Mesh quality | 9 |
| Export | 82 |
| Display | 117 |
| General | 332 |

## Geometry

Advanced section of the Geometry row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Geometry.AutoCoherence` | 1.0 |  |  | Should all duplicate entities be automatically removed with the built-in geometry kernel? If Geometry.AutoCoherence = 2, also remove degenerate entities. The option has no effect with the OpenCASCADE kernel |
| `Geometry.CopyMeshingMethod` | off |  |  | Copy meshing method (unstructured or transfinite) when duplicating geometrical entities with built-in geometry kernel? |
| `Geometry.ExactExtrusion` | on |  |  | Use exact extrusion formula in interpolations (set to 0 to allow geometrical transformations of extruded entities) |
| `Geometry.ExtrudeReturnLateralEntities` | on |  |  | Add lateral entities in lists returned by extrusion commands? |
| `Geometry.ExtrudeSplinePoints` | 5.0 |  |  | Number of control points for splines created during extrusion |
| `Geometry.FirstEntityTag` | 1.0 |  |  | First tag (>= 1) of entities when creating a model |
| `Geometry.FirstPhysicalTag` | 1.0 |  |  | First tag (>= 1) of physicall groups when creating a model |
| `Geometry.MatchGeomAndMesh` | 0.0 |  |  | Matches geometries and meshes |
| `Geometry.MatchMeshScaleFactor` | 1.0 |  |  | Rescaling factor for the mesh to correspond to size of the geometry |
| `Geometry.MatchMeshTolerance` | 1e-06 |  |  | Tolerance for matching mesh and geometry |
| `Geometry.NumSubEdges` | 100.0 |  |  | Number of subdivisions used to draw curves |
| `Geometry.OCCAutoEmbed` | on |  |  | Automatically embed points, curves and faces in higher dimensional entities if they are marked as 'internal' by OpenCASCADE |
| `Geometry.OCCAutoFix` | on |  |  | Automatically fix orientation of wires, faces, shells and volumes when creating new entities with the OpenCASCADE kernel |
| `Geometry.OCCBooleanCheckInverted` | on |  |  | Check input solid in boolean operations for inverted status? |
| `Geometry.OCCBooleanGlue` | 0.0 |  | (0, 'off'), (1, 'allow shapes with partial overlaps'), (2, 'only allow shapes with full coincidence') | Try to speed up boolean operation by speeding up the interference between shapes that do overlap but do not have real intersections (0: off, 1: allow shapes with partial overlaps, 2: only allow shapes with full coincidence) |
| `Geometry.OCCBooleanNonDestructive` | off |  |  | Do not modify the argument shapes during boolean operations? |
| `Geometry.OCCBooleanPreserveNumbering` | 1.0 |  |  | Try to preserve the numbering of entities through OpenCASCADE boolean operations |
| `Geometry.OCCBooleanSimplify` | 1.0 |  |  | Try to simplify the shape resulting from boolean operations (0: none, 1: only for unions, 2: for all boolean operations |
| `Geometry.OCCBoundsUseStl` | off |  |  | Use STL mesh for computing bounds of OpenCASCADE shapes (more accurate, but slower) |
| `Geometry.OCCBrepFormatVersion` | 1.0 |  |  | Version of BREP format used when saving BREP and XAO files (0: current, 1, 2, 3: currently supported versions) |
| `Geometry.OCCDisableStl` | off |  |  | Disable STL creation in OpenCASCADE kernel |
| `Geometry.OCCExportOnlyVisible` | 0.0 |  |  | Only consider visible shapes when exporting STEP or BREP models with the OpenCASCADE kernel |
| `Geometry.OCCFastUnbind` | on |  |  | Use fast (i.e. without recursive checks on boundaries) unbinding of entities in geometrical transformations (1), as well as in boolean operations (2) |
| `Geometry.OCCFixDegenerated` | 0.0 |  |  | Fix degenerated edges/faces when importing STEP, IGES and BRep models with the OpenCASCADE kernel |
| `Geometry.OCCFixSmallEdges` | 0.0 |  |  | Fix small edges when importing STEP, IGES and BRep models with the OpenCASCADE kernel |
| `Geometry.OCCFixSmallFaces` | 0.0 |  |  | Fix small faces when importing STEP, IGES and BRep models with the OpenCASCADE kernel |
| `Geometry.OCCImportLabels` | 1.0 |  | (0, 'no'), (1, 'yes'), (2, 'use slow workaround for bad shape locations') | Import labels and colors when importing STEP models with the OpenCASCADE kernel (0: no, 1: yes, 2: use slow workaround for bad shape locations) |
| `Geometry.OCCMakeSolids` | 0.0 |  |  | Fix shells and make solids when importing STEP, IGES and BRep models with the OpenCASCADE kernel |
| `Geometry.OCCParallel` | off |  |  | Use multi-threaded OpenCASCADE boolean operators |
| `Geometry.OCCSTEPAuthor` |  |  |  | Author of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPAuthorization` |  |  |  | Authorization of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPDescription` |  |  |  | Description of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPImplementationLevel` |  |  |  | Implementation level of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPModelName` |  |  |  | Model name of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPOrganization` |  |  |  | Organization of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPOriginatingSystem` |  |  |  | Originating system of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPPreprocessorVersion` |  |  |  | Preprocessor version of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPSchemaIdentifier` |  |  |  | Schema identifier of the STEP file to be written by OpenCASCADE. If left empty, the default value is set by OpenCASCADE |
| `Geometry.OCCSTEPTimeStamp` |  |  |  | Time stamp for the creation of the STEP file. If left empty, the default value is current time. |
| `Geometry.OCCScaling` | on |  |  | Scale STEP, IGES and BRep models by the given factor when importing them with the OpenCASCADE kernel |
| `Geometry.OCCSewFaces` | 0.0 |  |  | Sew faces when importing STEP, IGES and BRep models with the OpenCASCADE kernel |
| `Geometry.OCCTargetUnit` |  |  |  | Length unit to which coordinates from STEP and IGES files are converted to when imported by OpenCASCADE, e.g. 'M' for meters (leave empty to use the default OpenCASCADE behavior); the option should be set before importing the STEP or IGES file |
| `Geometry.OCCThruSectionsDegree` | -1.0 |  |  | Maximum degree of surfaces generated by thrusections with the OpenCASCADE kernel, if not explicitly specified (default OCC value if negative) |
| `Geometry.OCCUseGenericClosestPoint` | off |  |  | Use generic algrithm to compute point projections in the OpenCASCADE kernel (less robust, but significally faster in some configurations) |
| `Geometry.OldCircle` | off |  |  | Use old circle description (compatibility option for old Gmsh geometries) |
| `Geometry.OldNewReg` | on |  |  | Use old newreg definition for geometrical transformations (compatibility option for old Gmsh geometries) |
| `Geometry.OldRuledSurface` | off |  |  | Use old 3-sided ruled surface interpolation (compatibility option for old Gmsh geometries) |
| `Geometry.OrientedPhysicals` | on |  |  | Use sign of elementary entity in physical definition as orientation indicator |
| `Geometry.PipeDefaultTrihedron` | DiscreteTrihedron |  |  | Default trihedron type when creating pipes |
| `Geometry.ReparamOnFaceRobust` | off |  |  | Use projection for reparametrization of a point classified on GEdge on a GFace |
| `Geometry.ScalingFactor` | 1.0 |  |  | Global geometry scaling factor |
| `Geometry.SnapPoints` | 1.0 |  |  | Snap points on curves if their evaluation using the parametrization is larger than the geometrical tolerance (currently only with the OpenCASCADE kernel) |
| `Geometry.SnapX` | 0.1 |  |  | Snapping grid spacing along the X-axis |
| `Geometry.SnapY` | 0.1 |  |  | Snapping grid spacing along the Y-axis |
| `Geometry.SnapZ` | 0.1 |  |  | Snapping grid spacing along the Z-axis |
| `Geometry.Tolerance` | 1e-08 | length |  | Geometrical tolerance |
| `Geometry.ToleranceBoolean` | 0.0 | length |  | Geometrical tolerance for boolean operations |
| `Geometry.Transform` | 0.0 |  | (0, 'no'), (1, 'scale') | Transform model display coordinates (0: no, 1: scale) |
| `Geometry.TransformXX` | 1.0 |  |  | Element (1,1) of the 3×3 model display transformation matrix |
| `Geometry.TransformXY` | 0.0 |  |  | Element (1,2) of the 3×3 model display transformation matrix |
| `Geometry.TransformXZ` | 0.0 |  |  | Element (1,3) of the 3×3 model display transformation matrix |
| `Geometry.TransformYX` | 0.0 |  |  | Element (2,1) of the 3×3 model display transformation matrix |
| `Geometry.TransformYY` | 1.0 |  |  | Element (2,2) of the 3×3 model display transformation matrix |
| `Geometry.TransformYZ` | 0.0 |  |  | Element (2,3) of the 3×3 model display transformation matrix |
| `Geometry.TransformZX` | 0.0 |  |  | Element (3,1) of the 3×3 model display transformation matrix |
| `Geometry.TransformZY` | 0.0 |  |  | Element (3,2) of the 3×3 model display transformation matrix |
| `Geometry.TransformZZ` | 1.0 |  |  | Element (3,3) of the 3×3 model display transformation matrix |

## Global sizing

Advanced section of the Global sizing row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Mesh.Algorithm` | 6.0 |  | (1, 'MeshAdapt'), (2, 'Automatic'), (3, 'Initial mesh only'), (5, 'Delaunay'), (6, 'Frontal-Delaunay'), (7, 'BAMG'), (8, 'Frontal-Delaunay for Quads'), (9, 'Packing of Parallelograms'), (11, 'Quasi-structured Quad') | 2D mesh algorithm (1: MeshAdapt, 2: Automatic, 3: Initial mesh only, 5: Delaunay, 6: Frontal-Delaunay, 7: BAMG, 8: Frontal-Delaunay for Quads, 9: Packing of Parallelograms, 11: Quasi-structured Quad) |
| `Mesh.Algorithm3D` | 1.0 |  | (1, 'Delaunay'), (3, 'Initial mesh only'), (4, 'Frontal'), (7, 'MMG3D'), (9, 'R-tree'), (10, 'HXT') | 3D mesh algorithm (1: Delaunay, 3: Initial mesh only, 4: Frontal, 7: MMG3D, 9: R-tree, 10: HXT) |
| `Mesh.AlgorithmSwitchOnFailure` | 1.0 |  |  | Switch meshing algorithm on failure? (Currently only for 2D Delaunay-based algorithms, switching to MeshAdapt) |
| `Mesh.AllowSwapAngle` | 10.0 | deg |  | Threshold angle (in degrees) between faces normals under which we allow an edge swap |
| `Mesh.AngleSmoothNormals` | 30.0 |  |  | Threshold angle below which normals are not smoothed |
| `Mesh.AngleToleranceFacetOverlap` | 0.1 | deg |  | Consider connected facets as overlapping when the dihedral angle between the facets is smaller than the user's defined tolerance (in degrees) |
| `Mesh.CrossFieldClosestPoint` | on |  |  | Use closest point to compute 2D crossfield |
| `Mesh.IgnoreParametrization` | 0.0 |  |  | Skip parametrization section when reading meshes in the MSH4 format |
| `Mesh.LcIntegrationPrecision` | 1e-09 |  |  | Accuracy of evaluation of the LC field for 1D mesh generation |
| `Mesh.MaxIterDelaunay3D` | 0.0 |  |  | Maximum number of point insertion iterations in 3D Delaunay mesher (0: unlimited) |
| `Mesh.MaxNumThreads1D` | 0.0 |  |  | Maximum number of threads for 1D meshing (0: use General.NumThreads) |
| `Mesh.MaxNumThreads2D` | 0.0 |  |  | Maximum number of threads for 2D meshing (0: use General.NumThreads) |
| `Mesh.MaxNumThreads3D` | 0.0 |  |  | Maximum number of threads for 3D meshing (0: use General.NumThreads) |
| `Mesh.MaxRetries` | 10.0 |  |  | Maximum number of times meshing is retried on curves and surfaces with a pending mesh; also controls maximum number of recursive subdivisions when self-intersecting 1D meshes are detected |
| `Mesh.MeshSizeExtendFromBoundary` | on |  |  | Extend computation of mesh element sizes from the boundaries into the interior (0: never; 1: for surfaces and volumes; 2: for surfaces and volumes, but use smallest surface element edge length instead of longest length in 3D Delaunay; -2: only for surfaces; -3: only for volumes) |
| `Mesh.MeshSizeFactor` | 1.0 |  |  | Factor applied to all mesh element sizes |
| `Mesh.MeshSizeFromCurvature` | off | rad |  | Automatically compute mesh element sizes from curvature, using the value as the target number of elements per 2 * Pi radians |
| `Mesh.MeshSizeFromCurvatureIsotropic` | off |  |  | Force isotropic curvature estimation when the mesh size is computed from curvature |
| `Mesh.MeshSizeFromParametricPoints` | off |  |  | Compute mesh element sizes from values given at geometry points defining parametric curves |
| `Mesh.MeshSizeFromPoints` | on |  |  | Compute mesh element sizes from values given at geometry points |
| `Mesh.MeshSizeMax` | 1e+22 | length |  | Maximum mesh element size |
| `Mesh.MeshSizeMin` | 0.0 | length |  | Minimum mesh element size |
| `Mesh.MinCircleNodes` | 7.0 |  |  | Minimum number of nodes used to mesh circles and ellipses |
| `Mesh.MinCurveNodes` | 3.0 |  |  | Minimum number of nodes used to mesh curves other than lines, circles and ellipses |
| `Mesh.MinLineNodes` | 2.0 |  |  | Minimum number of nodes used to mesh (straight) lines |
| `Mesh.NewtonConvergenceTestXYZ` | off |  |  | Force inverse surface mapping algorithm (Newton-Raphson) to converge in real coordinates (experimental) |
| `Mesh.OldInitialDelaunay2D` | off |  |  | Use old initial 2D Delaunay code |
| `Mesh.RandomFactor` | 1e-09 |  |  | Random factor used in the 2D meshing algorithm (should be increased if RandomFactor * size(triangle)/size(model) approaches machine accuracy) |
| `Mesh.RandomFactor3D` | 1e-12 |  |  | Random factor used in the 3D meshing algorithm |
| `Mesh.RandomSeed` | 1.0 |  |  | Seed of pseudo-random number generator |
| `Mesh.ReparamMaxTriangles` | 250000.0 |  |  | Maximum number of triangles in a single parametrization patch |
| `Mesh.Reproducible` | 0.0 |  |  | Try to produce reproducible meshes even when multi-threaded (experimental) |
| `Mesh.SmoothCrossField` | off |  |  | Apply n barycentric smoothing passes to the 3D cross field |
| `Mesh.SmoothRatio` | 1.8 |  |  | Ratio between mesh sizes at nodes of a same edge (used in BAMG) |
| `Mesh.StlOneSolidPerSurface` | 0.0 |  | (0, 'single solid'), (1, 'one solid per face'), (2, 'one solid per physical surface') | Create one solid per surface when exporting STL files? (0: single solid, 1: one solid per face, 2: one solid per physical surface) |
| `Mesh.ToleranceEdgeLength` | 0.0 | length |  | Skip a model edge in mesh generation if its length is less than user's defined tolerance |
| `Mesh.ToleranceInitialDelaunay` | 1e-12 |  |  | Tolerance for initial 3D Delaunay mesher |
| `Mesh.ToleranceReferenceElement` | 1e-06 |  |  | Tolerance for classifying a point inside a reference element (of size 1) |
| `Mesh.Voronoi` | off |  |  | Display the voronoi diagram |

## Structure and volume controls

Advanced section of the Structure row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Mesh.CompoundClassify` | 1.0 |  | (0, 'on the new discrete surface'), (1, 'on the original geometrical surfaces — incompatible with e.g. high-order meshing') | How are surface mesh elements classified on compounds? (0: on the new discrete surface, 1: on the original geometrical surfaces — incompatible with e.g. high-order meshing) |
| `Mesh.CompoundMeshSizeFactor` | 0.5 |  |  | Mesh size factor applied to compound parts |
| `Mesh.FlexibleTransfinite` | off |  |  | Allow transfinite constraints to be modified for recombination (e.g. Blossom) or by global mesh size factor |
| `Mesh.QuadqsRemeshingBoldness` | 0.66 |  |  | Controls how much cavity remeshing is allowed to distort the quad mesh. From 0 (no quality decrease during remeshing) to 1 (quality can tend to 0 during remeshing). |
| `Mesh.QuadqsScalingOnTriangulation` | 0.75 |  |  | Ratio on the edge length between the triangulation and the quadrangulation. Use a small ratio (e.g. 0.5) to get a background triangulation finer than the quad mesh. Useful to get a more accurate cross-field. |
| `Mesh.QuadqsSizemapMethod` | 3.0 |  |  | Size map method in QuadQuasiStructured. 0: default, 1: cross-field,2: cross-field + CAD small features adaptation,3: from background mesh (e.g. sizes in current triangulation),4: cross-field + CAD small features adaptation (clamped by background mesh) |
| `Mesh.QuadqsTopologyOptimizationMethods` | 0.0 |  |  | Topology optimization methods in QuadQuasiStructured. 0: default (all),100: pattern-based CAD faces,010: disk quadrangulation remeshing,001: cavity remeshing,xxx: combination of multiple methods (e.g. 111 for all) |
| `Mesh.QuasiTransfinite` | off |  |  | Allow quasi-transfinite meshing of surfaces with non-matching number of nodes on opposite sides |
| `Mesh.RecombinationAlgorithm` | 1.0 |  | (0, 'simple'), (1, 'blossom'), (2, 'simple full-quad'), (3, 'blossom full-quad') | Mesh recombination algorithm (0: simple, 1: blossom, 2: simple full-quad, 3: blossom full-quad) |
| `Mesh.Recombine3DAll` | off |  |  | Apply recombination3D algorithm to all volumes, ignoring per-volume spec (experimental) |
| `Mesh.Recombine3DConformity` | 0.0 |  | (0, 'nonconforming'), (1, 'trihedra'), (2, 'pyramids+trihedra'), (3, 'pyramids+hexSplit+trihedra'), (4, 'hexSplit+trihedra') | 3d recombination conformity type (0: nonconforming, 1: trihedra, 2: pyramids+trihedra, 3:pyramids+hexSplit+trihedra, 4:hexSplit+trihedra)(experimental) |
| `Mesh.Recombine3DLevel` | 0.0 |  | (0, 'hex'), (1, 'hex+prisms'), (2, 'hex+prism+pyramids') | 3d recombination level (0: hex, 1: hex+prisms, 2: hex+prism+pyramids) (experimental) |
| `Mesh.RecombineAll` | off |  |  | Apply recombination algorithm to all surfaces, ignoring per-surface spec |
| `Mesh.RecombineMinimumQuality` | 0.01 |  |  | Minimum quality for quadrangle generated by recombination |
| `Mesh.RecombineNodeRepositioning` | on |  |  | Allow repositioning of nodes during recombination of surface meshes |
| `Mesh.RecombineOptimizeTopology` | 5.0 |  |  | Number of topological optimization passes (removal of diamonds, ...) of recombined surface meshes |
| `Mesh.SubdivisionAlgorithm` | 0.0 |  | (0, 'none'), (1, 'all quadrangles'), (2, 'all hexahedra'), (3, 'barycentric') | Mesh subdivision algorithm (0: none, 1: all quadrangles, 2: all hexahedra, 3: barycentric) |
| `Mesh.TransfiniteTri` | off |  |  | Use alternative transfinite arrangement when meshing 3-sided surfaces |

## Layers

Advanced section of the Layers row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Mesh.AnisoMax` | 1e+33 |  |  | Maximum anisotropy of the mesh |
| `Mesh.BoundaryLayerFanElements` | 5.0 | rad |  | Number of elements (per Pi radians) for 2D boundary layer fans |

## Periodic

Advanced section of the Periodic row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Mesh.HighOrderPeriodic` | off |  |  | Force location of nodes for periodic meshes using periodicity transform (0: assume identical parametrisations, 1: invert parametrisations, 2: compute closest point |
| `Mesh.HighOrderSavePeriodic` | off |  |  | Save high-order nodes in periodic section of MSH files? |
| `Mesh.IgnorePeriodicity` | 1.0 |  |  | Skip periodic node section and skip periodic boundary alignment step when reading meshes in the MSH2 format |

## Generate mesh

Advanced section of the Generate row; the Options panel there lists every option and searches them.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Mesh.ElementOrder` | 1.0 |  |  | Element order (1: first order elements) |
| `Mesh.HighOrderCurveOuterBL` | 0.0 |  |  | Curve also the outer surface of the boundary layer in the fast curving algorithm (0 = do not curve, 1 = curve according to boundary, 2 = curve without breaking outer elements) |
| `Mesh.HighOrderDistCAD` | off |  |  | Try to optimize distance to CAD in high-order optimizer? |
| `Mesh.HighOrderFastCurvingNewAlgo` | 0.0 |  |  | Curve boundary layer with new "fast curving" algorithm (experimental) |
| `Mesh.HighOrderFixBoundaryNodes` | off |  |  | Fix all (1) or periodic (2) boundary nodes during high-order optimization? |
| `Mesh.HighOrderIterMax` | 100.0 |  |  | Maximum number of iterations in high-order optimization pass |
| `Mesh.HighOrderMaxAngle` | 0.1745329277777778 |  |  | Maximum angle between layers of BL elements for the detection of columns in the fast curving algorithm |
| `Mesh.HighOrderMaxInnerAngle` | 0.5235987833333333 |  |  | Maximum angle between edges/faces within layers of BL triangles/tets for the detection of columns in the fast curving algorithm |
| `Mesh.HighOrderMaxRho` | 0.3 |  |  | Maximum min/max ratio of edge/face size for the detection of BL element columns in the fast curving algorithm |
| `Mesh.HighOrderNumLayers` | 6.0 |  |  | Number of layers around a problematic element to consider for high-order optimization, or number of element layers to consider in the boundary layer mesh for high-order fast curving |
| `Mesh.HighOrderOptimize` | 0.0 |  | (0, 'none'), (1, 'optimization'), (2, 'elastic+optimization'), (3, 'elastic'), (4, 'fast curving') | Optimize high-order meshes? (0: none, 1: optimization, 2: elastic+optimization, 3: elastic, 4: fast curving) |
| `Mesh.HighOrderPassMax` | 25.0 |  |  | Maximum number of high-order optimization passes (moving barrier) |
| `Mesh.HighOrderPoissonRatio` | 0.33 |  |  | Poisson ratio of the material used in the elastic smoother for high-order meshes (between -1.0 and 0.5, excluded) |
| `Mesh.HighOrderPrimSurfMesh` | off |  |  | Try to fix flipped surface mesh elements in high-order optimizer? |
| `Mesh.HighOrderSkipQualityCheck` | 0.0 |  |  | Skip element quality check after high-order mesh generation |
| `Mesh.HighOrderThresholdMax` | 2.0 |  |  | Maximum threshold for high-order element optimization |
| `Mesh.HighOrderThresholdMin` | 0.1 |  |  | Minimum threshold for high-order element optimization |
| `Mesh.MeshOnlyEmpty` | 0.0 |  |  | Mesh only entities that have no existing mesh |
| `Mesh.MeshOnlyVisible` | 0.0 |  |  | Mesh only visible entities (experimental) |
| `Mesh.MetisAlgorithm` | 1.0 |  | (1, 'Recursive'), (2, 'K-way') | METIS partitioning algorithm 'ptype' (1: Recursive, 2: K-way) |
| `Mesh.MetisEdgeMatching` | 2.0 |  | (1, 'Random'), (2, 'Sorted Heavy-Edge') | METIS edge matching type 'ctype' (1: Random, 2: Sorted Heavy-Edge) |
| `Mesh.MetisMaxLoadImbalance` | -1.0 |  |  | METIS maximum load imbalance 'ufactor' (-1: default, i.e. 30 for K-way and 1 for Recursive) |
| `Mesh.MetisMinConn` | -1.0 |  |  | METIS minimize maximum connectivity of partitions 'minconn' (-1: default) |
| `Mesh.MetisObjective` | 1.0 |  | (1, 'min. edge-cut'), (2, 'min. communication volume') | METIS objective type 'objtype' (1: min. edge-cut, 2: min. communication volume) |
| `Mesh.MetisRefinementAlgorithm` | 2.0 |  | (1, 'FM-based cut'), (2, 'Greedy'), (3, 'Two-sided node FM'), (4, 'One-sided node FM') | METIS algorithm for k-way refinement 'rtype' (1: FM-based cut, 2: Greedy, 3: Two-sided node FM, 4: One-sided node FM) |
| `Mesh.NbPartitions` | 0.0 |  |  | Number of partitions |
| `Mesh.Optimize` | 1.0 |  |  | Optimize the mesh to improve the quality of tetrahedral elements |
| `Mesh.OptimizeNetgen` | 0.0 |  |  | Optimize the mesh using Netgen to improve the quality of tetrahedral elements |
| `Mesh.OptimizePyramids` | 0.0 |  |  | Optimize pyramids in hybrid 3D meshes (0: smoother; 1: untangler) |
| `Mesh.OptimizeThreshold` | 0.3 |  |  | Optimize tetrahedra that have a quality below ... |
| `Mesh.PartitionConvertMsh2` | 1.0 |  |  | When reading partitioned meshes in MSH2 format, create new partition entities |
| `Mesh.PartitionCreateGhostCells` | 0.0 |  |  | Create ghost cells, i.e. create for each partition a ghost entity containing elements connected to neighboring partitions by at least one node. |
| `Mesh.PartitionCreatePhysicals` | 1.0 |  |  | Create physical groups for partitions, based on existing physical groups |
| `Mesh.PartitionCreateTopology` | 1.0 |  |  | Create boundary representation of partitions |
| `Mesh.PartitionHexWeight` | -1.0 |  |  | Weight of hexahedral element for METIS load balancing (-1: automatic) |
| `Mesh.PartitionLineWeight` | -1.0 |  |  | Weight of line element for METIS load balancing (-1: automatic) |
| `Mesh.PartitionOldStyleMsh2` | on |  |  | Write partitioned meshes in MSH2 format using old style (i.e. by not referencing new partitioned entities, except on partition boundaries), for backward compatibility |
| `Mesh.PartitionPrismWeight` | -1.0 |  |  | Weight of prismatic element (wedge) for METIS load balancing (-1: automatic) |
| `Mesh.PartitionPyramidWeight` | -1.0 |  |  | Weight of pyramidal element for METIS load balancing (-1: automatic) |
| `Mesh.PartitionQuadWeight` | -1.0 |  |  | Weight of quadrangle for METIS load balancing (-1: automatic) |
| `Mesh.PartitionSplitMeshFiles` | off |  |  | Write one file for each mesh partition |
| `Mesh.PartitionTetWeight` | -1.0 |  |  | Weight of tetrahedral element for METIS load balancing (-1: automatic) |
| `Mesh.PartitionTopologyFile` | off |  |  | Write a .pro file with the partition topology |
| `Mesh.PartitionTriWeight` | -1.0 |  |  | Weight of triangle element for METIS load balancing (-1: automatic) |
| `Mesh.PartitionTrihedronWeight` | 0.0 |  |  | Weight of trihedron element for METIS load balancing (-1: automatic) |
| `Mesh.RefineSteps` | 10.0 |  |  | Number of refinement steps in the MeshAdapt-based 2D algorithms |
| `Mesh.Renumber` | 1.0 |  |  | Renumber nodes and elements in a continuous sequence after mesh generation |
| `Mesh.SecondOrderIncomplete` | 0.0 |  |  | Create incomplete second order elements? (8-node quads, 20-node hexas, etc.) |
| `Mesh.SecondOrderLinear` | off |  |  | Should second order nodes (as well as nodes generated with subdivision algorithms) simply be created by linear interpolation? |
| `Mesh.Smoothing` | 1.0 |  |  | Number of smoothing steps applied to the final mesh |

## Mesh quality

Advanced section of the Quality row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Mesh.AvgQuality` | 0.0 |  |  | Average mesh quality (inverse conditioning number, ICN) after the generation of the current mesh (read-only) (read only) |
| `Mesh.CheckSurfaceNormalValidity` | on |  |  | Check surface mesh validity according to the geometry normal |
| `Mesh.MinQuality` | 0.0 |  |  | Minimum mesh quality (inverse conditioning number, ICN) after the generation of the current mesh (read-only) (read only) |
| `Mesh.QualityInf` | 0.0 |  |  | Only display elements whose quality measure is greater than QualityInf |
| `Mesh.QualitySup` | 0.0 |  |  | Only display elements whose quality measure is smaller than QualitySup |
| `Mesh.QualityType` | 2.0 |  |  | Type of quality measure (0: SICN~signed inverse condition number, 1: SIGE~signed inverse gradient error, 2: gamma~vol/sum_face/max_edge, 3: Disto~minJ/maxJ |
| `Mesh.RadiusInf` | 0.0 |  |  | Only display elements whose longest edge is greater than RadiusInf |
| `Mesh.RadiusSup` | 0.0 |  |  | Only display elements whose longest edge is smaller than RadiusSup |
| `Mesh.ScalingFactor` | 1.0 |  |  | Global scaling factor applied to the saved mesh |

## Export

Advanced section of the Export row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Mesh.BdfFieldFormat` | 1.0 |  | (0, 'free'), (1, 'small'), (2, 'large') | Field format for Nastran BDF files (0: free, 1: small, 2: large) |
| `Mesh.Binary` | off |  |  | Write mesh files in binary format (if possible) |
| `Mesh.CgnsConstructTopology` | 0.0 |  |  | Reconstruct the model topology (BREP) after reading a CGNS file |
| `Mesh.CgnsExportCPEX0045` | off |  |  | Use the CPEX0045 convention when exporting a high-order mesh to CGNS |
| `Mesh.CgnsExportStructured` | 0.0 |  |  | Export transfinite meshes as structured CGNS grids |
| `Mesh.CgnsImportIgnoreBC` | off |  |  | Ignore information in ZoneBC structures when reading a CGNS file |
| `Mesh.CgnsImportIgnoreSolution` | off |  |  | Ignore solution when reading a CGNS file |
| `Mesh.CgnsImportOrder` | 1.0 |  |  | Order of the mesh to be created by coarsening CGNS structured zones (1 to 4) |
| `Mesh.CreateTopologyMsh2` | 0.0 |  |  | Attempt to (re)create the model topology when reading MSH2 files |
| `Mesh.FirstElementTag` | 1.0 |  |  | First tag (>= 1) of mesh elements when generating or renumbering a mesh |
| `Mesh.FirstNodeTag` | 1.0 |  |  | First tag (>= 1) of mesh nodes when generating or renumbering a mesh |
| `Mesh.Format` | 10.0 |  | (1, 'msh'), (2, 'unv'), (10, 'auto'), (16, 'vtk'), (19, 'vrml'), (21, 'mail'), (26, 'pos stat'), (27, 'stl'), (28, 'p3d'), (30, 'mesh'), (31, 'bdf'), (32, 'cgns'), (33, 'med'), (34, 'diff'), (38, 'ir3'), (39, 'inp'), (40, 'ply2'), (41, 'celum'), (42, 'su2'), (47, 'tochnog'), (49, 'neu'), (50, 'matlab') | Mesh output format (1: msh, 2: unv, 10: auto, 16: vtk, 19: vrml, 21: mail, 26: pos stat, 27: stl, 28: p3d, 30: mesh, 31: bdf, 32: cgns, 33: med, 34: diff, 38: ir3, 39: inp, 40: ply2, 41: celum, 42: su2, 47: tochnog, 49: neu, 50: matlab) |
| `Mesh.IgnoreUnknownSections` | 0.0 |  |  | Skip unknown sections when reading meshes in the MSH4 format (otherwise the contents of these sections are stored as model attributes) |
| `Mesh.MedFileMinorVersion` | -1.0 |  |  | Minor version of the MED file format to use (-1: use minor version of the MED library) |
| `Mesh.MedImportGroupsOfNodes` | off |  |  | Import groups of nodes (0: no; 1: create geometrical point for each node)? |
| `Mesh.MedSingleModel` | 0.0 |  |  | Import MED meshes in the current model, even if several MED mesh names exist |
| `Mesh.MshFileVersion` | 4.1 |  |  | Version of the MSH file format to use |
| `Mesh.PreserveNumberingMsh2` | 0.0 |  |  | Preserve node and element numbering in MSH2 format (will break meshes with multiple physical groups for a single elementary entity) |
| `Mesh.ReadGroupsOfElements` | 1.0 |  |  | Read groups of elements in UNV meshes (this will discard the elementary entity tags inferred from the element section) |
| `Mesh.SaveAll` | off |  |  | Save all elements, even if they don't belong to physical groups (for some mesh formats, this removes physical groups altogether) |
| `Mesh.SaveElementTagType` | 1.0 |  | (1, 'elementary'), (2, 'physical'), (3, 'partition') | Type of the element tag saved in mesh formats that don't support saving physical or partition ids (1: elementary, 2: physical, 3: partition) |
| `Mesh.SaveGroupsOfElements` | on |  |  | Save groups of elements for each physical group (for UNV and INP mesh format) if value is positive; if negative, save groups of elements for physical groups of dimension dim if the (dim+1)^th least significant digit of -Mesh.SaveGroupsOfElements is 1 (for example: -100 will only savesurfaces, while -1010 will save volumes and curves), and for INP skip saving elements of dimension dim altogether if the (dim+1)^th least significant digit of -Mesh.SaveGroupsOfElements is 0 |
| `Mesh.SaveGroupsOfNodes` | off |  |  | Save groups of nodes for each physical group (for UNV, INP and Tochnog mesh formats) if value is positive; if negative, save groups of nodes for physical groups of dimension dim if the (dim+1)^th least significant digit of -Mesh.SaveGroupsOfNodes is 1 (for example: -100 will only save surfaces, while -1010 will save volumes and curves); for INP, save groups of nodes for all entities of dimension dim if the (dim+1)^th least significant digit of -Mesh.SaveGroupsOfNodes is 2 |
| `Mesh.SaveParametric` | off |  |  | Save parametric coordinates of nodes |
| `Mesh.SaveTopology` | off |  |  | Save model topology in MSH2 output files (this is always saved in MSH3 and above) |
| `Mesh.SaveWithoutOrphans` | 0.0 |  |  | Don't save orphan entities (not connected to any highest dimensional entity in the model) in MSH4 files |
| `Mesh.StlAngularDeflection` | 0.3 |  |  | Maximum angular deflection when creating STL representations of entities (currently only used with the OpenCASCADE kernel) |
| `Mesh.StlLinearDeflection` | 0.001 |  |  | Maximum relative linear deflection when creating STL representation of entities (currently only used with the OpenCASCADE kernel) |
| `Mesh.StlLinearDeflectionRelative` | on |  |  | Compute the linear deflection for STL representations relative to the length of curves (currently only used with the OpenCASCADE kernel) |
| `Mesh.StlRemoveBadTriangles` | off |  |  | Remove bad triangles when importing STL files (1: remove duplicates; 2: remove duplicate and zero-area triangles) |
| `Mesh.SwitchElementTags` | 0.0 |  |  | Invert elementary and physical tags when reading the mesh |
| `Mesh.UnvStrictFormat` | on |  |  | Use strict format specification for UNV files, with 'D' for exponents (instead of 'E' as used by some tools) |
| `Mesh.ZoneDefinition` | 0.0 |  | (0, 'single zone'), (1, 'by partition'), (2, 'by physical') | Method for defining a zone (0: single zone, 1: by partition, 2: by physical) |
| `Print.Background` | off |  |  | Print background (gradient and image)? |
| `Print.CompositeWindows` | 0.0 |  |  | Composite all window tiles in the same output image (for bitmap output only) |
| `Print.DeleteTemporaryFiles` | 1.0 |  |  | Delete temporary files used during printing |
| `Print.EpsBestRoot` | 1.0 |  |  | Try to minimize primitive splitting in BSP tree sorted PostScript/PDF output |
| `Print.EpsCompress` | 0.0 |  |  | Compress PostScript/PDF output using zlib |
| `Print.EpsLineWidthFactor` | 1.0 |  |  | Width factor for lines in PostScript/PDF output |
| `Print.EpsOcclusionCulling` | 1.0 |  |  | Cull occluded primitives (to reduce PostScript/PDF file size) |
| `Print.EpsPS3Shading` | off |  |  | Enable PostScript Level 3 shading |
| `Print.EpsPointSizeFactor` | 1.0 |  |  | Size factor for points in PostScript/PDF output |
| `Print.EpsQuality` | 1.0 |  | (0, 'bitmap'), (1, 'vector (simple sort') | PostScript/PDF quality (0: bitmap, 1: vector (simple sort), 2: vector (accurate sort), 3: vector (unsorted) |
| `Print.Format` | 10.0 |  |  | File format (10: automatic) |
| `Print.GeoLabels` | on |  |  | Save labels in unrolled Gmsh geometries |
| `Print.GeoOnlyPhysicals` | 0.0 |  |  | Only save entities that belong to physical groups |
| `Print.GifDither` | off |  |  | Apply dithering to GIF output |
| `Print.GifInterlace` | 0.0 |  |  | Interlace GIF output |
| `Print.GifSort` | 1.0 |  |  | Sort the colormap in GIF output |
| `Print.GifTransparent` | 0.0 |  |  | Output transparent GIF image |
| `Print.Height` | -1.0 |  |  | Height of printed image; use (possibly scaled) current height if < 0 |
| `Print.JpegQuality` | 100.0 |  |  | JPEG quality (between 1 and 100) |
| `Print.JpegSmoothing` | 0.0 |  |  | JPEG smoothing (between 0 and 100) |
| `Print.Parameter` | 0.0 |  |  | Current value of the print parameter |
| `Print.ParameterCommand` | Mesh.Clip=1; View.Clip=1; General.ClipWholeElements=1; General.Clip0D=Print.Parameter; SetChanged; |  |  | Command parsed when the print parameter is changed |
| `Print.ParameterFirst` | -1.0 |  |  | First value of print parameter in loop |
| `Print.ParameterLast` | 1.0 |  |  | Last value of print parameter in loop |
| `Print.ParameterSteps` | 10.0 |  |  | Number of steps in loop over print parameter |
| `Print.PgfExportAxis` | 0.0 |  |  | Include axis in export pgf code (not in the png). Default `0` (no). |
| `Print.PgfHorizontalBar` | off |  |  | Use a horizontal color bar in the pgf output. Default `0` (no). |
| `Print.PgfTwoDim` | 1.0 |  |  | Output PGF format for two dimensions. Mostly irrelevant if `PgfExportAxis=0`. Default `1` (yes). |
| `Print.PostDisto` | off |  |  | Save Disto quality measure in mesh statistics exported as post-processing views |
| `Print.PostElement` | off |  |  | Save element tags in mesh statistics exported as post-processing views |
| `Print.PostElementary` | on |  |  | Save elementary region tags in mesh statistics exported as post-processing views |
| `Print.PostEta` | off |  |  | Save Eta quality measure in mesh statistics exported as post-processing views |
| `Print.PostGamma` | off |  |  | Save Gamma quality measure in mesh statistics exported as post-processing views |
| `Print.PostSICN` | off |  |  | Save SICN (signed inverse condition number) quality measure in mesh statistics exported as post-processing views |
| `Print.PostSIGE` | off |  |  | Save SIGE (signed inverse gradient error) quality measure in mesh statistics exported as post-processing views |
| `Print.TexAsEquation` | off |  |  | Print all TeX strings as equations |
| `Print.TexForceFontSize` | off |  |  | Force font size of TeX strings to fontsize in the graphic window |
| `Print.TexWidthInMm` | 150.0 |  |  | Width of tex graphics in mm (use 0 for the natural width inferred from the image width in pixels) |
| `Print.Text` | on |  |  | Print text strings? |
| `Print.Width` | -1.0 |  |  | Width of printed image; use (possibly scaled) current width if < 0) |
| `Print.X3dColorize` | 0.0 |  | (0, 'no'), (1, 'yes') | Apply colors to faces (0: no, 1: yes) |
| `Print.X3dCompatibility` | 0.0 |  |  | Produce highly compatible X3D output (no scale bar) |
| `Print.X3dEdges` | 0.0 |  | (0, 'no'), (1, 'yes in a single X3D object'), (2, 'one X3D object per geometrical edge'), (3, 'one X3D object perphysical edge') | Save edges in CAD X3D output (0: no, 1: yes in a single X3D object,2: one X3D object per geometrical edge, 3: one X3D object perphysical edge) |
| `Print.X3dPrecision` | 1e-09 |  |  | Precision of X3D output |
| `Print.X3dRemoveInnerBorders` | off |  |  | Remove inner borders in X3D output |
| `Print.X3dSurfaces` | 1.0 |  | (0, 'no'), (1, 'yes in a single X3D object'), (2, 'one X3D object per geometrical surface'), (3, 'one X3D object perphysical surface') | Save surfaces in CAD X3D output (0: no, 1: yes in a single X3D object,2: one X3D object per geometrical surface, 3: one X3D object perphysical surface) |
| `Print.X3dTransparency` | 0.0 |  |  | Transparency for X3D output |
| `Print.X3dVertices` | 0.0 |  | (0, 'no'), (1, 'yes') | Save vertices in CAD X3D output (0: no, 1: yes) |
| `Print.X3dVolumes` | 0.0 |  | (0, 'no'), (1, 'yes') | Save separate volumes in CAD X3D output (0: no, 1: yes) |

## Display

The Display items of the Generate row (Visibility, Statistics and the viewport options).

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `Geometry.Clip` | off |  |  | Enable clipping planes? (Plane[i]=2^i, i=0,...,5) |
| `Geometry.Color.Curves` | (0, 0, 255, 255) |  |  | Normal geometry curve color |
| `Geometry.Color.HighlightOne` | (255, 150, 0, 255) |  |  | Highlight 1 color |
| `Geometry.Color.HighlightTwo` | (255, 255, 0, 255) |  |  | Highlight 2 color |
| `Geometry.Color.HighlightZero` | (255, 0, 0, 255) |  |  | Highlight 0 color |
| `Geometry.Color.Normals` | (255, 0, 0, 255) |  |  | Normal geometry vectors color |
| `Geometry.Color.Points` | (90, 90, 90, 255) |  |  | Normal geometry point color |
| `Geometry.Color.Projection` | (0, 255, 0, 255) |  |  | Projection surface color |
| `Geometry.Color.Selection` | (255, 0, 0, 255) |  |  | Selected geometry color |
| `Geometry.Color.Surfaces` | (128, 128, 128, 255) |  |  | Normal geometry surface color |
| `Geometry.Color.Tangents` | (255, 255, 0, 255) |  |  | Tangent geometry vectors color |
| `Geometry.Color.Volumes` | (200, 200, 0, 255) |  |  | Normal geometry volume color |
| `Geometry.CurveLabels` | off |  |  | Display curve labels? |
| `Geometry.CurveSelectWidth` | 3.0 |  |  | Display width of selected curves (in pixels) |
| `Geometry.CurveType` | off |  |  | Display curves as solid color segments (0) or 3D cylinders (1) |
| `Geometry.CurveWidth` | 2.0 |  |  | Display width of lines (in pixels) |
| `Geometry.Curves` | on |  |  | Display geometry curves? |
| `Geometry.DoubleClickedCurveCommand` | ONELAB |  |  | Command parsed when double-clicking on a curve, or 'ONELAB' to edit associated ONELAB parameters |
| `Geometry.DoubleClickedEntityTag` | 0.0 |  |  | Tag of last double-clicked geometrical entity |
| `Geometry.DoubleClickedPointCommand` | ONELAB |  |  | Command parsed when double-clicking on a point, or 'ONELAB' to edit associated ONELAB parameters |
| `Geometry.DoubleClickedSurfaceCommand` | ONELAB |  |  | Command parsed when double-clicking on a surface, or 'ONELAB' to edit associated ONELAB parameters |
| `Geometry.DoubleClickedVolumeCommand` | ONELAB |  |  | Command parsed when double-clicking on a volume, or 'ONELAB' to edit associated ONELAB parameters |
| `Geometry.HighlightOrphans` | off |  |  | Highlight orphan and boundary entities? |
| `Geometry.LabelType` | 0.0 |  | (0, 'description'), (1, 'elementary entity tag'), (2, 'physical group tag'), (3, 'elementary name'), (4, 'physical name'), (5, 'coordinates') | Type of entity label (0: description, 1: elementary entity tag, 2: physical group tag, 3: elementary name, 4: physical name, 5: coordinates) |
| `Geometry.Light` | on |  |  | Enable lighting for the geometry |
| `Geometry.LightTwoSide` | 1.0 |  |  | Light both sides of surfaces (leads to slower rendering) |
| `Geometry.Normals` | off |  |  | Display size of normal vectors (in pixels) |
| `Geometry.OffsetX` | 0.0 |  |  | Model display offset along X-axis (in model coordinates) |
| `Geometry.OffsetY` | 0.0 |  |  | Model display offset along Y-axis (in model coordinates) |
| `Geometry.OffsetZ` | 0.0 |  |  | Model display offset along Z-axis (in model coordinates) |
| `Geometry.PointLabels` | off |  |  | Display points labels? |
| `Geometry.PointSelectSize` | 6.0 |  |  | Display size of selected points (in pixels) |
| `Geometry.PointSize` | 4.0 |  |  | Display size of points (in pixels) |
| `Geometry.PointType` | off |  |  | Display points as solid color dots (0) or 3D spheres (1) |
| `Geometry.Points` | on |  |  | Display geometry points? |
| `Geometry.SurfaceLabels` | off |  |  | Display surface labels? |
| `Geometry.SurfaceType` | 0.0 |  | (0, 'cross'), (1, 'wireframe'), (2, 'solid') | Surface display type (0: cross, 1: wireframe, 2: solid). Wireframe and solid are not available with the built-in geometry kernel. |
| `Geometry.Surfaces` | off |  |  | Display geometry surfaces? |
| `Geometry.Tangents` | off |  |  | Display size of tangent vectors (in pixels) |
| `Geometry.VolumeLabels` | off |  |  | Display volume labels? |
| `Geometry.VolumeType` | 0.0 |  | (0, 'sphere'), (1, 'diamond') | Volume display type (0: sphere, 1: diamond) |
| `Geometry.Volumes` | off |  |  | Display geometry volumes? |
| `Mesh.Clip` | off |  |  | Enable clipping planes? (Plane[i]=2^i, i=0,...,5) |
| `Mesh.Color.Eight` | (228, 255, 0, 255) |  |  | Color 8 in color carousel |
| `Mesh.Color.Eighteen` | (0, 255, 52, 255) |  |  | Color 18 in color carousel |
| `Mesh.Color.Eleven` | (0, 76, 255, 255) |  |  | Color 11 in color carousel |
| `Mesh.Color.Fifteen` | (84, 0, 255, 255) |  |  | Color 15 in color carousel |
| `Mesh.Color.Five` | (0, 216, 255, 255) |  |  | Color 5 in color carousel |
| `Mesh.Color.Four` | (255, 200, 0, 255) |  |  | Color 4 in color carousel |
| `Mesh.Color.Fourteen` | (108, 255, 0, 255) |  |  | Color 14 in color carousel |
| `Mesh.Color.Hexahedra` | (130, 120, 225, 255) |  |  | Mesh hexahedron color (if Mesh.ColorCarousel=0) |
| `Mesh.Color.Lines` | (0, 0, 0, 255) |  |  | Mesh line color |
| `Mesh.Color.Nine` | (0, 116, 255, 255) |  |  | Color 9 in color carousel |
| `Mesh.Color.Nineteen` | (184, 0, 255, 255) |  |  | Color 19 in color carousel |
| `Mesh.Color.Nodes` | (0, 0, 255, 255) |  |  | Mesh node color |
| `Mesh.Color.NodesSup` | (255, 0, 255, 255) |  |  | Second order mesh node color |
| `Mesh.Color.Normals` | (255, 0, 0, 255) |  |  | Normal mesh vector color |
| `Mesh.Color.One` | (0, 255, 132, 255) |  |  | Color 1 in color carousel |
| `Mesh.Color.Prisms` | (232, 210, 23, 255) |  |  | Mesh prism color (if Mesh.ColorCarousel=0) |
| `Mesh.Color.Pyramids` | (217, 113, 38, 255) |  |  | Mesh pyramid color (if Mesh.ColorCarousel=0) |
| `Mesh.Color.Quadrangles` | (130, 120, 225, 255) |  |  | Mesh quadrangle color (if Mesh.ColorCarousel=0) |
| `Mesh.Color.Seven` | (0, 176, 255, 255) |  |  | Color 7 in color carousel |
| `Mesh.Color.Seventeen` | (104, 0, 255, 255) |  |  | Color 17 in color carousel |
| `Mesh.Color.Six` | (255, 240, 0, 255) |  |  | Color 6 in color carousel |
| `Mesh.Color.Sixteen` | (68, 255, 0, 255) |  |  | Color 16 in color carousel |
| `Mesh.Color.Tangents` | (255, 255, 0, 255) |  |  | Tangent mesh vector color |
| `Mesh.Color.Ten` | (188, 255, 0, 255) |  |  | Color 10 in color carousel |
| `Mesh.Color.Tetrahedra` | (160, 150, 255, 255) |  |  | Mesh tetrahedron color (if Mesh.ColorCarousel=0) |
| `Mesh.Color.Thirteen` | (24, 0, 255, 255) |  |  | Color 13 in color carousel |
| `Mesh.Color.Three` | (0, 255, 192, 255) |  |  | Color 3 in color carousel |
| `Mesh.Color.Triangles` | (160, 150, 255, 255) |  |  | Mesh triangle color (if Mesh.ColorCarousel=0) |
| `Mesh.Color.Trihedra` | (20, 255, 0, 255) |  |  | Mesh trihedron color (if Mesh.ColorCarousel=0) |
| `Mesh.Color.Twelve` | (148, 255, 0, 255) |  |  | Color 12 in color carousel |
| `Mesh.Color.Two` | (255, 160, 0, 255) |  |  | Color 2 in color carousel |
| `Mesh.Color.Zero` | (255, 120, 0, 255) |  |  | Color 0 in color carousel |
| `Mesh.ColorCarousel` | 1.0 |  | (0, 'by element type'), (1, 'by elementary entity'), (2, 'by physical group'), (3, 'by mesh partition') | Mesh coloring (0: by element type, 1: by elementary entity, 2: by physical group, 3: by mesh partition) |
| `Mesh.CpuTime` | 0.0 |  |  | CPU time (in seconds) for the generation of the current mesh (read-only) (read only) |
| `Mesh.DrawSkinOnly` | off |  |  | Draw only the skin of 3D meshes? |
| `Mesh.Dual` | off |  |  | Display the dual mesh obtained by barycentric subdivision |
| `Mesh.Explode` | 1.0 |  |  | Element shrinking factor (between 0 and 1) |
| `Mesh.Hexahedra` | on |  |  | Display mesh hexahedra? |
| `Mesh.LabelSampling` | 1.0 |  |  | Label sampling rate (display one label every `LabelSampling' elements) |
| `Mesh.LabelType` | 0.0 |  | (0, 'node/element tag'), (1, 'elementary entity tag'), (2, 'physical entity tag'), (3, 'partition'), (4, 'coordinates') | Type of element label (0: node/element tag, 1: elementary entity tag, 2: physical entity tag, 3: partition, 4: coordinates) |
| `Mesh.Light` | on |  |  | Enable lighting for the mesh |
| `Mesh.LightLines` | 2.0 |  |  | Enable lighting for mesh edges (0: no, 1: surfaces, 2: surfaces+volumes |
| `Mesh.LightTwoSide` | 1.0 |  |  | Light both sides of surfaces (leads to slower rendering) |
| `Mesh.LineLabels` | off |  |  | Display mesh line labels? |
| `Mesh.LineWidth` | on |  |  | Display width of mesh lines (in pixels) |
| `Mesh.Lines` | off |  |  | Display mesh lines (1D elements)? |
| `Mesh.NbHexahedra` | 0.0 |  |  | Number of hexahedra in the current mesh (read-only) (read only) |
| `Mesh.NbNodes` | 0.0 |  |  | Number of nodes in the current mesh (read-only) (read only) |
| `Mesh.NbPrisms` | 0.0 |  |  | Number of prisms in the current mesh (read-only) (read only) |
| `Mesh.NbPyramids` | 0.0 |  |  | Number of pyramids in the current mesh (read-only) (read only) |
| `Mesh.NbQuadrangles` | 0.0 |  |  | Number of quadrangles in the current mesh (read-only) (read only) |
| `Mesh.NbTetrahedra` | 0.0 |  |  | Number of tetrahedra in the current mesh (read-only) (read only) |
| `Mesh.NbTriangles` | 0.0 |  |  | Number of triangles in the current mesh (read-only) (read only) |
| `Mesh.NbTrihedra` | 0.0 |  |  | Number of trihedra in the current mesh (read-only) (read only) |
| `Mesh.NodeLabels` | off |  |  | Display mesh node labels? |
| `Mesh.NodeSize` | 4.0 |  |  | Display size of mesh nodes (in pixels) |
| `Mesh.NodeType` | off |  |  | Display mesh nodes as solid color dots (0) or 3D spheres (1) |
| `Mesh.Nodes` | off |  |  | Display mesh nodes? |
| `Mesh.Normals` | off |  |  | Display size of normal vectors (in pixels) |
| `Mesh.NumSubEdges` | 2.0 |  |  | Number of edge subdivisions used to draw high-order mesh elements |
| `Mesh.Prisms` | on |  |  | Display mesh prisms? |
| `Mesh.Pyramids` | on |  |  | Display mesh pyramids? |
| `Mesh.Quadrangles` | on |  |  | Display mesh quadrangles? |
| `Mesh.SmoothNormals` | off |  |  | Smooth the mesh normals? |
| `Mesh.SurfaceEdges` | on |  |  | Display edges of surface mesh? |
| `Mesh.SurfaceFaces` | off |  |  | Display faces of surface mesh? |
| `Mesh.SurfaceLabels` | off |  |  | Display surface mesh element labels? |
| `Mesh.Tangents` | off |  |  | Display size of tangent vectors (in pixels) |
| `Mesh.Tetrahedra` | on |  |  | Display mesh tetrahedra? |
| `Mesh.Triangles` | on |  |  | Display mesh triangles? |
| `Mesh.Trihedra` | on |  |  | Display mesh trihedra? |
| `Mesh.VolumeEdges` | on |  |  | Display edges of volume mesh? |
| `Mesh.VolumeFaces` | off |  |  | Display faces of volume mesh? |
| `Mesh.VolumeLabels` | off |  |  | Display volume mesh element labels? |

## General

The General tab of the Options panel on the Generate row.

| Option | Default | Unit | Choices | Help |
|---|---|---|---|---|
| `General.AbortOnError` | 2.0 |  | (0, 'no'), (1, 'abort meshing'), (2, 'throw an exception unless in interactive mode'), (3, 'throw an exception always'), (4, 'exit') | Abort on error? (0: no, 1: abort meshing, 2: throw an exception unless in interactive mode, 3: throw an exception always, 4: exit) |
| `General.AlphaBlending` | on |  |  | Enable alpha blending (transparency) in post-processing views |
| `General.Antialiasing` | off |  |  | Use multisample antialiasing (will slow down rendering) |
| `General.ArrowHeadRadius` | 0.12 |  |  | Relative radius of arrow head |
| `General.ArrowStemLength` | 0.56 |  |  | Relative length of arrow stem |
| `General.ArrowStemRadius` | 0.02 |  |  | Relative radius of arrow stem |
| `General.Axes` | 0.0 |  | (0, 'none'), (1, 'simple axes'), (2, 'box'), (3, 'full grid'), (4, 'open grid'), (5, 'ruler') | Axes (0: none, 1: simple axes, 2: box, 3: full grid, 4: open grid, 5: ruler) |
| `General.AxesAutoPosition` | 1.0 |  |  | Position the axes automatically |
| `General.AxesForceValue` | off |  |  | Force values on axes (otherwise use natural coordinates) |
| `General.AxesFormatX` | %.3g |  |  | Number format for X-axis (in standard C form) |
| `General.AxesFormatY` | %.3g |  |  | Number format for Y-axis (in standard C form) |
| `General.AxesFormatZ` | %.3g |  |  | Number format for Z-axis (in standard C form) |
| `General.AxesLabelX` |  |  |  | X-axis label |
| `General.AxesLabelY` |  |  |  | Y-axis label |
| `General.AxesLabelZ` |  |  |  | Z-axis label |
| `General.AxesMaxX` | 1.0 |  |  | Maximum X-axis coordinate |
| `General.AxesMaxY` | 1.0 |  |  | Maximum Y-axis coordinate |
| `General.AxesMaxZ` | 1.0 |  |  | Maximum Z-axis coordinate |
| `General.AxesMikado` | 0.0 |  |  | Mikado axes style |
| `General.AxesMinX` | 0.0 |  |  | Minimum X-axis coordinate |
| `General.AxesMinY` | 0.0 |  |  | Minimum Y-axis coordinate |
| `General.AxesMinZ` | 0.0 |  |  | Minimum Z-axis coordinate |
| `General.AxesTicsX` | 5.0 |  |  | Number of tics on the X-axis |
| `General.AxesTicsY` | 5.0 |  |  | Number of tics on the Y-axis |
| `General.AxesTicsZ` | 5.0 |  |  | Number of tics on the Z-axis |
| `General.AxesValueMaxX` | 1.0 |  |  | Maximum X-axis forced value |
| `General.AxesValueMaxY` | 1.0 |  |  | Maximum Y-axis forced value |
| `General.AxesValueMaxZ` | 1.0 |  |  | Maximum Z-axis forced value |
| `General.AxesValueMinX` | 0.0 |  |  | Minimum X-axis forced value |
| `General.AxesValueMinY` | 0.0 |  |  | Minimum Y-axis forced value |
| `General.AxesValueMinZ` | 0.0 |  |  | Minimum Z-axis forced value |
| `General.BackgroundGradient` | 1.0 |  | (0, 'none'), (1, 'vertical'), (2, 'horizontal'), (3, 'radial') | Draw background gradient (0: none, 1: vertical, 2: horizontal, 3: radial) |
| `General.BackgroundImage3D` | 0.0 |  |  | Create background image in the 3D model (units = model units) or as 2D background (units = pixels) |
| `General.BackgroundImageFileName` |  |  |  | Background image file in JPEG, PNG or PDF format |
| `General.BackgroundImageHeight` | -1.0 |  |  | Height of background image (0: actual height if width = 0, natural scaling if not; -1: graphic window height) |
| `General.BackgroundImagePage` | 0.0 |  |  | Page to render in the background image (for multi-page PDFs) |
| `General.BackgroundImagePositionX` | 0.0 |  |  | X position of background image (for 2D background: < 0: measure from right window edge; >= 1e5: centered) |
| `General.BackgroundImagePositionY` | 0.0 |  |  | Y position of background image (for 2D background: < 0: measure from bottom window edge; >= 1e5: centered) |
| `General.BackgroundImageWidth` | -1.0 |  |  | Width of background image (0: actual width if height = 0, natural scaling if not; -1: graphic window width) |
| `General.BoundingBoxSize` | 1.0 |  |  | Overall bounding box size (read-only) (read only) |
| `General.BuildInfo` | Version: 4.15.2; License: GNU General Public License; Build OS: Linux64-sdk; Build date: 20260324; Build host: gmsh.info; Build options: 64Bit ALGLIB[contrib] ANN[contrib] Bamg Blas[petsc] Blossom Cgns DIntegration Dlopen DomHex Eigen[contrib] Fltk Gmm[contrib] Hxt Jpeg Kbipack Lapack[petsc] LinuxJoystick MathEx[contrib] Med Mesh Metis[contrib] Mmg Mpeg Netgen Nii2mesh ONELAB ONELABMetamodel OpenCASCADE OpenCASCADE-CAF OpenGL OpenMP OptHom PETSc Parser Plugins Png Post QuadMeshingTools QuadTri Solver TetGen/BR TinyXML2[contrib] Untangle Voro++[contrib] WinslowUntangler Zlib tinyobjloader; FLTK version: 1.3.11; PETSc version: 3.14.4 (real arithmtic); OCC version: 7.8.1; MED version: 4.1.0; Packaged by: geuzaine; Web site: https://gmsh.info; Issue tracker: https://gitlab.onelab.info/gmsh/gmsh/issues |  |  | Gmsh build information (read-only) (read only) |
| `General.BuildOptions` | 64Bit ALGLIB[contrib] ANN[contrib] Bamg Blas[petsc] Blossom Cgns DIntegration Dlopen DomHex Eigen[contrib] Fltk Gmm[contrib] Hxt Jpeg Kbipack Lapack[petsc] LinuxJoystick MathEx[contrib] Med Mesh Metis[contrib] Mmg Mpeg Netgen Nii2mesh ONELAB ONELABMetamodel OpenCASCADE OpenCASCADE-CAF OpenGL OpenMP OptHom PETSc Parser Plugins Png Post QuadMeshingTools QuadTri Solver TetGen/BR TinyXML2[contrib] Untangle Voro++[contrib] WinslowUntangler Zlib tinyobjloader |  |  | Gmsh build options (read-only) (read only) |
| `General.Camera` | off |  |  | Enable camera view mode |
| `General.CameraAperture` | 40.0 | deg |  | Camera aperture in degrees |
| `General.CameraEyeSeparationRatio` | 1.5 |  |  | Eye separation ratio in % for stereo rendering |
| `General.CameraFocalLengthRatio` | 1.0 |  |  | Camera Focal length ratio |
| `General.Clip0A` | 1.0 |  |  | First coefficient in equation for clipping plane 0 (`A' in `AX+BY+CZ+D=0') |
| `General.Clip0B` | 0.0 |  |  | Second coefficient in equation for clipping plane 0 (`B' in `AX+BY+CZ+D=0') |
| `General.Clip0C` | 0.0 |  |  | Third coefficient in equation for clipping plane 0 (`C' in `AX+BY+CZ+D=0') |
| `General.Clip0D` | 0.0 |  |  | Fourth coefficient in equation for clipping plane 0 (`D' in `AX+BY+CZ+D=0') |
| `General.Clip1A` | 0.0 |  |  | First coefficient in equation for clipping plane 1 |
| `General.Clip1B` | 1.0 |  |  | Second coefficient in equation for clipping plane 1 |
| `General.Clip1C` | 0.0 |  |  | Third coefficient in equation for clipping plane 1 |
| `General.Clip1D` | 0.0 |  |  | Fourth coefficient in equation for clipping plane 1 |
| `General.Clip2A` | 0.0 |  |  | First coefficient in equation for clipping plane 2 |
| `General.Clip2B` | 0.0 |  |  | Second coefficient in equation for clipping plane 2 |
| `General.Clip2C` | 1.0 |  |  | Third coefficient in equation for clipping plane 2 |
| `General.Clip2D` | 0.0 |  |  | Fourth coefficient in equation for clipping plane 2 |
| `General.Clip3A` | -1.0 |  |  | First coefficient in equation for clipping plane 3 |
| `General.Clip3B` | 0.0 |  |  | Second coefficient in equation for clipping plane 3 |
| `General.Clip3C` | 0.0 |  |  | Third coefficient in equation for clipping plane 3 |
| `General.Clip3D` | 1.0 |  |  | Fourth coefficient in equation for clipping plane 3 |
| `General.Clip4A` | 0.0 |  |  | First coefficient in equation for clipping plane 4 |
| `General.Clip4B` | -1.0 |  |  | Second coefficient in equation for clipping plane 4 |
| `General.Clip4C` | 0.0 |  |  | Third coefficient in equation for clipping plane 4 |
| `General.Clip4D` | 1.0 |  |  | Fourth coefficient in equation for clipping plane 4 |
| `General.Clip5A` | 0.0 |  |  | First coefficient in equation for clipping plane 5 |
| `General.Clip5B` | 0.0 |  |  | Second coefficient in equation for clipping plane 5 |
| `General.Clip5C` | -1.0 |  |  | Third coefficient in equation for clipping plane 5 |
| `General.Clip5D` | 1.0 |  |  | Fourth coefficient in equation for clipping plane 5 |
| `General.ClipFactor` | 5.0 |  |  | Near and far clipping plane distance factor (decrease value for better z-buffer resolution) |
| `General.ClipOnlyDrawIntersectingVolume` | 0.0 |  |  | Only draw layer of elements that intersect the clipping plane |
| `General.ClipOnlyVolume` | 0.0 |  |  | Only clip volume elements |
| `General.ClipPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the clipping planes window |
| `General.ClipPositionY` | 150.0 |  |  | Vertical position (in pixels) of the upper left corner of the clipping planes window |
| `General.ClipWholeElements` | 0.0 |  |  | Clip whole elements |
| `General.Color.AmbientLight` | (25, 25, 25, 255) |  |  | Ambient light color |
| `General.Color.Axes` | (0, 0, 0, 255) |  |  | Axes color |
| `General.Color.Background` | (255, 255, 255, 255) |  |  | Background color |
| `General.Color.BackgroundGradient` | (208, 215, 255, 255) |  |  | Background gradient color |
| `General.Color.DiffuseLight` | (255, 255, 255, 255) |  |  | Diffuse light color |
| `General.Color.Foreground` | (85, 85, 85, 255) |  |  | Foreground color |
| `General.Color.SmallAxes` | (0, 0, 0, 255) |  |  | Small axes color |
| `General.Color.SpecularLight` | (255, 255, 255, 255) |  |  | Specular light color |
| `General.Color.Text` | (0, 0, 0, 255) |  |  | Text color |
| `General.ColorScheme` | 1.0 |  | (0, 'light'), (1, 'default'), (2, 'grayscale'), (3, 'dark') | Default color scheme for graphics (0: light, 1: default, 2: grayscale, 3: dark) |
| `General.ConfirmOverwrite` | on |  |  | Ask confirmation before overwriting files? |
| `General.ContextPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the contextual windows |
| `General.ContextPositionY` | 150.0 |  |  | Vertical position (in pixels) of the upper left corner of the contextual windows |
| `General.DefaultFileName` | untitled.geo |  |  | Default project file name |
| `General.DetachedMenu` | off |  |  | Should the menu window be detached from the graphic window? |
| `General.DetachedProcess` | on |  |  | On Windows, should processes created by Gmsh be detached? |
| `General.Display` |  |  |  | X server to use (only for Unix versions) |
| `General.DisplayBorderFactor` | 0.2 |  |  | Border factor for model display (0: model fits window size exactly) |
| `General.DoubleBuffer` | on |  |  | Use a double buffered graphic window (on Unix, should be set to 0 when working on a remote host without GLX) |
| `General.DrawBoundingBoxes` | off |  |  | Draw bounding boxes |
| `General.ErrorFileName` | .gmsh-errors |  |  | File into which the log is saved if a fatal error occurs |
| `General.ExecutableFileName` | /usr/bin/python3.10 |  |  | File name of the Gmsh executable (read-only) (read only) |
| `General.ExpertMode` | off |  |  | Enable expert mode (to disable all the messages meant for inexperienced users) |
| `General.ExtraHeight` | 100.0 |  |  | Height (in pixels) of the generic extra window |
| `General.ExtraPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the generic extra window |
| `General.ExtraPositionY` | 350.0 |  |  | Vertical position (in pixels) of the upper left corner of the generic extra window |
| `General.ExtraWidth` | 100.0 |  |  | Width (in pixels) of the generic extra window |
| `General.FastRedraw` | off |  |  | Draw simplified model while rotating, panning and zooming |
| `General.FieldHeight` | 320.0 |  |  | Height (in pixels) of the field window |
| `General.FieldPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the field window |
| `General.FieldPositionY` | 550.0 |  |  | Vertical position (in pixels) of the upper left corner of the field window |
| `General.FieldWidth` | 420.0 |  |  | Width (in pixels) of the field window |
| `General.FileChooserPositionX` | 200.0 |  |  | Horizontal position (in pixels) of the upper left corner of the file chooser windows |
| `General.FileChooserPositionY` | 200.0 |  |  | Vertical position (in pixels) of the upper left corner of the file chooser windows |
| `General.FileName` |  |  |  | Current project file name (read-only) (read only) |
| `General.FltkColorScheme` | 0.0 |  | (0, 'standard'), (1, 'dark') | FLTK user interface color theme (0: standard, 1:dark) |
| `General.FltkRefreshRate` | 5.0 |  |  | FLTK user interface maximum refresh rate, per second (0: no limit) |
| `General.FltkTheme` |  |  |  | FLTK user interface theme (try e.g. plastic or gtk+) |
| `General.FontSize` | -1.0 |  |  | Size of the font in the user interface, in pixels (-1: automatic) |
| `General.GraphicsFont` | Helvetica |  |  | Font used in the graphic window |
| `General.GraphicsFontEngine` | Native |  |  | Set graphics font engine (Native, StringTexture, Cairo) |
| `General.GraphicsFontSize` | 15.0 |  |  | Size of the font in the graphic window, in pixels |
| `General.GraphicsFontSizeTitle` | 18.0 |  |  | Size of the font in the graphic window for titles, in pixels |
| `General.GraphicsFontTitle` | Helvetica |  |  | Font used in the graphic window for titles |
| `General.GraphicsHeight` | 600.0 |  |  | Height (in pixels) of the graphic window |
| `General.GraphicsPositionX` | 50.0 |  |  | Horizontal position (in pixels) of the upper left corner of the graphic window |
| `General.GraphicsPositionY` | 50.0 |  |  | Vertical position (in pixels) of the upper left corner of the graphic window |
| `General.GraphicsWidth` | 800.0 |  |  | Width (in pixels) of the graphic window |
| `General.HighOrderToolsPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the high-order tools window |
| `General.HighOrderToolsPositionY` | 150.0 |  |  | Vertical position (in pixels) of the upper left corner of the high-order tools window |
| `General.HighResolutionGraphics` | on |  |  | Use high-resolution OpenGL graphics (e.g. for Macs with retina displays) |
| `General.InitialModule` | 0.0 |  | (0, 'automatic'), (1, 'geometry'), (2, 'mesh'), (3, 'solver'), (4, 'post-processing') | Module launched on startup (0: automatic, 1: geometry, 2: mesh, 3: solver, 4: post-processing) |
| `General.InputScrolling` | on |  |  | Enable numerical input scrolling in user interface (moving the mouse to change numbers) |
| `General.Light0` | on |  |  | Enable light source 0 |
| `General.Light0W` | 0.0 |  |  | Divisor of the X, Y and Z coordinates of light source 0 (W=0 means infinitely far source) |
| `General.Light0X` | 0.65 |  |  | X position of light source 0 |
| `General.Light0Y` | 0.65 |  |  | Y position of light source 0 |
| `General.Light0Z` | 1.0 |  |  | Z position of light source 0 |
| `General.Light1` | off |  |  | Enable light source 1 |
| `General.Light1W` | 0.0 |  |  | Divisor of the X, Y and Z coordinates of light source 1 (W=0 means infinitely far source) |
| `General.Light1X` | 0.5 |  |  | X position of light source 1 |
| `General.Light1Y` | 0.3 |  |  | Y position of light source 1 |
| `General.Light1Z` | 1.0 |  |  | Z position of light source 1 |
| `General.Light2` | off |  |  | Enable light source 2 |
| `General.Light2W` | 0.0 |  |  | Divisor of the X, Y and Z coordinates of light source 2 (W=0 means infinitely far source) |
| `General.Light2X` | 0.5 |  |  | X position of light source 2 |
| `General.Light2Y` | 0.3 |  |  | Y position of light source 2 |
| `General.Light2Z` | 1.0 |  |  | Z position of light source 2 |
| `General.Light3` | off |  |  | Enable light source 3 |
| `General.Light3W` | 0.0 |  |  | Divisor of the X, Y and Z coordinates of light source 3 (W=0 means infinitely far source) |
| `General.Light3X` | 0.5 |  |  | X position of light source 3 |
| `General.Light3Y` | 0.3 |  |  | Y position of light source 3 |
| `General.Light3Z` | 1.0 |  |  | Z position of light source 3 |
| `General.Light4` | off |  |  | Enable light source 4 |
| `General.Light4W` | 0.0 |  |  | Divisor of the X, Y and Z coordinates of light source 4 (W=0 means infinitely far source) |
| `General.Light4X` | 0.5 |  |  | X position of light source 4 |
| `General.Light4Y` | 0.3 |  |  | Y position of light source 4 |
| `General.Light4Z` | 1.0 |  |  | Z position of light source 4 |
| `General.Light5` | off |  |  | Enable light source 5 |
| `General.Light5W` | 0.0 |  |  | Divisor of the X, Y and Z coordinates of light source 5 (W=0 means infinitely far source) |
| `General.Light5X` | 0.5 |  |  | X position of light source 5 |
| `General.Light5Y` | 0.3 |  |  | Y position of light source 5 |
| `General.Light5Z` | 1.0 |  |  | Z position of light source 5 |
| `General.LineWidth` | on |  |  | Display width of lines (in pixels) |
| `General.LogFileName` |  |  |  | Name of log file into which all messages are saved |
| `General.ManipulatorPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the manipulator window |
| `General.ManipulatorPositionY` | 150.0 |  |  | Vertical position (in pixels) of the upper left corner of the manipulator window |
| `General.MaxX` | 0.0 |  |  | Maximum model coordinate along the X-axis (read-only) (read only) |
| `General.MaxY` | 0.0 |  |  | Maximum model coordinate along the Y-axis (read-only) (read only) |
| `General.MaxZ` | 0.0 |  |  | Maximum model coordinate along the Z-axis (read-only) (read only) |
| `General.MenuHeight` | 200.0 |  |  | Height (in pixels) of the (detached) menu tree |
| `General.MenuPositionX` | 400.0 |  |  | Horizontal position (in pixels) of the (detached) menu tree |
| `General.MenuPositionY` | 400.0 |  |  | Vertical position (in pixels) of the (detached) menu tree |
| `General.MenuWidth` | 200.0 |  |  | Width (in pixels) of the menu tree |
| `General.MessageFontSize` | -1.0 |  |  | Size of the font in the message window, in pixels (-1: automatic) |
| `General.MessageHeight` | 300.0 |  |  | Height (in pixels) of the message console when it is visible (should be > 0) |
| `General.MinX` | 0.0 |  |  | Minimum model coordinate along the X-axis (read-only) (read only) |
| `General.MinY` | 0.0 |  |  | Minimum model coordinate along the Y-axis (read-only) (read only) |
| `General.MinZ` | 0.0 |  |  | Minimum model coordinate along the Z-axis (read-only) (read only) |
| `General.MouseHoverMeshes` | off |  |  | Enable mouse hover on meshes |
| `General.MouseInvertZoom` | 0.0 |  |  | Invert mouse wheel zoom direction |
| `General.MouseSelection` | on |  |  | Enable mouse selection |
| `General.NativeFileChooser` | off |  |  | Use the native file chooser? |
| `General.NoPopup` | off |  |  | Disable interactive dialog windows in scripts (and use default values instead) |
| `General.NonModalWindows` | on |  |  | Force all control windows to be on top of the graphic window ("non-modal") |
| `General.NumThreads` | 1.0 |  |  | Maximum number of threads used by Gmsh when compiled with OpenMP support (0: use system default, i.e. OMP_NUM_THREADS) |
| `General.NumberFormat` | %.3g |  |  | Number format (in standard C form) |
| `General.OptionsFileName` | .gmsh-options |  |  | Option file created with `Tools → Options → Save'; automatically read on startup |
| `General.OptionsPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the option window |
| `General.OptionsPositionY` | 150.0 |  |  | Vertical position (in pixels) of the upper left corner of the option window |
| `General.Orthographic` | 1.0 |  |  | Orthographic projection mode (0: perspective projection) |
| `General.PluginHeight` | 320.0 |  |  | Height (in pixels) of the plugin window |
| `General.PluginPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the plugin window |
| `General.PluginPositionY` | 550.0 |  |  | Vertical position (in pixels) of the upper left corner of the plugin window |
| `General.PluginWidth` | 420.0 |  |  | Width (in pixels) of the plugin window |
| `General.PointSize` | 3.0 |  |  | Display size of points (in pixels) |
| `General.PolygonOffsetAlwaysOn` | 0.0 |  |  | Always apply polygon offset, instead of trying to detect when it is required |
| `General.PolygonOffsetFactor` | 1.0 |  |  | Polygon offset factor (offset = factor * DZ + r * units) |
| `General.PolygonOffsetUnits` | 1.0 |  |  | Polygon offset units (offset = factor * DZ + r * units) |
| `General.ProgressMeterStep` | 10.0 |  |  | Increment (in percent) of the progress meter bar |
| `General.QuadricSubdivisions` | 6.0 |  |  | Number of subdivisions used to draw points or lines as spheres or cylinders |
| `General.RecentFile0` | untitled.geo |  |  | Most recent opened file |
| `General.RecentFile1` | untitled.geo |  |  | 2nd most recent opened file |
| `General.RecentFile2` | untitled.geo |  |  | 3rd most recent opened file |
| `General.RecentFile3` | untitled.geo |  |  | 4th most recent opened file |
| `General.RecentFile4` | untitled.geo |  |  | 5th most recent opened file |
| `General.RecentFile5` | untitled.geo |  |  | 6th most recent opened file |
| `General.RecentFile6` | untitled.geo |  |  | 7th most recent opened file |
| `General.RecentFile7` | untitled.geo |  |  | 8th most recent opened file |
| `General.RecentFile8` | untitled.geo |  |  | 9th most recent opened file |
| `General.RecentFile9` | untitled.geo |  |  | 10th most recent opened file |
| `General.RotationCenterGravity` | 1.0 |  |  | Rotate around the (pseudo) center of mass instead of (RotationCenterX, RotationCenterY, RotationCenterZ) |
| `General.RotationCenterX` | 0.0 |  |  | X coordinate of the center of rotation |
| `General.RotationCenterY` | 0.0 |  |  | Y coordinate of the center of rotation |
| `General.RotationCenterZ` | 0.0 |  |  | Z coordinate of the center of rotation |
| `General.RotationX` | 0.0 |  |  | First Euler angle (used if Trackball=0) |
| `General.RotationY` | 0.0 |  |  | Second Euler angle (used if Trackball=0) |
| `General.RotationZ` | 0.0 |  |  | Third Euler angle (used if Trackball=0) |
| `General.SaveOptions` | off |  |  | Automatically save current options in General.OptionsFileName (1) or per model (2)when the graphical user interface is closed? |
| `General.SaveSession` | on |  |  | Automatically save session specific information in General.SessionFileName when the graphical user interface is closed? |
| `General.ScaleX` | 1.0 |  |  | X-axis scale factor |
| `General.ScaleY` | 1.0 |  |  | Y-axis scale factor |
| `General.ScaleZ` | 1.0 |  |  | Z-axis scale factor |
| `General.ScriptingLanguages` | geo |  |  | Languages in which scripting commands generated by the GUI are written |
| `General.SessionFileName` | .gmshrc |  |  | Option file into which session specific information is saved; automatically read on startup |
| `General.Shininess` | 0.4 |  |  | Material shininess |
| `General.ShininessExponent` | 40.0 |  |  | Material shininess exponent (between 0 and 128) |
| `General.ShowMessagesOnStartup` | off |  |  | Show message window on startup |
| `General.ShowModuleMenu` | on |  |  | Show the standard Gmsh menu in the tree |
| `General.ShowOptionsOnStartup` | off |  |  | Show option window on startup |
| `General.SmallAxes` | on |  |  | Display the small axes |
| `General.SmallAxesPositionX` | -60.0 |  |  | X position (in pixels) of small axes (< 0: measure from right window edge; >= 1e5: centered) |
| `General.SmallAxesPositionY` | -40.0 |  |  | Y position (in pixels) of small axes (< 0: measure from bottom window edge; >= 1e5: centered) |
| `General.SmallAxesSize` | 30.0 |  |  | Size (in pixels) of small axes |
| `General.StatisticsPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the statistic window |
| `General.StatisticsPositionY` | 150.0 |  |  | Vertical position (in pixels) of the upper left corner of the statistic window |
| `General.Stereo` | off |  |  | Use stereo rendering |
| `General.SystemMenuBar` | on |  |  | Use the system menu bar on macOS? |
| `General.Terminal` | off |  |  | Should information be printed on the terminal (if available)? |
| `General.TextEditor` | gedit %s |  |  | System command to launch a text editor |
| `General.TmpFileName` | .gmsh-tmp |  |  | Temporary file used by the geometry module |
| `General.Tooltips` | on |  |  | Show tooltips in the user interface |
| `General.Trackball` | on |  |  | Use trackball rotation mode |
| `General.TrackballHyperbolicSheet` | on |  |  | Use hyperbolic sheet away from trackball center for z-rotations |
| `General.TrackballQuaternion0` | 0.0 |  |  | First trackball quaternion component (used if General.Trackball=1) |
| `General.TrackballQuaternion1` | 0.0 |  |  | Second trackball quaternion component (used if General.Trackball=1) |
| `General.TrackballQuaternion2` | 0.0 |  |  | Third trackball quaternion component (used if General.Trackball=1) |
| `General.TrackballQuaternion3` | 1.0 |  |  | Fourth trackball quaternion component (used if General.Trackball=1) |
| `General.TranslationX` | 0.0 |  |  | X-axis translation (in model units) |
| `General.TranslationY` | 0.0 |  |  | Y-axis translation (in model units) |
| `General.TranslationZ` | 0.0 |  |  | Z-axis translation (in model units) |
| `General.VectorType` | 4.0 |  |  | Default vector display type (for normals, etc.) |
| `General.Verbosity` | 5.0 |  | (0, 'silent except for fatal errors'), (1, '+errors'), (2, '+warnings'), (3, '+direct'), (4, '+information'), (5, '+status'), (99, '+debug') | Level of information printed on the terminal and the message console (0: silent except for fatal errors, 1: +errors, 2: +warnings, 3: +direct, 4: +information, 5: +status, 99: +debug) |
| `General.Version` | 4.15.2 |  |  | Gmsh version (read-only) (read only) |
| `General.VisibilityPositionX` | 650.0 |  |  | Horizontal position (in pixels) of the upper left corner of the visibility window |
| `General.VisibilityPositionY` | 150.0 |  |  | Vertical position (in pixels) of the upper left corner of the visibility window |
| `General.WatchFilePattern` |  |  |  | Pattern of files to merge as they become available |
| `General.ZoomFactor` | 4.0 |  |  | Middle mouse button zoom acceleration factor |
| `PostProcessing.AnimationCycle` | 0.0 |  |  | Cycle through time steps (0) or views (1) for animations |
| `PostProcessing.AnimationDelay` | 0.1 |  |  | Delay (in seconds) between frames in automatic animation mode |
| `PostProcessing.AnimationStep` | 1.0 |  |  | Step increment for animations |
| `PostProcessing.Binary` | off |  |  | Write post-processing files in binary format (if possible) |
| `PostProcessing.CombineCopyOptions` | 1.0 |  |  | Copy options during Combine operation |
| `PostProcessing.CombineRemoveOriginal` | on |  |  | Remove original views after a Combine operation |
| `PostProcessing.DoubleClickedGraphPointCommand` |  |  |  | Command parsed when double-clicking on a graph data point (e.g. Merge Sprintf('file_%g.pos', PostProcessing.GraphPointX);) |
| `PostProcessing.DoubleClickedGraphPointX` | 0.0 |  |  | Abscissa of last double-clicked graph point |
| `PostProcessing.DoubleClickedGraphPointY` | 0.0 |  |  | Ordinate of last double-clicked graph point |
| `PostProcessing.DoubleClickedView` | 0.0 |  |  | Index of last double-clicked view |
| `PostProcessing.ForceElementData` | 0.0 |  |  | Try to force saving datasets as ElementData |
| `PostProcessing.ForceNodeData` | 0.0 |  |  | Try to force saving datasets as NodeData |
| `PostProcessing.Format` | 10.0 |  | (0, 'ASCII view'), (1, 'binary view'), (2, 'parsed view'), (3, 'STL triangulation'), (4, 'raw text'), (5, 'Gmsh mesh'), (6, 'MED file'), (10, 'automatic') | Default file format for post-processing views (0: ASCII view, 1: binary view, 2: parsed view, 3: STL triangulation, 4: raw text, 5: Gmsh mesh, 6: MED file, 10: automatic) |
| `PostProcessing.GraphPointCommand` |  |  |  | Synonym for `DoubleClickedGraphPointCommand' |
| `PostProcessing.GraphPointX` | 0.0 |  |  | Synonym for `DoubleClickedGraphPointX' |
| `PostProcessing.GraphPointY` | 0.0 |  |  | Synonym for `DoubleClickedGraphPointY' |
| `PostProcessing.HorizontalScales` | on |  |  | Display value scales horizontally |
| `PostProcessing.Link` | 0.0 |  | (0, 'apply next option changes to selected views'), (1, 'force same options for all selected views') | Post-processing view links (0: apply next option changes to selected views, 1: force same options for all selected views) |
| `PostProcessing.NbViews` | 0.0 |  |  | Current number of views merged (read-only) (read only) |
| `PostProcessing.Plugins` | on |  |  | Enable default post-processing plugins? |
| `PostProcessing.SaveInterpolationMatrices` | on |  |  | Save the interpolation matrices when exporting model-based data |
| `PostProcessing.SaveMesh` | on |  |  | Save the mesh when exporting model-based data |
| `PostProcessing.Smoothing` | off |  |  | Apply (non-reversible) smoothing to post-processing view when merged |
| `Solver.AlwaysListen` | off |  |  | Always listen to incoming connection requests? |
| `Solver.AutoArchiveOutputFiles` | off |  |  | Automatically archive output files after each computation |
| `Solver.AutoCheck` | on |  |  | Automatically check model every time a parameter is changed |
| `Solver.AutoLoadDatabase` | off |  |  | Automatically load the ONELAB database when launching a solver |
| `Solver.AutoMergeFile` | on |  |  | Automatically merge result files |
| `Solver.AutoMesh` | 2.0 |  |  | Automatically mesh (0: never; 1: if geometry changed, but use existing mesh on disk if available; 2: if geometry changed; -1: the geometry script creates the mesh) |
| `Solver.AutoSaveDatabase` | on |  |  | Automatically save the ONELAB database after each computation |
| `Solver.AutoShowLastStep` | on |  |  | Automatically show the last step in newly merged results, if there are more than 2 steps |
| `Solver.AutoShowViews` | 2.0 |  |  | Automcatically show newly merged results (0: none; 1: all; 2: last one) |
| `Solver.Executable0` |  |  |  | System command to launch solver 0 |
| `Solver.Executable1` |  |  |  | System command to launch solver 1 |
| `Solver.Executable2` |  |  |  | System command to launch solver 2 |
| `Solver.Executable3` |  |  |  | System command to launch solver 3 |
| `Solver.Executable4` |  |  |  | System command to launch solver 4 |
| `Solver.Executable5` |  |  |  | System command to launch solver 5 |
| `Solver.Executable6` |  |  |  | System command to launch solver 6 |
| `Solver.Executable7` |  |  |  | System command to launch solver 7 |
| `Solver.Executable8` |  |  |  | System command to launch solver 8 |
| `Solver.Executable9` |  |  |  | System command to launch solver 9 |
| `Solver.Extension0` | .pro |  |  | File extension for solver 0 |
| `Solver.Extension1` |  |  |  | File extension for solver 1 |
| `Solver.Extension2` |  |  |  | File extension for solver 2 |
| `Solver.Extension3` |  |  |  | File extension for solver 3 |
| `Solver.Extension4` |  |  |  | File extension for solver 4 |
| `Solver.Extension5` |  |  |  | File extension for solver 5 |
| `Solver.Extension6` |  |  |  | File extension for solver 6 |
| `Solver.Extension7` |  |  |  | File extension for solver 7 |
| `Solver.Extension8` |  |  |  | File extension for solver 8 |
| `Solver.Extension9` |  |  |  | File extension for solver 9 |
| `Solver.Name0` | GetDP |  |  | Name of solver 0 |
| `Solver.Name1` |  |  |  | Name of solver 1 |
| `Solver.Name2` |  |  |  | Name of solver 2 |
| `Solver.Name3` |  |  |  | Name of solver 3 |
| `Solver.Name4` |  |  |  | Name of solver 4 |
| `Solver.Name5` |  |  |  | Name of solver 5 |
| `Solver.Name6` |  |  |  | Name of solver 6 |
| `Solver.Name7` |  |  |  | Name of solver 7 |
| `Solver.Name8` |  |  |  | Name of solver 8 |
| `Solver.Name9` |  |  |  | Name of solver 9 |
| `Solver.OctaveInterpreter` | octave |  |  | Name of the Octave interpreter (used to run .m files) |
| `Solver.Plugins` | off |  |  | Enable default solver plugins? |
| `Solver.PythonInterpreter` | python |  |  | Name of the Python interpreter (used to run .py files if they are not executable) |
| `Solver.RemoteLogin0` |  |  |  | Command to login to a remote host to launch solver 0 |
| `Solver.RemoteLogin1` |  |  |  | Command to login to a remote host to launch solver 1 |
| `Solver.RemoteLogin2` |  |  |  | Command to login to a remote host to launch solver 2 |
| `Solver.RemoteLogin3` |  |  |  | Command to login to a remote host to launch solver 3 |
| `Solver.RemoteLogin4` |  |  |  | Command to login to a remote host to launch solver 4 |
| `Solver.RemoteLogin5` |  |  |  | Command to login to a remote host to launch solver 5 |
| `Solver.RemoteLogin6` |  |  |  | Command to login to a remote host to launch solver 6 |
| `Solver.RemoteLogin7` |  |  |  | Command to login to a remote host to launch solver 7 |
| `Solver.RemoteLogin8` |  |  |  | Command to login to a remote host to launch solver 8 |
| `Solver.RemoteLogin9` |  |  |  | Command to login to a remote host to launch solver 9 |
| `Solver.ShowInvisibleParameters` | off |  |  | Show all parameters, even those marked invisible |
| `Solver.SocketName` | .gmshsock |  |  | Base name of socket (UNIX socket if the name does not contain a colon, TCP/IP otherwise, in the form 'host:baseport'; the actual name/port is constructed by appending the unique client id. If baseport is 0 or is not provided, the port is chosen automatically (recommended)) |
| `Solver.Timeout` | 5.0 |  |  | Time (in seconds) before closing the socket if no connection is happening |
