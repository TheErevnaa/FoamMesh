"""The snappy control register: every meshing field, and the key it becomes.

This answers one question for every control FoamMesh shows on a snappy page:
*which keyword in which OpenFOAM dictionary does this actually reach?*  Before
this register existed the answer lived only in the writer, so nobody noticed
that several controls named keys OpenFOAM Foundation 13 does not read at all --
they came from the ESI fork, were shown, saved, written, and ignored.

The authority for "does v13 read this?" is not this file.  It is
``plans/evidence/plan29/snappy_v13_keys.json``, extracted by
``scripts/extract_snappy_keys.py`` from a live OpenFOAM 13 installation, and
``tests/unit/test_plan29_snappy_keys.py`` checks every entry below against it.
This file only records the mapping; the test decides whether the mapping is
true.

Three rules hold, and are enforced by that test:

* every schema path under the snappy groups appears here exactly once,
* a :data:`WRITER` entry names a section and keyword v13 really reads, and
* a :data:`DERIVATION` or :data:`GATE` entry names no keyword, because it never
  reaches the dictionary as itself.
"""

from __future__ import annotations

from foammesh.core.layer_patterns import (  # noqa: F401  (re-exported)
    BACKGROUND_PATCH_GROUP, BLOCK_FACE_NAMES)

from dataclasses import dataclass
from types import MappingProxyType


#: Consumers.  A control is only worth shipping if one of these reads it.
WRITER = 'writer'          #: written verbatim into a dictionary keyword
DERIVATION = 'derivation'  #: shapes what the writer emits, under another name
GATE = 'gate'              #: judged by FoamMesh, never written


#: The dictionary sections, spelled as ``extract_snappy_keys`` reports them.
BLOCK = 'blockMeshDict'
BLOCK_BLOCKS = 'blockMeshDict/blocks'
BLOCK_BOUNDARY = 'blockMeshDict/boundary/*'
SNAPPY = 'snappyHexMeshDict'
CASTELLATED = 'snappyHexMeshDict/castellatedMeshControls'
SNAP = 'snappyHexMeshDict/snapControls'
LAYERS = 'snappyHexMeshDict/addLayersControls'
QUALITY = 'snappyHexMeshDict/meshQualityControls'
QUALITY_RELAXED = 'snappyHexMeshDict/meshQualityControls/relaxed'


@dataclass(frozen=True)
class SnappyControl:
    """One control, and exactly how it reaches the mesh."""

    path: str
    consumer: str
    #: The dictionary this control lands in.  Empty for derivations and gates.
    section: str = ''
    #: The keyword within that section.  Empty for derivations and gates.
    key: str = ''
    note: str = ''

    def __post_init__(self) -> None:
        if self.consumer not in {WRITER, DERIVATION, GATE}:
            raise ValueError(f'{self.path}: unknown consumer {self.consumer!r}')
        if self.consumer == WRITER and not (self.section and self.key):
            raise ValueError(
                f'{self.path}: name the dictionary keyword this reaches, or '
                'make it a derivation; a control that reaches nothing must not '
                'ship')
        if self.consumer != WRITER and (self.section or self.key):
            raise ValueError(
                f'{self.path}: only a writer control names a keyword')


def _register(*controls: SnappyControl):
    seen: dict[str, SnappyControl] = {}
    for control in controls:
        if control.path in seen:
            raise ValueError(f'duplicate control: {control.path}')
        seen[control.path] = control
    return MappingProxyType(seen)


CONTROLS = _register(
    # -- background mesh ------------------------------------------------- #
    SnappyControl(
        'baseGrid/sizingMode', DERIVATION,
        note='chooses whether the cell counts are typed in or computed from a '
             'target cell size'),
    SnappyControl(
        'baseGrid/targetCellSize', DERIVATION,
        note='divided into the bounding box to give the counts below'),
    SnappyControl('baseGrid/numCellsX', WRITER, BLOCK_BLOCKS, 'hex',
                  note='the cell counts in the single hex block'),
    SnappyControl('baseGrid/numCellsY', WRITER, BLOCK_BLOCKS, 'hex'),
    SnappyControl('baseGrid/numCellsZ', WRITER, BLOCK_BLOCKS, 'hex'),
    SnappyControl(
        'baseGrid/boundingHex6', DERIVATION,
        note='picks which imported hex6 gives the background block its extent'),
    SnappyControl(
        'baseGrid/standoff', DERIVATION,
        note='pushes the derived block off the geometry on all six faces, as '
             'a fraction of its largest span, before the vertices are '
             'written; ignored while a bounding hex6 is chosen'),
    SnappyControl('baseGrid/scale', WRITER, BLOCK, 'scale',
                  note='factor from the vertex unit to metres'),
    SnappyControl('baseGrid/grading/x', WRITER, BLOCK_BLOCKS, 'simpleGrading'),
    SnappyControl('baseGrid/grading/y', WRITER, BLOCK_BLOCKS, 'simpleGrading'),
    SnappyControl('baseGrid/grading/z', WRITER, BLOCK_BLOCKS, 'simpleGrading'),
    SnappyControl('baseGrid/boundaryTypes/xMin', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryTypes/xMax', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryTypes/yMin', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryTypes/yMax', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryTypes/zMin', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryTypes/zMax', WRITER, BLOCK_BOUNDARY, 'type'),

    # -- background topology, authored (Plan 31 CP-07 items 1-3) --------- #
    # The six controls above describe one axis-aligned box, which is all the
    # product could ever mesh on.  These describe a topology: several blocks,
    # the vertices they share, the curved edges between them, and the patches
    # that own their outer faces.  They are keyed collections, so the register
    # names the container -- one ``vertices`` list, not one key per corner.
    SnappyControl('baseGrid/vertices', WRITER, BLOCK, 'vertices',
                  note='the authored vertex list; empty means the derived box '
                       'above still supplies all eight corners'),
    SnappyControl('baseGrid/blocks', WRITER, BLOCK_BLOCKS, 'hex',
                  note='one hex per row, with its own cell counts and grading; '
                       'empty means the single derived block'),
    SnappyControl('baseGrid/edges', WRITER, BLOCK, 'edges',
                  note='arc/spline/polyLine/BSpline edges, which is what makes '
                       'a curved duct a curved duct rather than a chamfer'),
    SnappyControl('baseGrid/patches', WRITER, BLOCK_BOUNDARY, 'faces',
                  note='authored boundary patches naming the block faces they '
                       'own; a face nobody claims is refused rather than swept '
                       'into blockMesh defaultFaces'),
    SnappyControl('baseGrid/mergePairs', WRITER, BLOCK, 'mergePatchPairs',
                  note='face-merged interfaces between blocks that do not share '
                       'vertices'),
    SnappyControl('baseGrid/boundaryNames/xMin', WRITER, BLOCK_BOUNDARY, 'type',
                  note='the name the user gave this face of the derived box; '
                       'left empty the generated xMin name is used, and the '
                       'category beside it must then stay unclassified'),
    SnappyControl('baseGrid/boundaryNames/xMax', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryNames/yMin', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryNames/yMax', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryNames/zMin', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl('baseGrid/boundaryNames/zMax', WRITER, BLOCK_BOUNDARY, 'type'),
    SnappyControl(
        'baseGrid/boundaryCategories/xMin', DERIVATION,
        note='inlet/outlet/wall ownership: it constrains the patch type and is '
             'carried into the group manifest, but is never written as a '
             'keyword of its own'),
    SnappyControl('baseGrid/boundaryCategories/xMax', DERIVATION),
    SnappyControl('baseGrid/boundaryCategories/yMin', DERIVATION),
    SnappyControl('baseGrid/boundaryCategories/yMax', DERIVATION),
    SnappyControl('baseGrid/boundaryCategories/zMin', DERIVATION),
    SnappyControl('baseGrid/boundaryCategories/zMax', DERIVATION),

    # -- castellation ---------------------------------------------------- #
    SnappyControl('castellation/nCellsBetweenLevels', WRITER, CASTELLATED,
                  'nCellsBetweenLevels'),
    SnappyControl('castellation/resolveFeatureAngle', WRITER, CASTELLATED,
                  'resolveFeatureAngle'),
    SnappyControl('castellation/maxGlobalCells', WRITER, CASTELLATED,
                  'maxGlobalCells'),
    SnappyControl('castellation/maxLocalCells', WRITER, CASTELLATED,
                  'maxLocalCells'),
    SnappyControl('castellation/minRefinementCells', WRITER, CASTELLATED,
                  'minRefinementCells'),
    SnappyControl('castellation/maxLoadUnbalance', WRITER, CASTELLATED,
                  'maxLoadUnbalance'),
    SnappyControl('castellation/allowFreeStandingZoneFaces', WRITER, CASTELLATED,
                  'allowFreeStandingZoneFaces'),
    SnappyControl(
        'castellation/gapLevelIncrement', WRITER, CASTELLATED,
        'gapLevelIncrement',
        note='the case-wide increment; each refinement surface may override '
             'it with its own gapLevelIncrement. The ESI gapLevel triple and '
             'gapMode are not read by Foundation 13 and have no control'),
    SnappyControl('castellation/planarAngle', WRITER, CASTELLATED,
                  'planarAngle'),
    SnappyControl(
        'castellation/extendedRefinementSpan', WRITER, CASTELLATED,
        'extendedRefinementSpan',
        note='the companion switch to the insideSpan/outsideSpan refinement '
             'modes: meshRefinement.C:1142 reads it out of '
             'castellatedMeshControls, defaulting true. Left unwritten while '
             'it is DEFAULT'),
    SnappyControl('castellation/useTopologicalSnapDetection', WRITER,
                  CASTELLATED, 'useTopologicalSnapDetection',
                  note='left unwritten while it is DEFAULT'),
    SnappyControl('castellation/handleSnapProblems', WRITER, CASTELLATED,
                  'handleSnapProblems',
                  note='left unwritten while it is DEFAULT'),
    SnappyControl('castellation/refinementSurfaces', WRITER, CASTELLATED,
                  'refinementSurfaces'),
    SnappyControl('castellation/refinementVolumes', WRITER, CASTELLATED,
                  'refinementRegions',
                  note='FoamMesh calls them volumes; OpenFOAM calls them '
                       'refinementRegions'),
    SnappyControl('castellation/featureBands', WRITER, CASTELLATED, 'features',
                  note='the levels ramp of the features entries; empty leaves '
                       'each entry with the single level it always had'),

    # -- snapping -------------------------------------------------------- #
    SnappyControl('snap/nSmoothPatch', WRITER, SNAP, 'nSmoothPatch'),
    SnappyControl('snap/nSolveIter', WRITER, SNAP, 'nSolveIter'),
    SnappyControl('snap/nRelaxIter', WRITER, SNAP, 'nRelaxIter'),
    SnappyControl('snap/nFeatureSnapIter', WRITER, SNAP, 'nFeatureSnapIter'),
    SnappyControl('snap/implicitFeatureSnap', WRITER, SNAP,
                  'implicitFeatureSnap'),
    SnappyControl('snap/explicitFeatureSnap', WRITER, SNAP,
                  'explicitFeatureSnap',
                  note='independent of implicitFeatureSnap; v13 accepts both '
                       'on, which is the usual setting for an STL with '
                       'extracted feature edges'),
    SnappyControl('snap/multiRegionFeatureSnap', WRITER, SNAP,
                  'multiRegionFeatureSnap'),
    SnappyControl('snap/detectNearSurfacesSnap', WRITER, SNAP,
                  'detectNearSurfacesSnap',
                  note='left unwritten while it is DEFAULT'),
    SnappyControl('snap/tolerance', WRITER, SNAP, 'tolerance'),

    # -- layers ---------------------------------------------------------- #
    SnappyControl('addLayers/layers', WRITER, LAYERS, 'layers'),
    SnappyControl('addLayers/nGrow', WRITER, LAYERS, 'nGrow'),
    SnappyControl('addLayers/featureAngle', WRITER, LAYERS, 'featureAngle'),
    SnappyControl('addLayers/slipFeatureAngle', WRITER, LAYERS,
                  'slipFeatureAngle'),
    SnappyControl('addLayers/maxFaceThicknessRatio', WRITER, LAYERS,
                  'maxFaceThicknessRatio'),
    SnappyControl('addLayers/nSmoothSurfaceNormals', WRITER, LAYERS,
                  'nSmoothSurfaceNormals'),
    SnappyControl('addLayers/nSmoothThickness', WRITER, LAYERS,
                  'nSmoothThickness'),
    SnappyControl(
        'addLayers/minMedialAxisAngle', WRITER, LAYERS, 'minMedialAxisAngle',
        note='OpenFOAM 13 spells it minMedialAxisAngle; the older '
             'minMedianAxisAngle survives only as the second name of the '
             'lookupBackwardsCompatible pair and is no longer written'),
    SnappyControl('addLayers/maxThicknessToMedialRatio', WRITER, LAYERS,
                  'maxThicknessToMedialRatio'),
    SnappyControl('addLayers/nSmoothNormals', WRITER, LAYERS, 'nSmoothNormals'),
    SnappyControl('addLayers/nRelaxIter', WRITER, LAYERS, 'nRelaxIter'),
    SnappyControl('addLayers/nBufferCellsNoExtrude', WRITER, LAYERS,
                  'nBufferCellsNoExtrude'),
    SnappyControl('addLayers/nLayerIter', WRITER, LAYERS, 'nLayerIter'),
    SnappyControl('addLayers/nRelaxedIter', WRITER, LAYERS, 'nRelaxedIter'),
    SnappyControl('addLayers/nMedialAxisIter', WRITER, LAYERS,
                  'nMedialAxisIter', note='left unwritten while it is unset'),
    SnappyControl('addLayers/nSmoothDisplacement', WRITER, LAYERS,
                  'nSmoothDisplacement',
                  note='left unwritten while it is unset'),
    SnappyControl('addLayers/detectExtrusionIsland', WRITER, LAYERS,
                  'detectExtrusionIsland',
                  note='left unwritten while it is DEFAULT'),
    SnappyControl('addLayers/additionalReporting', WRITER, LAYERS,
                  'additionalReporting',
                  note='left unwritten while it is DEFAULT'),
    SnappyControl(
        'addLayers/meshShrinker', WRITER, LAYERS, 'meshShrinker',
        note='displacementMedialAxis is the only mover v13 registers'),

    # -- mesh quality ---------------------------------------------------- #
    SnappyControl('meshQuality/maxNonOrtho', WRITER, QUALITY, 'maxNonOrtho'),
    SnappyControl('meshQuality/maxBoundarySkewness', WRITER, QUALITY,
                  'maxBoundarySkewness'),
    SnappyControl('meshQuality/maxInternalSkewness', WRITER, QUALITY,
                  'maxInternalSkewness'),
    SnappyControl('meshQuality/maxConcave', WRITER, QUALITY, 'maxConcave'),
    SnappyControl('meshQuality/minVol', WRITER, QUALITY, 'minVol'),
    SnappyControl('meshQuality/minTetQuality', WRITER, QUALITY,
                  'minTetQuality'),
    SnappyControl('meshQuality/minVolCollapseRatio', WRITER, QUALITY,
                  'minVolCollapseRatio'),
    SnappyControl('meshQuality/minArea', WRITER, QUALITY, 'minArea'),
    SnappyControl('meshQuality/minTwist', WRITER, QUALITY, 'minTwist'),
    SnappyControl('meshQuality/minDeterminant', WRITER, QUALITY,
                  'minDeterminant'),
    SnappyControl('meshQuality/minFaceWeight', WRITER, QUALITY,
                  'minFaceWeight'),
    SnappyControl('meshQuality/minVolRatio', WRITER, QUALITY, 'minVolRatio'),
    SnappyControl('meshQuality/nSmoothScale', WRITER, QUALITY, 'nSmoothScale'),
    SnappyControl('meshQuality/errorReduction', WRITER, QUALITY,
                  'errorReduction'),
    SnappyControl('meshQuality/mergeTolerance', WRITER, SNAPPY,
                  'mergeTolerance',
                  note='a top-level key, not part of meshQualityControls'),

    # The relaxed block takes the same keywords as the strict one -- the
    # annotated dictionary calls them "relaxed rules" and shows maxNonOrtho as
    # an example, and layer addition falls back to them after nRelaxedIter.
    SnappyControl('meshQuality/relaxed/maxNonOrtho', WRITER, QUALITY_RELAXED,
                  'maxNonOrtho'),
    SnappyControl('meshQuality/relaxed/maxBoundarySkewness', WRITER,
                  QUALITY_RELAXED, 'maxBoundarySkewness'),
    SnappyControl('meshQuality/relaxed/maxInternalSkewness', WRITER,
                  QUALITY_RELAXED, 'maxInternalSkewness'),
    SnappyControl('meshQuality/relaxed/maxConcave', WRITER, QUALITY_RELAXED,
                  'maxConcave'),
    SnappyControl('meshQuality/relaxed/minVol', WRITER, QUALITY_RELAXED,
                  'minVol'),
    SnappyControl('meshQuality/relaxed/minTetQuality', WRITER, QUALITY_RELAXED,
                  'minTetQuality'),
    SnappyControl('meshQuality/relaxed/minVolCollapseRatio', WRITER,
                  QUALITY_RELAXED, 'minVolCollapseRatio'),
    SnappyControl('meshQuality/relaxed/minArea', WRITER, QUALITY_RELAXED,
                  'minArea'),
    SnappyControl('meshQuality/relaxed/minTwist', WRITER, QUALITY_RELAXED,
                  'minTwist'),
    SnappyControl('meshQuality/relaxed/minDeterminant', WRITER,
                  QUALITY_RELAXED, 'minDeterminant'),
    SnappyControl('meshQuality/relaxed/minFaceWeight', WRITER, QUALITY_RELAXED,
                  'minFaceWeight'),
    SnappyControl('meshQuality/relaxed/minVolRatio', WRITER, QUALITY_RELAXED,
                  'minVolRatio'),

    # -- diagnostics ----------------------------------------------------- #
    # Both are lists of words, so several controls share one keyword; the
    # writer omits the keyword entirely when no flag is on.
    SnappyControl('snappyAdvanced/keepPatches', WRITER, SNAPPY, 'keepPatches',
                  note='keeps patches that ended the run with no faces; left '
                       'unwritten while it is DEFAULT'),
    SnappyControl('snappyAdvanced/writeFlags/scalarLevels', WRITER, SNAPPY,
                  'writeFlags'),
    SnappyControl('snappyAdvanced/writeFlags/layerSets', WRITER, SNAPPY,
                  'writeFlags'),
    SnappyControl('snappyAdvanced/writeFlags/layerFields', WRITER, SNAPPY,
                  'writeFlags'),
    SnappyControl('snappyAdvanced/debugFlags/mesh', WRITER, SNAPPY,
                  'debugFlags'),
    SnappyControl('snappyAdvanced/debugFlags/intersections', WRITER, SNAPPY,
                  'debugFlags'),
    SnappyControl('snappyAdvanced/debugFlags/featureSeeds', WRITER, SNAPPY,
                  'debugFlags'),
    SnappyControl('snappyAdvanced/debugFlags/attraction', WRITER, SNAPPY,
                  'debugFlags'),
    SnappyControl('snappyAdvanced/debugFlags/layerInfo', WRITER, SNAPPY,
                  'debugFlags'),
)


#: The flag names, in the order the annotated dictionary lists them.  The
#: writer emits the ones that are on, so the order here is the order on disk.
WRITE_FLAGS = ('scalarLevels', 'layerSets', 'layerFields')
DEBUG_FLAGS = ('mesh', 'intersections', 'featureSeeds', 'attraction',
               'layerInfo')

#: The background block's faces, in blockMeshDict vertex order.  The names
#: come from :mod:`foammesh.core.layer_patterns` so that the layer editor's
#: match preview and the writer cannot disagree about what blockMesh calls
#: them; only the vertex ordering is a fact about blockMesh, and it stays here.
_BLOCK_FACE_VERTICES = (
    '(0 3 7 4)',  # xMin
    '(1 5 6 2)',  # xMax
    '(0 4 5 1)',  # yMin
    '(3 2 6 7)',  # yMax
    '(0 1 2 3)',  # zMin
    '(4 7 6 5)',  # zMax
)
BOUNDARY_FACES = tuple(zip(BLOCK_FACE_NAMES, _BLOCK_FACE_VERTICES))


def by_section(section: str) -> tuple[SnappyControl, ...]:
    """Every writer control landing in *section*."""
    return tuple(control for control in CONTROLS.values()
                 if control.section == section)
