"""Import healing and OCC options.

Both healing options default off, and both defaults were chosen from measured
failures rather than from what sounds safe:

* ``Geometry.OCCSewFaces`` turns a closed solid into a bare face set. Volumes
  drop from one to zero and ``generate(3)`` produces a surface mesh with no
  cells and raises nothing at all.
* ``Geometry.OCCFixDegenerated`` strips the degenerate seam at a sphere's
  poles, after which the surface cannot be meshed: *"Impossible to mesh
  periodic surface 1"*.

Enabling either is therefore a deliberate act for a specific broken import,
and the derivation says so on the job.

Plan 31 adds the repair *pass* beside those import flags. The four above
reconfigure the OCC importer; ``gmsh.model.occ.healShapes`` repairs what it
produced, and until this plan nothing here called it -- a defect an audit of
this repository has recorded as "heal_shape() uncalled" since long before.
The pass performs exactly the repairs ticked on the healing page, so it can
never apply the two above on a case that did not ask for them, and it is off
by default so no shipped mesh changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

CALCULATION_VERSION = 'gmsh.topology.v1'


class TopologyError(ValueError):
    pass


@dataclass(frozen=True)
class Healing:
    import_tolerance: float
    sew_faces: bool
    fix_degenerated: bool
    make_solids: bool
    remove_duplicate_nodes: bool
    #: Fuse faces shared by two solids, so a multi-volume assembly has a
    #: conformal interface instead of two coincident copies.
    remove_duplicate_faces: bool = True
    #: Dihedral angle for recovering patches from a tessellated surface.
    classification_angle: float = 40.0
    #: Plan 31. Run ``gmsh.model.occ.healShapes`` explicitly after import.
    heal_shapes: bool = False
    fix_small_edges: bool = False
    fix_small_faces: bool = False
    auto_fix: bool = True
    union_unify: bool = True
    boolean_tolerance: float = 0.0
    import_scaling: float = 1.0
    import_labels: bool = True
    occ_parallel: bool = False
    warnings: tuple[str, ...] = ()
    calculation_version: str = CALCULATION_VERSION

    @property
    def heal_repairs(self) -> tuple[str, ...]:
        """Which repairs the explicit pass would perform, in Gmsh's names.

        The pass takes one boolean per repair. Handing it all five would run
        repairs the user never ticked -- and two of them, sewing and
        degenerate-edge fixing, are measured to destroy a good import. So the
        pass performs exactly what the healing page says, and nothing else.
        """
        selected = (
            ('fixDegenerated', self.fix_degenerated),
            ('fixSmallEdges', self.fix_small_edges),
            ('fixSmallFaces', self.fix_small_faces),
            ('sewFaces', self.sew_faces),
            ('makeSolids', self.make_solids),
        )
        return tuple(name for name, on in selected if on)

    def to_dict(self) -> dict:
        return {
            'importTolerance': self.import_tolerance,
            'sewFaces': self.sew_faces,
            'fixDegenerated': self.fix_degenerated,
            'makeSolids': self.make_solids,
            'removeDuplicateNodes': self.remove_duplicate_nodes,
            'removeDuplicateFaces': self.remove_duplicate_faces,
            'classificationAngle': self.classification_angle,
            'healShapes': self.heal_shapes,
            'healRepairs': list(self.heal_repairs),
            'fixSmallEdges': self.fix_small_edges,
            'fixSmallFaces': self.fix_small_faces,
            'autoFix': self.auto_fix,
            'unionUnify': self.union_unify,
            'booleanTolerance': self.boolean_tolerance,
            'importScaling': self.import_scaling,
            'importLabels': self.import_labels,
            'occParallel': self.occ_parallel,
            'warnings': list(self.warnings),
            'calculation_version': self.calculation_version,
        }


def derive_healing(values: dict) -> Healing:
    values = dict(values or {})
    try:
        tolerance = float(values.get('importTolerance', 1e-6) or 0.0)
    except (TypeError, ValueError) as error:
        raise TopologyError('importTolerance must be numeric') from error
    if tolerance <= 0:
        raise TopologyError('importTolerance must be positive')

    sew = bool(values.get('sewFaces', False))
    fix = bool(values.get('fixDegenerated', False))
    solids = bool(values.get('makeSolids', False))
    warnings = []
    if sew and not solids:
        warnings.append(
            'face sewing is on without solid reconstruction: if the import '
            'loses its volumes the mesh will come back as a surface with no '
            'cells. Turn on "rebuild solids" as well.')
    if fix:
        warnings.append(
            'degenerate-edge fixing is on: geometry with poles or apexes '
            '(spheres, cones) may become unmeshable')

    def _positive(key: str, fallback: float) -> float:
        try:
            number = float(values.get(key, fallback) or fallback)
        except (TypeError, ValueError) as error:
            raise TopologyError(f'{key} must be numeric') from error
        if number <= 0:
            raise TopologyError(f'{key} must be positive')
        return number

    try:
        boolean_tolerance = float(values.get('booleanTolerance', 0.0) or 0.0)
    except (TypeError, ValueError) as error:
        raise TopologyError('booleanTolerance must be numeric') from error
    if boolean_tolerance < 0:
        raise TopologyError('booleanTolerance must not be negative')

    healing = Healing(
        import_tolerance=tolerance, sew_faces=sew, fix_degenerated=fix,
        make_solids=solids,
        remove_duplicate_nodes=bool(values.get('removeDuplicateNodes', True)),
        remove_duplicate_faces=bool(values.get('removeDuplicateFaces', True)),
        classification_angle=float(values.get('classificationAngle', 40.0) or 40.0),
        heal_shapes=bool(values.get('healShapes', False)),
        fix_small_edges=bool(values.get('fixSmallEdges', False)),
        fix_small_faces=bool(values.get('fixSmallFaces', False)),
        auto_fix=bool(values.get('autoFix', True)),
        union_unify=bool(values.get('unionUnify', True)),
        boolean_tolerance=boolean_tolerance,
        import_scaling=_positive('importScaling', 1.0),
        import_labels=bool(values.get('importLabels', True)),
        occ_parallel=bool(values.get('occParallel', False)),
        warnings=tuple(warnings))
    # Plan 31. The repair pass performs the repairs ticked on the page. With
    # none ticked it is `healShapes()` with every flag off, which walks the
    # model and changes nothing -- so say so here rather than letting the run
    # report a healing pass that healed nothing.
    if healing.heal_shapes and not healing.heal_repairs:
        warnings.append(
            'the explicit healing pass is on but no repair is selected, so it '
            'will change nothing. Tick at least one of small edges, small '
            'faces, degenerate edges, face sewing or rebuild solids.')
        healing = replace(healing, warnings=tuple(warnings))
    return healing
