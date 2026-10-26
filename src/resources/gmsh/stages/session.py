"""The resident session's side of the shared stages (Plan 48 RR, DP-4009).

The batch runner (``runner_v1.GmshRun``) and the resident session apply the
farfield, the explicit healing pass, the scopes, the curve and volume
controls, the structuring options, the boundary layers, the periodic pairs,
the recombination marks and automatic structuring with the *same* functions:
the mixins of :class:`stages.StageRun`. This class is how the session reaches
them. It holds one ``StageRun`` -- the run state the batch runner holds for a
whole job -- on the session's own model, and plays its stage methods when the
controller asks, in the order ``GmshRun.execute`` plays them.

A stage that changes the model cannot be taken back, so the controller does
not play one twice: a changed input is a re-import (``stage_import`` starts a
new run on an empty model), exactly as an entity control already is.
"""
from __future__ import annotations

from pathlib import Path

#: Stage name -> the ``StageRun`` methods it plays, in the batch order. The
#: names are the controller's; the methods are the batch runner's own.
STAGE_CALLS = {
    'structuring_options': ('apply_structuring_options',),
    'curve_controls': ('apply_curve_controls',),
    'volume_controls': ('apply_volume_controls',),
    'boundary_surfaces': ('record_boundary_surfaces',),
    'layers': ('apply_boundary_layers',),
    'periodic': ('apply_periodic',),
    'groups': ('tag_physical_groups',),
    'recombination': ('apply_recombination',),
    'automatic_structuring': ('apply_automatic_structuring',),
}

#: ``GmshRun.execute`` order of the stages above, for a caller that plays more
#: than one at once.
BATCH_ORDER = ('structuring_options', 'curve_controls', 'volume_controls',
               'boundary_surfaces', 'layers', 'periodic', 'groups',
               'recombination', 'automatic_structuring')

TESSELLATED = ('.stl', '.obj', '.ply')

#: Logged operations that make or change a mesh. A rewind (``stage_unapply``)
#: drops the ones logged after the first stage it takes back: they meshed a
#: model those stages had changed, and the next generate meshes again.
_MESH_OPS = ('mesh.generate', 'session.regularize', 'session.remove_elements',
             'session.delete_entity_mesh', 'session.reclassify_subset',
             'session.quad_layout', 'session.cvt_apply')


def _stage_names(entry) -> list:
    args = entry.get('args') or []
    if args:
        return list(args[0] or ())
    return list((entry.get('kwargs') or {}).get('names') or ())


def _with_stage_names(entry, names) -> dict:
    if entry.get('args'):
        return dict(entry, args=[list(names), *entry['args'][1:]])
    return dict(entry, kwargs=dict(entry.get('kwargs') or {},
                                   names=list(names)))


class SessionStageOperations:
    """``Session`` operations that play the shared stage functions.

    A base of ``Session`` like the eight ``Session*Mixin`` classes, but named
    without the suffix: the seam test counts those eight by it.
    """

    _stage_run = None
    _stage_background: list = []
    _stage_applied: list = []

    def _stage_new(self, job):
        from . import NullReporter, StageRun

        run = StageRun()
        run.init_stage_state(job, NullReporter())
        run.gmsh = self.gmsh
        return run

    def _stage_new_tessellated(self, job):
        """The run a surface-mesh source takes: the batch runner's own class,
        because the classification, the shell topology and the entity identity
        of an STL/OBJ/PLY (``import_tessellated``) live there (DP-5000)."""
        try:
            from runner_v1 import GmshRun
        except ImportError:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from runner_v1 import GmshRun
        from . import NullReporter

        run = GmshRun(job, NullReporter())
        run.gmsh = self.gmsh
        return run

    def _stage_import_tessellated(self, run, sources, tessellated, healing):
        """``GmshRun.import_geometry``'s tessellated branch, step for step:
        the CAD-mix refusal, the healing note, then ``import_tessellated``
        (feature-angle classification, shells, volumes, entity identity)."""
        if len(tessellated) != len(sources):
            raise ValueError(
                'CAD and tessellated geometry cannot be mixed in one job; '
                'convert the CAD or supply all inputs as surfaces.')
        run.tessellated = True
        if healing.get('healShapes'):
            run.warnings.append(
                'the explicit healing pass was requested but this import '
                'is tessellated, and OCC healing needs CAD. The patch '
                'classification angle is what repairs a surface import.')
            run.ledger.record(
                'gmsh/healing/healShapes', True, False, applied=False,
                note='tessellated import: there is no OCC shape to heal')
        run.import_tessellated(sources, healing)

    def _stages_import(self, job, paths=None):
        """Import the sources of *job* the way the batch run does: the
        explicit healing pass, the duplicate-face fusion, the farfield and the
        scopes, in ``GeometryStage``'s own functions; a surface mesh (STL,
        OBJ, PLY) through the batch runner's ``import_tessellated``."""
        sources = [str(item) for item in (paths or job.get('geometry') or ())]
        if not sources:
            raise ValueError('a stage import needs at least one source')
        tessellated = [item for item in sources
                       if Path(item).suffix.lower() in TESSELLATED]
        self._stage_run = None
        self._stage_background = []
        self._stage_applied = []
        if tessellated:
            run = self._stage_new_tessellated(job)
            healing = run.intent.get('healing') or {}
            self._stage_import_tessellated(run, sources, tessellated, healing)
        else:
            run = self._stage_new(job)
            healing = run.intent.get('healing') or {}
            run.import_cad_sources(sources, healing)
        run.settle_import(healing)
        self._stage_run = run
        return self._stage_report(run)

    def stage_apply(self, names, **_):
        """Play the named stages, in the order given, on the model."""
        run = self._stage_run
        if run is None:
            raise ValueError(
                'there is no stage run: import the geometry with '
                'session.stage_import first')
        unknown = [name for name in names if name not in STAGE_CALLS]
        if unknown:
            raise ValueError('unknown stage ' + ', '.join(map(str, unknown)))
        for name in names:
            for method in STAGE_CALLS[name]:
                getattr(run, method)()
            self._stage_applied.append(name)
            if name == 'volume_controls':
                self._stage_take_background(run)
        return self._stage_report(run)

    def stage_unapply(self, names, **_):
        """Take the named stages back off the model (DP-5710).

        The window's geometry build must run on the model the batch run
        builds on: the import, before the boundary layers and the other late
        stages (``runner_v1.GmshRun.execute`` replays ``geometry_build`` at
        the ``geometry`` hook). A stage cannot be undone in Gmsh, so the
        names are taken out of the logged ``session.stage_apply`` entries
        (with any mesh made after the first of them) and the model is rebuilt
        from the rest of the op log, the way ``script_trim`` does. A rebuild
        that fails is undone by rebuilding from the whole log, and the error
        is raised. This operation is a read for the op log (it is not in
        ``_MUTATING_SESSION``): what it changes is the log itself.
        """
        drop = set(names or ())
        unknown = sorted(str(n) for n in drop if n not in STAGE_CALLS)
        if unknown:
            raise ValueError('unknown stage ' + ', '.join(unknown))
        entries = self._log_entries()
        if not any(e.get('op') == 'session.stage_import' for e in entries):
            raise ValueError(
                'the op log holds no stage import, so the stages cannot be '
                'taken back off the model')
        kept, removed, rewound = [], [], False
        for entry in entries:
            op = entry.get('op')
            if op == 'session.stage_apply':
                listed = _stage_names(entry)
                left = [n for n in listed if n not in drop]
                if left != listed:
                    rewound = True
                    removed.extend(n for n in listed if n in drop)
                    if not left:
                        continue
                    entry = _with_stage_names(entry, left)
            elif rewound and op in _MESH_OPS:
                continue
            kept.append(entry)
        if not removed:
            return dict(self.stage_report(), removed=[], ops=len(entries))
        try:
            self._rebuild(kept)
        except BaseException:
            self._rebuild(entries)
            raise
        self._write_log(kept)
        return dict(self.stage_report(), removed=removed, ops=len(kept))

    def stage_report(self, **_):
        """What the stages have done so far (a read)."""
        run = self._stage_run
        if run is None:
            return {'active': False, 'ledger': [], 'warnings': [],
                    'statistics': {}, 'unresolvedScopes': [], 'applied': []}
        return self._stage_report(run)

    def _stage_take_background(self, run):
        """The per-volume sizes the volume controls made join the background
        the session composes, as they join the batch run's."""
        known = set(self._stage_background)
        added = [tag for tag in run.background_fields if tag not in known]
        if added:
            self._stage_background = [*self._stage_background, *added]
            self._compose_background()

    def _stage_report(self, run):
        return {'active': True, 'ledger': run.ledger.to_list(),
                'warnings': list(run.warnings),
                'statistics': run.statistics,
                'unresolvedScopes': sorted(set(run.unresolved_scopes)),
                'applied': list(self._stage_applied)}
