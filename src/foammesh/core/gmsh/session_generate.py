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
                 on_stage: Callable | None = None):
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
        self._session: GmshSession | None = None
        self._imported: tuple = ()
        self._lock = threading.RLock()
        self._builder = None
        self._opened: list = []
        #: SG15A: key of the accepted run's mesh the session was loaded with
        self._loadedRunMeshKey = None
        self.timeline = actions.Timeline()
        self.warnings: list[str] = []

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
            self._imported = ()
            self._opened = []
            return session

    def ensure_geometry(self) -> dict:
        """Import the prepared geometry once; later calls change nothing."""
        with self._lock:
            session = self.ensure_session()
            if self._opened:
                # a file the user opened (GN5) is the model: it is never
                # replaced by the prepared geometry
                self.apply_fields()
                return {'imported': False, 'paths': (), 'opened': True}
            paths = tuple(str(Path(p)) for p in self._geometry_paths())
            if not paths and self._builder is not None                     and self._builder.steps:
                # a model built here, from primitives, is the geometry
                self.apply_fields()
                return {'imported': False, 'paths': (), 'built': True}
            if not paths:
                raise actions.ActionRefusal(
                    'there is no prepared geometry to mesh; prepare the '
                    'geometry first')
            if paths == self._imported:
                self.apply_fields()
                return {'imported': False, 'paths': paths}
            if self._imported:
                session.call('clear')
                session.call('model.add', 'foammesh')
            for path in paths:
                session.import_geometry(path)
            self._imported = paths
            self._loadedRunMeshKey = None
            self._fields_key = None
            self._automatic_key = None
            self._point_sizes_key = None
            self._entities_key = None
            self.apply_overrides()
            self.apply_fields()
            return {'imported': True, 'paths': paths}

    def apply_overrides(self) -> tuple:
        """Send the stored Options-panel values, each checked first."""
        text = self._overrides() if self._overrides else ''
        try:
            rows, warnings = catalogue.resolve_overrides(
                catalogue.overrides_from_text(text))
        except catalogue.OptionRefusal as error:
            self.warnings.append(str(error))
            return ()
        self.warnings.extend(warnings)
        session = self._session
        for name, storage, value in rows:
            if isinstance(value, list):
                continue
            session.call('option.setString' if storage == 'string'
                         else 'option.setNumber', name, value)
        return rows

    def apply_fields(self) -> dict:
        """The stored size fields and the sizes at points, on the model."""
        self.apply_automatic_field()
        result = self._apply_fields()
        self.apply_point_sizes()
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
                self._entities_key = None
                self._fields_key = None
                self._automatic_key = None
                self._point_sizes_key = None
                self.ensure_geometry()
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
        result = self._session.call('session.fields', nodes)
        self._fields_key = key
        return result

    def preview_fields(self, dim: int = 2, limit: int = 20000) -> dict:
        """Sizes the background field gives, read where Gmsh asks for them.

        Refused (``ActionRefusal``) while the model has a mesh.
        """
        with self._lock:
            self.ensure_geometry()
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
            timeout: float | None = None) -> actions.Stage:
        """Run one action; the stage is recorded whether it worked or not."""
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
                                timeline=self.timeline, timeout=timeout)
        self._notify(stage)
        return stage

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
                   other_view: int | None = None) -> dict:
        """Run Gmsh plugin *name* on the session; refuse what it cannot do."""
        from . import plugin_catalogue as catalogue

        try:
            entry = catalogue.plugin(name)
        except KeyError as error:
            raise actions.ActionRefusal(str(error.args[0])) from None
        if entry.unavailable:
            raise actions.ActionRefusal(entry.unavailable)
        try:
            numbers = catalogue.numbers_for(name, numbers)
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
            return self._session.plugin_run(
                name, self.plugin_directory(), numbers,
                edits=catalogue.edits_mesh(name, numbers), view=view,
                other_view=other_view)

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

    def entity_model(self, directory) -> tuple:
        """Import once, then ``(entity rows, geometry.vtp host path)``."""
        from . import session_entities as entities

        with self._lock:
            self.ensure_geometry()
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
            return self._builder

    def _build_session(self):
        """The session, with the prepared geometry in it when there is one."""
        paths = tuple(self._geometry_paths())
        if paths:
            self.ensure_geometry()
        else:
            self.ensure_session()
        return self._session

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
            result = session.call('session.open',
                                  session.runtime_path(request.path),
                                  **request.kwargs())
            self._loadedRunMeshKey = None
            if request.mode == 'open':
                self._opened = [request.path]
            else:
                self._opened.append(request.path)
            self._fields_key = None
            self._automatic_key = None
            self._point_sizes_key = None
            self._entities_key = None
            self.apply_overrides()
            return result

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

    def reclassify(self, **options) -> dict:
        """Mesh > Reclassify 2D with every classifySurfaces argument."""
        from . import import_formats as formats

        args = formats.reclassify_arguments(**options)
        with self._lock:
            self.ensure_geometry()
            return self._session.call('session.reclassify', **args)

    def create_topology(self, make_simply_connected: bool = True,
                        export_discrete: bool = True) -> dict:
        with self._lock:
            self.ensure_geometry()
            return self._session.call(
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
            self.ensure_geometry()
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
            self.ensure_geometry()
            editor = self.physical_editor()
            if self._opened:
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

    def coherence(self) -> dict:
        from . import session_entities as entities

        with self._lock:
            self.ensure_geometry()
            return entities.coherence(self._session)

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
            self._session = None
            self._imported = ()
            self._opened = []


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
