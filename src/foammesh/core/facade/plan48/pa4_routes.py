"""Plan 48 PA4 local-API routes: the Script, Statistics and Plugins panels,
the Options files, the Gmsh tool dock's links and menus, and the Export page's
source and ``.opt`` button.

A plain remote control for what a person has in the window.  The routes that
stand for a panel call the panel the window shows (the window registers
``PanelsApi`` and ``ToolsApi`` on the case session) and answer
``precondition_failed`` when no window shows the case; the routes that only
write a file or load a mesh into the session need no window.

``gmsh.options.opt.write`` / ``gmsh.options.defaults.save`` / ``.read``
    The Options panel's "Save .opt", "Save as my defaults" and "Use my
    defaults" (the saved defaults alone, not merged with the start-up file).
``gmsh.script.read`` / ``.save`` / ``.reload`` / ``.onelab`` / ``.run``
    The Script panel: the view, Save script, Reload from the edited script,
    Open .geo, the ONELAB parameters and Run again, Run script and its
    confirm strip.
``gmsh.statistics.read`` / ``.analyse``
    The Statistics panel's read-outs, Plot and Analyse mesh quality.
``gmsh.plugins.results`` / ``.view_show`` / ``.use_as_size_field`` / ``.run_in_panel``
    The Plugins panel's table (with its Shown column and the rows kept across
    runs), Show input view, Show, Hide, Use as size field and Run plugin.
``gmsh.tools.open`` / ``gmsh.tools.menus``
    The dock's links, the manipulator key, the rail popover and the Mesh menu
    entries that open the Generate panel; the menu entries as they show.
``gmsh.export.save_options`` / ``gmsh.export.load_run_mesh``
    The writer's "Save options as .opt" and the Accepted-run source.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

PANELS_ATTRIBUTE = 'gmsh_panels_api'
TOOLS_ATTRIBUTE = 'gmsh_tool_dock'

_LANGUAGE = {'type': 'string', 'enum': ['python', 'geo']}
_PATH = {'type': 'string'}


def _schema(required=(), **properties) -> dict:
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


OPERATIONS = (
    OperationDescriptor(
        'gmsh.options.opt.write', 'Save the Gmsh options as a .opt file',
        'case', ImpactClass.FILE_PRODUCING,
        summary='Write the options set from the Options list (or the given '
                'name: value pairs) to a Gmsh .opt file, as the Save .opt '
                'button does.',
        parameters_schema=_schema(
            ['path'], path=_PATH, options={'type': 'object'})),
    OperationDescriptor(
        'gmsh.options.defaults.save', 'Keep the Gmsh options as my defaults',
        'case', ImpactClass.FILE_PRODUCING,
        summary='Store the options set (or the given pairs) as the Gmsh '
                'defaults every new project starts with, as the Save as my '
                'defaults button does.',
        parameters_schema=_schema(options={'type': 'object'})),
    OperationDescriptor(
        'gmsh.options.defaults.read', 'Read my saved Gmsh defaults', 'case',
        ImpactClass.READ,
        summary='The saved defaults alone (not merged with the start-up .opt '
                'file), as the Use my defaults button would store them, and '
                'the patch for gmsh.options.overrides that makes them the '
                'set options.',
        parameters_schema=_schema()),
    OperationDescriptor(
        'gmsh.script.read', 'Script of the model', 'case', ImpactClass.READ,
        summary='The Python or .geo script of what the Gmsh session did, the '
                'sentence under the format box, the ONELAB parameters of an '
                'opened .geo and the last result; refresh=true rebuilds the '
                'view from the recorded calls.',
        parameters_schema=_schema(language=_LANGUAGE,
                                  refresh={'type': 'boolean'})),
    OperationDescriptor(
        'gmsh.script.save', 'Save the script', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Save the script in the chosen format to a .py or .geo '
                'file, as Save script does.',
        parameters_schema=_schema(['path'], path=_PATH, language=_LANGUAGE)),
    OperationDescriptor(
        'gmsh.script.reload', 'Reload the model from the edited script',
        'case', ImpactClass.MESH_MUTATION,
        summary='Put the given text in the .geo view (or use what the view '
                'holds), write it beside the project and open it in the '
                'session, as Reload model from this script does. Returns the '
                'ONELAB parameters the script defines.',
        parameters_schema=_schema(text={'type': 'string'})),
    OperationDescriptor(
        'gmsh.script.onelab', 'Open a .geo and set its ONELAB parameters',
        'case', ImpactClass.MESH_MUTATION,
        summary='path opens a .geo of your own in the session (Open .geo), '
                'values sets ONELAB parameters of the opened script and reads '
                'it again once, run_again reads it again with the current '
                'values (Run again); with none of them, lists the '
                'parameters.',
        parameters_schema=_schema(
            path=_PATH, values={'type': 'object'},
            run_again={'type': 'boolean'},
            rerun_on_change={'type': 'boolean'})),
    OperationDescriptor(
        'gmsh.script.run', 'Run a script on the model', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Run Python (gmsh API) or .geo lines on the model in the '
                'session. confirm=true (the default) answers the confirm '
                'strip with Run; confirm=false leaves the strip up; '
                'cancel=true answers Cancel. Returns the entity counts.',
        parameters_schema=_schema(
            language=_LANGUAGE, text={'type': 'string'},
            confirm={'type': 'boolean'}, cancel={'type': 'boolean'})),
    OperationDescriptor(
        'gmsh.statistics.read', 'Statistics of the mesh on screen', 'case',
        ImpactClass.READ,
        summary='Every read-out of the Statistics panel (nodes, elements, '
                'bounds, groups, counts, quality, passes, histogram) and the '
                'numbers behind them; refresh=false reads what the panel '
                'shows without reading again; plot picks the measure the '
                'histogram draws.',
        parameters_schema=_schema(
            refresh={'type': 'boolean'}, plot={'type': 'string'},
            dim={'type': ['string', 'integer']})),
    OperationDescriptor(
        'gmsh.statistics.analyse', 'Analyse mesh quality', 'case',
        ImpactClass.READ,
        summary="Run Gmsh's AnalyseMeshQuality plugin as the button does "
                'and return the measures table (Jacobian ratio, IGE, ICN: '
                'min, average, max, elements).',
        parameters_schema=_schema()),
    OperationDescriptor(
        'gmsh.plugins.results', 'Plugin results and what is drawn', 'case',
        ImpactClass.READ,
        summary='The Plugins panel: the chosen plugin, the input views, the '
                'results table (View, Name, Cells, Min, Average, Max, Shown) '
                'with the rows kept across runs, the keys drawn in the '
                'viewport, the panel message and the Gmsh log lines.',
        parameters_schema=_schema()),
    OperationDescriptor(
        'gmsh.plugins.view_show', 'Draw or hide a plugin view', 'case',
        ImpactClass.READ,
        summary='action show or hide draws or removes a result (by view tag '
                'or table row) in the viewport; show_input draws the chosen '
                'input view (tag picks it first).',
        parameters_schema=_schema(
            ['action'], action={'type': 'string',
                                'enum': ['show', 'hide', 'show_input']},
            tag={'type': 'integer'}, index={'type': 'integer', 'minimum': 0})),
    OperationDescriptor(
        'gmsh.plugins.use_as_size_field', 'Use a plugin view as a size field',
        'case', ImpactClass.REVERSIBLE_EDIT,
        summary='Write a PostView size field that reads the selected view as '
                'a background size, as Use as size field does.',
        parameters_schema=_schema(
            tag={'type': 'integer'}, index={'type': 'integer', 'minimum': 0})),
    OperationDescriptor(
        'gmsh.plugins.run_in_panel', 'Run a plugin through the panel', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Choose the plugin, set its options (omitted ones take '
                "Gmsh's defaults), pick the input view and press Run "
                'plugin: the results join the panel table and are drawn.',
        parameters_schema=_schema(
            ['name'], name={'type': 'string'}, options={'type': 'object'},
            view={'type': ['integer', 'null']})),
    OperationDescriptor(
        'gmsh.tools.open', 'Open a panel the Gmsh tools link to', 'case',
        ImpactClass.READ,
        summary='target entities, visibility or plugins opens that section '
                'of the dock (the panels are in the dock); the answer has '
                'section_open; manipulator is Shift+Ctrl+M (Camera tab); tools opens '
                'the Gmsh tools popover; generate_action opens the Generate '
                'panel with a Mesh menu action (action) chosen.',
        parameters_schema=_schema(
            ['target'], target={'type': 'string', 'enum': [
                'entities', 'visibility', 'plugins', 'manipulator', 'tools',
                'generate_action']},
            action={'type': 'string'})),
    OperationDescriptor(
        'gmsh.tools.menus', 'Gmsh menu entries and keys', 'case',
        ImpactClass.READ,
        summary='Each Gmsh menu entry as the window has it (menu, title, '
                'shortcut, shown, enabled) and the keys of the tool dock '
                'with whether each is live.',
        parameters_schema=_schema()),
    OperationDescriptor(
        'gmsh.export.save_options', 'Save the options of an export as .opt',
        'case', ImpactClass.FILE_PRODUCING,
        summary='Write the options set for one writer as a Gmsh .opt file, '
                'as the writer panel\'s Save options as .opt does.',
        parameters_schema=_schema(
            ['writer', 'path'], writer={'type': 'string'}, path=_PATH,
            options={'type': 'object'})),
    OperationDescriptor(
        'gmsh.export.load_run_mesh', "Load the accepted run's mesh", 'case',
        ImpactClass.MESH_MUTATION,
        summary="Export source = Accepted run: open the accepted run's "
                'mesh.msh in the Gmsh session. When the session holds a '
                'model, confirm_replace must be true (the window asks the '
                'same question).',
        parameters_schema=_schema(confirm_replace={'type': 'boolean'})),
)


# -- shared ---------------------------------------------------------------- #

def replacement_set(mapping) -> dict:
    """What "replace the set options by *mapping*" stores.

    Each value is parsed as a typed value is, and an option whose value is
    Gmsh's own default is left out, exactly as the Options panel's
    ``replace_with`` does; a route and the button agree.
    """
    from foammesh.core.gmsh import option_catalogue as catalogue

    wanted = {}
    for name, value in dict(mapping or {}).items():
        option = catalogue.get(name)
        parsed = catalogue.parse_value(name, value)
        parsed = list(parsed) if isinstance(parsed, tuple) else parsed
        if not catalogue.same_value(option, parsed, option.default):
            wanted[name] = parsed
    return wanted


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _window_api(session, attribute):
    api = getattr(session, attribute, None)
    if api is None:
        raise PreconditionFailedError(
            'no window is showing this case, and this panel lives in it: '
            'open the case in the FoamMesh window',
            details={'needs': attribute})
    return api


def _call(session, attribute, method, *args, **kwargs) -> dict:
    api = _window_api(session, attribute)
    try:
        return dict(getattr(api, method)(*args, **kwargs) or {})
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error


async def _call_waiting(session, attribute, method, *args, **kwargs) -> dict:
    """A panel call that waits for a worker: not on the event loop."""
    api = _window_api(session, attribute)

    def work():
        return dict(getattr(api, method)(*args, **kwargs) or {})

    try:
        return await asyncio.to_thread(work)
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error


def _stored_options(session) -> dict:
    from foammesh.core.facade.plan48.gm4_routes import _stored_options as read
    return read(session)


def _given_or_stored(session, parameters) -> dict:
    given = parameters.get('options')
    return dict(given) if given is not None else _stored_options(session)


# -- options files ---------------------------------------------------------- #

def _opt_write(session, command) -> OperationResult:
    from foammesh.core.gmsh import option_catalogue as catalogue

    path = str(command.parameters['path'])
    mapping = _given_or_stored(session, command.parameters)
    try:
        catalogue.write_opt_file(path, mapping)
    except OSError as error:
        raise ValidationFailedError(f'{path}: {error}') from error
    return _result(session, command, {'path': path, 'options': mapping,
                                      'text': catalogue.to_opt_text(mapping)})


def _defaults_save(session, command) -> OperationResult:
    from foammesh.core.gmsh import option_catalogue as catalogue

    mapping = _given_or_stored(session, command.parameters)
    try:
        path = catalogue.save_defaults(mapping)
    except OSError as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, {'path': str(path), 'options': mapping})


def _defaults_read(session, command) -> OperationResult:
    from foammesh.core.gmsh import option_catalogue as catalogue

    try:
        mapping = catalogue.load_defaults()
        catalogue.resolve_overrides(mapping)
    except catalogue.OptionRefusal as error:
        raise ValidationFailedError(str(error)) from error
    wanted = replacement_set(mapping)
    path = catalogue.defaults_path()
    return _result(session, command, {
        'options': wanted, 'saved_defaults': str(path),
        'present': path.is_file(),
        'patch': {catalogue.OVERRIDES_FIELD:
                  catalogue.overrides_to_text(wanted)}})


# -- the panels in the window ------------------------------------------------ #

def _plain_params(command, *names) -> dict:
    p = dict(command.parameters or {})
    return {name: p[name] for name in names if name in p}


def _handlers_panels() -> dict:
    async def script_read(session, command):
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'script_read',
            **_plain_params(command, 'language', 'refresh')))

    async def script_save(session, command):
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'script_save',
            **_plain_params(command, 'path', 'language')))

    async def script_reload(session, command):
        return _result(session, command, await _call_waiting(
            session, PANELS_ATTRIBUTE, 'script_reload',
            **_plain_params(command, 'text')))

    async def script_onelab(session, command):
        return _result(session, command, await _call_waiting(
            session, PANELS_ATTRIBUTE, 'script_onelab',
            **_plain_params(command, 'path', 'values', 'run_again',
                            'rerun_on_change')))

    async def script_run(session, command):
        p = dict(command.parameters or {})
        if not p.get('cancel') and 'text' not in p:
            raise ValidationFailedError('the script to run is required')
        if not p.get('cancel') and 'language' not in p:
            raise ValidationFailedError('the script format is required')
        return _result(session, command, await _call_waiting(
            session, PANELS_ATTRIBUTE, 'script_run',
            p.get('language'), p.get('text', ''),
            confirm=p.get('confirm', True), cancel=bool(p.get('cancel'))))

    async def statistics_read(session, command):
        p = dict(command.parameters or {})
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'statistics',
            refresh=p.get('refresh', True), plot=p.get('plot'),
            dim=p.get('dim')))

    async def statistics_analyse(session, command):
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'analyse'))

    async def plugins_results(session, command):
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'plugins_results'))

    async def plugins_view_show(session, command):
        p = dict(command.parameters or {})
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'plugins_view', p['action'],
            tag=p.get('tag'), index=p.get('index')))

    async def plugins_size_field(session, command):
        p = dict(command.parameters or {})
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'plugins_size_field',
            tag=p.get('tag'), index=p.get('index')))

    async def plugins_run(session, command):
        p = dict(command.parameters or {})
        return _result(session, command, _call(
            session, PANELS_ATTRIBUTE, 'plugins_run', p['name'],
            options=p.get('options'), view=p.get('view')))

    async def tools_open(session, command):
        p = dict(command.parameters or {})
        return _result(session, command, _call(
            session, TOOLS_ATTRIBUTE, 'open_panel', p['target'],
            action=p.get('action')))

    async def tools_menus(session, command):
        return _result(session, command, _call(
            session, TOOLS_ATTRIBUTE, 'menus'))

    return {
        'gmsh.script.read': script_read,
        'gmsh.script.save': script_save,
        'gmsh.script.reload': script_reload,
        'gmsh.script.onelab': script_onelab,
        'gmsh.script.run': script_run,
        'gmsh.statistics.read': statistics_read,
        'gmsh.statistics.analyse': statistics_analyse,
        'gmsh.plugins.results': plugins_results,
        'gmsh.plugins.view_show': plugins_view_show,
        'gmsh.plugins.use_as_size_field': plugins_size_field,
        'gmsh.plugins.run_in_panel': plugins_run,
        'gmsh.tools.open': tools_open,
        'gmsh.tools.menus': tools_menus,
    }


# -- export ------------------------------------------------------------------ #

def _save_export_options(session, command) -> OperationResult:
    from foammesh.core.gmsh import option_catalogue as catalogue
    from foammesh.core.gmsh.export_service import (
        ExportRefused, MeshExporter)

    p = command.parameters
    try:
        target = MeshExporter.write_options_file(
            p['path'], str(p['writer']), dict(p.get('options') or {}))
    except (ExportRefused, catalogue.OptionRefusal, ValueError, KeyError
            ) as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, {
        'path': str(target), 'writer': str(p['writer']),
        'result': f'Options saved to {p["path"]}.'})


async def _load_run_mesh(session, command) -> OperationResult:
    from foammesh.core.gmsh import run_mesh as rm
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)

    confirmed = bool((command.parameters or {}).get('confirm_replace'))
    case_path = Path(session.case_path)
    try:
        run = rm.accepted_mesh(case_path)
    except rm.RunMeshRefused as error:
        raise PreconditionFailedError(str(error)) from error
    controller = controller_for_case_session(session)
    if not rm.needs_load(controller, run):
        return _result(session, command, {
            'loaded': False, 'run_id': run.run_id, 'path': str(run.path),
            'reason': 'the session already holds this run\'s mesh'})
    sentence = rm.question(controller, run)
    if sentence and not confirmed:
        raise PreconditionFailedError(
            sentence, details={'needs': 'confirm_replace'})

    def work():
        return rm.load(controller, run, lambda _text, _fraction: None)

    try:
        outcome = await asyncio.to_thread(work)
    except rm.RunMeshRefused as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, {
        'loaded': True, 'run_id': run.run_id, 'path': str(run.path),
        'replaced': bool(sentence),
        'outcome': outcome if isinstance(outcome, dict) else {}})


def handlers() -> dict:
    table = {
        'gmsh.options.opt.write': _opt_write,
        'gmsh.options.defaults.save': _defaults_save,
        'gmsh.options.defaults.read': _defaults_read,
        'gmsh.export.save_options': _save_export_options,
        'gmsh.export.load_run_mesh': _load_run_mesh,
    }
    table.update(_handlers_panels())
    return table
