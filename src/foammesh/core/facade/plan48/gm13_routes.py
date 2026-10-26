"""Plan 48 GM13: the local API complete for mesh studies, and the window bridge.

Decision D7 (user, 2026-10-21): FoamMesh has no Mesh study module and no study
code. A program outside runs a study over this API: mesh the same geometry two
or three times at different densities and compare. For that to work from the
outside, two things must be on the API, on both routes (Gmsh and snappy):

* **Every size control is settable** (``sizing.controls.list`` and
  ``sizing.controls.set``). The list says which fields and which row
  collections are size controls on each route, with their unit, range and
  current value. Setting goes through the same path as ``configuration.patch``
  (same validation, locks, claims and invalidation); the route only refuses a
  field that is not a size control.
* **Every result a study reads is readable** (``results.counts``,
  ``results.quality``, ``results.deviation``, ``results.layer_coverage``,
  ``results.run_cost`` and ``results.snapshot``, which is the five at one
  revision). Each number is either ``measured`` (with where it was read from)
  or ``unavailable`` (with the reason); nothing is filled by a guess.

These are plain remote controls for what the window already shows: the same
stored documents the Quality, Mesh info and Runs pages read. There is no loop,
no comparison and no recipe here.

The second half is DP-4063, the bridge from a facade handler to the window's
resident Gmsh session, so the window-bound Gmsh actions (File > Open, Merge,
Clear model, Save as picture, Record, Save statistics, Save visibility and
Watch pattern) can be driven from the API:

* Open, Merge, Clear model and the two saves act on the resident Gmsh session.
  The window and the API share one ``GenerateController`` per case (the window
  registers it on the case session, ``session_generate.register_controller``),
  so these need nothing more from the window than that registration.
* A picture, a movie and a watch need the window's viewport and timers. The
  window registers an object with those methods on the case session with
  :func:`register_window_actions` (see :class:`WindowGmshActions`); with none
  registered the route refuses and says why.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime
from pathlib import Path

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult, measured, unavailable

# --------------------------------------------------------------------------- #
# What counts as a size control
# --------------------------------------------------------------------------- #

#: Field-id prefixes that are size controls, by the route they belong to.
#: ``common`` is shared by both routes (the automatic size field's inputs and
#: the size preset). The whole area is a size control unless named in
#: :data:`NOT_SIZE_PREFIXES`.
SIZE_PREFIXES = {
    'common': ('mesh.size_preset', 'sizing.field.'),
    'gmsh': ('gmsh.global_sizing.', 'gmsh.boundary_layers.',
             'gmsh.volume_controls.', 'gmsh.point_sizes.',
             'gmsh.native_fields.', 'gmsh.entity_controls.',
             'gmsh.structure.'),
    'snappy': ('meshing.base_grid.', 'meshing.castellation.',
               'meshing.layers.'),
}

#: Inside those areas, the fields that name or type something and set no size.
NOT_SIZE_PREFIXES = (
    'meshing.base_grid.boundary_categories.',
    'meshing.base_grid.boundary_names.',
    'meshing.base_grid.boundary_types.',
    'meshing.base_grid.bounding_hex6',
    'meshing.base_grid.grading_notice',
)

#: Row collections (one row per size field, refinement, layer group ...) that
#: are size controls. They are changed with the collection's own
#: ``<collection>.create`` / ``.patch`` / ``.remove`` operations, listed here.
SIZE_COLLECTIONS = {
    'gmsh': ('gmsh.size_fields.controls', 'gmsh.surface_sizes.controls',
             'gmsh.curve_controls.controls', 'gmsh.volume_controls.controls'),
    'snappy': ('base_grid.blocks', 'base_grid.edges',
               'meshing.castellation.surface_refinements',
               'meshing.castellation.volume_refinements',
               'meshing.castellation.feature_bands',
               'meshing.castellation.volume_bands',
               'meshing.layers.groups'),
}

ROUTES = ('gmsh', 'snappy')


def size_route_of(field_id: str) -> str:
    """``common``, ``gmsh`` or ``snappy`` for a size control, else ``''``."""
    field_id = str(field_id)
    if field_id.startswith(NOT_SIZE_PREFIXES):
        return ''
    for route, prefixes in SIZE_PREFIXES.items():
        if field_id.startswith(prefixes):
            return route
    return ''


def size_control_ids(route: str = 'all') -> tuple[str, ...]:
    """The ids of the scalar size controls of *route* (``all`` for every one)."""
    from foammesh.core.facade.fields import REGISTRY

    wanted = ('common', 'gmsh', 'snappy') if route == 'all' \
        else ('common', route)
    return tuple(descriptor.id for descriptor in REGISTRY.descriptors()
                 if size_route_of(descriptor.id) in wanted)


# --------------------------------------------------------------------------- #
# The window bridge (DP-4063)
# --------------------------------------------------------------------------- #

#: The attribute the window's actions are kept under on the case session; the
#: same convention as ``gmsh_generate_controller``.
WINDOW_ATTRIBUTE = 'gmsh_window_actions'


class WindowGmshActions:
    """What the window registers (documentation of the contract).

    Any object with these methods will do; a method the object lacks makes the
    matching route refuse with "the window did not register <name>". Every
    method runs what the person's control runs, on the GUI thread, and returns
    a JSON-safe ``dict``; it raises ``ValueError`` with the reason when the
    action cannot be done (the route reports it as a validation failure).

    ``save_image(path, format_id, options)``
        File > Save as picture: the current viewport view, written in the
        format of ``rendering.image_formats`` (PNG, JPEG, SVG, PDF, EPS, PS,
        GIF, PPM, BMP, YUV, XPM, TEX, PGF, TIKZ). ``options`` may carry
        ``width``, ``height`` and ``transparent``.
    ``record_movie(path, options)``
        Record: an MP4 of the turntable or of the animation the window shows.
        ``options`` may carry ``fps``, ``frames`` and ``quality``.
    ``watch_start(pattern, interval)`` / ``watch_stop()`` / ``watch_status()``
        File > Watch pattern: merge the files that match *pattern* when they
        appear or change, every *interval* seconds.
    """

    def save_image(self, path, format_id, options): ...      # pragma: no cover

    def record_movie(self, path, options): ...               # pragma: no cover

    def watch_start(self, pattern, interval): ...            # pragma: no cover

    def watch_stop(self): ...                                # pragma: no cover

    def watch_status(self): ...                              # pragma: no cover


def register_window_actions(case_session, actions) -> None:
    """The window tells the API what it can do for this case.

    The one line the window needs (GM11b, ``main_window.py``), once the case
    session and the viewport exist::

        register_window_actions(case_session, self.gmsh_window_actions)
    """
    setattr(case_session, WINDOW_ATTRIBUTE, actions)


def unregister_window_actions(case_session) -> None:
    """The window closed the case or the viewport."""
    if getattr(case_session, WINDOW_ATTRIBUTE, None) is not None:
        setattr(case_session, WINDOW_ATTRIBUTE, None)


def window_actions(case_session):
    """The registered actions, or ``None`` with no window."""
    return getattr(case_session, WINDOW_ATTRIBUTE, None)


def _window_call(session, method: str, *args):
    actions = window_actions(session)
    if actions is None:
        raise PreconditionFailedError(
            'no window is showing this case, and this needs its viewport: '
            'open the case in the FoamMesh window',
            details={'needs': method})
    function = getattr(actions, method, None)
    if not callable(function):
        raise PreconditionFailedError(
            f'the window did not register {method}', details={'needs': method})
    try:
        result = function(*args)
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error
    return dict(result or {})


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _resident(session):
    """``(controller, gmsh session)`` for an action that needs a live model."""
    controller = _controller(session)
    held = controller.session
    if held is None or not held.alive:
        raise PreconditionFailedError(
            'the Gmsh session holds no model: generate a mesh, or open or '
            'merge a file, first', details={'needs': 'gmsh session'})
    return controller, held


def _host_path(parameters, key: str, suffix: tuple = ()) -> str:
    raw = (parameters or {}).get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ValidationFailedError(f'{key} is required')
    path = Path(raw)
    if suffix and path.suffix.lower() not in suffix:
        raise ValidationFailedError(
            f'{key} must end in ' + ' or '.join(suffix),
            details={key: raw})
    return str(path)


def _save_target(parameters, key: str, suffix: tuple) -> str:
    raw = _host_path(parameters, key, suffix)
    if not Path(raw).parent.is_dir():
        raise ValidationFailedError(
            'the folder to save in does not exist',
            details={'folder': str(Path(raw).parent)})
    return raw


def _open(session, command, mode: str):
    from foammesh.core.gmsh import import_formats as formats

    parameters = command.parameters or {}
    path = _host_path(parameters, 'path')
    options = parameters.get('options') or {}
    if not isinstance(options, dict):
        raise ValidationFailedError('options must be an object')
    controller = _controller(session)

    def work():
        try:
            return controller.open_model(path, mode, **options)
        except formats.ImportRefusal as error:
            raise ValidationFailedError(str(error)) from error

    return work


async def _model_open(session, command):
    result = await asyncio.to_thread(_open(session, command, 'open'))
    return _done(session, command, {'mode': 'open', 'result': result})


async def _model_merge(session, command):
    result = await asyncio.to_thread(_open(session, command, 'merge'))
    return _done(session, command, {'mode': 'merge', 'result': result})


def _forget_model(controller) -> None:
    """The controller no longer holds what it imported or opened."""
    clear = getattr(controller, 'clear_model', None)
    if callable(clear):
        clear()
        return
    # The controller has no clear_model yet (requests/gm13.md): reset what
    # open_model resets, so the next Generate imports the geometry again.
    controller._imported = ()
    controller._opened = []
    controller._loadedRunMeshKey = None
    for key in ('_fields_key', '_automatic_key', '_point_sizes_key',
                '_entities_key', '_page_key', '_overrides_key'):
        setattr(controller, key, None)


async def _model_clear(session, command):
    controller, held = _resident(session)

    def work():
        with controller._lock:
            own = getattr(controller, 'clear_model', None)
            if callable(own):
                # Plan 48 merge: the controller clears the session and its
                # own caches in one call (GenerateController.clear_model).
                return own()
            state = held.call('session.clear_model')
            _forget_model(controller)
        return state

    return _done(session, command, {'state': await asyncio.to_thread(work)})


_POS_MEASURES = ('elementary', 'element', 'sicn', 'sige', 'gamma', 'disto')


async def _statistics_save(session, command):
    parameters = command.parameters or {}
    path = _save_target(parameters, 'path', ('.pos',))
    measures = {name: bool(parameters[name]) for name in _POS_MEASURES
                if name in parameters}
    if 'save_all' in parameters:
        measures['save_all'] = bool(parameters['save_all'])
    controller, held = _resident(session)

    def work():
        with controller._lock:
            return held.call('session.save_mesh_statistics',
                             held.runtime_path(path), **measures)

    try:
        result = await asyncio.to_thread(work)
    except Exception as error:                            # noqa: BLE001
        raise ValidationFailedError(_reason(error)) from error
    return _done(session, command, {'result': result}, files=(path,))


async def _visibility_save(session, command):
    path = _save_target(command.parameters, 'path', ('.vis',))
    controller, held = _resident(session)

    def work():
        with controller._lock:
            return held.call('session.save_visibility',
                             held.runtime_path(path))

    try:
        result = await asyncio.to_thread(work)
    except Exception as error:                            # noqa: BLE001
        raise ValidationFailedError(_reason(error)) from error
    return _done(session, command, {'result': result}, files=(path,))


def _reason(error) -> str:
    text = str(error).strip()
    return text or error.__class__.__name__


def _image_save(session, command):
    from foammesh.rendering import image_formats as imf

    parameters = command.parameters or {}
    try:
        item = imf.format_for(parameters.get('format') or '')
    except imf.ImageFormatError as error:
        raise ValidationFailedError(str(error)) from error
    if item.kind == 'movie':
        raise ValidationFailedError(
            'MP4 is a movie: use gmsh.movie.record', details={'format': item.id})
    path = _save_target(parameters, 'path', (item.suffix,))
    reason = imf.refusal(item.id)
    if reason:
        raise PreconditionFailedError(reason, details={'format': item.id})
    options = parameters.get('options') or {}
    if not isinstance(options, dict):
        raise ValidationFailedError('options must be an object')
    result = _window_call(session, 'save_image', path, item.id, options)
    return _done(session, command, {'format': item.id, 'result': result},
                 files=(path,))


def _movie_record(session, command):
    from foammesh.rendering import image_formats as imf

    parameters = command.parameters or {}
    path = _save_target(parameters, 'path', ('.mp4',))
    reason = imf.movie_refusal()
    if reason:
        raise PreconditionFailedError(reason, details={'format': 'MP4'})
    options = parameters.get('options') or {}
    if not isinstance(options, dict):
        raise ValidationFailedError('options must be an object')
    result = _window_call(session, 'record_movie', path, options)
    return _done(session, command, {'result': result}, files=(path,))


def _watch_start(session, command):
    from foammesh.core.gmsh import import_formats as formats

    parameters = command.parameters or {}
    pattern = parameters.get('pattern')
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValidationFailedError('pattern is required')
    try:
        # The same interval rule the Watch dialog applies.
        interval = formats.check_watch_interval(
            parameters.get('interval', 1.0))
    except formats.ImportRefusal as error:
        raise ValidationFailedError(str(error)) from error
    result = _window_call(session, 'watch_start', pattern, interval)
    return _done(session, command, {'result': result})


def _watch_stop(session, command):
    return _done(session, command, {'result': _window_call(
        session, 'watch_stop')})


def _watch_status(session, command):
    return _read(session, command, {'result': _window_call(
        session, 'watch_status')})


def _done(session, command, payload, *, files=()) -> OperationResult:
    return OperationResult(
        'accepted', command.operation, session.revisions,
        payload=dict(payload, files=[str(item) for item in files]))


def _read(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


# --------------------------------------------------------------------------- #
# Size controls
# --------------------------------------------------------------------------- #

def _routes_wanted(session, parameters) -> tuple[str, ...]:
    wanted = str((parameters or {}).get('route') or '').strip().lower()
    if wanted in ROUTES:
        return (wanted,)
    if wanted in ('', 'all', 'configured'):
        if wanted == 'configured' or not wanted:
            engine = _engine(session)
            if engine in ROUTES:
                return (engine,)
        return ROUTES
    raise ValidationFailedError(
        'route must be gmsh, snappy, configured or all', details={'route': wanted})


def _engine(session) -> str:
    from foammesh.core.facade.facade import _session_engine

    return _session_engine(session) or ''


def _target(session) -> str:
    from foammesh.core.engine.registry import configured_target_solver

    try:
        return configured_target_solver(session.state.db)
    except Exception:                                      # noqa: BLE001
        return ''


def _control_row(descriptor, configuration) -> dict:
    from foammesh.core.facade.field_adapters import read_value, typed_value

    stored = read_value(configuration, descriptor.storage_path)
    return {
        'id': descriptor.id, 'title': descriptor.title,
        'route': size_route_of(descriptor.id),
        'value_type': descriptor.value_type.value, 'unit': descriptor.unit,
        'value': typed_value(descriptor, stored), 'default': descriptor.default,
        'minimum': descriptor.minimum, 'maximum': descriptor.maximum,
        'exclusive_minimum': descriptor.exclusive_minimum,
        'exclusive_maximum': descriptor.exclusive_maximum,
        'enum': list(descriptor.enum) if descriptor.enum is not None else None,
        'read_only': descriptor.read_only,
        'applies_when': list(descriptor.applies_when),
        'set_with': 'sizing.controls.set',
    }


def _collection_row(collection_id, adapter, configuration) -> dict:
    from foammesh.core.facade.field_adapters import read_value

    rows = read_value(configuration, adapter.storage_path)
    return {
        'collection': collection_id, 'storage_path': adapter.storage_path,
        'fields': {name: adapter.descriptor(name).to_dict()
                   for name in adapter.fields},
        'rows': rows if isinstance(rows, (dict, list)) else {},
        'create_with': f'{collection_id}.create',
        'patch_with': f'{collection_id}.patch',
        'remove_with': f'{collection_id}.remove',
    }


def _controls_list(session, command):
    from foammesh.core.facade.field_adapters import build_entity_adapters
    from foammesh.core.facade.fields import REGISTRY

    parameters = command.parameters or {}
    routes = _routes_wanted(session, parameters)
    configuration = session.configuration()
    wanted = {'common', *routes}
    controls = [_control_row(descriptor, configuration)
                for descriptor in REGISTRY.descriptors()
                if size_route_of(descriptor.id) in wanted]
    adapters = build_entity_adapters(REGISTRY.collections)
    collections = []
    for route in routes:
        for collection_id in SIZE_COLLECTIONS[route]:
            row = _collection_row(collection_id, adapters[collection_id],
                                  configuration)
            row['route'] = route
            if not parameters.get('rows', True):
                row['rows'] = {}
            collections.append(row)
    return _read(session, command, {
        'engine': _engine(session), 'target_solver': _target(session),
        'routes': list(routes), 'controls': controls,
        'collections': collections})


def _facade_patch(session, command, values: dict):
    """``configuration.patch`` / ``configuration.dry_run`` for *values*.

    The facade's own functions run on a stand-in that carries only what they
    read (``fields``, ``_normalize_patch`` and the validator), so a size
    control changes by exactly the rules any other field does: read-only
    refusal, field claims, locked-task refusal, change-set, invalidation and
    staled stages.
    """
    from foammesh.core.facade.facade import FoamMeshFacade
    from foammesh.core.facade.fields import REGISTRY

    class _Patcher:
        fields = REGISTRY
        _normalize_patch = FoamMeshFacade._normalize_patch
        _validate_configuration_patch = staticmethod(
            FoamMeshFacade._validate_configuration_patch)

    patched = dataclasses.replace(
        command, parameters={**(command.parameters or {}), 'patch': values})
    runner = FoamMeshFacade._dry_run if command.parameters.get('dry_run') \
        else FoamMeshFacade._patch
    return runner(_Patcher(), session, patched)


def _controls_set(session, command):
    values = (command.parameters or {}).get('values')
    if not isinstance(values, dict) or not values:
        raise ValidationFailedError(
            'values must be a non-empty object of size control id: value')
    allowed = set(size_control_ids('all'))
    refused = sorted(name for name in values if name not in allowed)
    if refused:
        raise ValidationFailedError(
            'not a size control (sizing.controls.list names them); other '
            'fields are set with configuration.patch',
            details={'field_ids': refused})
    result = _facade_patch(session, command, dict(values))
    return dataclasses.replace(
        result, payload={**result.payload, 'controls': sorted(values)})


# --------------------------------------------------------------------------- #
# Results a study reads
# --------------------------------------------------------------------------- #

def _case(session) -> Path:
    return Path(session.case_path)


def _read_json(path):
    try:
        document = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _parse_time(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    except ValueError:
        return None


def _runs(session) -> list:
    from foammesh.core.gmsh.manifest import run_documents

    return [(Path(root), document) for root, document in
            run_documents(_case(session))]


#: A run whose mesh is the one the case keeps.
_KEPT = ('accepted',)


def _choose_run(session, run_id: str = ''):
    """``(root, document)`` of the run asked for, else the newest kept run,
    else the newest run that finished; ``None`` with no run at all."""
    from foammesh.core.gmsh.manifest import run_disposition

    runs = _runs(session)
    if run_id:
        for root, document in runs:
            if str(document.get('run_id') or root.name) == run_id:
                return root, document
        raise ValidationFailedError('no run with that id',
                                    details={'run_id': run_id})
    for root, document in runs:
        if run_disposition(document) in _KEPT:
            return root, document
    for root, document in runs:
        if document.get('finished_at'):
            return root, document
    return None


def _run_header(chosen) -> dict:
    if chosen is None:
        return unavailable('the case has no mesh run yet')
    from foammesh.core.gmsh.manifest import run_disposition

    root, document = chosen
    return measured({
        'run_id': str(document.get('run_id') or root.name),
        'engine_id': str(document.get('engine_id') or ''),
        'status': str(document.get('status') or ''),
        'disposition': run_disposition(document)},
        source=str(root))


def _family_sum(table) -> int:
    return sum(int(count) for count in (table or {}).values())


def _native_counts(chosen) -> dict:
    """What the run recorded about the mesh it made (Gmsh batch runs)."""
    if chosen is None:
        return unavailable('the case has no mesh run yet')
    root, document = chosen
    statistics = document.get('statistics') or {}
    mesh = statistics.get('mesh')
    source = f'run {document.get("run_id") or root.name}: statistics.mesh'
    if not isinstance(mesh, dict) or 'cells' not in mesh:
        return unavailable(
            'this run recorded no element counts (only Gmsh runs do; '
            'the published mesh is counted instead)', source=source)
    produced = (statistics.get('families') or {}).get('produced') or {}
    return {
        'cells': measured(int(mesh['cells']), source=source, unit='cells'),
        'points': measured(int(mesh.get('nodes') or 0), source=source,
                           unit='nodes'),
        'cells_by_type': measured(dict(mesh.get('cellsByType') or {}),
                                  source=source),
        'inverted': measured(int(mesh.get('inverted') or 0), source=source),
        'dimension': measured(int(mesh.get('dimension') or 3), source=source),
        'volume_by_family': (
            measured(dict(produced.get('volume') or {}), source=source)
            if produced else unavailable('no family census was recorded',
                                         source=source)),
        'surface_by_family': (
            measured(dict(produced.get('surface') or {}), source=source)
            if produced else unavailable('no family census was recorded',
                                         source=source)),
    }


def _published_counts(case: Path, families: bool) -> dict:
    """The counts of ``constant/polyMesh`` (what OpenFOAM solvers read)."""
    poly = case / 'constant' / 'polyMesh'
    if not poly.is_dir():
        return unavailable('the case has no polyMesh yet (mesh it, or export '
                           'for OpenFOAM)')
    from foammesh.core.mesh.info import MeshInfoService

    source = 'constant/polyMesh'
    try:
        info = MeshInfoService().inspect(case)
    except Exception as error:                            # noqa: BLE001
        return unavailable(f'the polyMesh could not be read: {_reason(error)}',
                           source=source)

    def count(value, unit):
        if value is None:
            return unavailable(
                'the polyMesh header does not state this count',
                source=source)
        return measured(int(value), source=source, unit=unit)

    published = {
        'cells': count(info.cells, 'cells'),
        'points': count(info.points, 'points'),
        'faces': count(info.faces, 'faces'),
        'internal_faces': count(info.internal_faces, 'faces'),
        'boundary_patches': measured(len(info.patches), source=source),
        'patches': measured(
            [{'name': patch.name, 'type': patch.patch_type,
              'faces': patch.face_count} for patch in info.patches],
            source=source),
        'fingerprint': measured(info.fingerprint, source=source),
        'warnings': list(info.warnings),
    }
    if families:
        published['cells_by_family'] = _family_census(case)
    return published


def _family_census(case: Path) -> dict:
    from foammesh.core.mesh.census import cell_census

    source = 'constant/polyMesh (cell census)'
    try:
        census = cell_census(case)
    except Exception as error:                            # noqa: BLE001
        return unavailable(f'the cell census failed: {_reason(error)}',
                           source=source)
    if census.read_error:
        return unavailable(census.read_error, source=source)
    return measured(dict(census.cells_by_family), source=source,
                    polygon_faces=int(census.polygon_faces))


def _file_counts(case: Path) -> dict:
    """The counts of the accepted native ``.msh`` or ``.su2`` file."""
    from foammesh.core.facade.mesh_presence import native_mesh_artifact

    path = native_mesh_artifact(case)
    if not path:
        return unavailable('no accepted .msh or .su2 file was written')
    from foammesh.core.mesh.census import element_census

    try:
        census = element_census(path)
    except Exception as error:                            # noqa: BLE001
        return unavailable(f'the mesh file could not be counted: '
                           f'{_reason(error)}', source=path)
    if census.read_error:
        return unavailable(census.read_error, source=path)
    return {
        'path': path,
        'format': census.file_format,
        'points': measured(int(census.point_count), source=path,
                           unit='nodes'),
        'volume_by_family': measured(dict(census.volume_by_family),
                                     source=path),
        'surface_by_family': measured(dict(census.surface_by_family),
                                      source=path),
        'markers': measured(list(census.markers), source=path),
        'element_order': measured(int(census.element_order), source=path),
    }


def _headline(native, published, file_counts, target: str) -> dict:
    """The one cell count a study reads, and which file it was read from."""
    from foammesh.core.facade.results import is_measured

    if is_measured(published.get('cells')):
        return dict(published['cells'], basis='published polyMesh')
    if isinstance(native, dict) and is_measured(native.get('cells')):
        return dict(native['cells'], basis='run statistics')
    if isinstance(file_counts, dict) and is_measured(
            file_counts.get('volume_by_family')):
        value = _family_sum(file_counts['volume_by_family']['value'])
        return measured(value, source=file_counts['path'], unit='cells',
                        basis='mesh file')
    return unavailable('the case holds no counted mesh yet: run the mesh')


def _context(session) -> dict:
    """What is read from the live session, taken on the owner loop so the
    file reads can run on a worker thread."""
    return {'engine': _engine(session), 'target': _target(session),
            'requested': _layers_requested(session)}


def _counts(session, command, ctx):
    parameters = command.parameters or {}
    run_id = str(parameters.get('run_id') or '').strip()
    families = bool(parameters.get('families', True))
    chosen = _choose_run(session, run_id)
    case = _case(session)
    native = _native_counts(chosen)
    published = _published_counts(case, families)
    file_counts = _file_counts(case)
    return {
        'engine': ctx['engine'], 'target_solver': ctx['target'],
        'run': _run_header(chosen),
        'cells': _headline(native, published, file_counts, ctx['target']),
        'native': native, 'published': published, 'file': file_counts}


async def _results_counts(session, command):
    payload = await asyncio.to_thread(_counts, session, command,
                                      _context(session))
    return _read(session, command, payload)


def _quality(session, ctx) -> dict:
    from foammesh.core.gmsh.quality import REPORT_PATH as NATIVE_REPORT
    from foammesh.core.quality import summary as summary_module
    from foammesh.core.quality.checkmesh_service import (
        NATIVE_CHECK, SU2_READINESS_CHECK, MeshCheckService,
    )
    from foammesh.core.quality.verdict import verdict_from_report

    case = _case(session)
    document = summary_module.read(case)
    if document is None:
        summary = unavailable(
            'no quality summary has been composed for this mesh: run the '
            'quality checks')
    else:
        summary = measured({
            'disposition': document.get('disposition'),
            'qualified': document.get('qualified'),
            'stale': document.get('stale'),
            'stale_reason': document.get('stale_reason'),
            'mode': document.get('mode'),
            'blocks': document.get('blocks') or {}},
            source='foammesh/quality/summary.json')
    checks = {}
    for check in (NATIVE_CHECK, SU2_READINESS_CHECK):
        try:
            report = MeshCheckService.load_report(case, check)
        except (OSError, ValueError) as error:
            checks[check] = unavailable(
                f'the {check} report cannot be read: {_reason(error)}')
            continue
        if report is None:
            checks[check] = unavailable(f'{check} has not run on this mesh')
            continue
        projected = verdict_from_report(report)
        checks[check] = measured({
            'verdict': projected.get('verdict'),
            'source': projected.get('source'),
            'metrics': projected.get('metrics'),
            'failed_checks': projected.get('failedChecks'),
            'failed_check_names': projected.get('failedCheckNames'),
            'blocking': projected.get('blocking'),
            'advisory': projected.get('advisory'),
            'stale': projected.get('stale'),
            'checked_at': projected.get('checkedAt')},
            source=f'foammesh/quality/{check}')
    native = _read_json(case / NATIVE_REPORT)
    if native is None:
        gmsh_gate = unavailable(
            'no Gmsh element quality report: it is written by a Gmsh run')
    else:
        gmsh_gate = measured({
            'verdict': native.get('gate_verdict') or native.get('verdict'),
            'metrics': native.get('metrics'),
            'measure': native.get('measure'),
            'below_threshold': native.get('below_threshold'),
            'total': native.get('total'),
            'minimum': native.get('minimum'),
            'reason': native.get('reason')}, source=NATIVE_REPORT)
    chosen = _choose_run(session)
    achieved = None
    if chosen is not None:
        achieved = (chosen[1].get('statistics') or {}).get('achievedQuality')
    return {
        'engine': ctx['engine'], 'target_solver': ctx['target'],
        'summary': summary, 'checks': checks, 'gmsh_element_quality': gmsh_gate,
        'achieved_at_run': (
            measured(achieved, source='run statistics.achievedQuality')
            if achieved else unavailable(
                'the latest run recorded no achieved-quality block')),
    }


async def _results_quality(session, command):
    payload = await asyncio.to_thread(_quality, session, _context(session))
    return _read(session, command, payload)


def _deviation(session, ctx) -> dict:
    from foammesh.core.quality.geometry_fidelity import report as fidelity

    case = _case(session)
    document = fidelity.latest(case)
    if document is None:
        return {'engine': ctx['engine'], 'deviation': unavailable(
            'the geometry deviation has not been measured: run the '
            'geometry fidelity check')}
    sections = []
    for item in document.get('sections') or ():
        sections.append({
            'name': item.get('name'), 'verdict': item.get('verdict'),
            'deviation': item.get('deviation'),
            'tolerance': item.get('tolerance'), 'ratio': item.get('ratio'),
            'reason': item.get('reason')})
    values = [item['deviation'] for item in sections
              if isinstance(item['deviation'], (int, float))]
    source = f'foammesh/quality/{document.get("task_id")} report'
    payload = {
        'engine': ctx['engine'], 'task_id': document.get('task_id'),
        'verdict': document.get('verdict'),
        'sections': sections,
        'measured_sections': len(values),
        'unmeasured_sections': len(sections) - len(values),
        'evidence': {key: document.get(key) for key in (
            'subject_mesh_fingerprint', 'checkpoint_fingerprint',
            'prepared_revision') if document.get(key)},
    }
    if values:
        worst = max(sections, key=lambda item: item['deviation']
                    if isinstance(item['deviation'], (int, float))
                    else float('-inf'))
        payload['maximum'] = measured(max(values), source=source, unit='m',
                                      section=worst['name'])
        payload['mean'] = measured(sum(values) / len(values), source=source,
                                   unit='m', basis='mean of section maxima')
    else:
        payload['maximum'] = unavailable(
            'no section was measured (every section reports why)',
            source=source)
        payload['mean'] = payload['maximum']
    return payload


async def _results_deviation(session, command):
    payload = await asyncio.to_thread(_deviation, session, _context(session))
    return _read(session, command, payload)


def _layers_asked_gmsh(configuration, read_value, adapters) -> dict:
    enabled = read_value(configuration, 'gmsh/boundaryLayers/enabled')
    return {'asked': str(enabled).lower() in ('true', '1'),
            'layer_count': read_value(configuration,
                                      'gmsh/boundaryLayers/layerCount')}


def _layers_asked_snappy(configuration, read_value, adapters) -> dict:
    groups = read_value(configuration,
                        adapters['meshing.layers.groups'].storage_path)
    return {'asked': bool(groups), 'groups': len(groups) if groups else 0}


#: How each route says what layers the project asks for.
_LAYERS_ASKED = {'gmsh': _layers_asked_gmsh, 'snappy': _layers_asked_snappy}


def _layers_requested(session) -> dict:
    """What the project asks for, so "none grown" can be told from "none
    asked for"."""
    reader = _LAYERS_ASKED.get(_engine(session))
    if reader is None:
        return {'asked': None}
    try:
        from foammesh.core.facade.field_adapters import (
            build_entity_adapters, read_value,
        )
        from foammesh.core.facade.fields import REGISTRY

        return reader(session.configuration(), read_value,
                      build_entity_adapters(REGISTRY.collections))
    except Exception:                                      # noqa: BLE001
        return {'asked': None}


def _layer_coverage(session, ctx) -> dict:
    case = _case(session)
    path = 'foammesh/quality/layer-coverage.json'
    document = _read_json(case / path)
    requested = ctx['requested']
    payload = {'engine': ctx['engine'], 'requested': requested}
    chosen = _choose_run(session)
    if chosen is not None:
        layers = (chosen[1].get('statistics') or {}).get('layers')
        if isinstance(layers, dict):
            payload['run_request'] = measured(
                {key: layers.get(key) for key in (
                    'requestedLayers', 'requestedFirstHeight',
                    'requestedTotalThickness', 'extrudedSurfaces',
                    'skippedSurfaces')},
                source='run statistics.layers')
    if document is None:
        if requested.get('asked') is False:
            reason = 'no boundary layers are asked for in this project'
        else:
            reason = ('no layer coverage was written: the layer stage has '
                      'not run on this mesh')
        payload['coverage'] = unavailable(reason, source=path)
        return payload
    patches = list(document.get('patches') or ())
    payload['coverage'] = measured(
        {'patches': patches, 'warnings': list(document.get('warnings') or ())},
        source=path)
    return payload


async def _results_layer_coverage(session, command):
    payload = await asyncio.to_thread(_layer_coverage, session,
                                      _context(session))
    return _read(session, command, payload)


#: Where a run may have recorded the peak memory its engine reached, in the
#: order they are looked for. The Gmsh runner writes ``resources`` itself; for
#: snappy and the OpenFOAM utilities the WSL wrapper measures it
#: (``/usr/bin/time -f %M``, Plan 48 PM) and the job result carries it into
#: the same key.
_MEMORY_KEYS = (('peak_rss_bytes',), ('resources', 'peak_rss_bytes'),
                ('statistics', 'resources', 'peakRssBytes'),
                ('statistics', 'peakRssBytes'))


def _dig(document, path):
    node = document
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _run_cost(session, command, ctx) -> dict:
    parameters = command.parameters or {}
    chosen = _choose_run(session, str(parameters.get('run_id') or '').strip())
    payload = {'engine': ctx['engine'], 'run': _run_header(chosen)}
    if chosen is None:
        absent = unavailable('the case has no mesh run yet')
        payload.update(wall_seconds=absent, meshing_seconds=absent,
                       peak_memory_bytes=absent)
        return payload
    root, document = chosen
    run = str(document.get('run_id') or root.name)
    started = _parse_time(document.get('started_at')
                          or document.get('created_at'))
    ended = _parse_time(document.get('finished_at'))
    if started is None or ended is None:
        payload['wall_seconds'] = unavailable(
            'the run recorded no start or no end time (it did not finish)',
            source=f'run {run}')
    else:
        payload['wall_seconds'] = measured(
            round((ended - started).total_seconds(), 3),
            source=f'run {run}: finished_at minus started_at', unit='s')
    mesh = (document.get('statistics') or {}).get('mesh')
    if isinstance(mesh, dict) and 'seconds' in mesh:
        payload['meshing_seconds'] = measured(
            float(mesh['seconds']), source=f'run {run}: statistics.mesh',
            unit='s')
    else:
        payload['meshing_seconds'] = unavailable(
            'this run did not time its meshing pass separately',
            source=f'run {run}')
    for path in _MEMORY_KEYS:
        found = _dig(document, path)
        if isinstance(found, (int, float)) and found > 0:
            payload['peak_memory_bytes'] = measured(
                int(found), source=f'run {run}: ' + '.'.join(path),
                unit='bytes')
            break
    else:
        why = (document.get('resources') or {}).get('reason') if isinstance(
            document.get('resources'), dict) else None
        payload['peak_memory_bytes'] = unavailable(
            f'the run did not record its peak memory: {why}' if why else
            'the run did not record its peak memory (it ran before runs '
            'were measured, or never reached the mesher)',
            source=f'run {run}')
    return payload


async def _results_run_cost(session, command):
    payload = await asyncio.to_thread(_run_cost, session, command,
                                      _context(session))
    return _read(session, command, payload)


def _snapshot(session, command, ctx) -> dict:
    return {'counts': _counts(session, command, ctx),
            'quality': _quality(session, ctx),
            'deviation': _deviation(session, ctx),
            'layer_coverage': _layer_coverage(session, ctx),
            'run_cost': _run_cost(session, command, ctx)}


async def _results_snapshot(session, command):
    """The five results at one revision, for a client that wants them whole."""
    payload = await asyncio.to_thread(_snapshot, session, command,
                                      _context(session))
    return _read(session, command, payload)


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

_RUN = {'type': 'object', 'properties': {'run_id': {'type': 'string'}}}
_PATH = {'type': 'object', 'required': ['path'], 'properties': {
    'path': {'type': 'string'}}}

OPERATIONS = (
    OperationDescriptor(
        'sizing.controls.list', 'List the size controls', 'case',
        ImpactClass.READ,
        summary='Every size control of the Gmsh route, the snappyHexMesh '
                'route or both: the fields with their unit, range and '
                'current value, and the row collections (size fields, '
                'refinements, layer groups) with their rows and the '
                'operations that change them.',
        parameters_schema={'type': 'object', 'properties': {
            'route': {'enum': ['configured', 'gmsh', 'snappy', 'all']},
            'rows': {'type': 'boolean'}}}),
    OperationDescriptor(
        'sizing.controls.set', 'Set size controls', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        summary='Set size control fields by id in one step, with the rules of '
                'configuration.patch. A field that is not a size control is '
                'refused. dry_run reports the change without making it.',
        parameters_schema={'type': 'object', 'required': ['values'],
                           'properties': {'values': {'type': 'object'},
                                          'dry_run': {'type': 'boolean'}}}),
    OperationDescriptor(
        'results.counts', 'Read the element and cell counts', 'case',
        ImpactClass.READ,
        summary='Cells, points, faces and patches of the mesh: from the '
                'run, from the published polyMesh and from the accepted '
                '.msh/.su2 file, each measured or unavailable with a reason.',
        parameters_schema={'type': 'object', 'properties': {
            'run_id': {'type': 'string'}, 'families': {'type': 'boolean'}}}),
    OperationDescriptor(
        'results.quality', 'Read the mesh quality results', 'case',
        ImpactClass.READ,
        summary='The stored quality summary, the checkMesh and SU2 readiness '
                'reports, the Gmsh element quality and the quality the latest '
                'run achieved; each measured or unavailable.'),
    OperationDescriptor(
        'results.deviation', 'Read the geometry deviation', 'case',
        ImpactClass.READ,
        summary='The stored geometry fidelity report: the deviation of each '
                'section in metres, the maximum and the mean, or why there '
                'is none.'),
    OperationDescriptor(
        'results.layer_coverage', 'Read the layer coverage', 'case',
        ImpactClass.READ,
        summary='Layers grown per patch against what the project asked for, '
                'on either route, or why there is no measurement.'),
    OperationDescriptor(
        'results.run_cost', 'Read the run time and peak memory', 'case',
        ImpactClass.READ,
        summary='Wall time of a mesh run and of its meshing pass, and the '
                'peak memory when the run recorded it; each measured or '
                'unavailable with the reason.',
        parameters_schema=_RUN),
    OperationDescriptor(
        'results.snapshot', 'Read every study result at one revision', 'case',
        ImpactClass.READ,
        summary='results.counts, results.quality, results.deviation, '
                'results.layer_coverage and results.run_cost together.',
        parameters_schema={'type': 'object', 'properties': {
            'run_id': {'type': 'string'}, 'families': {'type': 'boolean'}}}),
    OperationDescriptor(
        'gmsh.model.open', 'Open a file in the Gmsh session', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Replace the Gmsh model with a file, as the Gmsh Open command does. '
                'Lists of readable types: gmsh.reader.formats.',
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': {'type': 'string'},
                                          'options': {'type': 'object'}}}),
    OperationDescriptor(
        'gmsh.model.merge', 'Merge a file into the Gmsh session', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Add a file to the Gmsh model, as the Gmsh Merge command does.',
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': {'type': 'string'},
                                          'options': {'type': 'object'}}}),
    OperationDescriptor(
        'gmsh.model.clear', 'Clear the Gmsh session', 'case',
        ImpactClass.DESTRUCTIVE,
        summary='Empty the Gmsh model, as the Gmsh Clear command does.'),
    OperationDescriptor(
        'gmsh.statistics.save', 'Save the mesh statistics as .pos', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Save the mesh as a post-processing view of the chosen '
                'quality measures (gamma, sicn, sige, disto, elementary, '
                'element).',
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {
                               'path': {'type': 'string'},
                               'gamma': {'type': 'boolean'},
                               'sicn': {'type': 'boolean'},
                               'sige': {'type': 'boolean'},
                               'disto': {'type': 'boolean'},
                               'elementary': {'type': 'boolean'},
                               'element': {'type': 'boolean'},
                               'save_all': {'type': 'boolean'}}}),
    OperationDescriptor(
        'gmsh.visibility.save', 'Save the visibility as .vis', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Save which entities are hidden, as the Gmsh Save as .vis command does.',
        parameters_schema=_PATH),
    OperationDescriptor(
        'gmsh.image.save', 'Save the viewport as a picture', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Save the current viewport view in a picture format '
                '(gmsh.image.formats lists them). Needs the window.',
        parameters_schema={'type': 'object', 'required': ['path', 'format'],
                           'properties': {'path': {'type': 'string'},
                                          'format': {'type': 'string'},
                                          'options': {'type': 'object'}}}),
    OperationDescriptor(
        'gmsh.movie.record', 'Record a movie of the viewport', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Record an MP4 of the viewport (gmsh.movie.status says '
                'whether it can be encoded). Needs the window.',
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': {'type': 'string'},
                                          'options': {'type': 'object'}}}),
    OperationDescriptor(
        'gmsh.watch.start', 'Start watching a file pattern', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Merge the files matching a pattern as they appear or '
                'change (the Gmsh Watch pattern command). Needs the window.',
        parameters_schema={'type': 'object', 'required': ['pattern'],
                           'properties': {'pattern': {'type': 'string'},
                                          'interval': {'type': 'number'}}}),
    OperationDescriptor(
        'gmsh.watch.stop', 'Stop watching a file pattern', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        summary='Stop the Watch pattern. Needs the window.'),
    OperationDescriptor(
        'gmsh.watch.status', 'Say what the Watch pattern is doing', 'case',
        ImpactClass.READ,
        summary='The Watch pattern, its interval and the files it merged. '
                'Needs the window.'),
)


def handlers() -> dict:
    return {
        'sizing.controls.list': _controls_list,
        'sizing.controls.set': _controls_set,
        'results.counts': _results_counts,
        'results.quality': _results_quality,
        'results.deviation': _results_deviation,
        'results.layer_coverage': _results_layer_coverage,
        'results.run_cost': _results_run_cost,
        'results.snapshot': _results_snapshot,
        'gmsh.model.open': _model_open,
        'gmsh.model.merge': _model_merge,
        'gmsh.model.clear': _model_clear,
        'gmsh.statistics.save': _statistics_save,
        'gmsh.visibility.save': _visibility_save,
        'gmsh.image.save': _image_save,
        'gmsh.movie.record': _movie_record,
        'gmsh.watch.start': _watch_start,
        'gmsh.watch.stop': _watch_stop,
        'gmsh.watch.status': _watch_status,
    }
