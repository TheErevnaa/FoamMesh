"""Gmsh panel controls that had no route: layer stacks, point sizes, the
periodic arrow and the Import panel.

A plain remote control for what these panels do in the window. Each panel
registers an API object on the case session, as the PA2 panels do
(``pa2_routes.register``); a route calls it, so the window and the API run
the same handlers, and the routes answer ``precondition_failed`` when no
window shows the case.

``gmsh.layer_stacks.state`` / ``.preview``
    The Layers per patch panel: its rows, the status line under them and the
    Preview layers box (the ghost prisms it draws).
``gmsh.point_sizes.state``
    The Size at points panel: its rows and the status line.
``gmsh.periodic_pairs.arrow``
    Select a pair on the Periodic page; the source -> target arrow and the
    sentence under it.
``gmsh.model.state``
    The Import panel: the status line, Open or Merge, the file, the
    Reclassify 2D values, the time limit and whether it is working.
``gmsh.model.close_into_volume`` / ``gmsh.model.create_geometry``
    The Import panel's "Close into a volume" and "Create geometry" buttons,
    run on the project's Gmsh session (beside ``gmsh.model.reclassify`` in
    ``tl_routes``). With a window open, the panel then shows what it shows
    after the button (``run_import_action``): the status line, ``opened``
    and the Entity panel redraw. ``tl_routes`` uses the same for Reclassify
    and Create topology.
``gmsh.structure.show_tags``
    A Structure row's Show button beside its entity tags or corner points:
    the stored tags light in the viewport.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

LAYER_STACKS_ATTRIBUTE = 'gmsh_layer_stacks_api'
POINT_SIZES_ATTRIBUTE = 'gmsh_point_sizes_api'
PERIODIC_ATTRIBUTE = 'gmsh_periodic_api'
IMPORT_ATTRIBUTE = 'gmsh_import_api'


# -- the window registers its panels ------------------------------------------- #

def attach(widget, attribute: str, api, client=None) -> bool:
    """Make *api* the one the routes call for the case *widget* shows.

    Idempotent; the registration is dropped when *widget* is destroyed (if it
    is still the registered one).
    """
    from foammesh.core.facade.plan48 import pa2_routes

    session = pa2_routes.case_session_of(client)
    if session is None:
        return False
    pa2_routes.register(session, attribute, api)
    attached = getattr(widget, '_agAttached', None)
    if attached is None:
        attached = set()
        try:
            widget._agAttached = attached
        except Exception:                                # noqa: BLE001
            return True
    if (id(session), attribute) not in attached:
        attached.add((id(session), attribute))
        widget.destroyed.connect(
            lambda *_a, s=session, a=api, n=attribute:
            pa2_routes.unregister(s, n, a))
    return True


def on_gui(function, *args):
    """Run a panel read or change on the GUI thread."""
    from foammesh.view.gmsh_tools.window_actions import on_gui_thread
    return on_gui_thread(function, *args)


# -- schemas --------------------------------------------------------------------- #

def _schema(required=(), **properties) -> dict:
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


_ENTITY = {'type': 'array', 'minItems': 2, 'maxItems': 2,
           'items': {'type': 'integer'}}

OPERATIONS = (
    OperationDescriptor(
        'gmsh.layer_stacks.state', 'Layers per patch panel', 'case',
        ImpactClass.READ, parameters_schema=_schema(),
        summary='What the Layers per patch panel shows: its rows (pending '
                'edits included), the status line under them ("N layer '
                'stacks on." or why they cannot be grown), whether they '
                'differ from the stored ones, and the Preview layers box '
                'with the number of ghost prisms drawn.'),
    OperationDescriptor(
        'gmsh.layer_stacks.preview', 'Preview layers', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(('shown',), shown={'type': 'boolean'}),
        summary='Tick or clear the Preview layers box: the ghost prisms of '
                'the rows are drawn in the viewport (or cleared). Answers '
                'the number drawn and the status line.'),
    OperationDescriptor(
        'gmsh.point_sizes.state', 'Size at points panel', 'case',
        ImpactClass.READ, parameters_schema=_schema(),
        summary='What the Size at points panel shows: its rows (pending '
                'edits included), the status line under them and whether '
                'they differ from the stored ones.'),
    OperationDescriptor(
        'gmsh.periodic_pairs.arrow', 'Periodic pair arrow', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(row={'type': ['string', 'integer']}),
        summary='Select a pair on the Periodic page (by name, key or row '
                'number; none reads the selected one) and answer the '
                'source -> target arrow drawn for it (start, end, where the '
                'transform lands, the miss) and the sentence under it.'),
    OperationDescriptor(
        'gmsh.model.state', 'Import panel', 'case', ImpactClass.READ,
        parameters_schema=_schema(),
        summary='What the Import panel shows: the status line (the outcome '
                'of the last Open, Merge, Reclassify, Create topology, '
                'Create geometry or Close into a volume), Open or Merge, the '
                'file, the Reclassify 2D values, the time limit and whether '
                'it is working.'),
    OperationDescriptor(
        'gmsh.model.close_into_volume', 'Close the surfaces into a volume',
        'case', ImpactClass.MESH_MUTATION,
        parameters_schema=_schema(
            ('angle', 'curve_angle'), angle={'type': 'number'},
            curve_angle={'type': 'number'},
            size={'type': 'number', 'minimum': 0}),
        summary='The Import panel\'s "Close into a volume" button: drop the '
                'volumes, reclassify and parametrise the surfaces at angle '
                'and curve_angle (degrees), and close them with one surface '
                'loop and one volume. size (0 = the default) is the mesh '
                'size of the remeshed surfaces.'),
    OperationDescriptor(
        'gmsh.model.create_geometry', 'Create geometry in the Gmsh session',
        'case', ImpactClass.MESH_MUTATION,
        parameters_schema=_schema(entities={'type': 'array',
                                            'items': _ENTITY}),
        summary='The Import panel\'s "Create geometry" button: '
                'createGeometry on the session model (on the given '
                '[dimension, tag] entities, else on all of them).'),
    OperationDescriptor(
        'gmsh.structure.show_tags', "Show a structure row's tags", 'case',
        ImpactClass.READ,
        parameters_schema=_schema(
            ('row', 'key'), row={'type': ['string', 'integer']},
            key={'type': 'string', 'enum': ['tags', 'corners']}),
        summary="The Show button beside a Structure row's entity tags "
                '(key tags) or corner points (key corners): the stored '
                "tags light in the viewport. row is the row's name or "
                'number. Answers the lit [dimension, tag] pairs and the '
                'panel status line (it says so when no model is shown).'),
)


# -- helpers --------------------------------------------------------------------- #

def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _parameters(command) -> dict:
    return dict(command.parameters or {})


async def _call(session, attribute, what, method, *args):
    from foammesh.core.facade.plan48 import pa2_routes

    api = pa2_routes.window_api(session, attribute)
    if api is None:
        raise PreconditionFailedError(
            f'no window is showing this case, and the {what} lives in it: '
            'open the case in the FoamMesh window',
            details={'needs': attribute})
    try:
        return await asyncio.to_thread(lambda: getattr(api, method)(*args))
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _number(parameters, key, default=None):
    value = parameters.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationFailedError(f'{key} is a number',
                                    details={key: value})
    return float(value)


# -- handlers -------------------------------------------------------------------- #

async def _ls_state(session, command):
    return _result(session, command, await _call(
        session, LAYER_STACKS_ATTRIBUTE, 'Layers per patch panel', 'state'))


async def _ls_preview(session, command):
    shown = _parameters(command).get('shown')
    if not isinstance(shown, bool):
        raise ValidationFailedError('shown is true or false')
    return _result(session, command, await _call(
        session, LAYER_STACKS_ATTRIBUTE, 'Layers per patch panel',
        'set_preview', shown))


async def _ps_state(session, command):
    return _result(session, command, await _call(
        session, POINT_SIZES_ATTRIBUTE, 'Size at points panel', 'state'))


async def _pp_arrow(session, command):
    return _result(session, command, await _call(
        session, PERIODIC_ATTRIBUTE, 'Periodic page', 'arrow',
        _parameters(command).get('row')))


async def _m_state(session, command):
    return _result(session, command, await _call(
        session, IMPORT_ATTRIBUTE, 'Import panel', 'state'))


async def _st_show(session, command):
    from foammesh.core.facade.plan48 import pa3_routes

    parameters = _parameters(command)
    api = pa3_routes.window_panel(session, 'structure')
    if api is None:
        raise PreconditionFailedError(
            'no window is showing this case, and the Structure panel lives '
            'in it: open the case in the FoamMesh window',
            details={'needs': 'structure'})
    try:
        answer = await asyncio.to_thread(
            api.show_tags, parameters.get('row'), parameters.get('key'))
    except ValueError as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, answer)


def _refused(error):
    from foammesh.core.gmsh import generate_actions as ga

    details = {}
    if isinstance(error, ga.TimeLimitRefusal):
        details['limit'] = {'seconds': error.seconds, 'what': error.what,
                            'tag': ga.LIMIT_TAG}
    return PreconditionFailedError(str(error), details=details)


async def tell_import_panel(session, method: str, *args) -> None:
    """Show a route's outcome on the Import panel, as a click shows it.

    ``finished(what, result)`` runs ``GmshImportPanel.showOutcome`` (the
    status line, ``opened`` and the Entity panel redraw), ``failed(message)``
    runs ``showFailure``. With no window there is nothing to show.
    """
    from foammesh.core.facade.plan48 import pa2_routes

    api = pa2_routes.window_api(session, IMPORT_ATTRIBUTE)
    if api is None:
        return
    try:
        await asyncio.to_thread(lambda: getattr(api, method)(*args))
    except RuntimeError:            # the panel was deleted under the call
        pass


async def run_import_action(session, what: str, work):
    """Run an Import panel action for a route; the panel shows the outcome
    (or the refusal) exactly as after its button. Errors propagate."""
    try:
        answer = await asyncio.to_thread(work)
    except Exception as error:                           # noqa: BLE001
        await tell_import_panel(session, 'failed', str(error))
        raise
    await tell_import_panel(session, 'finished', what, answer)
    return answer


async def _run(session, what, work):
    from foammesh.core.gmsh import generate_actions as ga

    try:
        return await run_import_action(session, what, work)
    except ga.ActionRefusal as error:
        raise _refused(error) from error
    except RuntimeError as error:
        raise PreconditionFailedError(str(error)) from error


async def _close_into_volume(session, command):
    from foammesh.core.gmsh import import_formats as formats

    p = _parameters(command)
    angle = _number(p, 'angle')
    curve_angle = _number(p, 'curve_angle')
    size = _number(p, 'size', 0.0)
    if size < 0:
        raise ValidationFailedError('size is 0 (the default) or more',
                                    details={'size': size})
    try:
        formats.reclassify_arguments(angle, curve_angle=curve_angle)
    except formats.ImportRefusal as error:
        # the button meets this refusal in the controller and shows it
        await tell_import_panel(session, 'failed', str(error))
        raise ValidationFailedError(str(error)) from error
    controller = _controller(session)
    answer = await _run(session, 'close', lambda: controller.remesh_surfaces(
        angle=angle, curve_angle=curve_angle, size=size))
    return _result(session, command, {'result': answer})


async def _create_geometry(session, command):
    given = _parameters(command).get('entities')
    dim_tags = None
    if given is not None:
        try:
            dim_tags = [(int(d), int(t)) for d, t in given]
        except (TypeError, ValueError):
            raise ValidationFailedError(
                'entities must be a list of [dimension, tag] pairs') from None
    controller = _controller(session)
    answer = await _run(session, 'geometry',
                        lambda: controller.create_geometry(dim_tags))
    return _result(session, command, {'result': answer})


def handlers() -> dict:
    return {
        'gmsh.layer_stacks.state': _ls_state,
        'gmsh.layer_stacks.preview': _ls_preview,
        'gmsh.point_sizes.state': _ps_state,
        'gmsh.periodic_pairs.arrow': _pp_arrow,
        'gmsh.model.state': _m_state,
        'gmsh.model.close_into_volume': _close_into_volume,
        'gmsh.model.create_geometry': _create_geometry,
        'gmsh.structure.show_tags': _st_show,
    }
