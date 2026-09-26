#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""The capture gallery: find the picture again, and put its view back.

A picture with no record of what it shows is a liability in a design review --
nobody can tell whether it is of the mesh currently in the case. Each thumbnail
here carries its sidecar, so *Restore view* works and a capture of a superseded
mesh is badged rather than passed off as current.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget)

from foammesh.core.capture import CaptureRecord
from foammesh.view.theming.metrics import (
    FORM_MARGIN, GAP_TIGHT, apply_prose_measure)


THUMBNAIL = QSize(240, 150)
COLUMNS = 3


class CaptureCard(QFrame):
    restoreRequested = Signal(object)
    deleteRequested = Signal(object)
    revealRequested = Signal(object)

    def __init__(self, record: CaptureRecord, path: Path, stale: bool,
                 parent=None):
        super().__init__(parent)
        self._record = record
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)
        layout.setSpacing(GAP_TIGHT)

        thumbnail = QLabel()
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            thumbnail.setText(self.tr('Image missing'))
        else:
            thumbnail.setPixmap(pixmap.scaled(
                THUMBNAIL, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(thumbnail)

        caption = QLabel(record.label or record.created[:19].replace('T', ' '))
        caption.setToolTip(str(path))
        layout.addWidget(caption)

        if stale:
            badge = QLabel(self.tr('Mesh has changed since this picture'))
            badge.setObjectName('captureStale')
            badge.setProperty('severity', 'warning')
            badge.setWordWrap(True)
            layout.addWidget(badge)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        restore = QPushButton(self.tr('Restore view'))
        restore.setEnabled(bool(record.camera))
        restore.setToolTip(
            self.tr('Put the camera back where this picture was taken')
            if record.camera
            else self.tr('This picture has no saved view to restore'))
        reveal = QPushButton(self.tr('Reveal'))
        remove = QPushButton(self.tr('Delete'))
        buttons.addWidget(restore)
        buttons.addWidget(reveal)
        buttons.addWidget(remove)
        layout.addLayout(buttons)

        restore.clicked.connect(lambda: self.restoreRequested.emit(record))
        reveal.clicked.connect(lambda: self.revealRequested.emit(path))
        remove.clicked.connect(lambda: self.deleteRequested.emit(record))

    def record(self):
        return self._record


class CapturesPage(QWidget):
    restoreRequested = Signal(object)
    deleteRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('capturesPage')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._empty = QLabel(self.tr(
            'No captures yet. Use Capture on the viewport toolbar to save a '
            'picture of the mesh into this case.'))
        self._empty.setWordWrap(True)
        # DP-220. Running text is ranged left and held to a measure,
        # so its lines all start at one edge instead of at three.
        self._empty.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        apply_prose_measure(self._empty)
        layout.addWidget(self._empty)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._grid_host)
        layout.addWidget(self._scroll, 1)
        self._scroll.setVisible(False)

    def setCaptures(self, records, directory: Path, fingerprint: str = ''):
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._empty.setVisible(not records)
        self._scroll.setVisible(bool(records))

        for position, record in enumerate(records):
            card = CaptureCard(
                record, Path(directory) / record.image,
                record.is_stale(fingerprint), self._grid_host)
            card.restoreRequested.connect(self.restoreRequested)
            card.deleteRequested.connect(self.deleteRequested)
            card.revealRequested.connect(self._reveal)
            self._grid.addWidget(card, position // COLUMNS, position % COLUMNS)

    def cardCount(self) -> int:
        return self._grid.count()

    def _reveal(self, path: Path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
