"""Small immutable execution DAG used by serial and MPI meshing entry points."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .resources import ResourceAllocation


@dataclass(frozen=True)
class ExecutionNode:
    node_id: str
    argv: tuple[str, ...]
    cwd: Path
    depends_on: tuple[str, ...] = ()
    mutates_mesh: bool = False
    publishes_mesh: bool = False
    #: After this node, ``constant/polyMesh`` holds a boundary a gate must see
    #: before the next node overwrites it. The runner checkpoints here and, in
    #: enforcing mode, may stop (Plan 23 §8.2).
    pauses_after: bool = False

    def to_dict(self) -> dict:
        return {
            'node_id': self.node_id,
            'argv': list(self.argv),
            'cwd': str(self.cwd),
            'depends_on': list(self.depends_on),
            'mutates_mesh': self.mutates_mesh,
            'publishes_mesh': self.publishes_mesh,
            'pauses_after': self.pauses_after,
        }


@dataclass(frozen=True)
class ExecutionDag:
    nodes: tuple[ExecutionNode, ...]

    def __post_init__(self) -> None:
        known = set()
        for node in self.nodes:
            if node.node_id in known:
                raise ValueError(f'duplicate DAG node: {node.node_id}')
            missing = set(node.depends_on) - known
            if missing:
                raise ValueError(
                    f'node {node.node_id} precedes dependencies {sorted(missing)}')
            known.add(node.node_id)

    def to_dict(self) -> dict:
        return {'nodes': [node.to_dict() for node in self.nodes]}


def _with_interface_couples(nodes: tuple[ExecutionNode, ...], case: Path,
                            couples: tuple[tuple[str, str], ...],
                            ) -> tuple[ExecutionNode, ...]:
    """Append one ``createNonConformalCouples`` node per authored pair.

    F-13. The couples run last, on the assembled case-root mesh, because the
    utility rewrites ``constant/polyMesh`` and adds the non-conformal cyclic
    patches; whichever node published the mesh before hands publication to the
    final couple, so the mesh state the run registers is the coupled one and
    not the mesh as it stood one step earlier. With no pairs authored the
    chain is byte-for-byte the chain it always was.
    """
    if not couples:
        return nodes
    extended = list(nodes)
    previous = extended[-1].node_id
    added: list[ExecutionNode] = []
    for index, (owner, neighbour) in enumerate(couples):
        node_id = ('createNonConformalCouples' if not index
                   else f'createNonConformalCouples{index}')
        added.append(ExecutionNode(
            node_id,
            ('createNonConformalCouples', '-case', str(case), owner, neighbour),
            case, (previous,), mutates_mesh=True))
        previous = node_id
    for position, node in enumerate(extended):
        if node.publishes_mesh:
            extended[position] = replace(node, publishes_mesh=False)
    added[-1] = replace(added[-1], publishes_mesh=True)
    return tuple(extended) + tuple(added)


def openfoam_meshing_dag(case_path: str | Path, allocation: ResourceAllocation,
                         *, mpirun: str = 'mpirun',
                         mpi_options: tuple[str, ...] = (),
                         split_at_snap: bool = False,
                         interface_couples: tuple[tuple[str, str], ...] = (),
                         check_profile=None,
                         check_request=None,
                         ) -> ExecutionDag:
    """The snappy pipeline, optionally decomposed so it can pause after snapping.

    ``split_at_snap`` replaces the single ``snappyHexMesh`` node with the three
    phases as separate invocations, and in MPI inserts a reconstruction so the
    snapped boundary exists in the case root where GF1 and the snap checkpoint
    can reach it (Plan 23 §8.2).

    This is safe because OpenFOAM 13 overwrites ``constant/polyMesh`` in place
    by default -- ``-overwrite`` is deprecated and ``-noOverwrite`` opts out --
    so each phase picks up where the last left off with no flag changes.
    MEASURED in ``plans/evidence/plan23-wp7a-spike``: combined and split routes
    both produce 22,178 cells and pass ``checkMesh``, serial and MPI alike, and
    the mid-run reconstruction yields exactly the serial snapped boundary.

    The ``checkMesh`` node is built by
    :func:`core.quality.checkmesh_service.checkmesh_command`, the same builder
    ``mesh.check`` uses (Plan 30 F-04). It used to run bare, so a pipeline run
    wrote a report with no cell sets and no per-cell fields over whatever a
    full Mesh Check had produced. ``check_profile`` is the probed capability
    of the configured utility; without one the verified Foundation-13 baseline
    flags apply.

    ``check_request`` is what the *project* asked checkMesh to do -- its
    reporting thresholds, whether to write the problem faces as a surface,
    whether to judge against the mesher's own limits (Plan 31). It used to be
    unreachable from here, so a threshold set on the QA page changed the
    verdict of a manual Mesh Check and not the verdict of the pipeline's own
    check node, while both wrote to the same report file. Without one, the
    defaults are OpenFOAM 13's and the command line is the one this DAG has
    always built.

    ``mpi_options`` is what the probed runtime needs in front of ``-np``.
    Plan 31 CP-07 measured that on a root-user runtime Open MPI aborts with
    "mpirun has detected an attempt to run as root" and starts nothing, so
    without ``--allow-run-as-root`` there the requested ranks and the running
    ranks disagree by all of them. It stays empty for an ordinary user, where
    the same flag is itself an error.
    """
    from foammesh.core.quality.checkmesh_service import checkmesh_command

    case = Path(case_path).resolve()
    ranks = allocation.effective_ranks

    def check_argv(parallel: bool) -> tuple[str, ...]:
        return checkmesh_command(
            case, profile=check_profile, request=check_request,
            mpirun=mpirun, mpi_options=tuple(mpi_options),
            ranks=ranks if parallel else 1)

    def dag(*nodes: ExecutionNode) -> ExecutionDag:
        return ExecutionDag(
            _with_interface_couples(tuple(nodes), case, tuple(interface_couples)))

    serial = (
        ExecutionNode('blockMesh', ('blockMesh', '-case', str(case)), case,
                      mutates_mesh=True),
        ExecutionNode('surfaceFeatures', ('surfaceFeatures', '-case', str(case)),
                      case, ('blockMesh',)),
    )
    if allocation.effective_ranks == 1:
        if not split_at_snap:
            return dag(*serial, *(
                ExecutionNode(
                    'snappyHexMesh',
                    ('snappyHexMesh', '-case', str(case)),
                    case, ('surfaceFeatures',), mutates_mesh=True),
                ExecutionNode(
                    'checkMesh', check_argv(False),
                    case, ('snappyHexMesh',), publishes_mesh=True),
            ))
        snappy = ('snappyHexMesh', '-case', str(case))
        return dag(*serial, *(
            ExecutionNode('castellation', snappy, case, ('surfaceFeatures',),
                          mutates_mesh=True),
            # The pause. `constant/polyMesh` now holds the snapped boundary,
            # and nothing has overwritten it yet.
            ExecutionNode('snap', snappy, case, ('castellation',),
                          mutates_mesh=True, pauses_after=True),
            ExecutionNode('layers', snappy, case, ('snap',), mutates_mesh=True),
            ExecutionNode('checkMesh', check_argv(False),
                          case, ('layers',), publishes_mesh=True),
        ))

    parallel_snappy = (mpirun, *mpi_options, '-np', str(ranks),
                       'snappyHexMesh', '-parallel', '-case', str(case))
    decompose = ExecutionNode(
        'decomposePar', ('decomposePar', '-case', str(case), '-force'),
        case, ('surfaceFeatures',), mutates_mesh=True)
    if not split_at_snap:
        return dag(*serial, *(
            decompose,
            ExecutionNode('snappyHexMesh', parallel_snappy, case,
                          ('decomposePar',), mutates_mesh=True),
            ExecutionNode('checkMesh', check_argv(True),
                          case, ('snappyHexMesh',)),
            ExecutionNode(
                'reconstructPar',
                ('reconstructPar', '-constant', '-case', str(case)),
                case, ('checkMesh',), mutates_mesh=True, publishes_mesh=True),
        ))
    return dag(*serial, *(
        decompose,
        ExecutionNode('castellation', parallel_snappy, case, ('decomposePar',),
                      mutates_mesh=True),
        ExecutionNode('snap', parallel_snappy, case, ('castellation',),
                      mutates_mesh=True),
        # GF1 measures a reconstructed boundary, so MPI has to reconstruct
        # mid-run. This writes the case-root mesh and leaves `processor*/`
        # untouched, so the layers phase continues on the decomposed meshes.
        ExecutionNode('reconstructSnap',
                      ('reconstructPar', '-constant', '-case', str(case)),
                      case, ('snap',), pauses_after=True),
        ExecutionNode('layers', parallel_snappy, case, ('reconstructSnap',),
                      mutates_mesh=True),
        ExecutionNode('checkMesh', check_argv(True), case, ('layers',)),
        ExecutionNode(
            'reconstructPar',
            ('reconstructPar', '-constant', '-case', str(case)),
            case, ('checkMesh',), mutates_mesh=True, publishes_mesh=True),
    ))
