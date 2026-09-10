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
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.app import app
from foammesh.core.selection import SelectionKind
from foammesh.core.layer_patterns import (
    BACKGROUND_PATCH_GROUP, BLOCK_FACE_NAMES, PatternError, matching_patches)


def candidate_patch_names() -> tuple[str, ...]:
    """Patch names this case is expected to hand snappyHexMesh."""
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('layerPatternPreview')
        self.setWordWrap(True)
        self.setAccessibleName(self.tr('Pattern match preview'))
        self._names: tuple[str, ...] = ()
        self._matched: tuple[str, ...] = ()
        self.refreshNames()
        self.setPattern('')

    # -- state ------------------------------------------------------------- #

    def refreshNames(self, names=None) -> None:
        """Re-read the patches this case is expected to produce."""
        self._names = (tuple(str(name) for name in names)
                       if names is not None else candidate_patch_names())

    def names(self) -> tuple[str, ...]:
        return self._names

    def matched(self) -> tuple[str, ...]:
        """The last preview's answer, so a test can assert on it directly."""
        return self._matched

    # -- display ----------------------------------------------------------- #

    def setPattern(self, pattern) -> None:
        text = str(getattr(pattern, 'value', pattern) or '').strip()
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

    def _say(self, text: str, problem: bool) -> None:
        self.setText(text)
        self.setToolTip(text)
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
