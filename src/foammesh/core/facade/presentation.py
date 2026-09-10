"""Presentation operations and state (§6.7, §7) for the facade.

Presentation is the desktop rendering surface: camera, overlays, selection,
visibility, colour, and measurement. It lives in its own ``presentation_sequence``
revision domain and never stales an engineering plan. Presentation events are
memory-only. When no desktop rendering session is attached, presentation
commands return ``presentation_unavailable`` (§6.2).

The state here is headless and Qt-free; the desktop bridge (AF4) maps these
declarative operations onto VTK on the Qt owner thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import PresentationUnavailableError, ValidationFailedError


STANDARD_AXES = ('x', 'y', 'z', '-x', '-y', '-z', 'iso')


@dataclass
class PresentationState:
    """Declarative desktop view state, mutated by presentation operations."""
    attached: bool = True
    perspective: bool = True
    camera_axis: str = 'iso'
    roll_degrees: float = 0.0
    rotation_center: tuple[float, float, float] | None = None
    fit_requested: int = 0
    overlays: dict = field(default_factory=lambda: {
        'axis': True, 'cube_axis': False, 'ruler': False})
    selection: tuple[str, ...] = ()
    visibility: dict = field(default_factory=dict)
    colors: dict = field(default_factory=dict)
    opacity: dict = field(default_factory=dict)
    measurement: dict | None = None

    def to_dict(self) -> dict:
        return {
            'attached': self.attached, 'perspective': self.perspective,
            'camera_axis': self.camera_axis, 'roll_degrees': self.roll_degrees,
            'rotation_center': list(self.rotation_center) if self.rotation_center else None,
            'fit_requested': self.fit_requested, 'overlays': dict(self.overlays),
            'selection': list(self.selection),
            'visibility': dict(self.visibility), 'colors': dict(self.colors),
            'opacity': dict(self.opacity), 'measurement': self.measurement,
        }


class PresentationOperations:
    """Registered presentation handlers, keyed by operation id."""

    def __init__(self):
        self._handlers = {
            'presentation.view.fit': self._fit,
            'presentation.view.axis': self._axis,
            'presentation.view.cube_axis': self._toggle('cube_axis'),
            'presentation.view.ruler': self._toggle('ruler'),
            'presentation.view.perspective': self._perspective,
            'presentation.view.align_axis': self._align_axis,
            'presentation.view.roll': self._roll,
            'presentation.view.rotation_center': self._rotation_center,
            'presentation.select': self._select,
            'presentation.visibility': self._visibility,
            'presentation.color': self._color,
            'presentation.opacity': self._opacity,
            'presentation.measurement': self._measurement,
        }

    def operations(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def apply(self, state: PresentationState, operation: str, parameters: dict) -> dict:
        if not state.attached:
            raise PresentationUnavailableError('no desktop rendering session is attached')
        try:
            handler = self._handlers[operation]
        except KeyError as error:
            raise ValidationFailedError('unknown presentation operation', details={
                'operation': operation}) from error
        return handler(state, parameters or {})

    # -- handlers ---------------------------------------------------------- #

    @staticmethod
    def _fit(state: PresentationState, _parameters: dict) -> dict:
        state.fit_requested += 1
        return {'fit_requested': state.fit_requested}

    @staticmethod
    def _axis(state: PresentationState, parameters: dict) -> dict:
        axis = parameters.get('axis', 'iso')
        if axis not in STANDARD_AXES:
            raise ValidationFailedError('unknown camera axis', details={
                'axis': axis, 'allowed': list(STANDARD_AXES)})
        state.camera_axis = axis
        return {'camera_axis': axis}

    @staticmethod
    def _align_axis(state: PresentationState, parameters: dict) -> dict:
        axis = parameters.get('axis', 'x')
        if axis not in STANDARD_AXES:
            raise ValidationFailedError('unknown camera axis', details={'axis': axis})
        state.camera_axis = axis
        return {'aligned_axis': axis}

    def _toggle(self, overlay: str):
        def handler(state: PresentationState, parameters: dict) -> dict:
            visible = parameters.get('visible')
            state.overlays[overlay] = (not state.overlays.get(overlay, False)
                                       if visible is None else bool(visible))
            return {overlay: state.overlays[overlay]}
        return handler

    @staticmethod
    def _perspective(state: PresentationState, parameters: dict) -> dict:
        enabled = parameters.get('enabled')
        state.perspective = (not state.perspective if enabled is None else bool(enabled))
        return {'perspective': state.perspective}

    @staticmethod
    def _roll(state: PresentationState, parameters: dict) -> dict:
        degrees = parameters.get('degrees', 90.0)
        if not isinstance(degrees, (int, float)):
            raise ValidationFailedError('roll degrees must be numeric')
        state.roll_degrees = (state.roll_degrees + float(degrees)) % 360.0
        return {'roll_degrees': state.roll_degrees}

    @staticmethod
    def _rotation_center(state: PresentationState, parameters: dict) -> dict:
        point = parameters.get('point')
        if point is not None:
            if not (isinstance(point, (list, tuple)) and len(point) == 3):
                raise ValidationFailedError('rotation center must be [x, y, z]')
            state.rotation_center = tuple(float(value) for value in point)
        else:
            state.rotation_center = None  # reset to geometry centroid
        return {'rotation_center': list(state.rotation_center) if state.rotation_center else None}

    @staticmethod
    def _select(state: PresentationState, parameters: dict) -> dict:
        selection = parameters.get('entity_ids', [])
        if not isinstance(selection, (list, tuple)):
            raise ValidationFailedError('entity_ids must be a list')
        state.selection = tuple(str(item) for item in selection)
        return {'selection': list(state.selection)}

    @staticmethod
    def _visibility(state: PresentationState, parameters: dict) -> dict:
        entity_id = str(parameters.get('entity_id', ''))
        if not entity_id:
            raise ValidationFailedError('entity_id is required')
        state.visibility[entity_id] = bool(parameters.get('visible', True))
        return {'entity_id': entity_id, 'visible': state.visibility[entity_id]}

    @staticmethod
    def _color(state: PresentationState, parameters: dict) -> dict:
        entity_id = str(parameters.get('entity_id', ''))
        color = parameters.get('color')
        if not entity_id or not isinstance(color, str):
            raise ValidationFailedError('entity_id and a color string are required')
        state.colors[entity_id] = color
        return {'entity_id': entity_id, 'color': color}

    @staticmethod
    def _opacity(state: PresentationState, parameters: dict) -> dict:
        entity_id = str(parameters.get('entity_id', ''))
        opacity = parameters.get('opacity')
        if not entity_id or not isinstance(opacity, (int, float)) or not 0 <= opacity <= 1:
            raise ValidationFailedError('entity_id and opacity in [0, 1] are required')
        state.opacity[entity_id] = float(opacity)
        return {'entity_id': entity_id, 'opacity': state.opacity[entity_id]}

    @staticmethod
    def _measurement(state: PresentationState, parameters: dict) -> dict:
        start = parameters.get('start')
        end = parameters.get('end')
        if start is None and end is None:
            state.measurement = None
            return {'measurement': None}
        for point in (start, end):
            if not (isinstance(point, (list, tuple)) and len(point) == 3):
                raise ValidationFailedError('measurement start/end must be [x, y, z]')
        distance = sum((float(a) - float(b)) ** 2 for a, b in zip(start, end)) ** 0.5
        state.measurement = {'start': list(start), 'end': list(end), 'distance': distance}
        return {'measurement': state.measurement}
