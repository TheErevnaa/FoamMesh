"""Embed collection-detail dialogs in their owning Region B page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QFrame, QScrollArea, QWidget


def open_in_page(dialog, host: QWidget, above: QWidget = None) -> bool:
    """Show a real Qt dialog as page-local detail without cloning its fields.

    File choosers and confirmation dialogs continue to use modal ownership;
    this adapter is only for mature collection editors whose controls must
    remain visible beside the Region C viewport.

    `above` names the page's action row. G6: the editor used to be appended
    to the end of the page, below `Refine` / `Add` / `Reset`, and a form
    taller than the space left over pushed that row off the bottom of the
    panel -- so the form could not be submitted without scrolling a panel
    whose scrollbar was itself off screen. The editor now goes above the row
    and carries its own scroll area, so the row keeps its place whatever the
    form's height.
    """
    layout = host.layout()
    if not isinstance(dialog, QDialog) or layout is None:
        return False
    dialog.setObjectName(
        dialog.objectName() or 'inPageDetailEditor')
    dialog.setProperty('regionBInPageDetail', True)
    dialog.setAccessibleName(
        dialog.accessibleName() or dialog.windowTitle()
        or 'Selected collection row details')
    dialog.setWindowFlags(Qt.WindowType.Widget)

    holder = QScrollArea(host)
    holder.setObjectName('inPageDetailScroll')
    holder.setFrameShape(QFrame.Shape.NoFrame)
    holder.setWidgetResizable(True)
    # R81. C5 turned this off so the task page would not stack two sideways
    # scrollbars. It is the wrong policy *here*: this holder IS the innermost
    # scrollable thing, and a mature dialog laid out for its own window is
    # routinely wider than Region B. MEASURED on Castellation's Surface
    # Refinement editor at the default panel width -- the value column was cut
    # mid-number (`cell size (0.00384615 x 0.00384615 x 0.00`), and the two
    # rows that end in an expanding spacer put their buttons past the right
    # edge: `Select`, the only way to choose which surfaces the group refines,
    # and the `OK` / `Cancel` row. With no scrollbar there was no way to reach
    # any of them, so a surface refinement group could not be created at all.
    # AsNeeded costs nothing when the dialog does fit.
    holder.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    holder.setWidget(dialog)

    index = layout.indexOf(above) if above is not None else -1
    if index < 0:
        layout.addWidget(holder)
    else:
        layout.insertWidget(index, holder)

    def remove(_result):
        layout.removeWidget(holder)
        # The dialog belongs to the scroll area now; taking it back first
        # keeps `deleteLater` from running on a widget the holder owns.
        holder.takeWidget()
        holder.deleteLater()
        dialog.setParent(None)
        dialog.deleteLater()

    dialog.finished.connect(remove)
    holder.show()
    dialog.show()
    return True

