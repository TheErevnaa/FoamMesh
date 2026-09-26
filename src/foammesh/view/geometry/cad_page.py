#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CAD assembly panel: shows the imported CAD tree (bodies -> faces with names &
colors) and exposes tessellation controls.

Built programmatically (no .ui) so it is self-contained.
``retessellateRequested`` carries the chosen TessellationParams back to the
import flow (re-mesh without re-import).

The panel is filled from the geometry artifact store, by ``setStoreEntries``,
because the store is what the case keeps. It once had a second filler,
``setModel``, written against a live ``CadModel`` held by an importer the
Geometry page discards the moment an import finishes. DP-293 recorded that it
was reached by no product code and left it standing; Plan 33 W-O2 took it out,
and with it the last caller of the ``cad_tree_rows`` view-model, which is
still exercised on its own by ``tests/unit/test_cad_pipeline.py``.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QGroupBox, QTreeWidget, QTreeWidgetItem,
    QCheckBox, QPushButton, QLabel,
)

from foammesh.core.geometry.cad import TessellationParams
from foammesh.core.geometry.store import stored_tessellation
from foammesh.core.mesh.presentation import count_text
from foammesh.view.theming.metrics import CompactDoubleSpinBox, unit_cell


class CadPanel(QWidget):
    retessellateRequested = Signal(object)   # emits TessellationParams

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self._summary = QLabel('No CAD imported.')
        self._summary.setObjectName('cadSummary')
        layout.addWidget(self._summary)

        self._tree = QTreeWidget()
        self._tree.setObjectName('cadTree')
        # DP-B2. The third column was headed `Colour` and the only live
        # writer of it -- ``setStoreEntries`` -- put the tessellation *unit*
        # in it for the part row and nothing at all in the face rows. The
        # colour a surface is drawn in lives on the Geometry list, which is
        # where the user is choosing between the surfaces.
        self._tree.setHeaderLabels([self.tr('Body / face'), self.tr('Patch')])
        layout.addWidget(self._tree)

        box = QGroupBox(self.tr('Tessellation'))
        form = QFormLayout(box)
        self._linear = CompactDoubleSpinBox()
        self._linear.setObjectName('cadLinearDeflection')
        # DP-520. In metres, as its unit cell says and as the import and the
        # repair route now both apply it. Four decimals of a metre could not
        # show the 0.1 mm a CAD part is faceted at by default.
        self._linear.setDecimals(6); self._linear.setRange(1e-6, 10.0)
        self._linear.setValue(TessellationParams().linear_deflection)
        self._angular = CompactDoubleSpinBox()
        self._angular.setObjectName('cadAngularDeflection')
        self._angular.setRange(1.0, 179.0); self._angular.setValue(20.0)
        self._relative = QCheckBox()
        self._relative.setObjectName('cadRelativeDeflection')
        self._parallel = QCheckBox(); self._parallel.setChecked(True)
        self._parallel.setObjectName('cadParallelTessellation')
        # DP-164. `(deg)` was one of seven spellings of one unit. The unit
        # goes where the registry rows put it: its own column after the box.
        form.addRow(self.tr('Linear deflection'), unit_cell(self._linear, 'm'))
        form.addRow(self.tr('Angular deflection'),
                    unit_cell(self._angular, 'deg'))
        form.addRow(self.tr('Relative'), self._relative)
        form.addRow(self.tr('Parallel'), self._parallel)
        layout.addWidget(box)

        self._entries = []
        self._retess = QPushButton(self.tr('Re-tessellate'))
        self._retess.setObjectName('cadRetessellate')
        self._retess.clicked.connect(self._emitRetessellate)
        layout.addWidget(self._retess)

    def params(self) -> TessellationParams:
        return TessellationParams(
            linear_deflection=self._linear.value(),
            angular_deflection_deg=self._angular.value(),
            relative=self._relative.isChecked(),
            parallel=self._parallel.isChecked(),
        )

    def setParams(self, values) -> None:
        """Show *values* (a mapping of TessellationParams fields) as the
        current deflection, so the controls open on what the part in the case
        was actually faceted at rather than on the widget's own defaults."""
        if not values:
            return
        values = dict(values)
        if 'linear_deflection' in values:
            self._linear.setValue(float(values['linear_deflection']))
        if 'angular_deflection_deg' in values:
            self._angular.setValue(float(values['angular_deflection_deg']))
        if 'relative' in values:
            self._relative.setChecked(bool(values['relative']))
        if 'parallel' in values:
            self._parallel.setChecked(bool(values['parallel']))

    def setStoreEntries(self, entries) -> None:
        """Fill the panel from the geometry artifact store's CAD entries.

        F-10. The panel was written against a ``CadModel`` held by an importer
        that the Geometry page discards the moment an import finishes, so
        there was nothing to mount it with and the deflection controls were
        unreachable: every STEP in the application was faceted at a fixed
        0.1 nobody could see or change. What the case keeps is the store's
        entries, and they carry the same facts this panel shows -- the parts,
        their faces, the unit they are stored in and the deflection each was
        faceted at.
        """
        entries = [entry for entry in entries if entry.get('cad_artifact')]
        self._entries = entries
        faces = sum(len(entry.get('patches') or ()) for entry in entries)
        if entries:
            formats = sorted({str(entry.get('format') or '').upper()
                              for entry in entries} - {''})
            self._summary.setText(
                f"{'/'.join(formats) or 'CAD'} · "
                f"{count_text(len(entries), 'part')} · "
                f"{count_text(faces, 'face')} · "
                f"unit {entries[-1].get('unit') or 'm'}")
        else:
            self._summary.setText('No CAD imported.')
        self._tree.clear()
        for entry in entries:
            item = QTreeWidgetItem([str(entry.get('name') or ''), ''])
            for patch in entry.get('patches') or ():
                source = patch.get('source_ref') or {}
                face = source.get('original_name') or (
                    f"face{source.get('face_index')}"
                    if source.get('face_index') is not None else '')
                item.addChild(QTreeWidgetItem(
                    [str(face), str(patch.get('name') or '')]))
            self._tree.addTopLevelItem(item)
            item.setExpanded(True)
        if entries:
            self.setParams(stored_tessellation(entries[-1]))

    def geometryIds(self) -> list:
        """The CAD geometries the panel is showing, newest last."""
        return [str(entry.get('geometry_id')) for entry in getattr(self, '_entries', ())]

    def _emitRetessellate(self):
        self.retessellateRequested.emit(self.params())
