"""Versioned, deterministic geometry-readiness classification."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from .checks import Finding, Severity


#: 2 (DP-684): a planar section's outline is not fatal to Gmsh.
RULES_VERSION = 2


class ReadinessState(str, Enum):
    READY = 'ready'
    REPAIRABLE = 'repairable'
    WRAP_RECOMMENDED = 'wrap_recommended'
    BLOCKED = 'blocked'


@dataclass(frozen=True)
class ReadinessReport:
    state: ReadinessState
    rules_version: int
    reasons: tuple[str, ...]
    #: DP-114. Which mesher this verdict answers for, or ``None`` when it was
    #: graded without one. A reader who does not know which engine was asked
    #: cannot tell a tolerated fault from a fatal one.
    engine: str | None = None
    #: DP-125. The finding kinds that are fatal to :attr:`engine`, empty when
    #: the verdict rests on anything else. `blocked` is overridable on a
    #: written reason, which is right for a judgement call and wrong for a
    #: fact: nothing the user knows makes an open surface bound a volume.
    #: The Prepare step reads this to tell the two apart.
    engine_fatal: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            'state': self.state.value,
            'rules_version': self.rules_version,
            'reasons': list(self.reasons),
            'engine': self.engine,
            'engine_fatal': list(self.engine_fatal),
        }


#: DP-114. Faults that only one engine can survive. MEASURED on `cyclone`:
#: 54 free edges in 2 loops, graded `repairable`, accepted as-is with no
#: acknowledgement asked for, meshed by snappy to 107,975 cells and refused by
#: Gmsh with `shell ... is not closed: 54 edge(s) belong to one triangle
#: instead of two`. One verdict answered for two engines that disagree about
#: it, so it was necessarily wrong for one of them -- here in the expensive
#: direction, at the end of a meshing run.
FATAL_BY_ENGINE: dict[str, frozenset[str]] = {
    # DP-366 adds `surface_not_closed`: `open_edges` counts only the edges
    # with one triangle, and an edge with three leaves the surface just as
    # unable to bound a volume. `annulus_shell` refused mid-run for exactly
    # that shape while the report called its boundary closed.
    'gmsh': frozenset({'open_edges', 'surface_not_closed'}),
}


#: DP-684. What a planar section explains away for Gmsh: its outline.
SECTION_EXPLAINS: frozenset[str] = frozenset(
    {'open_edges', 'surface_not_closed'})


#: DP-396. What a proven conjugate assembly explains away. Several closed
#: bodies that touch tessellate to a surface that is *necessarily* not closed
#: where they meet: the shared face is tessellated once per body, so its edges
#: are used four times and its triangles appear twice. These three checks read
#: the merged surface and so report exactly that, and nothing else --
#: MEASURED on ``multiregion/jacketed_pipe.step``, 180 / 180 / 90 against two
#: bodies that each close with every edge used exactly twice.
CONJUGATE_EXPLAINS: frozenset[str] = frozenset(
    {'surface_not_closed', 'non_manifold_edges', 'duplicate_triangles'})


def conjugate_assembly(findings: list[Finding]) -> Finding | None:
    """The finding that proves the surface is a union of closed regions.

    Three things have to hold at once, and each is doing work. The check must
    have been *evaluated* -- which ``region_shells`` only reports when the
    regions between them account for every triangle, so nothing is explained
    away by regions that cover part of the surface. There must be two or more
    regions, since one region that closes is just a closed surface and needs
    no special case. And no region may be open: with every region closed and
    every triangle in one, an edge used other than twice in the merged surface
    is necessarily an edge two regions share, because within a region every
    edge is used exactly twice. That is why this licenses ignoring the merged
    verdict rather than merely softening it.
    """
    for finding in findings:
        if (finding.kind == 'open_region_shells' and finding.evaluated
                and finding.count == 0
                and int((finding.details or {}).get('regions', 0)) >= 2):
            return finding
    return None


#: DP-434. What the three explained findings say once the assembly is proven.
#: Each keeps its count -- the triangles really are written twice -- and says
#: what wrote them, so an operator reading the panel is told the same thing
#: the badge already says.
_CONJUGATE_WORDING = {
    'surface_not_closed': (
        'the surface is a union of closed bodies and is open only where they '
        'meet'),
    'non_manifold_edges': (
        'these edges are the rims of the faces two bodies share, used twice '
        'from each side'),
    'duplicate_triangles': (
        'these triangles are the shared faces themselves, tessellated once '
        'per body'),
}


def explain_conjugate(findings: list[Finding]) -> list[Finding]:
    """Re-grade what a proven conjugate assembly accounts for.

    DP-434. ``classify`` has dropped :data:`CONJUGATE_EXPLAINS` from the
    verdict since DP-396, so the state on a correct multi-region STEP reads
    ``ready``. The findings themselves went on unchanged: severity ``error``,
    and ``repairable_by`` naming ``tess.fix_nonmanifold`` and ``tess.dedupe``.
    MEASURED across the campaign: six STEP models, every one of them graded
    three errors against the very interface it was built to carry, and the
    two cures on offer would each delete one copy of the shared surface and
    take the conformality with it.

    So the same proof that licenses dropping them from the verdict re-grades
    them here: severity ``info``, no repair offered, and the message says what
    the count is of. The count itself is left alone -- it is a true
    measurement of the merged surface -- and ``details`` records which finding
    explained it, so nothing is quietly rewritten.
    """
    if conjugate_assembly(findings) is None:
        return list(findings)
    out = []
    for finding in findings:
        wording = _CONJUGATE_WORDING.get(finding.kind)
        if wording is None or finding.count <= 0:
            out.append(finding)
            continue
        out.append(replace(
            finding,
            severity=Severity.INFO,
            message=f'{finding.message} Explained: {wording}.',
            repairable_by=(),
            details={**(finding.details or {}),
                     'explained_by': 'open_region_shells'}))
    return out


def classify(findings: list[Finding], *, cell_count: int,
             model_diagonal: float | None = None,
             large_hole_fraction: float = 0.5,
             engine: str | None = None) -> ReadinessReport:
    """Classify findings without I/O or capability-dependent guesses.

    ``model_diagonal`` enables hole-size routing (Appendix A §2): a geometry
    with multiple openings each spanning a large fraction of the model is
    routed to wrapping, since exact hole-fill cannot reliably close it. A
    single planar opening stays ``repairable``.

    ``engine`` is the mesher the verdict is being asked for (DP-114). Some
    faults one engine tolerates are fatal to the other, so a verdict graded
    without an engine is advisory for both, and says so by carrying ``None``.

    A proven conjugate assembly (DP-396) drops :data:`CONJUGATE_EXPLAINS`
    from the grading entirely rather than downgrading it, because those three
    findings are measurements of the merged surface and the measurement that
    replaces them -- every region closed, every triangle in a region -- is
    strictly stronger: it says what each volume the mesher will fill actually
    is. A region that does *not* close still blocks, and names itself.
    """
    engine = str(engine).lower() if engine else None
    active = [finding for finding in findings if finding.count > 0]
    conjugate = conjugate_assembly(findings)
    if conjugate is not None:
        active = [finding for finding in active
                  if finding.kind not in CONJUGATE_EXPLAINS]
    if engine == 'gmsh' and any(finding.kind == 'planar_section'
                                for finding in active):
        # DP-684. A planar section bounds no volume by design, and Gmsh
        # meshes it as one (DP-673). Its outline is where the section ends,
        # not a hole to fill, so neither open-surface finding counts against
        # it -- as fatal or as a repair to offer.
        active = [finding for finding in active
                  if finding.kind not in SECTION_EXPLAINS]
    if cell_count <= 0:
        return ReadinessReport(
            ReadinessState.BLOCKED, RULES_VERSION,
            ('Geometry contains no usable surface cells.',), engine)
    fatal = [finding for finding in active
             if finding.kind in FATAL_BY_ENGINE.get(engine or '', ())]
    if fatal:
        return ReadinessReport(
            ReadinessState.BLOCKED, RULES_VERSION,
            tuple(' '.join(part for part in (
                finding.message,
                (finding.engine_impact or {}).get(engine, '')) if part)
                for finding in fatal), engine,
            tuple(sorted({finding.kind for finding in fatal})))
    # Only kinds that a check actually emits; overlapping/intersecting shells are
    # the exact-repair-is-unreliable signals.
    wrap_kinds = {'self_intersections', 'overlapping_shells'}
    wrapping = [finding for finding in active if finding.kind in wrap_kinds]
    if model_diagonal and model_diagonal > 0:
        for finding in active:
            if finding.kind != 'open_edges':
                continue
            loops = (finding.details or {}).get('loops', ())
            large = [loop for loop in loops
                     if loop.get('bbox_diagonal', 0) > large_hole_fraction * model_diagonal]
            if len(large) >= 2:
                wrapping.append(finding)
    if wrapping:
        return ReadinessReport(
            ReadinessState.WRAP_RECOMMENDED, RULES_VERSION,
            tuple(finding.message for finding in wrapping), engine)
    errors = [finding for finding in active if finding.severity is Severity.ERROR]
    if errors and any(not finding.repairable_by for finding in errors):
        return ReadinessReport(
            ReadinessState.BLOCKED, RULES_VERSION,
            tuple(finding.message for finding in errors), engine)
    repairable = [finding for finding in active if finding.repairable_by]
    if repairable:
        return ReadinessReport(
            ReadinessState.REPAIRABLE, RULES_VERSION,
            tuple(finding.message for finding in repairable), engine)
    return ReadinessReport(ReadinessState.READY, RULES_VERSION, (), engine)
