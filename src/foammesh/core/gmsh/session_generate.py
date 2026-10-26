"""The project's resident Gmsh session, driven by the Generate actions.

Plan 41 GN12.  One :class:`GenerateController` belongs to an open project.  It
starts (or claims) the resident session on first use, imports the prepared
geometry into it once, applies the stored Gmsh option overrides, and plays
:mod:`generate_actions` on it, recording the stage timeline.  Nothing is
re-imported between actions: 1D, then 2D, inspect, then 3D is three calls on
the same model.

The controller is plain Python (no Qt) so a worker thread can run it and the
page only shows what it reports.
"""
from __future__ import annotations

from pathlib import Path
import threading
from typing import Callable

from . import generate_actions as actions
from . import option_catalogue as catalogue
from .session import GmshSession, claim_idle_session


class GenerateController:
    """Owns the session of one project and the timeline of its stages."""

    def __init__(self, case_path, *, geometry_paths: Callable[[], tuple],
                 overrides: Callable[[], str] | None = None,
                 fields: Callable[[], list] | None = None,
                 point_sizes: Callable[[], list] | None = None,
                 entities: Callable[[], list] | None = None,
                 automatic: Callable[[], dict] | None = None,
                 profile=None, session_factory: Callable | None = None,
                 on_stage: Callable | None = None,
                 intent: Callable[[], dict] | None = None,
                 job: Callable[[], dict] | None = None):
        self.case_path = Path(case_path)
        self._geometry_paths = geometry_paths
        self._overrides = overrides
        self._fields = fields
        self._fields_key = None
        self._point_sizes = point_sizes
        self._point_sizes_key = None
        self._entities = entities
        self._entities_key = None
        self._automatic = automatic
        self._automatic_key = None
        self._automatic_on = False
        self._reimporting = False
        self._profile = profile
        self._session_factory = session_factory
        self._on_stage = on_stage
        #: Plan 48 GM3. The job intent (as the batch run reads it) of the
        #: page controls; ``None`` keeps the controller to the Options text.
        self._intent = intent
        #: Plan 48 RR (DP-4009). The job the batch run would be given (its
        #: scope maps, names and pairs), which the shared stage functions
        #: read in the session. ``page_intent_reader`` hands one on its
        #: reader; without either the stages are not applied.
        self._job = job if job is not None else getattr(intent, 'job', None)
        self._stage_on = False
        self._stage_digest = None
        self._stage_done: set = set()
        self._ledger_stage: list = []
        self.stage_statistics: dict = {}
        #: DP-5710: the entity rows and geometry of the model just before the
        #: late stages were applied (the model a build and the batch run's
        #: ``geometry_build`` work on), served to the pickers while the late
        #: stages are on the model.
        self._pick_snapshot: dict | None = None
        self._pick_dir: str | None = None
        self._import_key = None
        self._page_key = None
        self._page_names: set = set()
        #: what Gmsh held for an option before the page first wrote it
        self._held_before: dict = {}
        self._overrides_key = None
        self._override_names: set = set()
        self._ledger_import: list = []
        self._ledger_page: list = []
        self._ledger_overrides: list = []
        self._session: GmshSession | None = None
        self._imported: tuple = ()
        self._lock = threading.RLock()
        self._builder = None
        self._opened: list = []
        #: SG15A: key of the accepted run's mesh the session was loaded with
        self._loadedRunMeshKey = None
        self.timeline = actions.Timeline()
        self.warnings: list[str] = []
        #: Plan 48 TL: the project's time limits (seconds, 0 = none) for the
        #: calls Gmsh 4.15.2 can leave hanging; a kind not here has its
        #: default (``generate_actions.DEFAULT_LIMITS``).
        self.limits: dict = {}

    # -- time limits (Plan 48 TL) --------------------------------------- #

    def _limit_table(self) -> dict:
        return self.__dict__.setdefault('limits', {})

    def time_limits(self) -> dict:
        """Every kind's limit in seconds, as the next call would use it."""
        table = self._limit_table()
        return {kind: actions.limit_for(kind, table)
                for kind in actions.DEFAULT_LIMITS}

    def set_time_limit(self, kind: str, seconds) -> dict:
        """Set the limit of one kind of call; ``None`` restores its default."""
        if kind not in actions.DEFAULT_LIMITS:
            raise actions.ActionRefusal(
                f'"{kind}" has no time limit; they are: '
                + ', '.join(actions.DEFAULT_LIMITS))
        if seconds is None:
            self._limit_table().pop(kind, None)
            return self.time_limits()
        try:
            value = float(seconds)
        except (TypeError, ValueError):
            raise actions.ActionRefusal(
                'a time limit is a number of seconds, 0 for none') from None
        if value < 0 or value != value:
            raise actions.ActionRefusal(
                'a time limit is a number of seconds, 0 for none')
        self._limit_table()[kind] = value
        return self.time_limits()

    def _limit(self, kind: str, per_call=None) -> float:
        if per_call is not None:
            try:
                value = float(per_call)
            except (TypeError, ValueError):
                raise actions.ActionRefusal(
                    'the time limit is a number of seconds, 0 for none'
                    ) from None
            if value < 0:
                raise actions.ActionRefusal(
                    'the time limit is a number of seconds, 0 for none')
            return value
        return actions.limit_for(kind, self._limit_table())

    # -- the session ---------------------------------------------------- #

    @property
    def session(self) -> GmshSession | None:
        return self._session

    def profile(self):
        if self._profile is None:
            from .launch_profiles import configured_profiles
            self._profile = configured_profiles({})[0]
        return self._profile

    def ensure_session(self) -> GmshSession:
        with self._lock:
            if self._session is not None and self._session.alive:
                return self._session
            if self._session_factory is not None:
                session = self._session_factory(self.case_path)
            else:
                session = claim_idle_session(self.profile(), self.case_path)
                if session is None:
                    session = GmshSession(self.profile(), self.case_path)
                    session.start()
            self._session = session
            try:
                session.time_limits = self._limit_table()
            except AttributeError:
                pass
            self._imported = ()
            self._opened = []
            return session

    def ensure_geometry(self, *, late: bool = True) -> dict:
        """Import the prepared geometry once; later calls change nothing.

        Plan 48 GM3. The importer options are applied before the import and
        the page controls after it, in the order the batch run uses; a change
        to an importer option after the import re-imports.
        """
        with self._lock:
            session = self.ensure_session()
            if self._opened:
                # a file the user opened (GN5) is the model: it is never
                # replaced by the prepared geometry
                self.apply_page_controls()
                self.apply_overrides_if_changed()
                self.apply_fields()
                return {'imported': False, 'paths': (), 'opened': True}
            paths = tuple(str(Path(p)) for p in self._geometry_paths())
            if not paths and self._builder is not None \
                    and self._builder.steps:
                # a model built here, from primitives, is the geometry
                self.apply_page_controls()
                self.apply_overrides_if_changed()
                self.apply_fields()
                return {'imported': False, 'paths': (), 'built': True}
            if not paths:
                raise actions.ActionRefusal(
                    'there is no prepared geometry to mesh; prepare the '
                    'geometry first')
            if paths == self._imported:
                if (not self._reimporting and self._importer_changed(paths)
                        and self._reimport('an importer option changed',
                                           late=late)):
                    return {'imported': True, 'paths': paths,
                            'reimported': True}
                if (not self._reimporting and self._stage_on
                        and self._stage_changed(paths)
                        and self._reimport('a stage input changed',
                                           late=late)):
                    return {'imported': True, 'paths': paths,
                            'reimported': True}
                self.apply_page_controls()
                self.apply_overrides_if_changed()
                self.apply_fields()
                if late:
                    self.apply_late_stages()
                return {'imported': False, 'paths': paths}
            if self._imported:
                session.call('clear')
                session.call('model.add', 'foammesh')
            self.apply_importer_options(paths)
            self._forget_stages()
            self._import_paths(paths)
            self._replay_build()
            self._imported = paths
            self._loadedRunMeshKey = None
            self._fields_key = None
            self._automatic_key = None
            self._point_sizes_key = None
            self._entities_key = None
            self._page_key = None
            self._overrides_key = None
            self.apply_page_controls()
            self.apply_overrides_if_changed()
            self.apply_fields()
            if late:
                self.apply_late_stages()
            return {'imported': True, 'paths': paths}

    # -- Plan 48 RR (DP-4009): the shared stages --------------------------- #

    #: The stages that follow the fields, in the batch run's order. The
    #: physical groups are named before any mesh pass, as the batch run does:
    #: MEASURED on the sphere farfield case, the volume pass of a model that
    #: has its groups meshes 164,418 tetrahedra against 164,346 for the same
    #: model named afterwards.
    LATE_STAGES = ('boundary_surfaces', 'layers', 'periodic', 'groups',
                   'recombination', 'automatic_structuring')
    TESSELLATED = ('.stl', '.obj', '.ply')

    def _stage_job(self, paths):
        """The job the stage functions read, or ``None`` when this model does
        not take them (no job wired, or a model opened or built here). A
        surface-mesh source (STL, OBJ, PLY) takes them like a CAD one: the
        session gives it the entity identity the batch run gives it
        (DP-5000)."""
        if self._job is None or self._opened:
            return None
        from . import execution
        try:
            return self._job()
        except execution.ExecutionError as error:
            raise actions.ActionRefusal(str(error)) from error
        except Exception as error:                       # noqa: BLE001
            self.warnings.append(
                f'the job the stages read could not be built: {error}')
            return None

    def _replay_build(self) -> None:
        """DP-5724. Build the project's geometry build on a fresh import.

        A changed stage input (the roles a 2D-case build declares go into the
        job's ``surfaceCategories``), a changed importer option or a reopened
        case imports the prepared geometry again; without this the session
        held the pipe again while the build panel listed the built slab, the
        group table showed the pipe's groups and the declared roles were
        dropped as groups that had gone. The steps are the op log's, the
        ones the batch run replays at its ``geometry`` hook.

        DP-5738. They are logged again, marked ``again`` with the step's
        place in the build: a model rebuilt from the log (the late-stage
        rewind, crash recovery, a checkpoint) has the build after the import,
        and the build list skips the marked copies, so no step is made
        twice."""
        from .execution import geometry_build_steps

        steps = geometry_build_steps(self.case_path)
        send = getattr(self._session, '_send', None)
        if not steps or send is None:
            return
        for index, step in enumerate(steps, 1):
            response = send('session.build', [step['kind']],
                            dict(step['params']), None, again=index - 1)
            if not response.get('ok'):
                raise actions.ActionRefusal(
                    'the geometry build could not be made again on the '
                    're-imported model: step %d (%s): %s' % (
                        index, step['kind'], response.get('error')))
        self._name_built_groups()

    def _name_built_groups(self) -> None:
        """DP-5728. Give the replayed build's groups the names the person
        gave them in the physical-group table.

        The replay makes the groups a preset declares under the preset's
        names (``front``, ``back``). A rename in the group table is followed
        into the role rows, and that changes ``surfaceCategories``, a stage
        input, so the next read imports again and replays: the session held
        ``front`` again and the next follow put the row back to it (live
        walk wg_roles, run 6). The role rows are the record of the rename;
        each group holding exactly a row's surfaces takes the row's name,
        under its own tag, logged marked ``again`` (DP-5738): a rebuild from
        the log names them as here, and the build list skips them."""
        from . import geometry_build

        rows = [r for r in geometry_build.load_roles(self.case_path)
                if r.get('surfaces') and int(r.get('dim', 2)) == 2]
        send = getattr(self._session, '_send', None)
        if not rows or send is None:
            return
        response = send('session.physical_groups', [], {}, None)
        if not response.get('ok'):
            return
        groups = (response.get('result') or {}).get('groups') or ()
        taken = {(int(g['dim']), str(g['name'])) for g in groups}
        for row in rows:
            held = {int(t) for t in row['surfaces']}
            name = str(row['name'])
            group = next((g for g in groups if int(g['dim']) == 2
                          and {int(t) for t in g['entities']} == held), None)
            if group is None or group['name'] == name or (2, name) in taken:
                continue
            tag = int(group['tag'])
            send('model.removePhysicalGroups', [[[2, tag]]], {}, None,
                 again='names')
            send('model.addPhysicalGroup', [2, sorted(held)],
                 {'tag': tag, 'name': name}, None, again='names')
            taken.discard((2, str(group['name'])))
            taken.add((2, name))

    def _stage_changed(self, paths) -> bool:
        from . import execution

        job = self._stage_job(paths)
        if job is None:
            return False
        return repr(execution.stage_inputs(job)) != self._stage_digest

    def _forget_stages(self) -> None:
        self._stage_on = False
        self._stage_digest = None
        self._stage_done = set()
        self._ledger_stage = []
        self._pick_snapshot = None

    def _import_paths(self, paths) -> None:
        """Bring the geometry into the model: through the shared stage
        functions when the job is known, plainly otherwise."""
        session = self._session
        job = self._stage_job(paths)
        if job is None:
            # The importer options were set before this import by
            # ensure_geometry (apply_importer_options, then apply_overrides
            # for the rest once the geometry is in).
            for path in paths:
                session.import_geometry(path)
            return
        from . import execution
        runtime = [str(session.runtime_path(path)) for path in paths]
        try:
            report = session.call('session.stage_import', job, runtime)
        except Exception as error:                       # noqa: BLE001
            self._stage_failed(error)
        self._stage_on = True
        self._stage_digest = repr(execution.stage_inputs(job))
        self._stage_done = set()
        self._take_stage_report(report)

    def _take_stage_report(self, report) -> None:
        report = report or {}
        self._ledger_stage = list(report.get('ledger') or ())
        self.stage_statistics = dict(report.get('statistics') or {})
        for text in report.get('warnings') or ():
            if text not in self.warnings:
                self.warnings.append(text)

    def _stage_failed(self, error) -> None:
        """A stage the batch run would also refuse. The model holds a
        half-made import, so it is emptied and the next action starts again."""
        try:
            self._session.call('clear')
            self._session.call('model.add', 'foammesh')
        except Exception:                                # noqa: BLE001
            pass
        self._imported = ()
        self._forget_stages()
        self._fields_key = None
        self._automatic_key = None
        self._point_sizes_key = None
        self._entities_key = None
        self._page_key = None
        self._overrides_key = None
        text = str(error)
        if ': ' in text and text.split(': ', 1)[0].isidentifier():
            text = text.split(': ', 1)[1]
        raise actions.ActionRefusal(text) from error

    def _apply_stages(self, names) -> bool:
        """Play the stages in *names* that have not run on this model."""
        if not self._stage_on or self._session is None:
            return False
        todo = [name for name in names if name not in self._stage_done]
        if not todo:
            return False
        try:
            report = self._session.call('session.stage_apply', todo)
        except Exception as error:                       # noqa: BLE001
            self._stage_failed(error)
        self._stage_done.update(todo)
        self._take_stage_report(report)
        return True

    def apply_late_stages(self) -> bool:
        """The stages that follow the fields: the boundary layers, the
        periodic pairs, the recombination marks and automatic structuring.

        Then the Options-panel values are sent once more, because the batch
        run applies them after every stage ("last, so a value the user chose
        is the one Gmsh meshes with").
        """
        if (self._stage_on and self._session is not None
                and not self._stage_done & set(self.LATE_STAGES)):
            self._take_pick_snapshot()
        if not self._apply_stages(self.LATE_STAGES):
            return False
        self.apply_overrides(geometry=False)
        return True

    def _take_pick_snapshot(self) -> None:
        """DP-5710. Keep what a pick reads (the entity rows and
        ``geometry.vtp``) of the model before the late stages change it.

        The batch run makes the build steps and every pick-based control (the
        scopes, the curve, point, volume and entity controls) on the imported
        tags, before the boundary layers; the late stages add entities (the
        layer volumes) and can move the next OpenCASCADE tag, so the pickers
        must not offer the model the late stages leave.
        """
        from . import session_entities as entities
        import tempfile

        self._pick_snapshot = None
        try:
            if self._pick_dir is None:
                self._pick_dir = tempfile.mkdtemp(prefix='foammesh-picks-')
            written = self._session.handover(self._pick_dir, mesh=False,
                                             geometry=True)
            rows = entities.entity_rows(self._session)
        except Exception as error:                       # noqa: BLE001
            self.warnings.append(
                'the geometry the pickers show could not be kept before the '
                f'boundary layers and the other late stages: {error}')
            return
        self._pick_snapshot = {
            'rows': list(rows),
            'vtp': (written.get('geometry') or {}).get('host_path')}

    def tag_groups(self) -> dict:
        """Name the boundary surfaces and the volumes as physical groups the
        way the batch run does (``TaggingStage``), once per import.

        Not part of ``ensure_geometry``: a session mesh that is only
        inspected is not named, and the shared-name editor (GN4) owns the
        groups of a model the user edits. An export that wants the batch
        run's names asks for them here. Refused for a model the stages did
        not build (opened, built here, or a surface-mesh import).
        """
        with self._lock:
            self.ensure_geometry()
            if not self._stage_on:
                raise actions.ActionRefusal(
                    'the physical groups of the batch run are made for the '
                    'prepared CAD; this model was opened or built here, or is '
                    'a surface mesh')
            self._apply_stages(('groups',))
            return dict(self.stage_statistics.get('groups') or {})

    # -- Plan 48 GM3: the page controls, as the batch run applies them ------ #

    def page_intent(self) -> dict:
        """The job intent of the page controls, or ``{}`` when none is wired."""
        if self._intent is None:
            return {}
        try:
            intent = self._intent()
        except Exception as error:                       # noqa: BLE001
            self.warnings.append(
                f'the page controls could not be read: {error}')
            return {}
        if hasattr(intent, 'to_dict'):
            intent = intent.to_dict()
        return dict(intent or {})

    def ledger(self) -> list:
        """The requested-against-effective ledger of this session's passes.

        The same lines, in the batch run's shape, for the controls both paths
        apply (``execution.ledger_difference`` compares them).
        """
        return [*self._ledger_import, *self._ledger_page,
                *self._ledger_overrides, *self._ledger_stage]

    def importer_rows(self, paths=None) -> tuple:
        from . import execution

        intent = self.page_intent()
        if not intent:
            return ()
        sources = tuple(paths if paths is not None else self._imported)
        return execution.importer_option_rows(
            intent, tessellated=execution.sources_are_tessellated(sources))

    def _importer_key(self, paths) -> tuple:
        rows = tuple((r.control, r.name, r.value, r.requested)
                     for r in self.importer_rows(paths))
        return (rows, self._geometry_override_key())

    def _geometry_override_key(self) -> tuple:
        text = self._overrides() if self._overrides else ''
        try:
            mapping = catalogue.overrides_from_text(text)
        except catalogue.OptionRefusal:
            return ()
        return tuple(sorted((str(k), repr(v)) for k, v in mapping.items()
                            if str(k).startswith('Geometry.')))

    def _importer_changed(self, paths) -> bool:
        return (self._import_key is not None
                and self._importer_key(paths) != self._import_key)

    def _set_row(self, row, ledger: list) -> None:
        """One option row: write, read back, record (the runner's set_*)."""
        from . import execution

        session = self._session
        if row.warning:
            self.warnings.append(row.warning)
        if not row.control:
            return              # a warning only; the ledger has no line for it
        if row.name is None:
            ledger.append(execution.ledger_entry(
                row.control, row.requested, row.effective,
                applied=row.applied, note=row.note))
            return
        if row.readback:
            effective = session.call('option.getNumber', row.name)
            ledger.append(execution.ledger_entry(
                row.control, row.requested, effective,
                applied=row.applied, note=row.note))
            return
        if row.storage == 'string':
            try:
                session.call('option.setString', row.name, str(row.value))
            except Exception:                            # noqa: BLE001
                # DP-60: an IGES reader is built only on first use; build it
                # on an empty model and ask again.
                self._prime_target_unit()
                session.call('option.setString', row.name, str(row.value))
            effective = session.call('option.getString', row.name)
        else:
            session.call('option.setNumber', row.name, float(row.value))
            effective = session.call('option.getNumber', row.name)
        requested = row.requested if row.requested is not None else row.value
        ledger.append(execution.ledger_entry(
            row.control, requested, effective, applied=row.applied,
            note=row.note))

    def _remember(self, name: str) -> None:
        """Keep what Gmsh held for *name* before the first write to it."""
        if name in self._held_before or self._session is None:
            return
        try:
            self._held_before[name] = float(
                self._session.call('option.getNumber', name))
        except Exception:                                # noqa: BLE001
            pass

    def _give_back(self, name: str) -> None:
        """Put an option the page or the Options list let go of back."""
        if name in self._held_before and self._session is not None:
            self._session.call('option.setNumber', name,
                               self._held_before[name])

    def _prime_target_unit(self) -> None:
        session = self._session
        directory = self.case_path / 'foammesh' / 'gmsh'
        directory.mkdir(parents=True, exist_ok=True)
        empty = directory / 'prime.iges'
        empty.write_bytes(b'')
        current = session.call('model.getCurrent')
        session.call('model.add', '__foammesh_occ_unit_prime__')
        try:
            try:
                session.call('model.occ.importShapes',
                             session.runtime_path(empty))
            except Exception:                            # noqa: BLE001
                pass
        finally:
            session.call('model.remove')
            session.call('model.setCurrent', current)

    def apply_importer_options(self, paths) -> list:
        """Importer options, applied BEFORE the import (batch order)."""
        rows = self.importer_rows(paths)
        self._ledger_import = []
        for row in rows:
            self._set_row(row, self._ledger_import)
        text = self._overrides() if self._overrides else ''
        self._apply_override_rows(text, geometry=True)
        self._import_key = self._importer_key(paths)
        return list(self._ledger_import)

    def apply_page_controls(self) -> list:
        """Threads, sizing, algorithms, optimisation, element order, recombine.

        Sent as the batch run sends them, once per change of the page values;
        an option the page stopped writing goes back to Gmsh's own default.
        """
        from . import execution

        intent = self.page_intent()
        if not intent or self._session is None:
            return list(self._ledger_page)
        rows = execution.page_option_rows(intent)
        recombine = bool((intent.get('algorithms') or {}).get('recombine'))
        key = (tuple((r.control, r.name, repr(r.value), repr(r.requested))
                     for r in rows), recombine)
        if key == self._page_key:
            return list(self._ledger_page)
        if (self._page_key is not None and self._page_key[1] and not recombine
                and self._imported and not self._reimporting):
            # Gmsh cannot take a surface's recombine mark back
            self._page_key = None
            if self._reimport('recombination was switched off'):
                return list(self._ledger_page)
        ledger: list = []
        names = set()
        for row in rows:
            if row.name and not row.readback and row.storage == 'number':
                self._remember(row.name)
            self._set_row(row, ledger)
            if row.name and not row.readback:
                names.add(row.name)
        for name in sorted(self._page_names - names):
            self._give_back(name)
        if recombine and not self._stage_on:
            # (a staged model marks them in the recombination stage, after
            # the layers, as the batch run does)
            surfaces = [int(tag) for _dim, tag in
                        self._session.call('model.getEntities', 2) or ()]
            touched = 0
            for tag in surfaces:
                try:
                    self._session.call('model.mesh.setRecombine', 2, tag)
                    touched += 1
                except Exception as error:               # noqa: BLE001
                    self.warnings.append(
                        f'surface {tag} could not be marked for '
                        f'recombination: {error}')
            ledger.append(execution.ledger_entry(
                'gmsh/algorithms/recombine.surfaces', len(surfaces), touched,
                applied=bool(touched),
                note=f'{touched:,} of {len(surfaces):,} '
                     f'{"surface" if len(surfaces) == 1 else "surfaces"} '
                     'marked for recombination'))
        self._page_names = names
        self._ledger_page = ledger
        self._page_key = key
        return list(ledger)

    def _reimport(self, why: str, *, late: bool = False) -> bool:
        """Start the model again from the prepared geometry; False if it cannot.

        A model opened or built here has no prepared geometry to come back to,
        so the change waits (a warning) until that model is opened again.
        """
        if not self._imported or self._opened or self._session is None:
            self.warnings.append(
                f'{why}, but this model was opened or built here and Gmsh '
                'cannot re-read it from the prepared geometry; open it again '
                'to apply the change')
            return False
        self._reimporting = True
        try:
            self._session.call('clear')
            self._session.call('model.add', 'foammesh')
            self._imported = ()
            self._forget_stages()
            self._fields_key = None
            self._automatic_key = None
            self._point_sizes_key = None
            self._entities_key = None
            self._page_key = None
            self._import_key = None
            self._overrides_key = None
        finally:
            self._reimporting = False
        # DP-5710: the caller's ensure_geometry decides whether the late
        # stages follow; a page control or a pick never applies them.
        self.ensure_geometry(late=late)
        return True

    def clear_model(self) -> dict:
        """File > Clear: the session's model is emptied and the controller
        forgets what it imported or opened (Plan 48 GM13, DP-4410).

        Returns the session's account of the empty model.
        """
        with self._lock:
            state = None
            if self._session is not None and self._session.alive:
                state = self._session.call('session.clear_model')
            self._imported = ()
            self._opened = []
            self._forget_stages()
            self._loadedRunMeshKey = None
            self._fields_key = None
            self._automatic_key = None
            self._point_sizes_key = None
            self._entities_key = None
            self._page_key = None
            self._overrides_key = None
            return state

    def apply_overrides(self, *, geometry: bool | None = None) -> tuple:
        """Send the stored Options-panel values, each checked first.

        ``geometry=True`` sends the ``Geometry.*`` rows only (they configure
        the importer, so they go before the import), ``False`` every other
        row, ``None`` all of them.
        """
        text = self._overrides() if self._overrides else ''
        return self._apply_override_rows(text, geometry=geometry)

    def _apply_override_rows(self, text, *, geometry: bool | None) -> tuple:
        from . import execution

        try:
            rows, warnings = catalogue.resolve_overrides(
                catalogue.overrides_from_text(text))
        except catalogue.OptionRefusal as error:
            self.warnings.append(str(error))
            return ()
        self.warnings.extend(warnings)
        session = self._session
        sent = []
        # the ledger keeps the lines of the half this call does not redo
        if geometry is None:
            self._ledger_overrides = []
        else:
            self._ledger_overrides = [
                e for e in self._ledger_overrides
                if str(e['control']).startswith('gmsh/options/Geometry.')
                != bool(geometry)]
        for name, storage, value in rows:
            if geometry is not None and (
                    name.startswith('Geometry.') != bool(geometry)):
                continue
            control = f'gmsh/options/{name}'
            try:
                if storage == 'color':
                    session.call('option.setColor', name,
                                 *[int(v) for v in value])
                    effective = list(session.call('option.getColor', name))
                    requested = list(value)
                elif storage == 'string':
                    session.call('option.setString', name, value)
                    effective = session.call('option.getString', name)
                    requested = value
                else:
                    self._remember(name)
                    session.call('option.setNumber', name, float(value))
                    effective = session.call('option.getNumber', name)
                    requested = value
                self._ledger_overrides.append(execution.ledger_entry(
                    control, requested, effective))
            except Exception as error:                   # noqa: BLE001
                self._ledger_overrides.append(execution.ledger_entry(
                    control, value, None, applied=False, note=str(error)))
                self.warnings.append(
                    f'option {name} could not be set to {value!r}: {error}')
            sent.append((name, storage, value))
        return tuple(sent)

    def apply_overrides_if_changed(self) -> tuple:
        """The Options-panel values, re-sent when the stored text changed.

        An option the panel no longer lists goes back to Gmsh's own default; a
        ``Geometry.*`` change after the import re-imports (see
        :meth:`ensure_geometry`), the others apply now.
        """
        text = self._overrides() if self._overrides else ''
        if text == self._overrides_key:
            return ()
        first = self._overrides_key is None
        self._overrides_key = text
        sent = self._apply_override_rows(text, geometry=False)
        names = {name for name, _storage, _value in sent}
        if not first:
            for name in sorted(self._override_names - names):
                self._give_back(name)
        self._override_names = names
        return sent

    def sample_field(self, name: str, dim: int = 2,
                     limit: int = 20000) -> dict:
        """Sample one field of the resident session (refused while meshed).

        Plan 48 GM8. The single-field view meshes the model with the field
        alone, so the stored fields are applied first.
        """
        with self._lock:
            self.ensure_geometry()
            self.apply_fields()
            try:
                return self._session.call(
                    'session.field_visualize', name=name, dim=dim,
                    limit=limit)
            except Exception as error:                   # noqa: BLE001
                if 'mesh_exists' in str(error):
                    raise ValueError(
                        'clear the mesh first (Clear mesh): the viewport '
                        'view meshes the model with the field alone'
                    ) from None
                raise

    def apply_fields(self) -> dict:
        """The stored size fields and the sizes at points, on the model."""
        self.apply_automatic_field()
        result = self._apply_fields()
        # The batch order (GmshRun.execute): the structuring option and the
        # curve controls, then the sizes at points -- which outrank the
        # end-point size a curve control gave -- then the volume controls.
        self._apply_stages(('structuring_options', 'curve_controls'))
        self.apply_point_sizes()
        self._apply_stages(('volume_controls',))
        self.apply_entity_controls()
        return result

    def apply_automatic_field(self) -> dict:
        """Plan 41 GN25f. The saved size field as the background field.

        While ``sizeField/useAsBackground`` is on the session holds
        ``MeshSizeFromPoints``, ``MeshSizeFromCurvature`` and
        ``MeshSizeExtendFromBoundary`` at 0 and combines the field with the
        user's own fields; switched off, the three options come back. A field
        that is on and cannot be used refuses the run (it is never ignored).
        """
        if self._automatic is None or self._session is None:
            return {}
        from foammesh.core.sizing.background import AutomaticFieldRefusal
        try:
            wanted = self._automatic()
        except AutomaticFieldRefusal as error:
            raise actions.ActionRefusal(str(error)) from None
        directory = (wanted or {}).get('directory')
        key = (directory, (wanted or {}).get('key'))
        if key == self._automatic_key and (bool(directory) == self._automatic_on):
            return {}
        if directory:
            directory = self._session.runtime_path(directory)
            result = self._session.call('session.size_field',
                                        directory=str(directory),
                                        octree=bool(wanted.get('octree')))
            self._automatic_on = True
        elif self._automatic_on:
            result = self._session.call('session.size_field_off')
            self._automatic_on = False
        else:
            result = {}
        self._automatic_key = key
        return result

    def apply_entity_controls(self) -> dict:
        """Plan 41 GN9. Send the embedded entities and per-entity constraints.

        Gmsh has no call that takes a constraint back, so a list that differs
        from the one already applied makes the model start again from the
        imported geometry (the mesh is cleared with it) before the new list is
        applied. A model opened or built here cannot be re-imported: the change
        is then a warning and applies after the model is opened again.
        A list Gmsh refuses is an ActionRefusal that names the row.
        """
        if (self._entities is None or self._session is None
                or self._reimporting):
            return {}
        from . import entity_controls as entity
        try:
            ops = self._entities()
        except entity.EntityRefusal as error:
            self.warnings.append(str(error))
            return {}
        key = repr(ops)
        if key == self._entities_key:
            return {}
        if not ops and self._entities_key is None:
            return {}
        if self._entities_key is not None:
            if not self._imported:
                self.warnings.append(
                    'the entity controls changed after they were applied; '
                    'Gmsh cannot take a constraint back from a model that '
                    'was opened or built here, so open it again to apply '
                    'the new list')
                return {}
            self._reimporting = True
            try:
                self._session.call('clear')
                self._session.call('model.add', 'foammesh')
                self._imported = ()
                self._forget_stages()
                self._entities_key = None
                self._fields_key = None
                self._automatic_key = None
                self._point_sizes_key = None
                self.ensure_geometry(late=False)
            finally:
                self._reimporting = False
            return self.apply_entity_controls()
        try:
            result = entity.apply_to_session(self._session.call, ops)
        except ValueError as error:
            raise actions.ActionRefusal(str(error)) from error
        self._entities_key = key
        return result

    def apply_point_sizes(self) -> dict:
        """Plan 41 GN7. Send the stored sizes at picked points.

        The session resets the points it sized before and sets the new ones,
        so a row removed on the page stops applying. A refused row is a
        warning and sets nothing.
        """
        if self._point_sizes is None or self._session is None:
            return {}
        from . import point_sizes as points
        try:
            rows = self._point_sizes()
        except points.PointSizeRefusal as error:
            self.warnings.append(str(error))
            return {}
        key = repr(rows)
        if key == self._point_sizes_key:
            return {}
        if not rows and self._point_sizes_key is None:
            return {}
        result = self._session.call('session.point_sizes', rows)
        self._point_sizes_key = key
        for item in (result or {}).get('skipped', ()):
            self.warnings.append(
                f'point size {item["name"]!r} was skipped: {item["reason"]}')
        return result

    def _apply_fields(self) -> dict:
        """Plan 41 GN6. Build the size fields the user made on the model.

        A stored row the catalogue refuses is a warning and builds nothing,
        so the mesh is never made with half a graph.
        """
        if self._fields is None or self._session is None:
            return {}
        from . import field_catalogue as fields
        try:
            nodes = self._fields()
        except fields.FieldRefusal as error:
            self.warnings.append(str(error))
            return {}
        key = repr(nodes)
        if key == self._fields_key:
            return {}
        if not nodes and self._fields_key is None:
            return {}
        result = self._session.call(
            'session.fields', nodes,
            combiner=self._field_combiner())
        self._fields_key = key
        return result

    def _field_combiner(self) -> str:
        """How the background fields combine: the page's choice, as the
        batch run's ``assemble_background_field`` reads it."""
        sizing = self.page_intent().get('sizing') or {}
        return str(sizing.get('fieldCombiner') or 'min').lower()

    def preview_fields(self, dim: int = 2, limit: int = 20000) -> dict:
        """Sizes the background field gives, read where Gmsh asks for them.

        Refused (``ActionRefusal``) while the model has a mesh.
        """
        with self._lock:
            self.ensure_geometry(late=False)
            self.apply_fields()
            try:
                return self._session.call('session.field_preview', dim=dim,
                                          limit=limit)
            except Exception as error:               # noqa: BLE001
                text = str(error)
                if 'mesh_exists' in text:
                    raise actions.ActionRefusal(
                        'clear the mesh first (Clear mesh): the field '
                        'preview meshes the model with the field alone')                         from None
                raise

    # -- the actions ------------------------------------------------------ #

    def run(self, action_id: str, values: dict | None = None, *,
            timeout: float | None = None,
            time_limit: float | None = None) -> actions.Stage:
        """Run one action; the stage is recorded whether it worked or not.

        *time_limit* (Plan 48 TL) overrides, for this call, the limit of the
        Gmsh call that can hang (plugin, cross field, createTopology,
        Reclassify 2D); 0 means none.
        """
        limits = self._limit_table()
        if time_limit is not None:
            kind = actions.ACTION_LIMIT_KIND.get(action_id)
            if kind is not None:
                limits = dict(limits, **{kind: self._limit(kind, time_limit)})
        if action_id == 'quad_layout' and 'timeout' not in (values or {}):
            values = dict(values or {}, timeout=actions.limit_for(
                'cross_field', limits))
        with self._lock:
            if action_id in ('generate1d', 'generate2d', 'generate3d',
                             'generate_all'):
                self.ensure_geometry()
            elif self._session is None or not self._session.alive:
                stage = actions.Stage(action_id, actions.action(
                    action_id).title, ok=False,
                    error='there is no mesh yet; generate one first')
                self.timeline.add(stage)
                self._notify(stage)
                return stage
            stage = actions.run(self._session, action_id, values,
                                timeline=self.timeline, timeout=timeout,
                                limits=limits)
            if stage.ok and action_id in ('generate2d', 'generate3d',
                                          'generate_all'):
                self._refuse_empty_bipartite_recombination(stage)
            if stage.ok and action_id in ('generate3d', 'generate_all'):
                self._explicit_netgen_passes(timeout)
        self._notify(stage)
        return stage

    BIPARTITE_EMPTY = (
        'Mesh.RecombinationAlgorithm 4 (bipartite labelling) left no surface '
        'element on this Gmsh (4.15.2): it reports the quadrangles it made and '
        'then removes the surface mesh. Choose simple, blossom, simple '
        'full-quad or blossom full-quad (0-3) instead.')

    def _refuse_empty_bipartite_recombination(self, stage) -> bool:
        """Plan 48 FIX3 (DP-2964): the batch run's message, for the session.

        Option 4 is allowed (decision D4) but leaves no 2D element on 4.15.2.
        The stage is marked failed with the reason in words about the setting,
        instead of passing with an empty surface mesh.
        """
        try:
            algorithm = int(self._session.call(
                'option.getNumber', 'Mesh.RecombinationAlgorithm') or 0)
            if algorithm != 4:
                return False
            state = self._session.state()
        except Exception:                                # noqa: BLE001
            return False
        if int((state.get('elements') or {}).get('2', 0) or 0) > 0:
            return False
        if not int(state.get('nodes') or 0):
            return False
        stage.ok, stage.error = False, self.BIPARTITE_EMPTY
        return True

    def _explicit_netgen_passes(self, timeout) -> int:
        """The explicit Netgen passes the batch run makes after the 3D pass.

        Plan 48 FIX3 (DP-4008). The batch runner calls
        ``mesh.optimize('Netgen')`` ``quality.netgenPasses`` times after the
        volume pass, when Optimize is on. MEASURED on the elbow: without them
        the step-by-step session held 2,068 tetrahedra where the batch run held
        2,047. They are made here as ``Optimize`` stages, so the timeline shows
        each one, and from the same page value the batch run reads.
        """
        quality = self.page_intent().get('quality') or {}
        passes = int(quality.get('netgenPasses', 0) or 0)
        if passes < 1 or not bool(quality.get('optimize', True)):
            return 0
        done = 0
        for index in range(passes):
            stage = actions.run(
                self._session, 'optimize', {'method': 'Netgen'},
                timeline=self.timeline, measure=index == passes - 1,
                timeout=timeout)
            if not stage.ok:
                self.warnings.append(
                    f'Netgen pass {index + 1} of {passes}: {stage.error}')
                break
            done += 1
        return done

    def _notify(self, stage) -> None:
        if self._on_stage is not None:
            self._on_stage(stage)

    def cancel(self) -> None:
        if self._session is not None:
            self._session.cancel()

    def recover(self) -> None:
        """After a cancel or a crash: a fresh process plus the logged ops."""
        if self._session is not None:
            self._session.rebuild()

    def state(self) -> dict:
        if self._session is None or not self._session.alive:
            return {'nodes': 0, 'elements': {}, 'dims_meshed': []}
        return self._session.state()

    def handover(self, directory, **kwargs) -> dict:
        """The mesh, with quality arrays, for the viewport (``mesh.vtu``)."""
        kwargs.setdefault('surfaces', True)
        kwargs.setdefault('geometry', False)
        with self._lock:
            if self._session is None or not self._session.alive:
                raise actions.ActionRefusal(
                    'there is no mesh yet; generate one first')
            return self._session.handover(directory, quality=True, **kwargs)

    def reference_surface(self, directory, **kwargs) -> dict:
        """Plan 42 SG2 (D13). The open model's own surface, for the deviation
        tool when no geometry was imported in the shared step."""
        with self._lock:
            if self._session is None or not self._session.alive:
                raise actions.ActionRefusal(
                    'there is no Gmsh model open to measure against')
            return self._session.reference_surface(directory, **kwargs)

    def handover_path(self) -> Path:
        """Where the viewport's hand-over of this project's mesh is written."""
        return self.case_path / 'foammesh' / 'gmsh' / 'handover' / 'mesh.vtu'

    def analyse(self, dim: int = 3, hiding: float | None = None) -> dict:
        """Gmsh's AnalyseMeshQuality plugin on the session mesh (GN14)."""
        with self._lock:
            if self._session is None or not self._session.alive:
                raise actions.ActionRefusal(
                    'there is no mesh yet; generate one first')
            return self._session.analyse(dim=dim, hiding=hiding)

    # -- Gmsh plugins (GN20) -------------------------------------------------- #

    def plugin_directory(self) -> Path:
        """Where the ``.vtu`` of plugin views are written for the viewport."""
        return self.case_path / 'foammesh' / 'gmsh' / 'plugins'

    def run_plugin(self, name: str, numbers: dict | None = None, *,
                   view: int | None = None,
                   other_view: int | None = None,
                   time_limit: float | None = None) -> dict:
        """Run Gmsh plugin *name* on the session; refuse what it cannot do.

        Plan 48 TL: under the plugin time limit (*time_limit* seconds for
        this call, 0 for none); a plugin that does not return (CurvedBndDist
        on Gmsh 4.15.2) is refused with the limit named and the session is
        restarted.
        """
        from . import plugin_catalogue as catalogue

        try:
            entry = catalogue.plugin(name)
        except KeyError as error:
            raise actions.ActionRefusal(str(error.args[0])) from None
        if entry.unavailable:
            raise actions.ActionRefusal(entry.unavailable)
        try:
            # Plan 48 GM9 (DP-4301). Every option goes, the string ones too:
            # plugin options are sticky inside a Gmsh process.
            merged = catalogue.full_values(name, numbers)
            numbers, strings = catalogue.split_values(name, merged)
        except ValueError as error:
            raise actions.ActionRefusal(str(error)) from None
        if entry.needs_view and view is None:
            raise actions.ActionRefusal(
                f'{entry.title} works on a view: run a plugin that makes one '
                '(Distance, Analyse mesh quality, Skin of a view) and choose '
                'it as the input view.')
        with self._lock:
            if self._session is None or not self._session.alive:
                raise actions.ActionRefusal(
                    'there is no mesh yet; generate one first')
            if not (self._session.state() or {}).get('nodes'):
                raise actions.ActionRefusal(
                    'there is no mesh yet; generate one first')
            seconds = self._limit('plugin', time_limit)
            try:
                return self._session.plugin_run(
                    name, self.plugin_directory(), numbers,
                    edits=catalogue.edits_mesh(name, merged), view=view,
                    other_view=other_view, strings=strings,
                    timeout=seconds or None)
            except Exception as error:                  # noqa: BLE001
                self._raise_limit(error, f'the {name} plugin', seconds)
                raise

    def plugin_views(self) -> list:
        with self._lock:
            if self._session is None or not self._session.alive:
                return []
            return self._session.plugin_views()

    def remove_plugin_view(self, tag: int | None = None) -> dict:
        with self._lock:
            if self._session is None or not self._session.alive:
                return {'removed': [], 'left': []}
            return self._session.plugin_remove(tag)

    def statistics(self) -> dict:
        """Gmsh's Statistics window for the mesh last handed to the viewport.

        Read from the hand-over's own arrays, so it describes exactly the
        cells that are drawn, and carries the stage timings as they were run.
        """
        from . import element_quality

        arrays = element_quality.read_handover_arrays(self.handover_path())
        passes = [{'title': stage.title, 'seconds': stage.seconds,
                   'ok': stage.ok} for stage in self.timeline.stages]
        return element_quality.statistics(arrays, passes)

    # -- entities, picking, physical groups (GN4) ------------------------ #

    def entity_model(self, directory, *, meshed: bool = False) -> tuple:
        """Import once, then ``(entity rows, geometry.vtp host path)``.

        DP-5710. The model a person picks from is the one a build and the
        batch run work on: the import with the early controls, without the
        late stages (boundary layers, periodic pairs, groups, recombination,
        automatic structuring). Reading it never applies them; while they are
        on the model (after a generate) the rows and geometry kept just
        before they were applied are given. *meshed* asks for the model the
        mesh is made on instead (the visibility table of the mesh display).
        """
        import shutil

        from . import session_entities as entities

        with self._lock:
            self.ensure_geometry(late=False)
            snapshot = self._pick_snapshot
            if (not meshed and snapshot is not None
                    and self._stage_done & set(self.LATE_STAGES)):
                vtp = snapshot.get('vtp')
                if vtp:
                    target = Path(directory)
                    target.mkdir(parents=True, exist_ok=True)
                    vtp = str(shutil.copyfile(vtp, target / Path(vtp).name))
                return list(snapshot['rows']), vtp
            written = self._session.handover(directory, mesh=False,
                                             geometry=True)
            rows = entities.entity_rows(self._session)
        return rows, (written.get('geometry') or {}).get('host_path')

    # -- geometry build (GN3) ---------------------------------------------- #

    def builder(self):
        """The project's :class:`geometry_build.GeometryBuilder`."""
        from . import geometry_build

        with self._lock:
            if self._builder is None:
                self._builder = geometry_build.GeometryBuilder(
                    self._build_session)
                self._builder.on_restart = self._model_restarted
            return self._builder

    def _model_restarted(self) -> None:
        """A kernel change emptied the model (DP-5730).

        The import and the stages played on it went with it, so they are
        forgotten: the steps built again next go through
        :meth:`_build_session`, which imports the prepared geometry first,
        as the batch run imports before it plays the build.
        """
        with self._lock:
            self._imported = ()
            self._forget_stages()
            self._loadedRunMeshKey = None
            self._fields_key = None
            self._automatic_key = None
            self._point_sizes_key = None
            self._entities_key = None
            self._page_key = None
            self._overrides_key = None

    def _build_session(self):
        """The session, with the prepared geometry in it when there is one.

        DP-5710. The batch run replays the build steps right after the import
        (the ``geometry`` hook), before the boundary layers and the other late
        stages, so the build is made on that model here too: the late stages
        are not applied, and any already applied are taken back off. The next
        generate applies them again, after the build, in the batch order.
        """
        paths = tuple(self._geometry_paths())
        if paths:
            self.ensure_geometry(late=False)
            self._rewind_late_stages()
        else:
            self.ensure_session()
        return self._session

    def _rewind_late_stages(self) -> None:
        applied = [name for name in self.LATE_STAGES
                   if name in self._stage_done]
        if not applied or not self._stage_on or self._session is None:
            return
        try:
            report = self._session.call('session.stage_unapply', applied)
        except Exception as error:                       # noqa: BLE001
            text = str(error)
            if ': ' in text and text.split(': ', 1)[0].isidentifier():
                text = text.split(': ', 1)[1]
            raise actions.ActionRefusal(
                'the geometry is built on the imported model, before the '
                'boundary layers and the other late stages, as the batch run '
                'builds it; those stages could not be taken back off the '
                'model: ' + text) from error
        self._stage_done.difference_update(applied)
        self._pick_snapshot = None
        self._loadedRunMeshKey = None
        self._take_stage_report(report)

    def build(self, op_id: str, values: dict):
        with self._lock:
            return self.builder().run(op_id, values)

    def parameters(self):
        return self.builder().parameters

    # -- open, merge, reclassify (GN5) ------------------------------------- #

    def open_model(self, path, mode: str = 'open', **options) -> dict:
        """File > Open / Merge.  Returns the session's account of the read.

        Open replaces the model (and the prepared geometry stops being
        imported); Merge adds to what is there, importing the prepared
        geometry first when nothing else was opened.
        """
        from . import import_formats as formats

        request = formats.plan_open(path, mode, **options)
        with self._lock:
            if request.mode == 'merge' and not self._opened:
                self._build_session()
            else:
                self.ensure_session()
            session = self._session
            self._apply_importer_rows_for_open(request.path)
            # Plan 48 DP-4050 / DP-4051: a reader the Open list lacks, or a
            # gzip file, is read by ``session.read_file``.
            kwargs = request.kwargs()
            if request.reader:
                classify = kwargs.pop('classify', None)
                result = session.call(request.operation,
                                      session.runtime_path(request.path),
                                      mode=request.mode)
                if classify is not None:
                    result['reclassified'] = session.call(
                        'session.reclassify', **classify)
            else:
                result = session.call(request.operation,
                                      session.runtime_path(request.path),
                                      **kwargs)
            self._loadedRunMeshKey = None
            if request.mode == 'open':
                self._opened = [request.path]
            else:
                self._opened.append(request.path)
            self._fields_key = None
            self._automatic_key = None
            self._point_sizes_key = None
            self._entities_key = None
            self._page_key = None
            self._overrides_key = None
            self.apply_page_controls()
            self.apply_overrides_if_changed()
            return result

    def _apply_importer_rows_for_open(self, path) -> None:
        """Plan 48 GM3. File > Open reads through the same importer options.

        The unit and the scale are the dialog's own (``open_file`` sets them
        around the read), so those two rows are left out.
        """
        rows = [row for row in self.importer_rows([str(path)])
                if row.control not in ('geometry.unit',
                                       'gmsh/healing/importScaling')]
        for row in rows:
            self._set_row(row, self._ledger_import)

    def open_run_mesh(self, path) -> dict:
        """Plan 42 SG15A. Open an accepted run's mesh for the Export page.

        The mesh replaces the model, but it is not a file the user chose to
        work on: the prepared geometry stays the thing Generate meshes, so the
        next Generate clears this mesh and imports the geometry again (the
        marker in ``_imported`` is a path the prepared geometry never has).
        """
        result = self.open_model(path, 'open')
        with self._lock:
            self._opened = []
            self._imported = (str(path),)
        return result

    def _raise_limit(self, error, what: str, seconds) -> None:
        """Turn a call that passed its time limit into the refusal, with the
        session restarted from its log (Plan 48 TL)."""
        if seconds and actions.is_time_limit(error):
            raise actions.TimeLimitRefusal(
                what, seconds,
                actions.restart_after_limit(self._session)) from None

    def reclassify(self, *, time_limit: float | None = None,
                   **options) -> dict:
        """Mesh > Reclassify 2D with every classifySurfaces argument."""
        from . import import_formats as formats

        args = formats.reclassify_arguments(**options)
        with self._lock:
            self.ensure_geometry()
            return actions.call_with_limit(
                self._session, actions.LIMIT_WHAT['reclassify'],
                self._limit('reclassify', time_limit),
                'session.reclassify', **args)

    def create_topology(self, make_simply_connected: bool = True,
                        export_discrete: bool = True, *,
                        time_limit: float | None = None) -> dict:
        with self._lock:
            self.ensure_geometry()
            return actions.call_with_limit(
                self._session, actions.LIMIT_WHAT['topology'],
                self._limit('topology', time_limit),
                'session.create_topology',
                make_simply_connected=bool(make_simply_connected),
                export_discrete=bool(export_discrete))

    def create_geometry(self, dim_tags=None) -> dict:
        with self._lock:
            self.ensure_geometry()
            return self._session.call(
                'session.create_geometry',
                dim_tags=[list(t) for t in dim_tags or ()])

    def remesh_surfaces(self, angle: float = 40.0, curve_angle: float = 180.0,
                        size: float = 0.0) -> dict:
        """Close the surface mesh of the model into one meshable volume."""
        from . import import_formats as formats

        formats.reclassify_arguments(angle, curve_angle=curve_angle)
        with self._lock:
            self.ensure_geometry()
            return self._session.call(
                'session.remesh_surfaces', angle=float(angle),
                curve_angle=float(curve_angle), size=float(size))

    def physical_editor(self):
        from . import session_entities as entities

        with self._lock:
            # DP-5710: reading or editing the groups never applies the late
            # stages (they are applied by a generate)
            self.ensure_geometry(late=False)
            return entities.PhysicalGroupEditor(self._session)

    def seed_shared_groups(self) -> dict:
        """Plan 42 SG13 (D7). Seed the open model with the shared surface
        names as physical groups, and make linked groups follow a rename.

        Nothing is seeded once a file was opened or merged: the tag join is
        valid only while the session holds the staged files in job order.
        Returns ``{'groups': [...], 'report': [...], 'origins': {tag: uuid}}``.
        """
        from foammesh.core.geometry import PreparedGeometryStore
        from . import shared_names

        with self._lock:
            # DP-5731: seeding names the model a person picks from and
            # builds on, so the late stages are not played for it (a reseed
            # after each build played them, minutes on a swept volume)
            self.ensure_geometry(late=False)
            editor = self.physical_editor()
            if self._opened or self._built():
                # DP-5724: a geometry build renumbers the model, so a tag no
                # longer names the staged surface it named (the rectangle
                # that took tag 1 was put in the pipe's pipe_wall1 group)
                links = shared_names.load_links(self.case_path)
                return {'groups': editor.groups(), 'report': [],
                        'origins': {l['tag']: t for t, l in links.items()}}
            prepared = PreparedGeometryStore(self.case_path).current()
            surfaces = [int(t) for _d, t in self._session.call(
                'model.getEntities', 2) or ()]
            shared = shared_names.shared_surface_groups(
                prepared, surface_tags=surfaces,
                names=shared_patch_names(self.case_path))
            links, report = shared_names.sync(
                editor, shared, shared_names.load_links(self.case_path))
            shared_names.save_links(self.case_path, links)
            return {'groups': editor.groups(), 'report': report,
                    'origins': {l['tag']: t for t, l in links.items()}}

    def _built(self) -> bool:
        """Whether the model holds a geometry build (DP-5724)."""
        from .execution import geometry_build_steps

        if self._builder is not None and self._builder.steps:
            return True
        return bool(geometry_build_steps(self.case_path))

    def coherence(self) -> dict:
        from . import session_entities as entities

        with self._lock:
            self.ensure_geometry(late=False)
            return entities.coherence(self._session)

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
            self._session = None
            self._imported = ()
            self._opened = []
            self._pick_snapshot = None
            if self._pick_dir is not None:
                import shutil
                shutil.rmtree(self._pick_dir, ignore_errors=True)
                self._pick_dir = None


def shared_patch_names(case_path) -> dict:
    """``{patch_uuid: name}`` from the patch records the shared Rename edits."""
    try:
        from foammesh.core.geometry import GeometryArtifactStore
        from foammesh.core.geometry.patches.manifest import PatchGroupEditor

        rows = PatchGroupEditor(GeometryArtifactStore(Path(case_path))).rows()
    except Exception:                                # noqa: BLE001
        return {}
    return {str(r.get('patch_uuid')): str(r.get('name'))
            for r in rows if r.get('patch_uuid') and r.get('name')}


def geometry_paths_from_prepared(case_path) -> tuple:
    """The staged sources of the project's current prepared geometry."""
    from foammesh.core.geometry import PreparedGeometryStore
    from .execution import prepared_geometry_paths

    current = PreparedGeometryStore(Path(case_path)).current()
    if current is None:
        return ()
    # WALK-2. The reader wants the revision reference (its root and
    # manifest), as the facade's run hands it, not the store's result.
    reference = getattr(current, 'reference', None) or current
    return tuple(prepared_geometry_paths(reference, Path(case_path)))


# -- Plan 48 GM3: reaching the page controls from a project ----------------- #

def page_intent_reader(case_session, *, profile=None) -> Callable[[], dict]:
    """A callable giving the job intent (as a dict) of an open project.

    It is ``execution.derive_page_intent`` over the project's own database
    and prepared geometry: the very derivation ``write_job`` runs for the
    batch job, so a page control means the same on either path.
    """
    def read() -> dict:
        from foammesh.core.geometry import PreparedGeometryStore
        from .execution import derive_page_intent

        case_path = Path(case_session.case_path)
        prepared = PreparedGeometryStore(case_path).current()
        reference = getattr(prepared, 'reference', None) or prepared
        bbox = None
        if prepared is not None:
            from foammesh.core.facade.domain_operations import _prepared_bbox
            bbox = _prepared_bbox(prepared)
        return derive_page_intent(
            case_session.state.db, bbox, case_path,
            prepared_geometry=reference, profile=profile).to_dict()

    def job() -> dict:
        """Plan 48 RR (DP-4009). The job ``write_job`` would write for a
        batch run of this project, unwritten: what the shared stage
        functions read in the session."""
        from foammesh.core.geometry import PreparedGeometryStore
        from .execution import session_job

        case_path = Path(case_session.case_path)
        prepared = PreparedGeometryStore(case_path).current()
        reference = getattr(prepared, 'reference', None) or prepared
        bbox = None
        if prepared is not None:
            from foammesh.core.facade.domain_operations import (
                _prepared_bbox)
            bbox = _prepared_bbox(prepared)
        return session_job(case_session.state.db, bbox, case_path,
                           prepared_geometry=reference, profile=profile)

    read.job = job
    return read


def register_controller(case_session, controller) -> None:
    """Let the local API and the window share one controller per project."""
    case_session.gmsh_generate_controller = controller


def registered_controller(case_session):
    return getattr(case_session, 'gmsh_generate_controller', None)


def controller_for_case_session(case_session) -> GenerateController:
    """The project's controller: the window's when it registered one, else a
    controller made here from the same inputs the window gives it."""
    controller = registered_controller(case_session)
    if controller is not None:
        return controller
    case_path = Path(case_session.case_path)
    controller = GenerateController(
        case_path,
        geometry_paths=lambda: geometry_paths_from_prepared(case_path),
        intent=page_intent_reader(case_session),
        **stored_inputs_reader(case_session))
    register_controller(case_session, controller)
    return controller


def stored_inputs_reader(case_session) -> dict:
    """The stored Options-panel values, size fields, point sizes and entity
    controls of an open project, as the callables the controller takes.

    Plan 48 FIX3 (DP-4009). The window hands the controller these from its
    client; a controller made without a window (the local API, a script) got
    none, so the session ran without them while the batch run applied them.
    This reads the same fields from the project's own configuration.
    """
    from foammesh.core.facade import FIELD_REGISTRY
    from foammesh.core.facade.field_adapters import read_value

    def values(ids) -> dict:
        try:
            configuration = case_session.configuration()
            return {i: read_value(configuration,
                                  FIELD_REGISTRY.get(i).storage_path)
                    for i in ids}
        except Exception:                                # noqa: BLE001
            return {}

    def raw(field_id):
        return getattr(values((field_id,)).get(field_id), 'value',
                       values((field_id,)).get(field_id))

    def overrides():
        return raw(catalogue.OVERRIDES_FIELD) or ''

    def fields():
        from . import field_catalogue as fc
        stored = values((fc.FIELD_ID, 'gmsh.global_sizing.surface',
                         'gmsh.global_sizing.volume'))
        plain = {k: getattr(v, 'value', v) for k, v in stored.items()}
        # Plan 48 GM8 (DP-4764): a moved or copied project folder reads its
        # size data from its own gmsh/size_data/.
        rows = fc.rows_from_text(plain.get(fc.FIELD_ID))
        case_path = getattr(case_session, 'case_path', None)
        if case_path is not None:
            rows = fc.rebase_size_data(rows, case_path)
        return fc.validate(
            rows, surface_algorithm=str(
                plain.get('gmsh.global_sizing.surface') or ''),
            volume_algorithm=str(
                plain.get('gmsh.global_sizing.volume') or '')).to_list()

    def point_sizes():
        from . import point_sizes as points
        return points.validate(
            points.rows_from_text(raw(points.FIELD_ID))).to_list()

    def entities():
        from . import entity_controls as entity
        return entity.validate(
            entity.rows_from_text(raw(entity.FIELD_ID))).to_list()

    return {'overrides': overrides, 'fields': fields,
            'point_sizes': point_sizes, 'entities': entities}
