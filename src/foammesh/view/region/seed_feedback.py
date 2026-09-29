"""Say, while a region seed is being placed, whether it is in the fluid.

DP-818. MEASURED on ``annulus.stl`` (radii 0.06 and 0.1, z 0 to 0.5): the user
typed a seed of (0, 0, 0), which sits in the empty core of the pipe, and got a
mesh of the core -- "just a pipe" -- with nothing on the form or in the
viewport having said the point was in the hole. The form showed three numbers;
the viewport drew the seed, if at all, behind the wall that hid it.

This is the one object both region forms use for the answer. While a form is
open it:

* stands the region's stored glyph down (the form owns the point now),
* fades the geometry so a seed in a core or a pocket can be seen through it,
* draws the point being placed, green inside the geometry and red outside,
* and says the same thing in words, on a status line under the coordinates.

Plan 36 RP10. A screen reader hears where the seed is and what it will mesh,
as an accessible description ("x 0.08, y 0, z 0.25 metres. Inside Fluid space
1, 0.0100 cubic metres."). It is written when a placement finishes -- a drag
released, a nudge, a typed value -- and not on every frame of a drag, which
would be read out as noise.

Every point is judged by the geometry manager's classifier, which is built
once per geometry, so the answer keeps up with a drag or with typing. Nothing
here stops a save: a seed outside every body is exactly right for external
flow round it, and the launch gate still has the last word.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.app import app
from foammesh.view.theming.status_colors import set_status


#: What the status line says, by verdict. The status each one wears follows.
MESSAGES = {
    'inside': 'Inside the geometry — this region will be meshed.',
    'outside': ('Outside the geometry (in a hole or beyond the surface) — '
                'snappy will mesh the space outside the surface. That is '
                'only right for flow around a body.'),
    'on_surface': ('On the surface — move the point off the wall; snappy '
                   'cannot tell which side it is on.'),
    'unknown': ('Cannot tell inside from outside — the surface is not '
                'closed, or there is no geometry yet.'),
    'open_to_outside': ('Inside the geometry, but this space is connected '
                        'to the outside — the surface has a gap, so snappy '
                        'would mesh the space outside the surface too.'),
}
STATUSES = {
    'inside': 'success',
    'outside': 'error',
    'on_surface': 'error',
    'unknown': 'warning',
    'open_to_outside': 'error',
}

#: Plan 36 RP6. What the line says once the seed's space is known (§4.1).
SPACE_MESSAGES = {
    'inside': 'Inside the geometry — this region will mesh {name} ({volume}).',
    'core': ('In an open core or hole, which is connected to the outside — '
             'snappy would mesh the space outside the surface. That is only '
             'right for flow around a body.'),
    'external': ('Outside the body — with external flow, this region will '
                 'mesh the space around it ({volume}).'),
    # DP-923: outside, and not in a core -- beside the body, beyond it.
    'beyond': ('Outside the geometry, beyond the surface — snappy would mesh '
               'the space outside the surface. That is only right for flow '
               'around a body.'),
}
#: While the space is still being worked out; added to the verdict's line.
WORKING = 'Working out the space…'

#: Plan 36 RP10. What the accessible description says after the position.
SPOKEN = {
    'inside': 'Inside the geometry.',
    'outside': 'Outside the geometry.',
    'on_surface': 'On the surface.',
    'unknown': 'Cannot tell inside from outside.',
    'open_to_outside': 'Inside, but open to the outside.',
}
SPOKEN_SPACE = 'Inside {name}, {volume:.4f} cubic metres.'


def spoken_position(point) -> str:
    """``x 0.08, y 0, z 0.25 metres.`` -- a point as it is read out."""
    x, y, z = (float(value) for value in point)
    return f'x {x:.4g}, y {y:.4g}, z {z:.4g} metres.'


def _volume(value) -> str:
    return f'{float(value):.4f} m³'


def _geometryManager():
    window = app.window
    return getattr(window, 'geometryManager', None) if window else None


class SeedFeedback(QLabel):
    """The status line under a seed's coordinates, and the viewport behind it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('regionSeedFeedback')
        self.setWordWrap(True)
        self._active = False
        self._suppressed = False
        self._verdict = None
        #: Plan 36 RP6. The domain box the spaces are labelled in, or
        #: ``None``: the page has not opted into drawing them.
        self._volumeDomain = None
        self._volumes = False
        self._point = None
        self.setAccessibleName(self.tr('Seed position'))
        #: RP10. What `announce` last wrote, or ``''``.
        self._spoken = ''
        self._spaceText = ''
        self.clear()

    def verdict(self):
        """The last verdict shown, or ``None`` when nothing is being placed."""
        return self._verdict

    def isActive(self) -> bool:
        return self._active

    def setVolumeDomain(self, provider) -> None:
        """Draw the space each seed will mesh, labelled within ``provider()``.

        Plan 36 RP6. *provider* answers the domain box
        ``(x0, x1, y0, y1, z0, z1)`` or ``None``; ``None`` for the provider
        itself turns the volumes off.
        """
        self._volumeDomain = provider

    def _beginVolumes(self, manager, region_id) -> None:
        if self._volumeDomain is None:
            return
        begin = getattr(manager, 'beginRegionVolumes', None)
        if begin is None:
            return
        try:
            box = self._volumeDomain()
        except Exception:                                     # noqa: BLE001
            box = None
        if box is None or not begin(region_id, box):
            return
        self._volumes = True
        manager.seedSpaceChanged.connect(self._rejudge)

    def _endVolumes(self, manager) -> None:
        if not self._volumes:
            return
        self._volumes = False
        try:
            manager.seedSpaceChanged.disconnect(self._rejudge)
        except (RuntimeError, TypeError):
            pass
        manager.endRegionVolumes()

    def _rejudge(self) -> None:
        if self._active and self._point is not None:
            self.judge(self._point)

    def followSection(self, section) -> None:
        """RP9's section plane: each shown volume draws its cut-fill on it."""
        manager = _geometryManager()
        getter = getattr(manager, 'regionVolumes', None)
        if not self._volumes or getter is None:
            return
        getter().followSection(section)

    def begin(self, region_id=None) -> None:
        """A form has opened on a seed: fade the walls and hand over the glyph."""
        manager = _geometryManager()
        self._active = True
        if manager is None:
            return
        if region_id is not None:
            suppress = getattr(manager, 'suppressRegionMarker', None)
            if suppress is not None:
                suppress(region_id)
                self._suppressed = True
        fade = getattr(manager, 'fadeGeometryForSeed', None)
        if fade is not None:
            fade(True)
        self._beginVolumes(manager, region_id)

    def judge(self, point, announce: bool = True) -> str | None:
        """Judge *point* and say so, on this line and in the viewport.

        Plan 36 RP10. *announce* also writes the accessible description; a
        drag passes False on every frame and `announce`s on release.
        """
        if not self._active:
            return self._verdict
        manager = _geometryManager()
        verdict = None
        self._point = point
        if manager is not None:
            preview = getattr(manager, 'previewSeed', None)
            if preview is not None:
                verdict = preview(point)
            elif hasattr(manager, 'classifySeed'):
                verdict = manager.classifySeed(point)
        self._show(verdict or 'unknown')
        self._spaceText = ''
        if self._volumes and manager is not None:
            state = manager.seedSpace()
            self._showSpace(state)
            # RP13 #7: a space past the triangle budget is drawn as its box
            # (or not at all), and the line says so.
            note = state.get('note') if isinstance(state, dict) else None
            if note:
                self.setText(self.text() + ' ' + self.tr(note))
        if announce:
            self.announce()
        return self._verdict

    def announce(self) -> str:
        """Write where the seed is, and what it meshes, for a screen reader.

        Plan 36 RP10: called once per placement, never per drag frame.
        """
        if not self._active or self._point is None:
            return self._spoken
        said = self._spaceText or self.tr(
            SPOKEN.get(self._verdict, SPOKEN['unknown']))
        spoken = self.tr(spoken_position(self._point)) + ' ' + said
        if spoken != self._spoken:
            self._spoken = spoken
            self.setAccessibleDescription(spoken)
        return spoken

    def spoken(self) -> str:
        """The accessible description last written, or ``''``."""
        return self._spoken

    def end(self) -> None:
        """The form has closed: the walls, the glyph and the line go back."""
        if not self._active:
            return
        self._active = False
        manager = _geometryManager()
        if manager is not None:
            self._endVolumes(manager)
            preview = getattr(manager, 'previewSeed', None)
            if preview is not None:
                preview(None)
            fade = getattr(manager, 'fadeGeometryForSeed', None)
            if fade is not None:
                fade(False)
            if self._suppressed:
                suppress = getattr(manager, 'suppressRegionMarker', None)
                if suppress is not None:
                    suppress(None)
        self._suppressed = False
        self._volumes = False
        self._point = None
        self.clear()

    def clear(self) -> None:
        self._verdict = None
        if getattr(self, '_spoken', ''):
            self._spoken = ''
            self.setAccessibleDescription('')
        super().clear()
        set_status(self, None)

    def _show(self, verdict: str) -> None:
        self._verdict = verdict
        self.setText(self.tr(MESSAGES.get(verdict, MESSAGES['unknown'])))
        set_status(self, STATUSES.get(verdict, 'warning'))

    def _showSpace(self, state: dict) -> None:
        """Say which space the seed will mesh, once that is known (RP6)."""
        if not state.get('active'):
            return
        verdict, space = self._verdict, state.get('space')
        if state.get('working'):
            self.setText(self.text() + ' ' + self.tr(WORKING))
            return
        if space is None or not space.label:
            return
        if not space.outside and verdict == 'inside':
            name = state.get('name') or self.tr('the fluid space')
            self.setText(self.tr(SPACE_MESSAGES['inside']).format(
                name=name, volume=_volume(space.volume)))
            self._spaceText = self.tr(SPOKEN_SPACE).format(
                name=name, volume=float(space.volume))
        elif space.outside and state.get('external'):
            self.setText(self.tr(SPACE_MESSAGES['external']).format(
                volume=_volume(space.volume)))
            set_status(self, 'success')
        elif space.outside and verdict == 'outside':
            # DP-923: the core sentence only for a point the surface wraps.
            # The bounding box alone called every point beside the elbow --
            # which has no core -- "an open core or hole".
            which = 'core' if state.get('inCore') else 'beyond'
            self.setText(self.tr(SPACE_MESSAGES[which]))
