"""The small dialogs of the Gmsh File entries (Plan 48 GM11b).

Each returns what the person chose, or ``None`` when they cancelled; the
entries then run the same :class:`window_actions.WindowGmshActions` method the
local API does, so a dialog adds no behaviour of its own.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QLabel, QLineEdit,
)

from foammesh.view.theming.metrics import CompactDoubleSpinBox, unit_cell


def ask_watch(parent, pattern: str = '', interval: float = 1.0):
    """File > Watch pattern: ``(pattern, seconds)`` or ``None``."""
    dialog = QDialog(parent)
    dialog.setObjectName('gmshWatchDialog')
    dialog.setWindowTitle(dialog.tr('Watch pattern'))
    form = QFormLayout(dialog)
    note = QLabel(dialog.tr(
        'Merge the files that match a pattern when one appears or is '
        'written again.'), dialog)
    note.setWordWrap(True)
    form.addRow(note)
    edit = QLineEdit(pattern, dialog)
    edit.setObjectName('gmshWatchPattern')
    edit.setPlaceholderText(dialog.tr('results/*.pos'))
    form.addRow(dialog.tr('Pattern'), edit)
    seconds = CompactDoubleSpinBox(dialog)
    seconds.setObjectName('gmshWatchInterval')
    seconds.setRange(0.2, 3600.0)
    seconds.setDecimals(1)
    seconds.setValue(interval)
    form.addRow(dialog.tr('Check every'), unit_cell(seconds, 's'))
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok
        | QDialogButtonBox.StandardButton.Cancel, dialog)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    form.addRow(buttons)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    return edit.text().strip(), seconds.value()


def picture_filter() -> str:
    """The Save as picture file filter, one entry per format written here."""
    from foammesh.rendering import image_formats as images

    entries = [f'{row["label"]} (*{row["suffix"]})'
               for row in images.listing()
               if row['available'] and row['kind'] != 'movie']
    return ';;'.join(entries)


def ask_picture(parent):
    """File > Save as picture: ``(path, format id)`` or ``None``."""
    from foammesh.rendering import image_formats as images

    name, chosen = QFileDialog.getSaveFileName(
        parent, parent.tr('Save as picture'), 'view.png', picture_filter())
    if not name:
        return None
    for row in images.listing():
        if chosen.startswith(row['label'] + ' '):
            return name, row['id']
    suffix = name.rsplit('.', 1)[-1] if '.' in name else 'png'
    return name, images.format_for(suffix).id


def ask_movie(parent):
    """File > Record: the MP4 path or ``None``."""
    name, _chosen = QFileDialog.getSaveFileName(
        parent, parent.tr('Record a movie'), 'view.mp4',
        parent.tr('MP4 movie (*.mp4)'))
    return name or None
