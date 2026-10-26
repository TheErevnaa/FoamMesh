"""Plan 48 GM2: local-API routes for the Gmsh options and the drawing options.

A plain remote control for what the Options panel and the Drawing tab do: read
what the catalogue says about an option, check a value, read an old ``.opt``
file, read the start-up defaults, and read or set a drawing option.  Nothing
here is specific to any one client.

The options themselves are stored in the project (``gmsh.options.overrides``)
and are already writable through ``configuration.patch``; these routes add the
checks and the reading a person gets from the panel.
"""
from __future__ import annotations

from foammesh.core.facade.errors import ValidationFailedError
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

OPERATIONS = (
    OperationDescriptor(
        'gmsh.options.describe', 'Describe Gmsh options', 'case',
        ImpactClass.READ,
        summary='Kind, native range, choices, bits, evidence and the page '
                'control of Gmsh options, by name, group or search text.',
        parameters_schema={'type': 'object', 'properties': {
            'name': {'type': 'string'}, 'group': {'type': 'string'},
            'search': {'type': 'string'}}}),
    OperationDescriptor(
        'gmsh.options.validate', 'Check Gmsh option values', 'case',
        ImpactClass.READ,
        summary='Check {name: value} against the catalogue; answers the '
                'rows Gmsh would be given, or every refusal.',
        parameters_schema={'type': 'object', 'properties': {
            'options': {'type': 'object'}}}),
    OperationDescriptor(
        'gmsh.options.opt.read', 'Read a Gmsh .opt file', 'case',
        ImpactClass.READ,
        summary='Parse the text of a .opt file (deprecated names accepted) '
                'into {option: value}.',
        parameters_schema={'type': 'object', 'properties': {
            'text': {'type': 'string'}}}),
    OperationDescriptor(
        'gmsh.options.startup_defaults', 'Read the start-up Gmsh defaults',
        'case', ImpactClass.READ,
        summary='The options every project starts with: the start-up .opt '
                'file and the saved defaults.'),
    OperationDescriptor(
        'gmsh.display.options', 'List the Gmsh drawing options', 'case',
        ImpactClass.READ,
        summary='Every Gmsh display option that draws something, the viewport '
                'control that holds it, and its value.'),
    OperationDescriptor(
        'gmsh.display.set', 'Set Gmsh drawing options', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        summary='Set drawing options by Gmsh name, or reset them to '
                'Gmsh defaults. In the window this is what the Drawing '
                'tab does: its controls move, the values are kept and the '
                'viewport draws them; with no window the values are kept '
                'for the next time the panel is shown.',
        parameters_schema={'type': 'object', 'properties': {
            'options': {'type': 'object'}, 'reset': {'type': 'boolean'}}}),
)


def _describe(option) -> dict:
    from foammesh.core.gmsh import option_catalogue as catalogue

    return {
        'name': option.name, 'kind': option.kind, 'storage': option.storage,
        'default': option.default, 'unit': option.unit, 'page': option.page,
        'read_only': option.read_only,
        'choices': [list(item) for item in option.choices],
        'minimum': option.minimum, 'maximum': option.maximum,
        'hard': list(option.hard) if option.hard else None,
        'bits': [list(item) for item in option.bits],
        'ctype': option.ctype, 'evidence': option.evidence,
        'help': option.help,
        'page_control': catalogue.control_for_option().get(option.name, ''),
        'aliases': sorted(alias for alias, target in catalogue.ALIASES.items()
                          if target == option.name),
    }


def _read(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _describe_options(session, command):
    from foammesh.core.gmsh import option_catalogue as catalogue

    parameters = command.parameters or {}
    name = parameters.get('name')
    if name:
        option = catalogue.get(name)
        if option is None:
            raise ValidationFailedError(catalogue.refusal_for_unknown(name))
        found = [option]
        alias_of = catalogue.canonical_name(name) if \
            catalogue.is_alias(name) else ''
    else:
        found = list(catalogue.search(parameters.get('search', ''),
                                      parameters.get('group', '')))
        alias_of = ''
    return _read(session, command, {
        'count': len(found), 'alias_of': alias_of,
        'options': [_describe(option) for option in found]})


def _validate_options(session, command):
    from foammesh.core.gmsh import option_catalogue as catalogue

    mapping = (command.parameters or {}).get('options')
    if not isinstance(mapping, dict):
        raise ValidationFailedError('options must be an object of '
                                    'name: value pairs')
    try:
        rows, warnings = catalogue.resolve_overrides(mapping)
    except catalogue.OptionRefusal as error:
        raise ValidationFailedError(str(error)) from None
    return _read(session, command, {
        'rows': [{'name': n, 'storage': s, 'value': v} for n, s, v in rows],
        'warnings': list(warnings)})


def _read_opt(session, command):
    from foammesh.core.gmsh import option_catalogue as catalogue

    text = (command.parameters or {}).get('text')
    if not isinstance(text, str):
        raise ValidationFailedError('text must be the contents of a .opt file')
    try:
        mapping = catalogue.from_opt_text(text)
        catalogue.resolve_overrides(mapping)
    except catalogue.OptionRefusal as error:
        raise ValidationFailedError(str(error)) from None
    return _read(session, command, {'options': mapping})


def _startup_defaults(session, command):
    from foammesh.core.gmsh import option_catalogue as catalogue

    try:
        mapping = catalogue.startup_defaults()
    except catalogue.OptionRefusal as error:
        raise ValidationFailedError(str(error)) from None
    return _read(session, command, {
        'options': mapping, 'startup_file': str(catalogue.startup_opt_path()),
        'saved_defaults': str(catalogue.defaults_path())})


def _display_rows(settings) -> list:
    from foammesh.core.gmsh import session_display as sd

    rows = []
    for name, binding in sd.DISPLAY_BINDINGS.items():
        value = settings.get(name)
        rows.append({
            'name': name, 'kind': binding.kind, 'control': binding.control,
            'setting': binding.setting, 'shared': binding.shared,
            'vtk': binding.vtk,
            'value': list(value) if isinstance(value, tuple) else value})
    return rows


def _display_options(session, command):
    from foammesh.core.gmsh import session_display as sd
    from foammesh.core.facade.plan48 import pa1_routes

    panel = pa1_routes.display_panel_or_none(session)
    if panel is None:
        settings = sd.load_saved_settings()
        return _read(session, command, {
            'options': _display_rows(settings),
            'nothing_to_draw': dict(sd.NOTHING_TO_DRAW), 'window': False})

    # PA1: with a window the values are the ones the Drawing tab holds (they
    # differ from the kept file until the panel is shown), and it says
    # whether the viewport draws from them.
    def read():
        return {'options': _display_rows(panel.drawingSettings()),
                'nothing_to_draw': dict(sd.NOTHING_TO_DRAW), 'window': True,
                'viewport_follows': panel.drawingFollowedBy()}
    return _read(session, command, pa1_routes.on_gui(read))


def _display_set(session, command):
    from foammesh.core.gmsh import session_display as sd
    from foammesh.core.facade.plan48 import pa1_routes

    parameters = command.parameters or {}
    wanted = parameters.get('options') or {}
    if not isinstance(wanted, dict):
        raise ValidationFailedError('options must be an object of '
                                    'name: value pairs')
    if not wanted and not parameters.get('reset'):
        raise ValidationFailedError('say which options to set, or reset')
    panel = pa1_routes.display_panel_or_none(session)
    if panel is not None:
        return _display_set_in_window(session, command, panel, wanted,
                                      bool(parameters.get('reset')))
    settings = (sd.GmshDisplaySettings() if parameters.get('reset')
                else sd.load_saved_settings())
    try:
        for name, value in wanted.items():
            settings.set(name, value)
    except ValueError as error:
        raise ValidationFailedError(str(error)) from None
    if not sd.save_settings(settings):
        raise ValidationFailedError('the drawing settings could not be kept')
    return OperationResult(
        'accepted', command.operation, session.revisions,
        tuple(sorted(wanted)), payload={'changed': settings.changed(),
                                        'window': False})


def _display_set_in_window(session, command, panel, wanted, reset):
    """PA1. Set the options through the Drawing tab, as its controls do: the
    control moves, the kept values are written, and the viewport draws."""
    from foammesh.core.gmsh import session_display as sd
    from foammesh.core.facade.plan48 import pa1_routes

    def run():
        # Nothing changes unless every value is one the controls accept.
        scratch = (sd.GmshDisplaySettings() if reset else
                   sd.GmshDisplaySettings(panel.drawingSettings().changed()))
        for name, value in wanted.items():
            scratch.set(name, value)
        if reset:
            panel.resetDrawing()
        for name, value in wanted.items():
            if not panel.setDrawingOption(name, value):
                raise ValueError(panel.status() or f'{name} was refused')
        return {'changed': panel.drawingSettings().changed(),
                'window': True,
                'viewport_follows': panel.drawingFollowedBy()}
    payload = pa1_routes.on_gui(run)
    return OperationResult('accepted', command.operation, session.revisions,
                           tuple(sorted(wanted)), payload=payload)


def handlers() -> dict:
    return {
        'gmsh.options.describe': _describe_options,
        'gmsh.options.validate': _validate_options,
        'gmsh.options.opt.read': _read_opt,
        'gmsh.options.startup_defaults': _startup_defaults,
        'gmsh.display.options': _display_options,
        'gmsh.display.set': _display_set,
    }
