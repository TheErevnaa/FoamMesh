"""The always-visible mesh verdict line at the foot of Region C.

Plan 26 WP3.1. Three properties are the reason this is its own widget rather
than a label the main window sets text on.

**It has three states and the third is mandatory.** ``dormant`` (no mesh),
``current`` (the mesh matches the configuration) and ``stale`` (the
configuration changed after meshing). A permanently visible "PASS" for a
superseded mesh is the same defect as a gate certifying layers that are not
there, and better placement does not excuse it -- so the stale state is not an
optional refinement, it is what makes the strip honest.

**It never signals with colour alone.** pass/blemish/fail maps naturally to
green/amber/red, which fails colour-blind users and the project's own a11y
sweep. Every state carries a text verdict and an icon glyph; colour is
reinforcement, and the accessible description repeats the whole line.

**One verdict, not a row of indicators.** Six live metrics is a dashboard, and
dashboards get ignored. The strip names the governing metric and its count, and
clicking it raises the Mesh quality tab where the rest lives.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from foammesh.core.quality.phrasing import NO_MESH_YET
from foammesh.core.quality.verdict import verdict_source
from foammesh.view.theming.metrics import GAP, apply_bar_metrics

#: Verdict -> (glyph, word, ``foammeshStatus`` role). The glyph and the word
#: carry the meaning; the role only picks a themed colour to reinforce it.
#: Roles rather than hex, so the strip follows the light/dark tokens and stays
#: inside the project's rule that theme_manager owns every colour.
_APPEARANCE = {
    'pass': ('✓', 'pass', 'success'),
    'blemish': ('⚠', 'blemish', 'warning'),
    #: R166. The same mark the outline paints for a failed row, so one
    #: verdict does not wear two faces on one screen. It used to be
    #: U+2715, which `Pretendard Variable` does not carry; the strip
    #: fell back to another face for that one character.
    'fail': ('✗', 'fail', 'error'),
    #: Worse than `fail` (``verdict._RANK`` 3 against 2): the result is
    #: not a usable measurement at all, so it gets a harder mark.
    'invalid': ('‼', 'invalid', 'error'),
    # A mesh that exists but has never been measured. Distinct from dormant:
    # falling back to the dormant appearance made an unchecked mesh report
    # "no mesh yet" over a mesh that was drawn on screen.
    'unrated': ('?', 'not checked', ''),
}
_DORMANT = ('○', 'no mesh yet', '')
#: R119. A verdict a human had to override to reach. The glyph is the one the
#: outline already uses for :class:`WorkflowRowState.WAIVED`, so the same
#: decision reads the same way in both places. R166 moved both off the
#: flag U+2691, which the application font does not carry, onto the
#: reference mark: an engineer wrote a note against this result. Measured symptom: after
#: **Accept anyway** on a failing sicn gate the strip painted
#: ``Quality limits: pass · quality: good`` and nothing said a gate had been
#: overridden. It is `warning`, never `success`: a waiver is not a pass.
_WAIVED = ('※', 'waived', 'warning')
#: A superseded verdict is muted rather than coloured: it is no longer making
#: a claim about the mesh in front of the user.
_STALE_ROLE = ''


class VerdictStrip(QFrame):
    """One line: verdict, governing metric, count. Always visible."""

    #: Emitted when the user clicks the strip. The window raises Mesh quality.
    activated = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('meshVerdictStrip')
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setAccessibleName('Mesh quality verdict')
        self._state = 'dormant'
        self._verdict: dict = {}

        layout = QHBoxLayout(self)
        # DP-191. This strip draws a frame, so the frame is part of the
        # inset rather than added to it -- otherwise its glyph starts a
        # pixel right of the run status line directly above it.
        apply_bar_metrics(self, layout)
        layout.setSpacing(GAP)
        self._glyph = QLabel(self)
        self._glyph.setObjectName('meshVerdictGlyph')
        self._text = QLabel(self)
        self._text.setObjectName('meshVerdictText')
        self._text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._details = QPushButton(self.tr('Details'), self)
        self._details.setObjectName('meshVerdictDetails')
        self._details.setFlat(True)
        # DP-186. The button paints `Details`; the name said nothing of
        # the sort, so `click Details` reached no control.
        self._details.setAccessibleDescription(
            self.tr('Open the Mesh quality tab.'))
        self._details.clicked.connect(self.activated.emit)
        layout.addWidget(self._glyph)
        layout.addWidget(self._text, 1)
        layout.addWidget(self._details)
        self.clear()

    # -- state ------------------------------------------------------------- #

    @property
    def state(self) -> str:
        return self._state

    @property
    def verdict(self) -> dict:
        return dict(self._verdict)

    def clear(self) -> None:
        """No mesh has been produced in this session."""
        self._state = 'dormant'
        self._verdict = {}
        glyph, word, role = _DORMANT
        # DP-107. The same sentence the Mesh quality tab a centimetre
        # below uses, because it is the same fact.
        self._render(glyph, self.tr(NO_MESH_YET), role,
                     self.tr('Mesh quality: %s') % word)
        self._details.setEnabled(False)

    def show_verdict(self, verdict: dict) -> None:
        """Display a fresh verdict for the mesh that was just produced."""
        self._verdict = dict(verdict or {})
        self._state = 'current'
        self._details.setEnabled(True)
        # D9/F14. The strip showed the bare word `pass` and the Mesh quality
        # tab immediately beneath it showed the same verdict as a sentence, so
        # one judgement was stated twice -- and neither said *what* had been
        # judged, while the outline row for the same mesh carried checkMesh's
        # warning. This line is the whole verdict, and it names its scope.
        glyph, _word, role = self._appearance()
        self._render(glyph, self.scopedSummary(), role, self.scopedSummary())

    def scopedSummary(self) -> str:
        """The verdict line, prefixed with who produced it.

        DP-761. ``Quality limits:`` stood for the Gmsh element gate, checkMesh
        and the SU2 readiness check alike; the line now names which one.
        """
        if not self._verdict:
            return self.summary()
        return '{0}: {1}'.format(verdict_source(self._verdict), self.summary())

    def mark_stale(self, reason: str = '') -> None:
        """This verdict describes a superseded mesh.

        Greying without saying why would be worse than leaving the figures
        alone, so the line states that it is superseded rather than only
        looking dim. ``reason`` is parameterised because there are two ways to
        get here and they are not the same fact: the settings moved on after
        the mesh was made, or a stored check was run against a *different*
        mesh than the one now loaded. Reporting the first when the second is
        true would send a user to re-check settings that are fine.
        """
        if self._state == 'dormant':
            return
        self._state = 'stale'
        glyph, _word, _role = self._appearance()
        text = (reason or self.tr('Superseded — settings changed since this '
                                  'mesh')) + ': ' + self.summary()
        self._render(glyph, text, _STALE_ROLE, text)

    # -- rendering --------------------------------------------------------- #

    def summary(self) -> str:
        """Verdict, governing metric and count, as one readable line."""
        if not self._verdict:
            return str(self.tr(NO_MESH_YET))
        _glyph, word, _role = _APPEARANCE.get(
            str(self._verdict.get('verdict') or 'pass'), _DORMANT)
        if self._verdict.get('waived'):
            # R119. The word says the decision *and* what was overridden. A
            # line reading only "waived" would have moved the failure out of
            # sight again rather than into view.
            word = str(self.tr('waived — {0} accepted by {1}')).format(
                str(self._verdict.get('waived_verdict') or word),
                str(self._verdict.get('waiver_actor')
                    or self.tr('an engineer')))
        measure = str(self._verdict.get('measure') or '')
        below = self._verdict.get('belowThreshold')
        total = self._verdict.get('total')
        parts = [str(word)]
        # DP-542. checkMesh's own tally goes straight after the word. S5 and
        # S6 both ended `Failed 1 mesh checks` and this line read `blemish ·
        # mesh grades marginal`, which is how a formally failed check reached
        # Export looking like a pass.
        if self._verdict.get('failedCheckLine'):
            parts.append(str(self._verdict['failedCheckLine']))
        # DP-664. Requested layers that were not grown, which checkMesh
        # cannot see: it grades the cells that exist.
        if self._verdict.get('layerLine'):
            parts.append(str(self._verdict['layerLine']))
        if measure:
            parts.append(measure)
        if below is not None and total:
            parts.append(f'{int(below):,} / {int(total):,}')
        elif self._verdict.get('detail'):
            # checkMesh reports extrema, not counts. Without this the line
            # collapsed to a single word -- "blemish" -- with the figure that
            # earned it sitting unused in the verdict.
            parts.append(str(self._verdict['detail']))
        return ' · '.join(parts)

    def _appearance(self):
        if self._verdict.get('waived'):
            return _WAIVED
        return _APPEARANCE.get(
            str(self._verdict.get('verdict') or 'pass'), _DORMANT)

    def _render(self, glyph: str, text: str, role: str,
                accessible: str) -> None:
        self._glyph.setText(glyph)
        self._text.setText(text)
        # Colour is reinforcement only: the glyph and the word above already
        # carry the verdict, and the accessible description repeats the line in
        # full for a reader that renders neither. The colour itself comes from
        # the theme's status tokens, through the same `foammeshStatus` property
        # every other status label uses -- so it follows light and dark, and no
        # hex lives outside theme_manager.
        for label in (self._glyph, self._text):
            label.setProperty('foammeshStatus', role or None)
            label.setProperty('foammeshTone', None if role else 'muted')
            style = label.style()
            style.unpolish(label)
            style.polish(label)
        self.setAccessibleDescription(accessible)
        self._text.setToolTip(str(self._verdict.get('reason') or accessible))
