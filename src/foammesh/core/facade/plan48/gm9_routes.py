"""Plan 48 GM9 local-API routes: the Gmsh mesh plugins.

Plain remote control of the Plugins panel, nothing agent-specific:

``gmsh.plugins.catalogue``
    Every plugin the panel lists with each option, its kind and Gmsh's own
    default (strings included), whether it needs an input view and whether it
    changes the mesh. With ``name``, only that plugin.
``gmsh.plugins.views``
    Every view in the resident session (any of them can be an input view).
``gmsh.plugins.run``
    Run a plugin with the given options (``options`` omitted = Gmsh's
    defaults), on an input view where it takes one. Returns the views and
    entities it made, with the host path of their ``.vtu`` and Gmsh's log.
``gmsh.plugins.describe_view``
    Write any view of the session as a ``.vtu`` and describe it (what the
    panel does to draw it).
``gmsh.plugins.remove_view``
    Remove one view, or all of them.
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

OPERATIONS = (
    OperationDescriptor(
        'gmsh.plugins.catalogue', 'Gmsh plugins and their options', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object',
                           'properties': {'name': {'type': 'string'}}},
        summary='The mesh plugins the Plugins panel lists: every option with '
                'its kind (float, int, bool, string), Gmsh default and unit, '
                'the plugins that need an input view or change the mesh.'),
    OperationDescriptor(
        'gmsh.plugins.views', 'Views in the Gmsh session', 'case',
        ImpactClass.READ,
        summary='Every view of the resident session (tag, name, index, kind): '
                'any of them can be the input view of a plugin.'),
    OperationDescriptor(
        'gmsh.plugins.run', 'Run a Gmsh plugin', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema={
            'type': 'object', 'required': ['name'],
            'properties': {
                'name': {'type': 'string'},
                'options': {'type': 'object'},
                'view': {'type': ['integer', 'null']},
                'other_view': {'type': ['integer', 'null']}}},
        summary='Run one plugin on the session mesh with the given number '
                'and string options (omitted ones take Gmsh defaults). '
                'Returns the views and entities it made and its log.'),
    OperationDescriptor(
        'gmsh.plugins.describe_view', 'Describe a view of the Gmsh session',
        'case', ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['tag'],
                           'properties': {'tag': {'type': 'integer'}}},
        summary='Write the view as a .vtu and report its elements, extremes '
                'and the file the viewport draws.'),
    OperationDescriptor(
        'gmsh.plugins.remove_view', 'Remove a view from the Gmsh session',
        'case', ImpactClass.MESH_MUTATION,
        parameters_schema={'type': 'object',
                           'properties': {'tag': {'type': ['integer', 'null']}}},
        summary='Remove one view by tag, or every view when no tag is given.'),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def option_dict(option) -> dict:
    return {'name': option.name, 'kind': option.kind,
            'default': option.default, 'unit': option.unit,
            'help': option.help}


def plugin_dict(entry) -> dict:
    return {'name': entry.name, 'title': entry.title,
            'summary': entry.summary, 'needs_view': entry.needs_view,
            'takes_view': entry.takes_view,
            'changes_mesh': entry.changes_mesh,
            'unavailable': entry.unavailable, 'source': entry.source,
            'build_gate': list(entry.build_gate),
            'options': [option_dict(o) for o in entry.options]}


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _catalogue(session, command) -> OperationResult:
    from foammesh.core.gmsh import plugin_catalogue as catalogue

    name = str(command.parameters.get('name') or '').strip()
    if name:
        try:
            entries = (catalogue.plugin(name),)
        except KeyError as error:
            raise ValidationFailedError(
                str(error.args[0]), details={'name': name}) from error
    else:
        entries = catalogue.PLUGINS
    return _result(session, command, {
        'gmsh_version': catalogue.GMSH_VERSION,
        'catalogue': catalogue.CATALOGUE_FINGERPRINT,
        'plugins': [plugin_dict(e) for e in entries],
        'excluded_post_processing': list(catalogue.EXCLUDED_POST),
        'built_elsewhere': dict(catalogue.DEFERRED)})


def _views(session, command) -> OperationResult:
    controller = _controller(session)
    return _result(session, command, {'views': controller.plugin_views()})


async def _run(session, command) -> OperationResult:
    from foammesh.core.gmsh import plugin_catalogue as catalogue
    from foammesh.core.gmsh.generate_actions import ActionRefusal

    parameters = command.parameters
    name = str(parameters.get('name') or '').strip()
    if not name:
        raise ValidationFailedError('a plugin name is required')
    options = parameters.get('options') or {}
    if not isinstance(options, dict):
        raise ValidationFailedError('options must be an object')
    controller = _controller(session)

    def work():
        return catalogue.run_on_controller(
            controller, name, options, view=parameters.get('view'),
            other_view=parameters.get('other_view'))

    try:
        answer = await asyncio.to_thread(work)
    except ActionRefusal as error:
        raise PreconditionFailedError(str(error)) from error
    except RuntimeError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, answer)


async def _describe_view(session, command) -> OperationResult:
    from foammesh.core.gmsh.generate_actions import ActionRefusal

    try:
        tag = int(command.parameters['tag'])
    except (KeyError, TypeError, ValueError):
        raise ValidationFailedError('an integer view tag is required') \
            from None
    controller = _controller(session)

    def work():
        with controller._lock:
            live = controller.session
            if live is None or not live.alive:
                raise ActionRefusal('there is no Gmsh session; generate a '
                                    'mesh first')
            directory = controller.plugin_directory()
            row = live.call('session.plugin_describe', tag,
                            live.runtime_path(directory))
            if row.get('path'):
                row['host_path'] = str(Path(directory) / Path(
                    row['path']).name)
            return row

    try:
        row = await asyncio.to_thread(work)
    except (ActionRefusal, RuntimeError) as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, row)


def _remove_view(session, command) -> OperationResult:
    tag = command.parameters.get('tag')
    controller = _controller(session)
    left = controller.remove_plugin_view(None if tag is None else int(tag))
    return _result(session, command, left)


def handlers() -> dict:
    return {
        'gmsh.plugins.catalogue': _catalogue,
        'gmsh.plugins.views': _views,
        'gmsh.plugins.run': _run,
        'gmsh.plugins.describe_view': _describe_view,
        'gmsh.plugins.remove_view': _remove_view,
    }
