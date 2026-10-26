"""CVT surface remeshing on the Gmsh route: the host side (Plan 48 GM16b).

"Remesh surface (CVT)" cleans a poor STL or CAD tessellation into an even,
well-shaped triangle surface with Plugin(CVTRemesh) of FoamMesh's own Gmsh
build (GM16a). The plugin takes every surface of the model and returns one
new surface (``plugin/CVTRemesh.cpp:90``), so a selection is run in an
isolated worker process (``resources/gmsh/cvt_worker_v1.py``, started by
``session_cvt_v1``) and the result is read back:

* **keep** puts the result beside the originals;
* **replace** swaps the originals for it, and only when every check of the
  replacement contract passed (one new face per original face, interfaces that
  conform, volumes still closed, a stitch no worse than the remeshed surface).
  Otherwise it is refused with the failed checks named, and keeping both
  remains. A pick of several separate pieces (two walls) is remeshed piece by
  piece, so a piece never fuses with another.

Everything that changes the model is preceded by a session checkpoint, and
:func:`undo` restores it. No import of Qt; the window, the local API and the
tests all call these functions.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import weakref
from pathlib import Path

from . import plugin_catalogue as catalogue

PLUGIN_NAME = 'CVTRemesh'
#: Where the panel is placed on the Geometry row.
GEOMETRY_AREA = 'geometry.import_healing'
PANEL_TITLE = 'Remesh surface (CVT)'

#: Said when Gmsh is the official wheel (it has no CVTRemesh).
NO_BUILD = ("Remesh surface (CVT) needs FoamMesh's own Gmsh build, which has "
            "Revoropt. Set it up from the Preparation page (\"Set up "
            "FoamMesh's Gmsh\") and restart the Gmsh session.")


class CvtRefusal(RuntimeError):
    """The action cannot run; the message says why."""


def worker():
    """The shared module of the worker, loaded once from the resource tree."""
    module = sys.modules.get('foammesh._cvt_worker')
    if module is not None:
        return module
    from resources import resource

    location = Path(resource.file('gmsh/cvt_worker_v1.py')).resolve()
    spec = importlib.util.spec_from_file_location('foammesh._cvt_worker',
                                                  location)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- the plugin entry ---------------------------------------------------------- #

_HELP = {
    'Sites': 'Sample points on the surface, not triangles. About twice as '
             'many triangles come out.',
    'Iterations': 'LBFGS iterations that even out the sampling.',
    'Anisotropy': 'Weight of the surface normal in the sampling distance.',
    'Variable density': '0 gives a uniform size; higher refines where the '
                        'curvature is high.',
    'Feature sensitivity': 'How strongly sharp edges are kept.',
    'Normal computation radius':
        'Not in model units: a fraction of the scale radius Revoropt divides '
        'the mesh by. The length equivalent is shown beside it.',
}


def options() -> tuple:
    """The six options with Gmsh's own defaults."""
    out = []
    for name, default in worker().CVT_DEFAULTS.items():
        kind = 'int' if name in ('Sites', 'Iterations') else 'float'
        out.append(catalogue.PluginOption(name, kind, default, _HELP[name]))
    return tuple(out)


def plugin_entry() -> catalogue.Plugin:
    return catalogue.Plugin(
        PLUGIN_NAME, PANEL_TITLE,
        'Even, well-shaped triangles from a poor surface mesh, for the '
        'picked surfaces only.',
        options=options(), needs_view=False, changes_mesh=True,
        unavailable='Use Remesh surface (CVT): it runs the picked surfaces '
                    'in an isolated worker, because the plugin itself takes '
                    'every surface of the model.',
        shows='a remeshed surface', source='GM16b',
        help='Plugin(CVTRemesh) of FoamMesh\'s Gmsh 4.15.2 build.')


def register_plugin() -> catalogue.Plugin:
    """List CVTRemesh in the Plugins catalogue (once)."""
    if PLUGIN_NAME in catalogue.BY_NAME:
        return catalogue.BY_NAME[PLUGIN_NAME]
    return catalogue.register_extra(plugin_entry())


def unregister_plugin() -> None:
    catalogue.unregister_extra(PLUGIN_NAME)


def register_window_entries(factory=None) -> None:
    """Register the Plugins form and the Geometry-row panel in the registry.

    ``factory(parent)`` builds the panel; the default builds
    :class:`~foammesh.view.gmsh_workflow.cvt_panel.CvtRemeshPanel`. Calling it
    twice is harmless. The window (GM11b) mounts what is registered.
    """
    from foammesh.view.gmsh_tools import registry
    from foammesh.view.gmsh_workflow import plugins_panel

    register_plugin()
    if factory is None:
        def factory(parent=None):
            from foammesh.view.gmsh_workflow.cvt_panel import CvtRemeshPanel
            return CvtRemeshPanel(parent=parent)
    known = {e.id for e in registry.panels()}
    plugin_id = plugins_panel.extra_panel_id(PLUGIN_NAME)
    if plugin_id not in known:
        registry.register_panel(plugin_id, PANEL_TITLE, factory,
                                area=plugins_panel.PLUGIN_PANEL_AREA)
    if 'gmsh.geometry.cvt_remesh' not in known:
        registry.register_panel('gmsh.geometry.cvt_remesh', PANEL_TITLE,
                                factory, area=GEOMETRY_AREA, order=300)


# -- the formulas -------------------------------------------------------------- #

def sites_for_edge_length(area: float, edge: float) -> int:
    try:
        return worker().sites_for_edge_length(area, edge)
    except ValueError as error:
        raise CvtRefusal(str(error)) from None


def edge_length_for_sites(area: float, sites: float) -> float:
    try:
        return worker().edge_length_for_sites(area, sites)
    except ValueError as error:
        raise CvtRefusal(str(error)) from None


def radius_to_length(native: float, scale: float) -> float:
    return worker().radius_to_length(native, scale)


def length_to_radius(length: float, scale: float) -> float:
    return worker().length_to_radius(length, scale)


def convert(*, area=None, scale=None, sites=None, edge=None,
            radius=None, radius_length=None) -> dict:
    """Whatever of the paired fields can be worked out from what was given."""
    out = {}
    if area:
        if sites:
            out['target_edge_length'] = edge_length_for_sites(area, sites)
        if edge:
            out['sites'] = sites_for_edge_length(area, edge)
    if scale:
        if radius is not None:
            out['normal_radius_length'] = radius_to_length(radius, scale)
        if radius_length is not None:
            out['normal_radius'] = length_to_radius(radius_length, scale)
    return out


# -- the session --------------------------------------------------------------- #

_state_lock = threading.Lock()
_STATE: 'weakref.WeakKeyDictionary' = weakref.WeakKeyDictionary()


def _state(controller) -> dict:
    with _state_lock:
        try:
            return _STATE.setdefault(controller, {'runs': {}, 'undo': [],
                                                  'counter': 0})
        except TypeError:                     # not weak-referenceable
            holder = controller.__dict__.setdefault(
                '_cvt_state', {'runs': {}, 'undo': [], 'counter': 0})
            return holder


def build_notice(session) -> str:
    """'' when the session runs FoamMesh's Gmsh build, else the sentence."""
    from . import runtime

    text = (getattr(session, 'hello', None) or {}).get('build', '')
    options = runtime.build_options_from(text)
    if runtime.build_kind(options,
                          (session.hello or {}).get('gmsh_file', '')) \
            == runtime.BUILD_FOAMMESH:
        return ''
    return NO_BUILD


def _live(controller):
    session = getattr(controller, 'session', None)
    if session is None or not session.alive:
        raise CvtRefusal('there is no mesh yet; generate the surface mesh '
                         'first')
    return session


def _faces(items) -> list:
    out = []
    for item in items or []:
        if isinstance(item, (list, tuple)):
            if int(item[0]) != 2:
                raise CvtRefusal('only surfaces can be remeshed')
            item = item[1]
        out.append(int(item))
    out = sorted(set(out))
    if not out:
        raise CvtRefusal('pick at least one surface')
    return out


def cvt_directory(controller) -> Path:
    return Path(controller.case_path) / 'foammesh' / 'gmsh' / 'cvt'


def measure(controller, faces) -> dict:
    """Area, counts and scale of the picked surfaces, with the equivalents."""
    faces = _faces(faces)
    with controller._lock:
        session = _live(controller)
        try:
            found = session.call('session.cvt_measure', faces=faces)
        except Exception as error:                    # noqa: BLE001
            raise CvtRefusal(_message(error)) from None
    found['defaults'] = dict(worker().CVT_DEFAULTS)
    found['normal_radius_length'] = radius_to_length(
        worker().CVT_DEFAULTS['Normal computation radius'], found['scale'])
    found['sites_default_edge_length'] = edge_length_for_sites(
        found['area'], worker().CVT_DEFAULTS['Sites'])
    return found


def _message(error) -> str:
    text = str(error)
    return text.split('Error: ')[-1] if text else 'the session refused'


def run(controller, faces, options=None, *, target_edge_length=None,
        replace=False, tolerance=None, apply=True, timeout=900.0) -> dict:
    """Remesh the picked surfaces in the isolated worker.

    With ``apply`` the result goes into the model: beside the originals, or in
    their place when ``replace`` is set and the checks passed. A refused
    replacement keeps the run (``run_id``) so "Keep both" can still be chosen.
    """
    faces = _faces(faces)
    values = {k: float(v) for k, v in (options or {}).items()}
    unknown = [k for k in values if k not in worker().CVT_DEFAULTS]
    if unknown:
        raise CvtRefusal('CVTRemesh has no option %s' % unknown[0])
    for name in ('Sites', 'Iterations'):
        if name in values and values[name] < (4 if name == 'Sites' else 0):
            raise CvtRefusal('%s is too small' % name)
    if values.get('Normal computation radius', 1) <= 0:
        raise CvtRefusal('the normal computation radius must be positive')
    with controller._lock:
        session = _live(controller)
        notice = build_notice(session)
        if notice:
            raise CvtRefusal(notice)
        holder = _state(controller)
        holder['counter'] += 1
        run_id = 'run%03d' % holder['counter']
        directory = cvt_directory(controller) / run_id
        directory.mkdir(parents=True, exist_ok=True)
        try:
            report = session.call(
                'session.cvt_run', faces=faces,
                directory=session.runtime_path(directory), options=values,
                target_edge_length=target_edge_length,
                mode='replace' if replace else 'keep', tolerance=tolerance,
                limit=timeout)
        except Exception as error:                    # noqa: BLE001
            raise CvtRefusal(_message(error)) from None
        if 'refused' in report:
            raise CvtRefusal(report['refused'])
        report['run_id'] = run_id
        report['host_directory'] = str(directory)
        holder['runs'][run_id] = {'directory': directory, 'faces': faces,
                                  'replace': bool(replace), 'applied': None,
                                  'report': report}
        report['summary'] = summary(report)
        if apply:
            mode = 'replace' if (replace and report['replace']['ok']) \
                else 'keep'
            report['applied'] = apply_run(controller, run_id, mode)
            if replace and mode == 'keep':
                report['applied']['note'] = report['replace'].get(
                    'refusal', '') + ' The result was kept beside the ' \
                    'originals instead.'
        return report


def apply_run(controller, run_id: str, mode: str = 'keep',
              name: str = '') -> dict:
    """Put a finished run into the model, after a checkpoint."""
    with controller._lock:
        session = _live(controller)
        holder = _state(controller)
        entry = holder['runs'].get(run_id)
        if entry is None:
            raise CvtRefusal('there is no run %s' % run_id)
        if mode not in ('keep', 'replace'):
            raise CvtRefusal("mode must be 'keep' or 'replace'")
        if mode == 'replace':
            replace = (entry['report'].get('replace') or {})
            if not replace.get('requested'):
                raise CvtRefusal('this run was made without Replace '
                                 'originals; run it again with it on')
            if not replace.get('ok'):
                raise CvtRefusal(replace.get('refusal')
                                 or 'Replace is refused: a check failed')
        checkpoint = 'cvt-' + run_id
        session.call('session.checkpoint_save', checkpoint, mesh=True)
        try:
            done = session.call(
                'session.cvt_apply',
                directory=session.runtime_path(entry['directory']),
                mode=mode, name=name or 'CVT remesh ' + run_id)
        except Exception as error:                    # noqa: BLE001
            try:
                session.call('session.checkpoint_restore', checkpoint)
            except Exception:                         # noqa: BLE001
                pass
            raise CvtRefusal(_message(error)) from None
        entry['applied'] = mode
        holder['undo'].append({'run_id': run_id, 'checkpoint': checkpoint,
                               'mode': mode})
        done['run_id'] = run_id
        done['checkpoint'] = checkpoint
        return done


def undo(controller) -> dict:
    """Restore the checkpoint taken before the last remesh."""
    with controller._lock:
        session = _live(controller)
        holder = _state(controller)
        if not holder['undo']:
            raise CvtRefusal('there is no remesh to undo')
        last = holder['undo'][-1]
        try:
            back = session.call('session.checkpoint_restore',
                                last['checkpoint'])
        except Exception as error:                    # noqa: BLE001
            raise CvtRefusal(_message(error)) from None
        holder['undo'].pop()
        holder['runs'].get(last['run_id'], {})['applied'] = None
        return {'undone': last['run_id'], 'mode': last['mode'],
                'verified': bool(back.get('verified')),
                'differences': back.get('differences') or [],
                'counts': back.get('counts')}


def preview(controller, run_id: str) -> dict:
    """The before and after surfaces as ``.vtu`` files, coloured by gamma."""
    with controller._lock:
        session = _live(controller)
        entry = _state(controller)['runs'].get(run_id)
        if entry is None:
            raise CvtRefusal('there is no run %s' % run_id)
        out = entry['directory'] / 'preview'
        answer = session.call(
            'session.cvt_describe',
            directory=session.runtime_path(entry['directory']),
            out_directory=session.runtime_path(out))
        for row in answer['rows'].values():
            row['host_path'] = str(out / Path(row['path']).name)
        answer['run_id'] = run_id
        return answer


def status(controller) -> dict:
    holder = _state(controller)
    session = getattr(controller, 'session', None)
    live = session is not None and session.alive
    return {
        'runs': [{'run_id': k, 'faces': v['faces'], 'applied': v['applied'],
                  'replace_requested': v['replace'],
                  'replace_ok': (v['report'].get('replace') or {}).get('ok')}
                 for k, v in sorted(holder['runs'].items())],
        'can_undo': bool(holder['undo']),
        'undo': list(holder['undo']),
        'build_notice': build_notice(session) if live else NO_BUILD}


def export_surface(controller, run_id: str, path) -> dict:
    """Write the result of a run as an ASCII STL (a new surface, no model edit)."""
    import numpy as np

    entry = _state(controller)['runs'].get(run_id)
    if entry is None:
        raise CvtRefusal('there is no run %s' % run_id)
    data = np.load(entry['directory'] / 'result.npz')
    if 'final_points' in data.files:
        points, tris = data['final_points'], data['final_tris']
    else:
        points, tris = data['raw_points'], data['raw_tris']
    w = worker()
    normals = w.tri_normals(points, tris)
    lines = ['solid cvt_remesh']
    for tri, n in zip(tris, normals):
        lines.append('facet normal %.9g %.9g %.9g' % tuple(n))
        lines.append(' outer loop')
        for v in tri:
            lines.append('  vertex %.9g %.9g %.9g' % tuple(points[v]))
        lines.append(' endloop')
        lines.append('endfacet')
    lines.append('endsolid cvt_remesh')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    return {'path': str(target), 'triangles': int(len(tris)), 'run_id': run_id}


# -- what the panel and the Statistics panel show ------------------------------- #

def _fmt(value, digits=4):
    return '-' if value is None else ('%.*g' % (digits, value))


def summary(report: dict) -> dict:
    """Before / after counts and quality, and the deviation, as rows."""
    before = report.get('before') or {}
    after = report.get('after') or report.get('raw') or {}
    dev = report.get('deviation') or {}
    rows = [
        ('Triangles', before.get('triangles'), after.get('triangles')),
        ('Points', before.get('points'), after.get('points')),
        ('Mean edge length', before.get('mean_edge'), after.get('mean_edge')),
        ('Gamma, minimum', before.get('gamma_min'), after.get('gamma_min')),
        ('Gamma, mean', before.get('gamma_mean'), after.get('gamma_mean')),
        ('Area', before.get('area'), after.get('area')),
    ]
    return {
        'rows': [{'name': n, 'before': b, 'after': a} for n, b, a in rows],
        'deviation': dev,
        'text': ('%s triangles became %s; minimum gamma %s -> %s; the '
                 'surface moved by at most %s (limit %s)%s.' % (
                     before.get('triangles'), after.get('triangles'),
                     _fmt(before.get('gamma_min')),
                     _fmt(after.get('gamma_min')), _fmt(dev.get('max')),
                     _fmt(dev.get('tolerance')),
                     '' if dev.get('within', True)
                     else ' (more than the limit)')),
    }


def report_json(report: dict) -> str:
    return json.dumps(report, indent=1, default=str)
