"""Plan 48 PA5 local-API routes: the window's own controls.

A plain remote control for what the main window controls: opening a panel,
step or tab, the viewport rail, the run panel's read-outs, the sheet header,
the recovery offer and WSL health bars, the export page's read-outs, File >
Save and Save as, and a few table panels of the geometry and snappy pages.
The window registers its API on the case session (``pa5_window``); without a
window these answer ``precondition_failed``.

Save, Save as and block grading are declared READ on purpose: they run the
window's own code, which submits ``case.save`` or ``base_grid.blocks.patch``
through the facade, and a mutation waiting on a mutation would sit behind
itself in the single-owner queue. The write the facade makes is a mutation
all the same (see requests/pa5.md).
"""
from __future__ import annotations

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

ATTRIBUTE = 'pa5_window'

_NUMBER = {'type': 'number'}
_BOOL = {'type': 'boolean'}
_STRING = {'type': 'string'}


def _schema(required=(), **properties) -> dict:
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


OPERATIONS = (
    OperationDescriptor(
        'window.navigate', 'Open a panel, step or tab', 'case',
        ImpactClass.READ,
        summary='Open what a press in the window opens: kind step (a step '
                'name or number), branch (a route token of the step tree), '
                'task (a task id a failed check named), rail (a viewport '
                'rail tool), output_tab (a tab of the lower region), '
                'action (a menu action by object name) or button (a Gmsh '
                'tools link button, gmshTools_*). Returns where the window '
                'stands now.',
        parameters_schema=_schema(
            ('kind',), kind={'type': 'string', 'enum': [
                'step', 'branch', 'task', 'rail', 'output_tab', 'action',
                'button']},
            target={'type': ['string', 'integer']})),
    OperationDescriptor(
        'window.rail', 'Viewport tool rail', 'case', ImpactClass.READ,
        summary='Read the viewport tool rail (tools, open and pinned '
                'popovers, sizes, the ... overflow menu, Customise choices) '
                'and press its controls: open or close a tool, pin a '
                'popover or the rail, set a popover size, choose an '
                'overflow entry, set the Customise options.',
        parameters_schema=_schema(
            tool=_STRING, open=_BOOL, pinned=_BOOL, rail_pinned=_BOOL,
            width={'type': 'integer', 'minimum': 1},
            height={'type': 'integer', 'minimum': 1}, reset_size=_BOOL,
            overflow=_STRING,
            preferences={'type': 'object',
                         'additionalProperties': _BOOL})),
    OperationDescriptor(
        'window.run_panel', 'Run panel read-out', 'case', ImpactClass.READ,
        summary='What the run panel shows: status and timing lines, the '
                'progress bar, facts, summary, the stage list and the '
                'Refinement, Snap and Layer chart series.'),
    OperationDescriptor(
        'window.sheet', 'Settings sheet header', 'case', ImpactClass.READ,
        summary='The sheet header as shown (title, subtitle, step chip, '
                'buttons) and its pin, side and minimise controls: set '
                'mode (pinned or floating), side (left or right), '
                'minimised, or reset to the standard layout.',
        parameters_schema=_schema(
            mode={'type': 'string', 'enum': ['pinned', 'floating']},
            side={'type': 'string', 'enum': ['left', 'right']},
            minimised=_BOOL, reset=_BOOL)),
    OperationDescriptor(
        'window.recovery', 'Unsaved-changes offer', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='The recovery offer bar and the protection bar as shown. '
                'action restore brings the unsaved changes back, discard '
                'opens the last saved state, show lists what changed '
                '(changes_text).',
        parameters_schema=_schema(
            action={'type': 'string',
                    'enum': ['restore', 'discard', 'show']})),
    OperationDescriptor(
        'window.runtime', 'WSL runtime health bar', 'case',
        ImpactClass.FILE_PRODUCING,
        summary='The WSL health bar as shown, and its Retry connection, '
                'Restart WSL runtime and Shut down WSL controls. Restart '
                'and shutdown stop programs, so they need confirm=true; '
                'without it the refusal carries the question the window '
                'asks.',
        parameters_schema=_schema(
            action={'type': 'string',
                    'enum': ['retry', 'restart', 'shutdown']},
            confirm=_BOOL)),
    OperationDescriptor(
        'window.export', 'Export page read-outs', 'case', ImpactClass.READ,
        summary='What the Export page shows: readiness, failed checks, the '
                'record, the missing-destination warning, the target solver '
                'and whether Change target applies, and the Gmsh script '
                'panel. action change_target goes to the meshing-method '
                'step; script shows or hides the Gmsh script panel.',
        parameters_schema=_schema(
            action={'type': 'string', 'enum': ['change_target', 'script']},
            shown=_BOOL)),
    OperationDescriptor(
        'window.save', 'File → Save', 'case', ImpactClass.READ,
        summary='Save the case as File → Save does (the current page '
                'first). A case changed outside FoamMesh is refused with '
                'the reason; nothing is reloaded or copied.'),
    OperationDescriptor(
        'window.save_as', 'File → Save as', 'case',
        ImpactClass.READ,
        summary='Save the FoamMesh state to a new case folder as File → '
                'Save as does (native geometry and mesh files are not '
                'copied); open_copy opens the copy afterwards.',
        parameters_schema=_schema(('path',), path=_STRING, open_copy=_BOOL)),
    OperationDescriptor(
        'window.save_as.cancel', 'Cancel File → Save as', 'case',
        ImpactClass.READ,
        summary='Cancel the window.save_as that is running, as Cancel on '
                'the Save as progress window does: the copy stops between '
                'its writes and leaves no destination. Answers cancelled '
                'false when no Save as is running.'),
    OperationDescriptor(
        'geometry.interface_pair.check', 'Interface pair contact check',
        'case', ImpactClass.READ,
        summary='Whether the two faces of a coincident interface pair '
                'touch, the question OK would ask if they do not, and the '
                'touching pair the prepared geometry proposes instead.',
        parameters_schema=_schema(
            ('master_scope_token', 'slave_scope_token'),
            master_scope_token=_STRING, slave_scope_token=_STRING,
            transform=_STRING, match_tolerance=_NUMBER)),
    OperationDescriptor(
        'snappy.exclude_points.findings', 'Exclusion point findings',
        'case', ImpactClass.READ,
        summary='The findings under the exclusion points table: the text '
                'shown, its status, and the errors, warnings and removals '
                'of the preflight report.'),
    OperationDescriptor(
        'snappy.region_seed.placement', 'Region seed placement', 'case',
        ImpactClass.READ,
        summary='The region seed editor (point, placements, undo and redo) '
                'and its controls: move the seed handle to a point, nudge '
                'it along an axis, undo or redo a placement, or merge the '
                'selected region away.',
        parameters_schema=_schema(
            action={'type': 'string', 'enum': [
                'move', 'nudge', 'undo', 'redo', 'merge']},
            point={'type': 'array', 'items': _NUMBER, 'minItems': 3,
                   'maxItems': 3},
            axis={'type': 'integer', 'minimum': 0, 'maximum': 2},
            steps={'type': 'integer'})),
    OperationDescriptor(
        'snappy.layers.first_layer', 'First-layer height from y+', 'case',
        ImpactClass.READ,
        summary='The y+ helper under the layer group first-layer box: the '
                'first-layer height for a target y+, free-stream velocity, '
                'density, viscosity and length; with apply it fills the '
                'group editor, or says why it could not.',
        parameters_schema=_schema(
            ('target_yplus', 'velocity', 'density', 'viscosity', 'length'),
            target_yplus=_NUMBER, velocity=_NUMBER, density=_NUMBER,
            viscosity=_NUMBER, length=_NUMBER, apply=_BOOL)),
    OperationDescriptor(
        'snappy.base_grid.block_grading', 'Background block grading',
        'case', ImpactClass.READ,
        summary='Set one direction of a background-mesh block grading as '
                'its row editor does: a side (Start, End, Centre, Both '
                'edges) with a ratio regenerates the grading text; Custom '
                'profile keeps the text given. apply=false only computes '
                'the fields; vertices also returns the Start/End '
                'sentences.',
        parameters_schema=_schema(
            ('entity_id', 'axis'), entity_id=_STRING,
            axis={'type': 'string', 'enum': ['x', 'y', 'z']},
            fine=_STRING, ratio=_NUMBER, text=_STRING, vertices=_STRING,
            apply=_BOOL)),
)


def _api(session):
    api = getattr(session, ATTRIBUTE, None)
    if api is None:
        raise PreconditionFailedError(
            'no window is showing this case, and this control lives in '
            'it: open the case in the FoamMesh window',
            details={'needs': ATTRIBUTE})
    return api


def _guard(function):
    try:
        return function()
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error
    except LookupError as error:
        raise PreconditionFailedError(str(error.args[0] if error.args
                                          else error)) from error


async def _aguard(coroutine):
    try:
        return await coroutine
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error
    except LookupError as error:
        raise PreconditionFailedError(str(error.args[0] if error.args
                                          else error)) from error
    except OSError as error:
        raise ValidationFailedError(str(error)) from error


def _done(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=dict(payload or {}))


def _p(command) -> dict:
    return dict(command.parameters or {})


def handlers():
    async def navigate(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, _guard(
            lambda: api.navigate(p.get('kind'), p.get('target'))))

    async def rail(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, _guard(lambda: api.rail(**p)))

    async def run_panel(session, command):
        api = _api(session)
        return _done(session, command, _guard(api.run_panel))

    async def sheet(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, _guard(lambda: api.sheet(**p)))

    async def recovery(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, _guard(
            lambda: api.recovery(p.get('action'))))

    async def runtime(session, command):
        p = _p(command)
        api = _api(session)
        if p.get('action') is None:
            return _done(session, command, _guard(api.runtime))
        return _done(session, command, await _aguard(
            api.runtime_act(p['action'], bool(p.get('confirm')))))

    async def export(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, _guard(lambda: api.export(
            p.get('action'), shown=p.get('shown'))))

    async def save(session, command):
        api = _api(session)
        return _done(session, command, await _aguard(api.save()))

    async def save_as(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, await _aguard(api.save_as(
            p['path'], bool(p.get('open_copy')))))

    async def save_as_cancel(session, command):
        api = _api(session)
        return _done(session, command, _guard(api.save_as_cancel))

    async def pair_check(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, _guard(
            lambda: api.interface_pair_check(p)))

    async def findings(session, command):
        api = _api(session)
        return _done(session, command, _guard(api.exclude_findings))

    async def seed(session, command):
        p = _p(command)
        api = _api(session)
        action = p.pop('action', None)
        return _done(session, command, _guard(
            lambda: api.seed(action, **p)))

    async def first_layer(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, _guard(lambda: api.first_layer(**p)))

    async def grading(session, command):
        p = _p(command)
        api = _api(session)
        return _done(session, command, await _aguard(api.block_grading(
            p['entity_id'], p['axis'], p.get('fine'), p.get('ratio'),
            p.get('text'), p.get('vertices'), p.get('apply', True))))

    return {
        'window.navigate': navigate,
        'window.rail': rail,
        'window.run_panel': run_panel,
        'window.sheet': sheet,
        'window.recovery': recovery,
        'window.runtime': runtime,
        'window.export': export,
        'window.save': save,
        'window.save_as': save_as,
        'window.save_as.cancel': save_as_cancel,
        'geometry.interface_pair.check': pair_check,
        'snappy.exclude_points.findings': findings,
        'snappy.region_seed.placement': seed,
        'snappy.layers.first_layer': first_layer,
        'snappy.base_grid.block_grading': grading,
    }
