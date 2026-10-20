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

#: DP-2750. The view mode combo's modes (``view_modes.MODE_IDS``; a test holds
#: the two equal -- the facade does not import the view layer).
VIEW_MODES = ('geometry', 'boundary', 'volume', 'slice', 'quality')
#: DP-2765. What ``presentation.view.named`` does with the Saved views menu.
NAMED_VIEW_ACTIONS = ('list', 'save', 'apply', 'delete')
#: DP-2752. The toolbar switches that paint over the model.
INSPECTIONS = ('deviation', 'feature_edges', 'worst_cells', 'failed_cells',
               'section')
#: DP-2753. The colour standard's scales and ranges (``colour_standard``; a
#: test holds them equal -- that module loads VTK).
COLOUR_SCALES = ('jet', 'turbo', 'viridis', 'inferno', 'plasma', 'blackbody',
                 'white_to_red', 'coolwarm', 'gray')
COLOUR_RANGES = ('whole', 'limit')
COLOUR_BANDS = (2, 64)
COLOUR_SCALE_ACTIONS = ('make_standard', 'back_to_standard', 'app_standard')
#: DP-2757. The mesh-line panel's limits (``actor_info.MESH_LINE_WIDTH_RANGE``).
MESH_LINE_WIDTH = (0.5, 3.0)
#: DP-2759. A part's right-click Display mode.
DISPLAY_MODES = ('wireframe', 'surface', 'surface_edge')
#: DP-2760. The Elements group's switches and numbers, with their ranges.
ELEMENT_FLAGS = ('points', 'lines', 'surface_faces', 'surface_edges',
                 'volume_faces', 'volume_edges', 'draw_skin_only',
                 'element_labels', 'node_labels', 'light', 'light_two_side',
                 'voronoi')
ELEMENT_NUMBERS = {'node_size': (0.5, 50.0), 'line_width': (0.5, 20.0),
                   'shrink': (0.01, 1.0), 'label_sampling': (1, 100000),
                   'normals': (0.0, 20.0), 'tangents': (0.0, 20.0),
                   'sub_edges': (1, 64)}
#: DP-2766. The Elements group's combos. What *Colour by* and *Label type*
#: offer depends on the mesh on screen, so the window checks the choice
#: against its combo; *Node style* is fixed.
ELEMENT_CHOICES = ('colour_by', 'label_type', 'node_style')
NODE_STYLES = ('dots', 'spheres')


def _point(value, what: str):
    if not (isinstance(value, (list, tuple)) and len(value) == 3
            and all(isinstance(item, (int, float)) and not isinstance(item, bool)
                    for item in value)):
        raise ValidationFailedError(f'{what} must be [x, y, z]', details={what: value})
    return [float(item) for item in value]


def _flag(parameters: dict, name: str):
    value = parameters.get(name)
    if value is not None and not isinstance(value, bool):
        raise ValidationFailedError(f'{name} must be true, false or left out',
                                    details={name: value})
    return value


def _number(parameters: dict, name: str, low=None, high=None):
    value = parameters.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationFailedError(f'{name} must be a number', details={name: value})
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValidationFailedError(f'{name} is out of range', details={
            name: value, 'range': [low, high]})
    return float(value)


def _colour_text(parameters: dict, name: str):
    value = parameters.get(name)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise ValidationFailedError(f'{name} must be a colour string', details={
            name: value, 'example': '#ff8800 or a name such as red'})
    return value


def _entity_ids(parameters: dict, required: bool = True):
    ids = parameters.get('entity_ids')
    if ids is None and not required:
        return None
    if not isinstance(ids, (list, tuple)) or (required and not ids):
        raise ValidationFailedError('entity_ids must be a non-empty list')
    return [str(item) for item in ids]


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
    #: DP-2750. The view mode combo: '' until a mode is entered.
    view_mode: str = ''
    #: DP-2752. The toolbar's inspection switches, as the window shows them.
    inspection: dict = field(default_factory=lambda: {
        name: False for name in INSPECTIONS})
    failed_cell_set: str = ''
    deviation: dict = field(default_factory=dict)
    colour_scale: dict = field(default_factory=dict)
    colour_mesh_by: str = 'solid'
    section: dict = field(default_factory=dict)
    explode: float = 0.0
    mesh_lines: dict = field(default_factory=dict)
    background: dict = field(default_factory=dict)
    elements: dict = field(default_factory=lambda: {'visible': False})
    display_modes: dict = field(default_factory=dict)
    camera_history: dict = field(default_factory=lambda: {
        'can_go_back': False, 'can_go_forward': False})
    last_snapshot: dict | None = None
    #: DP-2765. The names in the camera button's Saved views menu.
    named_views: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            'attached': self.attached, 'perspective': self.perspective,
            'camera_axis': self.camera_axis, 'roll_degrees': self.roll_degrees,
            'rotation_center': list(self.rotation_center) if self.rotation_center else None,
            'fit_requested': self.fit_requested, 'overlays': dict(self.overlays),
            'selection': list(self.selection),
            'visibility': dict(self.visibility), 'colors': dict(self.colors),
            'opacity': dict(self.opacity), 'measurement': self.measurement,
            'view_mode': self.view_mode, 'inspection': dict(self.inspection),
            'failed_cell_set': self.failed_cell_set,
            'deviation': dict(self.deviation),
            'colour_scale': dict(self.colour_scale),
            'colour_mesh_by': self.colour_mesh_by,
            'section': dict(self.section), 'explode': self.explode,
            'mesh_lines': dict(self.mesh_lines),
            'background': dict(self.background),
            'elements': dict(self.elements),
            'display_modes': dict(self.display_modes),
            'camera_history': dict(self.camera_history),
            'last_snapshot': self.last_snapshot,
            'named_views': list(self.named_views),
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
            # DP-2750..2761. The rest of the viewport's controls.
            'presentation.view.mode': self._view_mode,
            'presentation.view.back': self._camera_step('back'),
            'presentation.view.forward': self._camera_step('forward'),
            'presentation.view.fit_selection': self._fit_selection,
            'presentation.deviation': self._inspection('deviation'),
            'presentation.deviation.settings': self._deviation_settings,
            'presentation.feature_edges': self._inspection('feature_edges'),
            'presentation.worst_cells': self._inspection('worst_cells'),
            'presentation.failed_cells': self._failed_cells,
            'presentation.reset_inspection': self._reset_inspection,
            'presentation.colour_scale': self._colour_scale,
            'presentation.colour_mesh_by': self._colour_mesh_by,
            'presentation.section': self._section,
            'presentation.explode': self._explode,
            'presentation.snapshot': self._snapshot,
            'presentation.mesh_lines': self._mesh_lines,
            'presentation.background': self._background,
            'presentation.elements': self._elements,
            'presentation.display_mode': self._display_mode,
            'presentation.isolate': self._isolate,
            'presentation.show_all': self._show_all,
            # DP-2765. The camera button's Saved views.
            'presentation.view.named': self._named_view,
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

    # -- DP-2750..2761: the rest of the viewport ---------------------------- #
    #
    # Each handler checks its input and records what was asked; the window's
    # renderer then presses the control a person would and reports what the
    # window did, which the facade folds into the result.

    @staticmethod
    def _view_mode(state: PresentationState, parameters: dict) -> dict:
        mode = parameters.get('mode')
        if mode not in VIEW_MODES:
            raise ValidationFailedError('unknown view mode', details={
                'mode': mode, 'allowed': list(VIEW_MODES)})
        state.view_mode = mode
        return {'view_mode': mode}

    @staticmethod
    def _camera_step(direction: str):
        def handler(state: PresentationState, _parameters: dict) -> dict:
            return {'camera_step': direction}
        return handler

    @staticmethod
    def _fit_selection(state: PresentationState, _parameters: dict) -> dict:
        state.fit_requested += 1
        return {'fit_selection': True}

    @staticmethod
    def _inspection(name: str):
        def handler(state: PresentationState, parameters: dict) -> dict:
            visible = _flag(parameters, 'visible')
            current = bool(state.inspection.get(name, False))
            wanted = (not current) if visible is None else visible
            state.inspection[name] = wanted
            return {'inspection': name, 'visible': wanted}
        return handler

    def _failed_cells(self, state: PresentationState, parameters: dict) -> dict:
        name = parameters.get('set')
        if name is not None and (not isinstance(name, str) or not name):
            raise ValidationFailedError('set must be the name of a failed-cell set')
        if name is not None and parameters.get('visible') is False:
            raise ValidationFailedError('a set is named only to show it', details={
                'set': name, 'visible': False})
        if name is not None and parameters.get('visible') is None:
            parameters = {**parameters, 'visible': True}
        changed = self._inspection('failed_cells')(state, parameters)
        state.failed_cell_set = (name or '') if changed['visible'] else ''
        if name is not None:
            changed['set'] = name
        return changed

    @staticmethod
    def _reset_inspection(state: PresentationState, _parameters: dict) -> dict:
        for name in INSPECTIONS:
            state.inspection[name] = False
        state.failed_cell_set = ''
        state.colour_mesh_by = 'solid'
        return {'inspection': {name: False for name in INSPECTIONS}}

    @staticmethod
    def _deviation_settings(state: PresentationState, parameters: dict) -> dict:
        changed = {}
        tolerance = _number(parameters, 'tolerance_mm', 0.0, 1.0e6)
        if tolerance is not None:
            changed['tolerance_mm'] = tolerance
        scale = _number(parameters, 'scale_mm', 0.0, 1.0e6)
        if scale is not None:
            # 0 is the panel's Auto: the 98th percentile of the distances.
            if 0.0 < scale < 1.0e-6:
                raise ValidationFailedError(
                    'scale_mm is 0 (automatic) or at least 1e-6')
            changed['scale_mm'] = scale
        surfaces = parameters.get('surfaces')
        if surfaces is not None:
            if not (isinstance(surfaces, dict) and surfaces
                    and all(isinstance(v, bool) for v in surfaces.values())):
                raise ValidationFailedError(
                    'surfaces must map patch names to true or false')
            changed['surfaces'] = {str(k): v for k, v in surfaces.items()}
        if not changed:
            raise ValidationFailedError('give tolerance_mm, scale_mm or surfaces')
        state.deviation.update(changed)
        return {'deviation': changed}

    @staticmethod
    def _colour_scale(state: PresentationState, parameters: dict) -> dict:
        changed = {}
        action = parameters.get('action')
        if action is not None and action not in COLOUR_SCALE_ACTIONS:
            raise ValidationFailedError('unknown colour scale action', details={
                'action': action, 'allowed': list(COLOUR_SCALE_ACTIONS)})
        scale = parameters.get('scale')
        if scale is not None:
            if scale not in COLOUR_SCALES:
                raise ValidationFailedError('unknown colour scale', details={
                    'scale': scale, 'allowed': list(COLOUR_SCALES)})
            changed['scale'] = scale
        for name in ('reverse', 'legend'):
            value = _flag(parameters, name)
            if value is not None:
                changed[name] = value
        bands = parameters.get('bands')
        if bands is not None:
            low, high = COLOUR_BANDS
            if (isinstance(bands, bool) or not isinstance(bands, int)
                    or not (bands == 0 or low <= bands <= high)):
                raise ValidationFailedError('bands is 0 (smooth) or 2 to 64',
                                            details={'bands': bands})
            changed['bands'] = bands
        rng = parameters.get('range')
        if rng is not None:
            if rng not in COLOUR_RANGES:
                raise ValidationFailedError('unknown colour range', details={
                    'range': rng, 'allowed': list(COLOUR_RANGES)})
            changed['range'] = rng
        if not changed and action is None:
            raise ValidationFailedError(
                'give scale, reverse, bands, range, legend or action')
        if changed and action is not None:
            raise ValidationFailedError('an action is sent on its own')
        state.colour_scale.update(changed)
        return {'colour_scale': changed} if action is None else {'action': action}

    @staticmethod
    def _colour_mesh_by(state: PresentationState, parameters: dict) -> dict:
        key = parameters.get('key')
        if not isinstance(key, str) or not key:
            raise ValidationFailedError('key must name a Colour mesh by choice',
                                        details={'example': 'solid'})
        state.colour_mesh_by = key
        return {'colour_mesh_by': key}

    @staticmethod
    def _section(state: PresentationState, parameters: dict) -> dict:
        from foammesh.core.section.modes import SectionMode
        changed = {}
        enabled = _flag(parameters, 'enabled')
        mode = parameters.get('mode')
        if mode is not None:
            allowed = [member.value for member in SectionMode]
            if mode not in allowed:
                raise ValidationFailedError('unknown section mode', details={
                    'mode': mode, 'allowed': allowed})
            changed['mode'] = mode
        for name in ('origin', 'normal'):
            if parameters.get(name) is not None:
                changed[name] = _point(parameters[name], name)
        if 'normal' in changed and not any(changed['normal']):
            raise ValidationFailedError('normal must not be zero')
        if enabled is False and changed:
            raise ValidationFailedError(
                'a section taken down takes no mode, origin or normal')
        if enabled is None and not changed:
            raise ValidationFailedError('give enabled, mode, origin or normal')
        wanted = True if enabled is None else enabled
        changed['enabled'] = wanted
        state.inspection['section'] = wanted
        state.section = ({**state.section, **changed} if wanted
                         else {'enabled': False})
        return {'section': changed}

    @staticmethod
    def _explode(state: PresentationState, parameters: dict) -> dict:
        factor = _number(parameters, 'factor', 0.0, 1.0)
        if factor is None:
            raise ValidationFailedError('factor in [0, 1] is required')
        state.explode = factor
        return {'explode': factor}

    @staticmethod
    def _snapshot(state: PresentationState, parameters: dict) -> dict:
        from foammesh.core import snapshot as snapshot_core
        name = parameters.get('name', '')
        if (not isinstance(name, str) or any(c in name for c in '/\\:')
                or name in ('.', '..')):
            raise ValidationFailedError(
                'name is a plain file name; the snapshot goes into the '
                "case's captures folder", details={'name': name})
        fmt = str(parameters.get('format', 'PNG')).upper()
        fmt = 'JPEG' if fmt == 'JPG' else fmt
        if fmt not in snapshot_core.FORMATS:
            raise ValidationFailedError('unknown snapshot format', details={
                'format': parameters.get('format'),
                'allowed': list(snapshot_core.FORMATS)})
        scale = parameters.get('scale', snapshot_core.DEFAULT_SCALE)
        if isinstance(scale, bool) or scale not in snapshot_core.SCALES:
            raise ValidationFailedError('unknown snapshot scale', details={
                'scale': scale, 'allowed': list(snapshot_core.SCALES)})
        includes = parameters.get('includes', {})
        if not isinstance(includes, dict) or any(
                key not in snapshot_core.INCLUDES or not isinstance(value, bool)
                for key, value in includes.items()):
            raise ValidationFailedError(
                'includes maps a known item to true or false',
                details={'allowed': list(snapshot_core.INCLUDES)})
        return {'snapshot': {'name': name, 'format': fmt, 'scale': scale,
                             'includes': dict(includes)}}

    @staticmethod
    def _named_view(state: PresentationState, parameters: dict) -> dict:
        action = parameters.get('action', 'list')
        if action not in NAMED_VIEW_ACTIONS:
            raise ValidationFailedError('unknown named view action', details={
                'action': action, 'allowed': list(NAMED_VIEW_ACTIONS)})
        name = parameters.get('name')
        if action == 'list':
            if name is not None:
                raise ValidationFailedError('list takes no name')
            return {'named_view': {'action': 'list'}}
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailedError(
                f'{action} needs the name of a view', details={'name': name})
        return {'named_view': {'action': action, 'name': name.strip()}}

    @staticmethod
    def _mesh_lines(state: PresentationState, parameters: dict) -> dict:
        changed = {}
        opacity = _number(parameters, 'opacity', 0.0, 1.0)
        if opacity is not None:
            changed['opacity'] = opacity
        width = _number(parameters, 'width', *MESH_LINE_WIDTH)
        if width is not None:
            changed['width'] = width
        color = _colour_text(parameters, 'color')
        if color is not None:
            changed['color'] = color
        if not changed:
            raise ValidationFailedError('give opacity, color or width')
        state.mesh_lines.update(changed)
        return {'mesh_lines': changed}

    @staticmethod
    def _background(state: PresentationState, parameters: dict) -> dict:
        changed = {}
        for name in ('top', 'bottom'):
            value = _colour_text(parameters, name)
            if value is not None:
                changed[name] = value
        reset = _flag(parameters, 'reset')
        if reset and changed:
            raise ValidationFailedError('reset is sent on its own')
        if reset:
            state.background = {}
            return {'background': {'reset': True}}
        if not changed:
            raise ValidationFailedError('give top, bottom or reset')
        state.background.update(changed)
        return {'background': changed}

    @staticmethod
    def _elements(state: PresentationState, parameters: dict) -> dict:
        changed = {}
        visible = _flag(parameters, 'visible')
        if visible is not None:
            changed['visible'] = visible
        options = parameters.get('options')
        if options is not None:
            if not isinstance(options, dict) or not options:
                raise ValidationFailedError('options must be a non-empty object')
            checked = {}
            for key, value in options.items():
                if key in ELEMENT_FLAGS:
                    if not isinstance(value, bool):
                        raise ValidationFailedError(f'{key} must be true or false')
                    checked[key] = value
                elif key in ELEMENT_NUMBERS:
                    checked[key] = _number(options, key, *ELEMENT_NUMBERS[key])
                elif key in ELEMENT_CHOICES:
                    if not isinstance(value, str) or not value.strip():
                        raise ValidationFailedError(
                            f'{key} must name an entry of its combo',
                            details={key: value})
                    if key == 'node_style' and value.strip().lower() not in NODE_STYLES:
                        raise ValidationFailedError('unknown node style', details={
                            'node_style': value, 'allowed': list(NODE_STYLES)})
                    checked[key] = value.strip()
                elif key == 'cell_types':
                    if not (isinstance(value, dict) and value and all(
                            isinstance(v, bool) for v in value.values())):
                        raise ValidationFailedError(
                            'cell_types maps a cell type to true or false',
                            details={'example': {'Tetrahedra': False}})
                    checked[key] = {str(k): v for k, v in value.items()}
                else:
                    raise ValidationFailedError('unknown element option', details={
                        'option': key,
                        'allowed': list(ELEMENT_FLAGS) + list(ELEMENT_NUMBERS)
                        + list(ELEMENT_CHOICES) + ['cell_types']})
            changed['options'] = checked
        if not changed:
            raise ValidationFailedError('give visible or options')
        if 'visible' in changed:
            state.elements['visible'] = changed['visible']
        if 'options' in changed:
            state.elements['options'] = {**state.elements.get('options', {}),
                                         **changed['options']}
        return {'elements': changed}

    @staticmethod
    def _display_mode(state: PresentationState, parameters: dict) -> dict:
        ids = _entity_ids(parameters)
        mode = parameters.get('mode')
        if mode not in DISPLAY_MODES:
            raise ValidationFailedError('unknown display mode', details={
                'mode': mode, 'allowed': list(DISPLAY_MODES)})
        for entity_id in ids:
            state.display_modes[entity_id] = mode
        return {'entity_ids': ids, 'mode': mode}

    @staticmethod
    def _isolate(state: PresentationState, parameters: dict) -> dict:
        ids = _entity_ids(parameters, required=False)
        if ids is not None:
            state.selection = tuple(ids)
        return {'isolate': list(state.selection) if ids is None else ids}

    @staticmethod
    def _show_all(state: PresentationState, _parameters: dict) -> dict:
        state.visibility = {}
        return {'show_all': True}
