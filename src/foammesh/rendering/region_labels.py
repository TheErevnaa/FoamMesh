"""The names the proposed regions carry in the viewport.

Plan 36 RP10 (§4.4 step 3). "How many fluid regions?" draws each space it
proposes as a translucent volume in its colour, and the plan labels each one
in the viewport, "Fluid 1 · 0.0100 m³", so the row in the panel and the
volume in the view can be matched by name and not by colour alone -- which a
colour-blind user, or one on a high-contrast theme, cannot do.

A label is text anchored at the space's seed, the point furthest from every
wall: it always faces the camera, keeps its size on screen and follows the
camera, as its anchor is a world point. It is drawn over the scene, not in
it -- DP-925: the seed of an internal flow is inside the surface by
definition, so the RP10 billboard, depth-tested at the seed, was hidden
behind the surface for every region the live pass detected. It is drawn in
the theme's tooltip colours, a pair every theme holds at 4.5:1 contrast,
so it reads on the light and the dark background alike; and it cannot be
picked.
"""
from __future__ import annotations

#: The label's font size on screen, in points.
FONT_SIZE = 13

#: Used when no theme is loaded (tests, early start-up).
_FALLBACK_TEXT = '#e8ebef'
_FALLBACK_BACKGROUND = '#1f2328'
_BACKGROUND_OPACITY = 0.75


def label_text(name: str, volume) -> str:
    """``Fluid 1 · 0.0100 m³``: the name, and the volume if there is one."""
    name = str(name or '').strip() or 'Region'
    try:
        value = float(volume)
    except (TypeError, ValueError):
        return name
    if value <= 0 or value != value:
        return name
    return f'{name} · {value:.4f} m³'


def _token(name: str, fallback: str) -> str:
    from foammesh.app import app

    try:
        tokens = app.themeManager.tokens if app.themeManager else None
        value = tokens.values.get(name) if tokens is not None else None
        if value:
            return str(value)
    except Exception:                                         # noqa: BLE001
        pass
    return fallback


def regionLabelActors(candidates) -> list:
    """One non-pickable label per ticked candidate, anchored at its seed.

    *candidates* are `RegionDetectionPanel.candidates()` rows: ``name``,
    ``volume``, ``seed`` and ``ticked``. Unticked rows are not drawn, as
    their volumes are not.
    """
    from vtkmodules.vtkRenderingCore import vtkTextActor
    from foammesh.view.theming.vtk_theme import rgb

    text = rgb(_token('tooltip.foreground', _FALLBACK_TEXT))
    background = rgb(_token('tooltip.background', _FALLBACK_BACKGROUND))
    actors = []
    for row in candidates or ():
        if not row.get('ticked'):
            continue
        seed = row.get('seed')
        try:
            position = tuple(float(value) for value in seed)
        except (TypeError, ValueError):
            continue
        if len(position) != 3:
            continue
        # DP-925. A 2-D actor at a world anchor is drawn in the overlay
        # pass: over the surface that wraps the seed, not hidden behind it.
        actor = vtkTextActor()
        actor.SetInput(label_text(row.get('name'), row.get('volume')))
        anchor = actor.GetPositionCoordinate()
        anchor.SetCoordinateSystemToWorld()
        anchor.SetValue(*position)
        actor.SetObjectName('regionLabel')
        prop = actor.GetTextProperty()
        prop.SetFontSize(FONT_SIZE)
        prop.SetColor(*text)
        prop.SetBackgroundColor(*background)
        prop.SetBackgroundOpacity(_BACKGROUND_OPACITY)
        prop.SetFrame(True)
        prop.SetFrameColor(*text)
        prop.SetJustificationToCentered()
        prop.SetVerticalJustificationToBottom()
        actor.PickableOff()
        actors.append(actor)
    return actors
