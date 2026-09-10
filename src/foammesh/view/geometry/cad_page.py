#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CAD assembly panel: shows the imported CAD tree (bodies -> faces with names &
colors) and exposes tessellation controls.

Built programmatically (no .ui) so it is self-contained. It binds to the
``cad_tree_rows`` view-model, so its data logic is tested headlessly even though
the widget itself needs a display. ``retessellateRequested`` carries the chosen
TessellationParams back to the import flow (re-mesh without re-import).
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QGroupBox, QTreeWidget, QTreeWidgetItem,
    QDoubleSpinBox, QCheckBox, QPushButton, QLabel,
)

from foammesh.core.geometry.cad import TessellationParams
from foammesh.view.view_models import cad_tree_rows


class CadPanel(QWidget):
    retessellateRequested = Signal(object)   # emits TessellationParams

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)

        self._summary = QLabel('No CAD imported.')
        layout.addWidget(self._summary)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(['Body / Face', 'Patch', 'Color'])
        layout.addWidget(self._tree)

        box = QGroupBox(self.tr('Tessellation'))
        form = QFormLayout(box)
        self._linear = QDoubleSpinBox()
        self._linear.setDecimals(4); self._linear.setRange(1e-4, 1e4)
        self._linear.setValue(0.1)
        self._angular = QDoubleSpinBox()
        self._angular.setRange(1.0, 179.0); self._angular.setValue(20.0)
        self._relative = QCheckBox()
        self._parallel = QCheckBox(); self._parallel.setChecked(True)
        form.addRow(self.tr('Linear deflection'), self._linear)
        form.addRow(self.tr('Angular deflection (deg)'), self._angular)
        form.addRow(self.tr('Relative'), self._relative)
        form.addRow(self.tr('Parallel'), self._parallel)
        layout.addWidget(box)

        self._entries = []
        self._retess = QPushButton(self.tr('Re-tessellate'))
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
                f"{'/'.join(formats) or 'CAD'} · {len(entries)} parts · "
                f"{faces} faces · unit {entries[-1].get('unit') or 'm'}")
        else:
            self._summary.setText('No CAD imported.')
        self._tree.clear()
        for entry in entries:
            item = QTreeWidgetItem(
                [str(entry.get('name') or ''), '', str(entry.get('unit') or 'm')])
            for patch in entry.get('patches') or ():
                source = patch.get('source_ref') or {}
                face = source.get('original_name') or (
                    f"face{source.get('face_index')}"
                    if source.get('face_index') is not None else '')
                item.addChild(QTreeWidgetItem(
                    [str(face), str(patch.get('name') or ''), '']))
            self._tree.addTopLevelItem(item)
            item.setExpanded(True)
        if entries:
            self.setParams(entries[-1].get('tessellation'))

    def geometryIds(self) -> list:
        """The CAD geometries the panel is showing, newest last."""
        return [str(entry.get('geometry_id')) for entry in getattr(self, '_entries', ())]

    def setModel(self, model) -> None:
        """Populate the tree from a CadModel (via the cad_tree_rows view-model)."""
        data = cad_tree_rows(model)
        self._summary.setText(
            f"{data['format'].upper()} · {data['n_bodies']} bodies · "
            f"{data['n_faces']} faces · unit {data['unit']}")
        self._tree.clear()
        for body in data['bodies']:
            bitem = QTreeWidgetItem([body['name'], '', body.get('color', '')])
            for face in body['faces']:
                bitem.addChild(QTreeWidgetItem(
                    [face['name'] or face['id'], face['patch'], face.get('color', '')]))
            self._tree.addTopLevelItem(bitem)
            bitem.setExpanded(True)

    def _emitRetessellate(self):
        self.retessellateRequested.emit(self.params())
