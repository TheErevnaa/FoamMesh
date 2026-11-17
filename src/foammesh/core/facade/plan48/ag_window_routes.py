"""Plan 48 AG local-API routes: more of the window's own controls.

A plain remote control for controls a person has in the window that had no
route: the console's level filter, Fold stages and search bar; the
notification centre; the Mesh quality tab's saved element selections; the
fold headers of the page the sheet shows; *Show expert settings*; the
Preparation choice; the Mesh size read-out and physics preset; the page
filter box; and the help strip. Each presses or sets the widget itself
(``view/main_window/window_controls_api.py``), so the same handler runs as on
a click. Without a window these answer ``precondition_failed``.

The view-only controls are READ, as in ``pa5_routes``. Saving and deleting a
saved selection write the case's selections file, as ``gmsh.selections.save``
and ``.delete`` do, and are classed as they are (REVERSIBLE_EDIT). Applying a
physics preset submits ``configuration.patch`` through the page's own client,
as the button does; like PA5's Save it is declared READ so that it does not
wait behind itself in the single-owner queue.
"""
from __future__ import annotations

from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.plan48.pa5_routes import _api, _done, _guard, _p
from foammesh.core.facade.policy import ImpactClass

_BOOL = {'type': 'boolean'}
_STRING = {'type': 'string'}
_NAME = {'type': 'string', 'minLength': 1}


def _schema(required=(), **properties) -> dict:
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


OPERATIONS = (
    OperationDescriptor(
        'window.console', 'Console filter, folds and search', 'case',
        ImpactClass.READ,
        summary='The console as shown (level, Fold stages, the search bar, '
                'its match count, the current match, the lines on screen), '
                'and its controls: choose the level, fold or open every '
                'stage, open or close the search bar, type into it, and '
                'step to the next or previous match.',
        parameters_schema=_schema(
            level={'type': 'string', 'enum': ['info', 'warning', 'error']},
            folded=_BOOL, search_open=_BOOL, find=_STRING,
            step={'type': 'string', 'enum': ['next', 'previous']})),
    OperationDescriptor(
        'window.notifications', 'Notification centre', 'case',
        ImpactClass.READ,
        summary='The notifications the bell keeps (id, text, severity, '
                'when, repeat count, seen, whether Go to applies) and the '
                'unseen count; open or close the panel, Clear all, or press '
                'Go to on one entry.',
        parameters_schema=_schema(
            clear=_BOOL, open=_BOOL, go_to={'type': 'integer'})),
    OperationDescriptor(
        'quality.selections.list', 'Saved element selections', 'case',
        ImpactClass.READ,
        summary='The Mesh quality tab saved-selection row: the saved '
                'element selections (name, text shown, other-mesh note), '
                'the chosen one, the name box and the listed cells.'),
    OperationDescriptor(
        'quality.selections.save', 'Save the listed cells', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        summary='Type a name and press Save listed cells on the Mesh '
                'quality tab: the cells listed there are kept under that '
                'name. Refused with the status-line reason when nothing is '
                'saved.',
        parameters_schema=_schema(('name',), name=_NAME)),
    OperationDescriptor(
        'quality.selections.show', 'Show a saved element selection', 'case',
        ImpactClass.READ,
        summary='Choose a saved element selection and press Show: its cells '
                'are shown as a failed-cell set; the answer carries the '
                'status-line sentence.',
        parameters_schema=_schema(('name',), name=_NAME)),
    OperationDescriptor(
        'quality.selections.delete', 'Delete a saved element selection',
        'case', ImpactClass.REVERSIBLE_EDIT,
        summary='Choose a saved element selection and press Delete.',
        parameters_schema=_schema(('name',), name=_NAME)),
    OperationDescriptor(
        'window.fold', 'Fold headers of the page shown', 'case',
        ImpactClass.READ,
        summary='The fold headers on the page the sheet shows (the Gmsh '
                'tiers, Generate step by step, Advanced) with their open '
                'state; with name (and index when several share it) open '
                'or close that one, as clicking it does.',
        parameters_schema=_schema(
            name=_STRING, open=_BOOL, index={'type': 'integer',
                                             'minimum': 0})),
    OperationDescriptor(
        'window.expert_settings', 'Show expert settings', 'case',
        ImpactClass.READ,
        summary='Whether Expert settings are shown, application-wide; '
                'shown sets the switch every page box follows.',
        parameters_schema=_schema(shown=_BOOL)),
    OperationDescriptor(
        'window.preparation_choice', 'Preparation choice', 'case',
        ImpactClass.READ,
        summary='Which of Repair, Wrap and Use as-is the Preparation step '
                'shows; choice selects one as its segment does.',
        parameters_schema=_schema(choice={
            'type': 'string', 'enum': ['repair', 'wrap', 'use_as_is']})),
    OperationDescriptor(
        'window.mesh_size', 'Mesh size read-out and physics preset', 'case',
        ImpactClass.READ,
        summary='The Mesh size read-out and the physics preset row (the '
                'presets, the chosen one, the result line); preset chooses '
                'one, apply presses Apply preset, which fills only the '
                'settings not changed and lists them.',
        parameters_schema=_schema(preset=_STRING, apply=_BOOL)),
    OperationDescriptor(
        'window.page_filter', 'Filter settings on the page shown', 'case',
        ImpactClass.READ,
        summary='The filter box of the page the sheet shows and the field '
                'ids still on screen; text types into it (empty clears it).',
        parameters_schema=_schema(text=_STRING)),
    OperationDescriptor(
        'window.help', 'Help strip', 'case', ImpactClass.READ,
        summary='The help strip for the setting in focus: open or closed, '
                'and the name, explanation, keyword and manual link it '
                'shows; open shows or hides it.',
        parameters_schema=_schema(open=_BOOL)),
)


def handlers():
    def call(method, **fixed):
        async def handler(session, command):
            parameters = _p(command)
            parameters.update(fixed)
            api = _api(session)
            return _done(session, command, _guard(
                lambda: getattr(api, method)(**parameters)))
        return handler

    return {
        'window.console': call('console'),
        'window.notifications': call('notifications'),
        'quality.selections.list': call('quality_selections'),
        'quality.selections.save': call('quality_selections',
                                        action='save'),
        'quality.selections.show': call('quality_selections',
                                        action='show'),
        'quality.selections.delete': call('quality_selections',
                                          action='delete'),
        'window.fold': call('fold'),
        'window.expert_settings': call('expert_settings'),
        'window.preparation_choice': call('preparation_choice'),
        'window.mesh_size': call('mesh_size'),
        'window.page_filter': call('page_filter'),
        'window.help': call('help'),
    }
