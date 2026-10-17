#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""View > Copy view and View > Save view as... (Plan 39 VI15, I3).

``CaptureManager`` already writes a numbered PNG with a sidecar into the case.
That is a record, not what a user pastes into a report. These two are:

* *Copy view* puts the picture on the clipboard at 2x;
* *Save view as...* writes a PNG at 1x, 2x or 4x, optionally with a
  transparent background, and optionally with or without the legends and the
  overlay on top of the picture.

The legends are drawn by the viewport itself; the overlay (the card, the tool
rail, the progress strip) is Qt, so it is painted over the image from the live
widgets: a capture shows what the screen shows.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLineEdit, QPushButton, QWidget)

COPY_SCALE = 2
SCALES = (1, 2, 4)
#: Children of the view that are the picture itself, not overlay.
_NOT_OVERLAY = ('_widget', '_placeholder', '_holdCover', '_strip', '_presetBar',
                '_hoverTip')


def overlayWidgets(view: QWidget) -> list:
    """The visible Qt widgets drawn over the picture, back to front."""
    skip = {id(getattr(view, name, None)) for name in _NOT_OVERLAY}
    return [child for child in view.children()
            if isinstance(child, QWidget) and id(child) not in skip
            and child.isVisible() and not child.isWindow()]


def composeView(view, *, scale: int = COPY_SCALE, transparent: bool = False,
                legends: bool = True, overlay: bool = True,
                includes=None) -> QImage | None:
    """The picture, with the Qt overlay painted over it when asked.

    *includes* (Plan 43 LY15) is the snapshot's Include boxes: ``legend``,
    ``worst``, ``caption``."""
    image = view.grabImage(scale, transparent=transparent, legends=legends,
                           includes=includes)
    if image is None or not overlay:
        return image
    factor = image.width() / max(view.width(), 1)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for widget in overlayWidgets(view):
            # Plan 43 LY11. The legend is a Qt widget now, so "without the
            # legends" has to leave it out of the overlay as well.
            if not legends and widget.objectName() == 'viewportLegend':
                continue
            painter.save()
            painter.translate(widget.x() * factor, widget.y() * factor)
            painter.scale(factor, factor)
            widget.render(painter, QPoint())
            painter.restore()
    finally:
        painter.end()
    return image


def copyView(view, *, scale: int = COPY_SCALE, transparent: bool = False,
             overlay: bool = True, includes=None) -> QImage | None:
    """Put the picture on the clipboard. Returns the image, None if it could not draw."""
    image = composeView(view, scale=scale, transparent=transparent,
                        overlay=overlay, includes=includes)
    if image is not None:
        QGuiApplication.clipboard().setImage(image)
    return image


def saveView(view, path, *, scale: int = COPY_SCALE, transparent: bool = False,
             legends: bool = True, overlay: bool = True) -> bool:
    """Write the picture as a PNG at *path*."""
    image = composeView(view, scale=scale, transparent=transparent,
                        legends=legends, overlay=overlay)
    if image is None:
        return False
    target = Path(path)
    if target.suffix.lower() != '.png':
        target = target.with_suffix('.png')
    return bool(image.save(str(target), 'PNG'))


class SaveViewDialog(QDialog):
    """The options of *Save view as...* and where to put the file."""

    def __init__(self, parent=None, *, directory: str = ''):
        super().__init__(parent)
        self.setObjectName('saveViewDialog')
        self.setWindowTitle(self.tr('Save view as'))
        form = QFormLayout(self)
        row = QHBoxLayout()
        self._path = QLineEdit(str(Path(directory) / 'view.png') if directory
                               else 'view.png')
        self._path.setObjectName('saveViewPath')
        browse = QPushButton(self.tr('Browse…'))
        browse.clicked.connect(self._browse)
        row.addWidget(self._path, 1)
        row.addWidget(browse)
        form.addRow(self.tr('File'), row)
        self._scale = QComboBox()
        self._scale.setObjectName('saveViewScale')
        for value in SCALES:
            self._scale.addItem('{0}×'.format(value), value)
        self._scale.setCurrentIndex(SCALES.index(COPY_SCALE))
        form.addRow(self.tr('Size'), self._scale)
        self._transparent = QCheckBox(self.tr('Transparent background'))
        self._transparent.setObjectName('saveViewTransparent')
        self._legends = QCheckBox(self.tr('Include the legend'))
        self._legends.setObjectName('saveViewLegend')
        self._legends.setChecked(True)
        self._overlay = QCheckBox(self.tr('Include the overlay'))
        self._overlay.setObjectName('saveViewOverlay')
        self._overlay.setChecked(True)
        for box in (self._transparent, self._legends, self._overlay):
            form.addRow(box)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self):
        path, _filter = QFileDialog.getSaveFileName(
            self, self.tr('Save view as'), self._path.text(),
            self.tr('PNG image (*.png)'))
        if path:
            self._path.setText(path)

    def setPath(self, path: str) -> None:
        self._path.setText(str(path))

    def options(self) -> dict:
        return {'path': self._path.text().strip(),
                'scale': int(self._scale.currentData()),
                'transparent': self._transparent.isChecked(),
                'legends': self._legends.isChecked(),
                'overlay': self._overlay.isChecked()}


def saveViewAs(view, parent=None, *, directory: str = '') -> str | None:
    """Ask, then write. Returns the path written, or None.

    Plan 43 LY15. *Save view as…* is the snapshot popover now, with the
    options of this dialog and the other five formats; the dialog below is
    what answers when the window has no popover to show.
    """
    window = view.window() if hasattr(view, 'window') else None
    show = getattr(window, 'showSnapshot', None)
    if callable(show):
        show()
        return None
    dialog = SaveViewDialog(parent, directory=directory)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    options = dialog.options()
    path = options.pop('path')
    if not path:
        return None
    return path if saveView(view, path, **options) else None
