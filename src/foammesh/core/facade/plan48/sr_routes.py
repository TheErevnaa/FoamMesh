"""Plan 49 SR local-API routes: the Geometry tree's body show / hide button.

A plain remote control for a window control:

* ``window.body_visibility`` -- the show / hide button on a body's row of
  the Geometry tree (DP-6090, ``GeometryList._mountEye``). It presses that
  button, so the body's surfaces are hidden or shown through Display control
  exactly as the click does, and answers whether each body reads as shown
  (a body reads as shown while any of its surfaces is drawn).

The button lives in the window, so the route goes through the window the
case session holds (``pa5_window``); without one it answers
``precondition_failed``.
"""
from __future__ import annotations

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

#: The object name of a body row's button (``GeometryList._mountEye``).
EYE_PREFIX = 'geometryEye_'

OPERATIONS = (
    OperationDescriptor(
        'window.body_visibility', 'Show or hide a body', 'case',
        ImpactClass.READ,
        summary='The show / hide button on each body row of the Geometry '
                'tree. Without parameters, lists every body row with '
                'whether it reads as shown (shown while any of its '
                'surfaces is drawn). With geometry_id (a body) and visible, '
                'presses that button as a click does: every surface of the '
                'body is hidden or shown, and the Parts list, the region '
                'picker and Show all agree. A surface id or an id with no '
                'body row is refused.',
        parameters_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'geometry_id': {'type': ['string', 'integer']},
                'visible': {'type': 'boolean'}}}),
)


def _eyes(window) -> dict:
    """``{body geometry id: button}`` for every body row the window holds."""
    from PySide6.QtWidgets import QToolButton

    found = {}
    for button in window.findChildren(QToolButton):
        name = button.objectName()
        if name.startswith(EYE_PREFIX):
            found[name[len(EYE_PREFIX):]] = button
    return found


def body_visibility(window, geometry_id=None, visible=None) -> dict:
    """Read the body rows' buttons, or press one as a click does."""
    eyes = _eyes(window)
    if geometry_id is None:
        if visible is not None:
            raise ValueError('visible needs geometry_id, the body to show '
                             'or hide')
    else:
        key = str(geometry_id)
        button = eyes.get(key)
        if button is None:
            raise LookupError(
                f'no body row {key} in the Geometry tree: only a body (a '
                f'volume row) has the show / hide button')
        if visible is not None and button.isChecked() != bool(visible):
            button.click()
    bodies = [{'geometry_id': key, 'visible': button.isChecked()}
              for key, button in sorted(eyes.items())]
    payload = {'bodies': bodies}
    if geometry_id is not None:
        payload.update(geometry_id=str(geometry_id),
                       visible=eyes[str(geometry_id)].isChecked())
    return payload


def handlers():
    async def body_visibility_route(session, command):
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread

        from .pa5_routes import _api

        parameters = dict(command.parameters or {})
        window = _api(session).window
        try:
            payload = on_gui_thread(
                body_visibility, window, parameters.get('geometry_id'),
                parameters.get('visible'))
        except ValueError as error:
            raise ValidationFailedError(str(error)) from error
        except LookupError as error:
            raise PreconditionFailedError(
                str(error.args[0] if error.args else error),
                details={'error': 'no_body_row'}) from error
        return OperationResult('accepted', command.operation,
                               session.revisions, payload=payload)

    return {'window.body_visibility': body_visibility_route}
