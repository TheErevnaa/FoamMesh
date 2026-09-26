"""What a layer patch pattern currently matches, shown where it is typed.

C31-11. ``addLayersControls/layers`` accepts a regular expression as well as a
patch name, and the expression is the only sensible way to say "every wall" on
a case whose walls are named by the CAD. But a pattern is write-only in a text
box: if it matches nothing, ``layerParameters.C:277-282`` issues an IOWarning
into a long log and snappyHexMesh meshes on with no layers there, so the first
sign of a typo is a mesh that came out wrong. And a pattern that matches *too
much* -- ``.*`` catching the six background faces -- gives no sign at all.

So the editor answers the question the user actually has while typing, which
is not "is this valid regex" but "which of my patches does this select". The
matching is :func:`foammesh.core.layer_patterns.matching_patches`, the same
call the writer makes, so the preview cannot drift into being a second opinion
about what the dictionary will do.

The candidate names are a prediction, not a reading of the finished mesh: the
patches snappyHexMesh will create are the prepared surface groups by their
solver names, plus the six faces of the background block and the group they
share. That is the same list :meth:`CaseBuilder.known_patch_names` builds from
the prepared case, arrived at from the selection service because that is what
the GUI has in front of it.

DP-491 (audit MA-11). MEASURED on the audit's S2 case: the editor said
``box_inner`` "Matches none of the 7 patches in this case: xMin, xMax, yMin,
yMax, zMin, zMax, background", and the layer stage then found 84 faces on
``box_inner``. The prepared case published ``box_inner`` all along; the
preview read its names from the selection catalogue once, when the page was
last refreshed, and the catalogue did not hold the prepared groups then --
so it compared the pattern against the background block alone and called
that "this case". The names now come first from the prepared revision the
writer itself reads (``geometry.prepared.current``), are read again whenever
the editor shows the preview, and a list that holds no geometry patch at all
says that it checked only the block faces.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QLabel

from foammesh.app import app
from foammesh.core.selection import SelectionKind
from foammesh.core.layer_patterns import (
    BACKGROUND_PATCH_GROUP, BLOCK_FACE_NAMES, PatternError, matching_patches)


def _catalogue_names() -> list[str]:
    """Prepared group solver names the selection catalogue has published."""
    names: list[str] = []
    try:
        entities = app.selectionService.entities(
            kinds=(SelectionKind.SURFACE_GROUP,))
    except Exception:                            # no project open yet
        entities = ()
    for entity in entities:
        if getattr(entity, 'owner', '') != 'prepared-groups':
            continue
        metadata = dict(getattr(entity, 'metadata', ()) or ())
        name = str(metadata.get('solver_name') or entity.label or '').strip()
        if name and name not in names:
            names.append(name)
    return names


def candidate_patch_names(prepared=None, geometry=None) -> tuple[str, ...]:
    """Patch names this case is expected to hand snappyHexMesh.

    The order of sources is :meth:`CaseBuilder.known_patch_names`' own:
    *prepared* -- the solver names of the published prepared groups, which
    is what the writer reads -- then, when the page could not reach them, the
    catalogue's copy of the same groups, then *geometry*, the configuration's
    row names the writer falls back to when nothing was prepared. The
    background block's faces and their group always follow.
    """
    names: list[str] = []
    for source in (prepared, None, geometry):
        found = (_catalogue_names() if source is None
                 else [str(name).strip() for name in source or ()])
        for name in found:
            if name and name not in names:
                names.append(name)
        if names:
            break
    for label in BLOCK_FACE_NAMES:
        if label not in names:
            names.append(label)
    if BACKGROUND_PATCH_GROUP not in names:
        names.append(BACKGROUND_PATCH_GROUP)
    return tuple(names)


class LayerPatternPreview(QLabel):
    """A live answer to "which patches does this pattern select?"."""

    #: Past this many, the list is summarised rather than printed in full --
    #: a label that wraps to fifteen lines stops being read.
    MAX_LISTED = 12

    def __init__(self, parent=None, *, source=None):
        super().__init__(parent)
        self.setObjectName('layerPatternPreview')
        self.setWordWrap(True)
        self.setAccessibleName(self.tr('Pattern match preview'))
        #: The owning page's answer to "which patches will this case have";
        #: None reads the catalogue alone.
        self._source = source
        self._names: tuple[str, ...] = ()
        self._matched: tuple[str, ...] = ()
        self._pattern = ''
        self.refreshNames()
        self.setPattern('')

    # -- state ------------------------------------------------------------- #

    def refreshNames(self, names=None) -> None:
        """Re-read the patches this case is expected to produce."""
        if names is None and self._source is not None:
            try:
                names = self._source()
            except Exception:                    # noqa: BLE001 - advisory
                names = None
        self._names = (tuple(str(name) for name in names)
                       if names is not None else candidate_patch_names())

    def geometryKnown(self) -> bool:
        """Whether any name beyond the background block's is in the list."""
        block = set(BLOCK_FACE_NAMES) | {BACKGROUND_PATCH_GROUP}
        return any(name not in block for name in self._names)

    def showEvent(self, event) -> None:
        # DP-491. The editor is where the pattern is typed, and the case can
        # have been prepared since the page last looked: read the names again
        # every time the editor shows this, and answer the pattern it holds.
        super().showEvent(event)
        self.refreshNames()
        self.setPattern(self._pattern)

    def names(self) -> tuple[str, ...]:
        return self._names

    def matched(self) -> tuple[str, ...]:
        """The last preview's answer, so a test can assert on it directly."""
        return self._matched

    # -- display ----------------------------------------------------------- #

    def setPattern(self, pattern) -> None:
        text = str(getattr(pattern, 'value', pattern) or '').strip()
        self._pattern = text
        if not text:
            self._matched = ()
            self._say(self.tr(
                'Type a regular expression to see which patches it selects. '
                'It must match a whole patch name: "wall.*" selects '
                '"wall_inlet", "wall" alone does not.'), False)
            return
        try:
            self._matched = matching_patches(text, self._names)
        except PatternError as error:
            self._matched = ()
            self._say(self.tr('Not a usable pattern: %s') % str(error), True)
            return
        if not self._matched and not self.geometryKnown():
            # Only the background block is known, so "none of the patches in
            # this case" would be a claim about patches nobody has read.
            self._say(
                self.tr("The geometry's patches are not known yet: prepare "
                        "the geometry to check against them. Of the "
                        "background block's faces it matches none: %s")
                % ', '.join(self._names), True)
            return
        if not self._matched:
            self._say(
                self.tr('Matches none of the %s patches in this case: %s')
                % (len(self._names), ', '.join(self._names) or '-'), True)
            return
        self._say(
            self.tr('Matches %s of %s patches: %s')
            % (len(self._matched), len(self._names), self._listed()), False)

    def _listed(self) -> str:
        if len(self._matched) <= self.MAX_LISTED:
            return ', '.join(self._matched)
        shown = ', '.join(self._matched[:self.MAX_LISTED])
        return self.tr('%s and %s more') % (
            shown, len(self._matched) - self.MAX_LISTED)

    def _reserveRoom(self) -> None:
        """Ask the row for as much height as the wrapped answer needs.

        DP-312. MEASURED in the layer group editor at its own width of 366 px,
        with twenty prepared patch names and the pattern ``wall.*``: the
        answer needed 82 px of wrapped text and the row gave the label 54, so
        the last two lines of it were drawn outside the row. A word-wrapped
        label reports a minimum height for the shortest sensible wrap, not
        for the text it is currently holding, and the form has no other way
        to learn that the text changed. The height is measured in the font
        the label already has, at the width it has already been given, so no
        number here is one this module chose.
        """
        width = self.width()
        if width <= 0 and self.parentWidget() is not None:
            width = self.parentWidget().width()
        if width <= 0:
            return
        border = 2 * self.frameWidth() + 2 * self.margin()
        inner = max(width - border, 1)
        wrapped = self.fontMetrics().boundingRect(
            QRect(0, 0, inner, 0),
            int(Qt.TextFlag.TextWordWrap), self.text())
        needed = wrapped.height() + border
        if needed != self.minimumHeight():
            self.setMinimumHeight(needed)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reserveRoom()

    def _say(self, text: str, problem: bool) -> None:
        self.setText(text)
        self.setToolTip(text)
        self._reserveRoom()
        # Colour is the fast read; the sentence is the real one, so the
        # problem is stated in words as well as in the warning colour. The
        # colour comes from the theme's own `foammeshStatus` rule rather than
        # a stylesheet written here -- a screen that styles itself does not
        # follow a theme change, and the repo forbids it for that reason.
        self.setProperty('foammeshStatus', 'warning' if problem else '')
        self.setProperty('foammeshPatternProblem', bool(problem))
        # A dynamic property already used in a selector needs the style
        # re-evaluated; without this the first problem shows in plain text.
        style = self.style()
        if style is not None:
            style.unpolish(self)
            style.polish(self)
