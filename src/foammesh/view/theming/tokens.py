"""Strict, versioned visual tokens shared by Qt-facing theme code."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

TOKEN_SCHEMA_VERSION = 2
#: Schema 1 documents (custom themes written before Plan 44 MF0) still load;
#: see :func:`migrate_v1`.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)
_V1_REQUIRED_TOKENS = frozenset({
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

#: Plan 44 MF0. The v1 name that became a v2 name. A v2 file spells the new
#: name only; the loader fills the old one in from it, so every template and
#: caller that still says ``background.elevated`` keeps working for one release
#: (a file may still carry the old name; it then wins).
V1_ALIASES = {'background.elevated': 'background.raised'}

_STEP_STATES = ('done', 'current', 'todo', 'attention', 'stale', 'locked', 'na', 'plain')
_STEP_FIELDS = ('fill', 'text', 'border', 'bar', 'fill.hover')

#: Plan 44 MF0. Names new in schema 2, grouped as the plan's table groups them.
V2_ADDED_TOKENS = frozenset({
    'background.chrome', 'background.field', 'background.status',
    'background.raised', 'background.control', 'background.neutral',
    'border.quiet', 'border.rule', 'border.group', 'border.legend',
    'foreground.strong', 'foreground.sub', 'foreground.legend',
    'accent.soft', 'selection.open', 'menu.shortcut.hover',
    # Overlays: opaque, pre-blended over the view (Plan 44 §3) ...
    'overlay.edge', 'overlay.legend', 'overlay.card', 'overlay.scrim',
    'overlay.popover', 'overlay.parts', 'overlay.chip.border',
    # ... and the alpha originals, for top-level popups only.
    'overlay.edge.alpha', 'overlay.legend.alpha', 'overlay.scrim.alpha',
    'overlay.parts.alpha',
    'shadow.menu', 'shadow.sheet', 'shadow.low', 'shadow.card',
    'shadow.popover', 'shadow.snap',
    'lock.ok.border', 'lock.ok.bg', 'lock.ok.fg', 'lock.ok.hover',
    'lock.open.border', 'lock.open.bg', 'lock.open.fg',
    'lock.warn.border', 'lock.warn.bg', 'ask.fg',
    'tab.pill.bg', 'tab.pill.border',
    'console.text', 'console.stage', 'console.warning', 'console.ok', 'console.dim',
    'value.bad', 'value.ok', 'value.amber',
    'pin.head', 'pin.head.stroke', 'explode.accent',
    'viewport.centre', 'viewport.mid', 'viewport.edge', 'viewport.lines',
    'triad.fill', 'triad.stroke', 'triad.up', 'triad.x', 'triad.y',
    'step.chapter.fill', 'step.chapter.text', 'step.chapter.border',
} | {f'step.{state}.{field}' for state in _STEP_STATES for field in _STEP_FIELDS})

# -- SG2 (Plan 44 suite green, round 2): the link ink --------------------
#: A link-role push button (core.qss.tmpl, MF20) wrote ``accent.default``,
#: which is 3.77:1 on the light surface. ``foreground.link`` is the ink a link
#: is read in: light #2559c4, dark #6ba0ff, at least 4.5:1 on every v2 ground.
V2_ADDED_TOKENS = V2_ADDED_TOKENS | {'foreground.link'}
# -- end SG2 ---------------------------------------------------------------

#: Names that may be written ``#rrggbbaa`` (and are rendered as ``rgba()``).
#: Every other token is an opaque ``#rrggbb``.
ALPHA_TOKENS = frozenset({
    'overlay.edge.alpha', 'overlay.legend.alpha', 'overlay.scrim.alpha',
    'overlay.parts.alpha', 'overlay.chip.border',
    'shadow.menu', 'shadow.sheet', 'shadow.low', 'shadow.card',
    'shadow.popover', 'shadow.snap',
})

#: The v1 names a v2 file no longer spells (they are filled from the alias).
REQUIRED_TOKENS = frozenset(
    (_V1_REQUIRED_TOKENS - set(V1_ALIASES)) | V2_ADDED_TOKENS)

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
_COLOUR_ALPHA = re.compile(r'^#[0-9a-fA-F]{8}$')

# Text pairs are part of the theme contract, not merely recommendations.  This
# prevents a syntactically valid custom theme from making core UI unreadable.
#: DP-193. Every colour a theme paints words in, against every ground it can
#: be painted on. The status colours have no ground of their own -- a status
#: label inherits whichever page surface it lands on -- so each is held
#: against all three, and the darkest of the three (`background.elevated` in
#: both shipped themes) is what actually binds.
_PAGE_GROUNDS = ('background.canvas', 'background.surface', 'background.raised')
#: Plan 44 MF0. Every surface a v2 theme paints words on.
_V2_GROUNDS = _PAGE_GROUNDS + (
    'background.chrome', 'background.field', 'background.status',
    'background.control', 'background.neutral')
_STATUS_TEXT = ('status.success', 'status.warning', 'status.error', 'status.info')
#: Every colour the theme paints words in on an ordinary page.
_PAGE_TEXT = ('foreground.primary', 'foreground.secondary') + _STATUS_TEXT

#: Plan 44 MF0. ``foreground.muted`` and ``foreground.sub`` are captions: the
#: mockup sets them at 7d838c / 8b919a, which is 3.8-4.5:1 on the raised
#: surfaces, so they are held to 3:1 and may only be used for text of 11 px or
#: more that is not the only carrier of a meaning (a caption beside a value).
_CAPTION_TEXT = ('foreground.muted', 'foreground.sub')

_CONTRAST_PAIRS = (
    ('foreground.primary', 'input.background', 4.5),
    ('console.foreground', 'console.background', 4.5),
    ('tooltip.foreground', 'tooltip.background', 4.5),
    ('disabled.foreground', 'disabled.background', 2.5),
    # The label on the one filled button that advances the workflow.
    ('accent.text', 'accent.fill', 4.5),
    ('accent.text', 'accent.fill.hover', 4.5),
    ('foreground.legend', 'overlay.legend', 4.5),
    ('foreground.legend', 'overlay.card', 4.5),
    ('console.text', 'console.background', 4.5),
    ('console.text', 'overlay.card', 4.5),
    ('menu.shortcut.hover', 'selection.background', 4.5),
    # Plan 44 F2 (MF24 items 1-3, 5, 8): the hot menu row, the selected
    # menu-bar item, a checked segment or rail tile and a checked sheet
    # button write foreground.strong on the selection or the soft accent.
    ('foreground.strong', 'selection.background', 4.5),
    ('foreground.strong', 'accent.soft', 4.5),
    # Plan 44 MF18: the lock chip in its three states (forms.qss.tmpl), and
    # the console text on the card's field (MF6, console.qss.tmpl).
    ('lock.ok.fg', 'lock.ok.bg', 4.5),
    ('foreground.strong', 'lock.ok.bg', 4.5),
    ('lock.open.fg', 'lock.open.bg', 4.5),
    # DP-2554. The confirm press writes accent.text on lock.warn.bg; a
    # schema-1 theme took lock.warn.bg straight from status.error, a text
    # colour light enough to fail under white (see `migrate_v1`).
    ('accent.text', 'lock.warn.bg', 4.5),
    ('console.foreground', 'background.field', 4.5),
    # Plan 44 MF26: the sheet dialog (dialogs.qss.tmpl) -- its body text, the
    # caps title and the label column on the raised surface, and the primary
    # (default) button's label on the accent.
    ('foreground.primary', 'background.raised', 4.5),
    ('foreground.secondary', 'background.raised', 4.5),
    ('accent.text', 'accent.fill', 4.5),
) + tuple((foreground, ground, 4.5)
          for foreground in _PAGE_TEXT for ground in _PAGE_GROUNDS
          ) + tuple((foreground, ground, 4.5)
                    for foreground in ('foreground.primary', 'foreground.strong',
                                       'foreground.secondary', 'foreground.legend')
                    for ground in _V2_GROUNDS
                    ) + tuple((foreground, ground, 3.0)
                              for foreground in _CAPTION_TEXT for ground in _V2_GROUNDS
                              ) + tuple(
    (f'step.{state}.text', f'step.{state}.fill', 4.5)
    for state in ('done', 'current', 'todo', 'attention', 'stale', 'plain', 'chapter')
) + (
    # Locked and not-applicable tabs are meant to look switched off, so they
    # are held to the disabled-control floor, not the reading floor.
    ('step.locked.text', 'step.locked.fill', 2.5),
    ('step.na.text', 'step.na.fill', 2.5),
)

# A stylesheet that still says ``background.elevated`` puts the same two colours
# against each other as one that says ``background.raised``; hold both spellings.
_ALIAS_OF = {new: old for old, new in V1_ALIASES.items()}
_CONTRAST_PAIRS = _CONTRAST_PAIRS + tuple(
    (foreground, _ALIAS_OF.get(ground, ground), minimum)
    for foreground, ground, minimum in _CONTRAST_PAIRS if ground in _ALIAS_OF)

# -- SG2 (Plan 44 suite green, round 2): pairs the stylesheet makes ----------
#: The link ink on every v2 ground (core.qss.tmpl), and the MF18 lock chip's
#: confirm state, ``accent.text`` on ``lock.warn.bg`` (forms.qss.tmpl).
_CONTRAST_PAIRS = _CONTRAST_PAIRS + tuple(
    ('foreground.link', ground, 4.5) for ground in _V2_GROUNDS) + (
    ('accent.text', 'lock.warn.bg', 4.5),)
# -- end SG2 ---------------------------------------------------------------

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


def split_alpha(value: str) -> tuple[str, float]:
    """``#rrggbb[aa]`` -> (``#rrggbb``, alpha 0..1)."""
    if len(value) == 9:
        return value[:7], int(value[7:9], 16) / 255
    return value, 1.0


def composite(foreground: str, ground: str) -> str:
    """Flatten ``foreground`` (which may carry alpha) over an opaque ``ground``."""
    colour, alpha = split_alpha(foreground)
    base, _ = split_alpha(ground)
    channels = [round(int(colour[i:i + 2], 16) * alpha + int(base[i:i + 2], 16) * (1 - alpha))
                for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(channels)


def relative_luminance(value: str) -> float:
    value = split_alpha(value)[0]
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
    """WCAG ratio of ``first`` over ``second``; an alpha ``first`` is flattened
    over ``second`` before it is measured (Plan 44 MF0)."""
    if len(first) == 9:
        first = composite(first, second)
    light, dark = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


_V1_ROLE_SOURCE = {
    'background.chrome': 'background.canvas', 'background.field': 'input.background',
    'background.status': 'background.surface', 'background.control': 'background.elevated',
    'background.neutral': 'background.surface',
    'border.quiet': 'border.default', 'border.rule': 'border.default',
    'border.group': 'border.default', 'border.legend': 'border.default',
    'foreground.strong': 'foreground.primary', 'foreground.sub': 'foreground.muted',
    'foreground.legend': 'foreground.primary',
    'accent.soft': 'selection.background', 'selection.open': 'selection.background',
    'menu.shortcut.hover': 'foreground.primary',
    'overlay.edge': 'background.surface', 'overlay.legend': 'background.surface',
    'overlay.card': 'background.surface', 'overlay.scrim': 'background.canvas',
    'overlay.popover': 'background.elevated', 'overlay.parts': 'background.elevated',
    'overlay.edge.alpha': 'background.surface', 'overlay.legend.alpha': 'background.surface',
    'overlay.scrim.alpha': 'background.canvas', 'overlay.parts.alpha': 'background.elevated',
    'overlay.chip.border': 'border.default',
    'lock.ok.border': 'status.success', 'lock.ok.bg': 'background.surface',
    'lock.ok.fg': 'foreground.primary', 'lock.ok.hover': 'status.success',
    'lock.open.border': 'border.default', 'lock.open.bg': 'background.surface',
    'lock.open.fg': 'foreground.primary',
    'lock.warn.border': 'status.error', 'lock.warn.bg': 'status.error',
    'ask.fg': 'foreground.primary',
    'tab.pill.bg': 'selection.background', 'tab.pill.border': 'accent.default',
    'console.text': 'console.foreground', 'console.stage': 'accent.default',
    'console.warning': 'status.warning', 'console.ok': 'status.success',
    'console.dim': 'foreground.muted',
    'value.bad': 'status.error', 'value.ok': 'status.success', 'value.amber': 'status.warning',
    'pin.head': 'status.error', 'pin.head.stroke': 'status.error',
    'explode.accent': 'accent.default',
    'viewport.centre': 'viewport.bottom', 'viewport.mid': 'viewport.bottom',
    'viewport.edge': 'viewport.top', 'viewport.lines': 'viewport.silhouette',
    'triad.fill': 'foreground.primary', 'triad.stroke': 'foreground.secondary',
    'triad.up': 'accent.default', 'triad.x': 'status.error', 'triad.y': 'status.success',
}


def _mix(colour: str, towards: str, share: float) -> str:
    channels = [round(int(colour[i:i + 2], 16) * (1 - share) + int(towards[i:i + 2], 16) * share)
                for i in (1, 3, 5)]
    return '#%02x%02x%02x' % tuple(channels)


def ground_for(ink: str, colour: str, minimum: float = 4.5) -> str:
    """*colour*, darkened (or lightened) just enough to carry *ink*.

    DP-2554. A v1 theme has no fill for the red confirm press, only the red
    it writes words in; that red is a text colour, so it is moved away from
    the ink -- towards black under light ink, towards white under dark -- in
    5 % steps until the ink reads at *minimum* on it.
    """
    towards = '#000000' if relative_luminance(ink) > relative_luminance(colour) else '#ffffff'
    for step in range(21):
        ground = _mix(colour, towards, step / 20)
        if contrast_ratio(ink, ground) >= minimum:
            return ground
    return towards

# -- SG2: what a schema-1 theme fills the SG2 names from --------------------
#: A v1 theme has no link ink of its own; its body ink already reads on every
#: ground. The lock chip's red confirm ground (MF18) took ``status.error``,
#: which no v1 theme is held to carry white words on; ``migrate_v1`` derives
#: it from ``status.error`` with ``ground_for`` (DP-2554), so it stays red.
_V1_ROLE_SOURCE['foreground.link'] = 'foreground.primary'
# -- end SG2 ---------------------------------------------------------------

def migrate_v1(tokens: dict[str, str]) -> dict[str, str]:
    """Fill the names schema 2 added from the roles they play in a v1 theme.

    A custom theme written against schema 1 keeps loading; every new name takes
    the value of the v1 token that already played that role, so nothing in it
    can fail a contrast test it already passed.
    """
    out = dict(tokens)
    for old, new in V1_ALIASES.items():
        out[new] = tokens[old]
    source = dict(_V1_ROLE_SOURCE)
    for state in ('done', 'current', 'todo', 'attention', 'stale', 'locked', 'na', 'plain', 'chapter'):
        bar = {'done': 'status.success', 'current': 'accent.default',
               'attention': 'status.error', 'stale': 'status.warning'}.get(state, 'border.default')
        source[f'step.{state}.fill'] = 'background.surface'
        source[f'step.{state}.text'] = 'foreground.primary'
        source[f'step.{state}.border'] = 'border.default'
        if state != 'chapter':
            source[f'step.{state}.bar'] = bar
            source[f'step.{state}.fill.hover'] = 'background.elevated'
    for new, old in source.items():
        value = tokens[old]
        out[new] = value + 'ff' if new in ALPHA_TOKENS else value
    for name in ('shadow.menu', 'shadow.sheet', 'shadow.low', 'shadow.card',
                 'shadow.popover', 'shadow.snap'):
        out[name] = '#00000066'
    # DP-2554. The confirm chip writes accent.text on this fill.
    out['lock.warn.bg'] = ground_for(tokens['accent.text'], tokens['status.error'])
    return out


def load_theme_tokens(path: str | Path) -> ThemeTokens:
    source = Path(path)
    try:
        document = json.loads(source.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise TokenValidationError(f'cannot read theme tokens: {error}') from error
    if set(document) != {'schema_version', 'name', 'tokens'}:
        raise TokenValidationError('theme document must contain only schema_version, name, and tokens')
    version = document['schema_version']
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise TokenValidationError(f'unsupported theme token schema: {version}')
    if not isinstance(document['name'], str) or not document['name']:
        raise TokenValidationError('theme name must be a non-empty string')
    tokens = document['tokens']
    if not isinstance(tokens, dict):
        raise TokenValidationError('tokens must be an object')
    # A v2 file may still spell an alias; it is allowed, never required.
    required = _V1_REQUIRED_TOKENS if version == 1 else REQUIRED_TOKENS
    allowed = required | (set(V1_ALIASES) if version == 2 else set())
    missing = required - set(tokens)
    unknown = set(tokens) - allowed
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f'missing: {", ".join(sorted(missing))}')
        if unknown:
            parts.append(f'unknown: {", ".join(sorted(unknown))}')
        raise TokenValidationError('; '.join(parts))

    def valid(name, value):
        if not isinstance(value, str):
            return False
        if _COLOUR.fullmatch(value):
            return True
        return version == 2 and name in ALPHA_TOKENS and bool(_COLOUR_ALPHA.fullmatch(value))
    bad = sorted(name for name, value in tokens.items() if not valid(name, value))
    if bad:
        raise TokenValidationError(
            'every token value must be a six-digit hexadecimal colour '
            '(eight digits only for alpha-capable tokens): ' + ', '.join(bad))
    tokens = dict(tokens)
    if version == 1:
        tokens = migrate_v1(tokens)
    else:
        for old, new in V1_ALIASES.items():
            tokens.setdefault(old, tokens[new])
    failures = [f'{foreground}/{background} < {minimum:g}:1'
                for foreground, background, minimum in _CONTRAST_PAIRS + _NON_TEXT_PAIRS
                if contrast_ratio(tokens[foreground], tokens[background]) < minimum]
    if failures:
        raise TokenValidationError('insufficient contrast: ' + '; '.join(failures))
    return ThemeTokens(document['name'], tokens)
