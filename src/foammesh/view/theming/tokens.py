"""Strict, versioned visual tokens shared by Qt-facing theme code."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

TOKEN_SCHEMA_VERSION = 1
REQUIRED_TOKENS = frozenset({
    'background.canvas', 'background.surface', 'background.elevated',
    'foreground.primary', 'foreground.secondary', 'foreground.muted',
    'border.default', 'accent.default', 'accent.hover',
    # DP-193. The accent has two jobs and they want opposite lightnesses: a
    # hairline, ring or rail has to stand off the page, and a filled button has
    # to carry words on top of it. `accent.default` stayed the bright line
    # colour and read at 3.22:1 under white -- so `accent.fill` is the surface
    # the label sits on, dark enough to carry it, and the button keeps its
    # `accent.default` border so its edge is still the bright accent.
    'accent.fill', 'accent.fill.hover',
    # Text drawn *on* the accent, for the one filled primary button per dialog.
    # Without it a default button had to reuse the page foreground, which is
    # why FoamMesh had no primary action colour at all while FoamFlow did.
    'accent.text',
    'focus.ring', 'selection.background',
    'status.success', 'status.warning', 'status.error', 'status.info',
    'disabled.background', 'disabled.foreground', 'input.background',
    'viewport.top', 'viewport.bottom', 'console.background', 'console.foreground',
    'tooltip.background', 'tooltip.foreground',
    # Viewport surfaces. Every boundary patch and the internal mesh used to
    # render in VTK's default white, so what a user actually saw was the edge
    # colour tiling the surface -- a mesh with no material identity at all.
    'viewport.surface', 'viewport.silhouette',
    'viewport.patch.1', 'viewport.patch.2', 'viewport.patch.3',
    'viewport.patch.4', 'viewport.patch.5', 'viewport.patch.6',
    # Cell and face zones are built as actors and were then left in the neutral
    # surface colour, so a multi-zone mesh read as one undifferentiated solid.
    # A separate hue family keeps a zone from ever being mistaken for a patch.
    'viewport.zone.1', 'viewport.zone.2', 'viewport.zone.3', 'viewport.zone.4',
    # Plan 36 RP8: a case's fluid and solid regions take zone colours too, and
    # eight covers the regions a case realistically has before it cycles.
    'viewport.zone.5', 'viewport.zone.6', 'viewport.zone.7', 'viewport.zone.8',
})

#: Ordered categorical palette handed to boundary patches. Six is past the
#: patch count of most cases and cycles predictably beyond it; the entries sit
#: at similar lightness so no patch shouts, and they stay separable under the
#: common colour-vision deficiencies.
PATCH_TOKENS = tuple(f'viewport.patch.{index}' for index in range(1, 7))

#: The zone palette. Deliberately a different hue family from the patch palette
#: so that "which of these is a zone" is answerable without reading the tree.
ZONE_TOKENS = tuple(f'viewport.zone.{index}' for index in range(1, 9))

#: Named palette families addressable by :meth:`ActorInfo.setPaletteIndex`.
PALETTE_FAMILIES = {'patch': PATCH_TOKENS, 'zone': ZONE_TOKENS}
_COLOUR = re.compile(r'^#[0-9a-fA-F]{6}$')

# Text pairs are part of the theme contract, not merely recommendations.  This
# prevents a syntactically valid custom theme from making core UI unreadable.
#: DP-193. Every colour a theme paints words in, against every ground it can
#: be painted on. The status colours have no ground of their own -- a status
#: label inherits whichever page surface it lands on -- so each is held
#: against all three, and the darkest of the three (`background.elevated` in
#: both shipped themes) is what actually binds.
_PAGE_GROUNDS = ('background.canvas', 'background.surface', 'background.elevated')
_STATUS_TEXT = ('status.success', 'status.warning', 'status.error', 'status.info')
#: Every colour the theme paints words in on an ordinary page.
_PAGE_TEXT = ('foreground.primary', 'foreground.secondary',
              'foreground.muted') + _STATUS_TEXT

_CONTRAST_PAIRS = (
    ('foreground.primary', 'input.background', 4.5),
    ('console.foreground', 'console.background', 4.5),
    ('tooltip.foreground', 'tooltip.background', 4.5),
    ('disabled.foreground', 'disabled.background', 2.5),
    # The label on the one filled button that advances the workflow.
    ('accent.text', 'accent.fill', 4.5),
    ('accent.text', 'accent.fill.hover', 4.5),
) + tuple((foreground, ground, 4.5)
          for foreground in _PAGE_TEXT for ground in _PAGE_GROUNDS)

#: DP-193. Lines, rings and rails are not text: WCAG 2.1 SC 1.4.11 asks 3:1 of
#: them, not 4.5:1. Held separately so that darkening a fill to carry a label
#: can never quietly drag the focus ring down with it.
_NON_TEXT_PAIRS = tuple(
    (foreground, ground, 3.0)
    for foreground in ('accent.default', 'focus.ring') for ground in _PAGE_GROUNDS)


class TokenValidationError(ValueError):
    """A theme file does not meet the stable token contract."""


@dataclass(frozen=True)
class ThemeTokens:
    name: str
    values: dict[str, str]

    def value(self, name: str) -> str:
        return self.values[name]


def relative_luminance(value: str) -> float:
    channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [channel / 12.92 if channel <= 0.04045
              else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def icon_ink_prefix(tokens: 'ThemeTokens') -> str:
    """Which of the two shipped icon sets this theme can be read in.

    DP-203. Qt hands a sub-control back to the base style only while the
    style sheet says nothing about it. The sheet sizes
    ``QAbstractSpinBox::up-button`` -- it has to, or the step buttons come out
    as a 12px column -- and that alone moves the whole spin box onto the
    style-sheet paint path, where an arrow with no ``image`` is not a default
    arrow but no arrow at all. The same is true of ``QComboBox::down-arrow``.
    So the sheet has to name the picture, and a named picture does not follow
    the palette the way a base-style arrow would: the ionicons ship as black
    ink, which is invisible on a dark canvas, and the white set is invisible
    on a light one. This picks the set by the ink the theme writes its words
    in, so a custom theme gets the legible one without listing an icon path
    among its colours.
    """
    return ('icons-white'
            if relative_luminance(tokens.value('foreground.primary')) > 0.5
            else 'icons')


def contrast_ratio(first: str, second: str) -> float:
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def load_theme_tokens(path: str | Path) -> ThemeTokens:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise TokenValidationError(f'cannot read theme tokens: {error}') from error
    if set(document) != {'schema_version', 'name', 'tokens'}:
        raise TokenValidationError('theme document must contain only schema_version, name, and tokens')
    if document['schema_version'] != TOKEN_SCHEMA_VERSION:
        raise TokenValidationError(f'unsupported theme token schema: {document["schema_version"]}')
    if not isinstance(document['name'], str) or not document['name']:
        raise TokenValidationError('theme name must be a non-empty string')
    tokens = document['tokens']
    if not isinstance(tokens, dict):
        raise TokenValidationError('tokens must be an object')
    missing = REQUIRED_TOKENS - set(tokens)
    unknown = set(tokens) - REQUIRED_TOKENS
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f'missing: {", ".join(sorted(missing))}')
        if unknown:
            parts.append(f'unknown: {", ".join(sorted(unknown))}')
        raise TokenValidationError('; '.join(parts))
    if any(not isinstance(value, str) or not _COLOUR.fullmatch(value) for value in tokens.values()):
        raise TokenValidationError('every token value must be a six-digit hexadecimal colour')
    failures = [f'{foreground}/{background} < {minimum:g}:1'
                for foreground, background, minimum in _CONTRAST_PAIRS + _NON_TEXT_PAIRS
                if contrast_ratio(tokens[foreground], tokens[background]) < minimum]
    if failures:
        raise TokenValidationError('insufficient contrast: ' + '; '.join(failures))
    return ThemeTokens(document['name'], dict(tokens))
