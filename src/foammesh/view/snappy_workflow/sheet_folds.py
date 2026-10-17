"""Secondary groups of a snappy page, folded behind a header (Plan 44 MF27).

Plan 44 MF27 asks every guided step to fit the settings sheet without a
scroll bar: a 300 px sheet in a 1280x800 window at the Guided tier, and a
360 px sheet at 1920x1080 with Advanced open. The mockup's sheet
(``plans/mockups/gui-layout-20261012/final-layout.html``) shows a step's
essentials and folds the secondary groups behind fold headers and Advanced.

MEASURED at b59ac794 with the MF27 fit test: Base grid ran 718 px past its
491 px scroller, Castellation 374, Snap 190 and Layers 101. What took the
room were groups a reader opens on purpose, not the step's own settings:
the Automatic Sizing card (309 px, on Base grid and again on Castellation),
the six background faces (616 px) and the mesher limits (297 px on Snap,
174 px on Layers). Each now sits behind one closed header that carries its
name, so each of its controls is one press away (two for the card's field
inputs and the limits' Advanced box, which keep their own folds).

These folds open closed. Plan 33 check 2 (`test_every_fold_opens_open`)
names each of them as a Plan 44 MF27 exception, by object name.
"""
from __future__ import annotations

from foammesh.view.widgets.folder_header import FolderHeader


def fold_behind(layout, widget, title: str, name: str, *,
                tooltip: str = '') -> FolderHeader:
    """Put ``widget`` behind a closed ``FolderHeader`` placed just above it.

    ``widget`` must already be in ``layout``. The header becomes its fold:
    a press shows it, a second press hides it again.
    """
    header = FolderHeader(title, widget.parentWidget())
    header.setObjectName(name)
    # Plan 44 MF27: a secondary group opens closed (see the module doc).
    header.setChecked(False)
    header.setAccessibleName(title)
    if tooltip:
        header.setToolTip(tooltip)
    layout.insertWidget(max(0, layout.indexOf(widget)), header)
    header.setContents(widget)
    return header


def open_on_filter(page, *headers) -> None:
    """Open the folds while the page filter holds text.

    The filter (Plan 40 UXD3) shows only the settings whose name matches,
    and a match behind a closed fold would be a match nobody sees.
    """
    box = getattr(page, '_filter', None)
    if box is None or not hasattr(box, 'textChanged'):
        return

    def follow(text, _headers=headers):
        if str(text or '').strip():
            for header in _headers:
                if header is not None:
                    header.setChecked(True)

    box.textChanged.connect(follow)


def fold_sizing_card(page, layout, card) -> FolderHeader:
    """The Automatic Sizing card behind a closed header named like it.

    The header is the card's heading, so the card's own title line is not
    drawn twice; the title's explanation moves to the header's tooltip.
    Compute, Preview and Apply are one press away, the card's field inputs
    two (they keep the card's own fold).
    """
    from PySide6.QtWidgets import QLabel

    title = card.findChild(QLabel, 'automaticSizingTitle')
    header = fold_behind(
        layout, card, page.tr('Automatic sizing'), 'automaticSizingFold',
        tooltip=title.toolTip() if title is not None else '')
    if title is not None:
        title.setVisible(False)
    open_on_filter(page, header)
    return header
