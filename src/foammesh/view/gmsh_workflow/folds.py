"""Plan 44 FIT-G (MF27). A secondary section of a Gmsh page, folded.

The MF27 fit gate asks every guided step to fit the 300 px sheet of a
1280x800 window without a scroll bar. MEASURED at b59ac794 over the house
harness: Sizing ran 1492 px over its 491 px scroller, Structure 279, Layers 43
and Quality 235, because every table, panel and catalogue the Gmsh route owns
was drawn open, one under the other.

The Plan 41 rule is that the Gmsh route keeps every native control in reach.
So nothing here is removed: each secondary section -- a collection table, a
picked-entity panel, the native field catalogue -- sits behind one fold
header (the MF14 disclosure idiom, ``FolderHeader``), closed until asked for.
One press opens it; the control inside is the second. The header says how many
rows the section holds, so a closed fold never hides a configured section
without saying so, the way *Advanced* says how many settings behind it are
off their default.

The header is not titled *Advanced*: these are sections of the page, not the
Common tier, and the gate's 1920 pass opens only the Advanced disclosures.
"""
from __future__ import annotations

from PySide6.QtWidgets import QTableWidget, QWidget

from foammesh.view.widgets.folder_header import FolderHeader


def add_fold(page, layout, *, name: str, title: str, description: str,
             panel: QWidget, index: int | None = None) -> FolderHeader:
    """Put *panel* behind a closed fold header on *layout*; return the header.

    ``index`` inserts at that position (the header, then the panel);
    otherwise both are appended. The header's object name is
    ``gmsh<name>Fold``.
    """
    # Parentless: the layout reparents it, and the pages' tests call
    # `build_sections` on a bare instance (as `quality_items.mount_below`).
    header = FolderHeader(title)
    header.setObjectName(f'gmsh{name}Fold')
    header.setAccessibleName(title)
    header.setAccessibleDescription(description)
    header.setToolTip(description)
    header.setProperty('foammeshFoldTitle', title)
    # What `test_every_fold_opens_open` checks this fold by: a section fold
    # may open closed, and then its header counts the rows behind it.
    header.setProperty('foammeshSectionFold', True)
    # A secondary section opens closed (FolderHeader opens open by default;
    # the reason lives here, at the site): the guided form is what fits.
    header.setChecked(False)
    if index is None:
        layout.addWidget(header)
        layout.addWidget(panel)
    else:
        layout.insertWidget(index, header)
        layout.insertWidget(index + 1, panel)
    header.setContents(panel)
    return header


def clip_header(header: FolderHeader) -> FolderHeader:
    """Let a header whose text carries a summary be narrower than its text.

    A ``QCheckBox`` will not be narrower than its text, so a long summary
    widened the whole page and made it scroll sideways. The full text is the
    header's tooltip.
    """
    from PySide6.QtWidgets import QSizePolicy

    header.setSizePolicy(QSizePolicy.Policy.Ignored,
                         QSizePolicy.Policy.Preferred)
    header.setMinimumWidth(0)
    # Its text is the title and a one-line summary of the values behind it.
    header.setProperty('foammeshSummaryFold', True)
    return header


def fold_rows(panel: QWidget) -> int:
    """How many rows the section's first table holds (0 when it has none)."""
    if panel is None:
        return 0
    table = panel if isinstance(panel, QTableWidget) else panel.findChild(
        QTableWidget)
    try:
        return int(table.rowCount()) if table is not None else 0
    except RuntimeError:                         # deleted under us
        return 0


def count_fold(header: FolderHeader, rows: int) -> None:
    """Say on the header how many rows are behind it."""
    if header is None:
        return
    title = str(header.property('foammeshFoldTitle') or header.text())
    header.setText(title if rows <= 0 else f'{title}  ({rows})')


def refresh_folds(folds) -> None:
    """Re-count every ``(header, panel)`` pair."""
    for header, panel in folds:
        count_fold(header, fold_rows(panel))
