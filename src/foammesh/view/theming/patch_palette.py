"""The one place a patch's colour comes from (DP-819).

A patch is drawn in three places: the split-by-angle preview, the colour
swatch on its geometry row, and the 3D viewport. The preview used to paint
with matplotlib's ``rainbow`` scale while the other two used the theme's
patch palette, so the same patch changed colour when the split was applied.
Every view now asks here: a patch's colour is the theme's palette entry for
its slot, and slots are given out in the order the surfaces were written to
the case.
"""
from __future__ import annotations

from pathlib import Path

from .tokens import PALETTE_FAMILIES, load_theme_tokens

#: The theme a palette is read from when no theme is running (tests, tools).
DEFAULT_THEME = Path(__file__).parents[3] / 'resources' / 'theme' / 'dark.json'


def palette_colours(tokens, family: str = 'patch') -> tuple[str, ...]:
    """The ``#rrggbb`` entries of one palette family, in slot order."""
    return tuple(tokens.value(name) for name in PALETTE_FAMILIES[family])


def slot_colour(colours, slot) -> str | None:
    """The colour of ``slot``; the palette repeats past its last entry."""
    if slot is None or not colours:
        return None
    return colours[int(slot) % len(colours)]


def active_palette(tokens=None, family: str = 'patch') -> tuple[str, ...]:
    """The palette of ``tokens``, or of the shipped default theme."""
    if tokens is None:
        tokens = load_theme_tokens(DEFAULT_THEME)
    return palette_colours(tokens, family)


def slot_order(keys) -> list:
    """``keys`` in the order they take palette slots.

    Geometry ids are integers held as text, and as text ``10`` sorts before
    ``2``: a split into nine or more pieces, or a second import, handed the
    first colour to the wrong surface and moved every colour after it.
    Numeric ids sort as numbers, and before any key that is not one.
    """
    def key(value):
        text = str(value)
        return (0, int(text), '') if text.isdigit() else (1, 0, text)

    return sorted(keys, key=key)


# -- Plan 36 RP8: the region (zone) palette --------------------------------- #

def live_tokens():
    """The running theme's tokens, or ``None`` when no theme is running."""
    try:
        from foammesh.app import app
    except Exception:  # noqa: BLE001 - tools and tests without the app
        return None
    manager = getattr(app, 'themeManager', None)
    return getattr(manager, 'tokens', None) if manager is not None else None


def zone_colour(slot, tokens=None) -> str | None:
    """The zone-palette colour of ``slot`` (0-based), cycling past the end.

    The one lookup for a region's colour: the regions table's chip, the
    detection panel's chip and the region volume in the viewport all ask
    here, so a region never changes colour between them. ``tokens`` defaults
    to the running theme, then the shipped default theme.
    """
    if tokens is None:
        tokens = live_tokens()
    return slot_colour(active_palette(tokens, 'zone'), slot)


def region_zone_colours(region_ids, tokens=None) -> dict:
    """``{region_id: '#rrggbb'}``, slots handed out in numeric id order.

    Region ids are integers held as text; :func:`slot_order` sorts them as
    numbers, so region 10 does not take region 2's colour (the DP-819
    lesson).
    """
    if tokens is None:
        tokens = live_tokens()
    colours = active_palette(tokens, 'zone')
    return {key: slot_colour(colours, slot)
            for slot, key in enumerate(slot_order(region_ids))}
