"""Remesh surface (CVT): the panel (Plan 48 GM16b, section 4.11.1).

Cleans the picked surfaces of the session mesh into an even, well-shaped
triangle surface with Plugin(CVTRemesh). It is the form of the plugin in the
Plugins panel and sits on the Geometry row under Import and healing. Every
control is a plain number field with Gmsh's own default:

* **Points on the surface** (Sites) and **Target edge length** are two views of
  one number: sites are sample points, ``area / (sqrt(3)/2 h^2)``, and editing
  one updates the other once the surfaces are known.
* **Normal computation radius** is a fraction of Revoropt's scale radius, not a
  length; the length it equals is shown beside it, and either can be typed.
* **Surfaces** are typed or picked in the viewport. The run is isolated, so
  hiding is not what restricts it; only the picked surfaces are remeshed.
* **Replace originals** swaps the picked surfaces for the result face by face
  and is refused, with the failed check named, unless the replacement contract
  holds. **Keep both** then keeps the result beside the originals.

The panel is built by the registry with a parent only; the window gives it the
session with :meth:`set_context`.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core.facade.plan48 import pa3_routes
from foammesh.core.gmsh import cvt_remesh
from foammesh.core.quantities import count_text
from foammesh.view.theming.metrics import CompactDoubleSpinBox, CompactSpinBox
from foammesh.view.workflow_controls.length_box import ModelLengthBox

COLUMNS = ('', 'Before', 'After')


class _Bridge(QObject):
    done = Signal(object)
    failed = Signal(str)


class CvtRemeshPanel(QWidget):
    """The controls of Remesh surface (CVT) and its before/after summary."""

    #: A remesh (or its undo) changed the model.
    meshEdited = Signal()
    #: A run finished; the report.
    ran = Signal(dict)

    def __init__(self, controller=lambda: None, viewport=lambda: None,
                 parent=None):
        super().__init__(parent)
        self._controller = controller
        self._viewport = viewport
        self._picker = None
        self._plugin_view = None
        self._area = None
        self._scale = None
        self._last = None
        self._busy = False
        self._updating = False
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._finished)
        self._bridge.failed.connect(self._failed)
        self.setObjectName('gmshCvtPanel')
        w = cvt_remesh.worker()
        defaults = w.CVT_DEFAULTS
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        intro = QLabel(self.tr(
            'Remesh the picked surfaces into even, well-shaped triangles. '
            'Only the picked surfaces are remeshed.'), self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(form)

        row = QHBoxLayout()
        self._faces = QLineEdit(self)
        self._faces.setObjectName('gmshCvtSurfaces')
        self._faces.setAccessibleName(self.tr('Surfaces'))
        self._faces.setPlaceholderText(self.tr('surface tags, e.g. 1, 4'))
        self._faces.editingFinished.connect(self.refreshMeasure)
        row.addWidget(self._faces, 1)
        self._pick = QPushButton(self.tr('Pick'), self)
        self._pick.setObjectName('gmshCvtPick')
        self._pick.setAccessibleName(self.tr('Pick surfaces in the viewport'))
        self._pick.clicked.connect(lambda _c=False: self.startPick())
        row.addWidget(self._pick)
        form.addRow(self.tr('Surfaces'), row)

        self._sites = CompactSpinBox(self)
        self._sites.setObjectName('gmshCvtSites')
        self._sites.setRange(4, 2_000_000_000)
        self._sites.setValue(int(defaults['Sites']))
        self._sites.setToolTip(cvt_remesh.options()[0].help)
        self._sites.valueChanged.connect(self._sitesEdited)
        form.addRow(self.tr('Points on the surface'), self._sites)

        self._edge = ModelLengthBox(self)
        self._edge.setObjectName('gmshCvtEdge')
        self._edge.setEnabled(False)
        self._edge.setToolTip(self.tr(
            'Mean edge length of the new triangles, in model units. Needs '
            'the surfaces: points = area / (sqrt(3)/2 * h^2).'))
        self._edge.editingFinished.connect(self._edgeEdited)
        form.addRow(self.tr('Target edge length'), self._edge)

        self._iterations = CompactSpinBox(self)
        self._iterations.setObjectName('gmshCvtIterations')
        self._iterations.setRange(0, 100000)
        self._iterations.setValue(int(defaults['Iterations']))
        form.addRow(self.tr('Iterations'), self._iterations)

        self._aniso = self._number('gmshCvtAnisotropy', defaults['Anisotropy'])
        form.addRow(self.tr('Anisotropy'), self._aniso)
        self._density = self._number('gmshCvtDensity',
                                     defaults['Variable density'])
        form.addRow(self.tr('Variable density'), self._density)
        self._feature = self._number('gmshCvtFeature',
                                     defaults['Feature sensitivity'])
        form.addRow(self.tr('Feature sensitivity'), self._feature)

        self._radius = self._number('gmshCvtNormalRadius',
                                    defaults['Normal computation radius'])
        self._radius.editingFinished.connect(self._radiusEdited)
        form.addRow(self.tr('Normal computation radius'), self._radius)
        self._radius_length = ModelLengthBox(self)
        self._radius_length.setObjectName('gmshCvtNormalRadiusLength')
        self._radius_length.setEnabled(False)
        self._radius_length.setToolTip(self.tr(
            'The same radius as a length in model units: the radius times '
            'the scale radius Revoropt divides the mesh by'))
        self._radius_length.editingFinished.connect(self._radiusLengthEdited)
        form.addRow(self.tr('  = length'), self._radius_length)

        self._tolerance = ModelLengthBox(self)
        self._tolerance.setObjectName('gmshCvtTolerance')
        self._tolerance.setToolTip(self.tr(
            'Largest distance the new surface may move from the old one. 0 '
            'means half a mean edge length.'))
        form.addRow(self.tr('Deviation limit'), self._tolerance)

        self._replace = QCheckBox(self.tr('Replace originals'), self)
        self._replace.setObjectName('gmshCvtReplace')
        self._replace.setToolTip(self.tr(
            'Swap the picked surfaces for the result, one new face per '
            'original face. Refused, with the failed check named, unless '
            'interfaces still conform and volumes stay closed.'))
        layout.addWidget(self._replace)

        buttons = QHBoxLayout()
        self._run = QPushButton(self.tr('Remesh'), self)
        self._run.setObjectName('gmshCvtRun')
        self._run.clicked.connect(lambda _c=False: self.runAsync())
        buttons.addWidget(self._run)
        self._keep = QPushButton(self.tr('Keep both'), self)
        self._keep.setObjectName('gmshCvtKeep')
        self._keep.setToolTip(self.tr(
            'Keep the last result beside the originals'))
        self._keep.setEnabled(False)
        self._keep.clicked.connect(lambda _c=False: self.keepBoth())
        buttons.addWidget(self._keep)
        self._undo = QPushButton(self.tr('Undo'), self)
        self._undo.setObjectName('gmshCvtUndo')
        self._undo.setToolTip(self.tr(
            'Go back to the model before the last remesh'))
        self._undo.setEnabled(False)
        self._undo.clicked.connect(lambda _c=False: self.undo())
        buttons.addWidget(self._undo)
        self._defaults = QPushButton(self.tr('Gmsh defaults'), self)
        self._defaults.setObjectName('gmshCvtDefaults')
        self._defaults.clicked.connect(lambda _c=False: self.resetDefaults())
        buttons.addWidget(self._defaults)
        self._export = QPushButton(self.tr('Export result…'), self)
        self._export.setObjectName('gmshCvtExport')
        self._export.setEnabled(False)
        self._export.clicked.connect(lambda _c=False: self.exportResult())
        buttons.addWidget(self._export)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._note = QLabel(self)
        self._note.setObjectName('gmshCvtNote')
        self._note.setWordWrap(True)
        self._note.setVisible(False)
        layout.addWidget(self._note)

        self._table = QTableWidget(0, len(COLUMNS), self)
        self._table.setObjectName('gmshCvtSummary')
        self._table.setAccessibleName(self.tr('Before and after'))
        self._table.setHorizontalHeaderLabels([self.tr(c) for c in COLUMNS])
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(150)
        self._table.setVisible(False)
        layout.addWidget(self._table)

        self._preview = QCheckBox(self.tr('Show before and after'), self)
        self._preview.setObjectName('gmshCvtPreview')
        self._preview.setEnabled(False)
        self._preview.toggled.connect(self._showPreview)
        layout.addWidget(self._preview)
        # Plan 48 PA3: the local API drives this panel as a person does.
        self._api = CvtApi(self)
        session = pa3_routes.announce_from_app('cvt', self._api)
        if session is not None:
            self.destroyed.connect(
                lambda *_a, s=session, a=self._api: pa3_routes.unregister_panel(
                    s, 'cvt', a))

    # -- small helpers ------------------------------------------------------ #

    def _number(self, name, value):
        box = CompactDoubleSpinBox(self)
        box.setObjectName(name)
        box.setDecimals(9)
        box.setRange(0.0, 1e12)
        box.setValue(float(value))
        return box

    def set_context(self, controller=None, viewport=None, picker=None) -> None:
        """Give the panel the window's session, viewport and Pick tool."""
        if controller is not None:
            self._controller = controller
        if viewport is not None:
            self._viewport = viewport
        if picker is not None:
            self.set_picker(picker)

    def set_picker(self, picker) -> None:
        if self._picker is not None:
            try:
                self._picker.pickAccepted.disconnect(self._onPicked)
            except (RuntimeError, TypeError):
                pass
        self._picker = picker
        picker.pickAccepted.connect(self._onPicked)

    def _say(self, text: str) -> None:
        self._note.setText(text)
        self._note.setVisible(bool(text))

    def note(self) -> str:
        return self._note.text()

    # -- the surfaces -------------------------------------------------------- #

    def faces(self) -> list:
        out = []
        for word in self._faces.text().replace(';', ',').replace(
                ' ', ',').split(','):
            word = word.strip()
            if not word:
                continue
            word = word.split(':')[-1]
            try:
                out.append(int(word))
            except ValueError:
                continue
        return sorted(set(out))

    def setFaces(self, faces) -> None:
        self._faces.setText(', '.join(str(int(f)) for f in faces))
        self.refreshMeasure()

    def startPick(self) -> bool:
        if self._picker is None:
            from .shared_pick import SharedPick
            self.set_picker(SharedPick('gmsh.cvt', parent=self))
        if not self._picker.pickFor('Surfaces'):
            self._say(self._picker.status()
                      or self.tr('Nothing can be picked yet.'))
            return False
        self._say(self.tr('Pick surfaces in the viewport; Enter accepts.'))
        return True

    def _onPicked(self, chosen) -> None:
        surfaces = [int(e[1]) for e in chosen if int(e[0]) == 2]
        if not surfaces:
            self._say(self.tr('No surface was picked.'))
            return
        self.setFaces(surfaces)
        self._say(count_text(len(surfaces), 'surface') + self.tr(' picked.'))

    def refreshMeasure(self) -> dict:
        """Measure the surfaces so the paired fields can follow each other."""
        faces = self.faces()
        controller = self._controller()
        self._area = self._scale = None
        self._edge.setEnabled(False)
        self._radius_length.setEnabled(False)
        if not faces or controller is None:
            return {}
        try:
            found = cvt_remesh.measure(controller, faces)
        except cvt_remesh.CvtRefusal as error:
            self._say(str(error))
            return {}
        self._area, self._scale = found['area'], found['scale']
        self._edge.setEnabled(True)
        self._radius_length.setEnabled(True)
        self._sitesEdited()
        self._radiusEdited()
        self._say(self.tr('%d triangles, area %.6g, mean edge %.4g.') % (
            found['triangles'], found['area'], found['mean_edge'] or 0.0))
        return found

    # -- the paired fields --------------------------------------------------- #

    def _sitesEdited(self, *_args) -> None:
        if self._updating or not self._area:
            return
        self._updating = True
        try:
            self._edge.setModelValue(cvt_remesh.edge_length_for_sites(
                self._area, self._sites.value()))
        finally:
            self._updating = False

    def _edgeEdited(self) -> None:
        if self._updating or not self._area or not self._edge.modelValue() > 0:
            return
        self._updating = True
        try:
            self._sites.setValue(min(2_000_000_000,
                                     cvt_remesh.sites_for_edge_length(
                                         self._area, self._edge.modelValue())))
        finally:
            self._updating = False

    def _radiusEdited(self) -> None:
        if self._updating or not self._scale:
            return
        self._updating = True
        try:
            self._radius_length.setModelValue(cvt_remesh.radius_to_length(
                self._radius.value(), self._scale))
        finally:
            self._updating = False

    def _radiusLengthEdited(self) -> None:
        if self._updating or not self._scale or not self._radius_length.modelValue():
            return
        self._updating = True
        try:
            self._radius.setValue(cvt_remesh.length_to_radius(
                self._radius_length.modelValue(), self._scale))
        finally:
            self._updating = False

    def setTargetEdgeLength(self, edge: float) -> None:
        self._edge.setModelValue(float(edge))
        self._edgeEdited()

    def setNormalRadiusLength(self, length: float) -> None:
        self._radius_length.setModelValue(float(length))
        self._radiusLengthEdited()

    # -- values -------------------------------------------------------------- #

    def values(self) -> dict:
        return {
            'Sites': float(self._sites.value()),
            'Iterations': float(self._iterations.value()),
            'Anisotropy': float(self._aniso.value()),
            'Variable density': float(self._density.value()),
            'Feature sensitivity': float(self._feature.value()),
            'Normal computation radius': float(self._radius.value()),
        }

    def setValue(self, name: str, value) -> None:
        box = {'Sites': self._sites, 'Iterations': self._iterations,
               'Anisotropy': self._aniso, 'Variable density': self._density,
               'Feature sensitivity': self._feature,
               'Normal computation radius': self._radius}[name]
        box.setValue(int(value) if box in (self._sites, self._iterations)
                     else float(value))
        if name == 'Sites':
            self._sitesEdited()
        elif name == 'Normal computation radius':
            self._radiusEdited()

    def resetDefaults(self) -> None:
        for name, value in cvt_remesh.worker().CVT_DEFAULTS.items():
            self.setValue(name, value)
        self._tolerance.setModelValue(0.0)
        self._replace.setChecked(False)

    def replaceOriginals(self) -> bool:
        return self._replace.isChecked()

    def setReplaceOriginals(self, on: bool) -> None:
        self._replace.setChecked(bool(on))

    # -- running ------------------------------------------------------------- #

    def busy(self) -> bool:
        return self._busy

    def run(self) -> dict:
        """Remesh the picked surfaces; returns the report (``{}`` on refusal)."""
        controller = self._controller()
        if controller is None:
            self._say(self.tr('There is no mesh yet; generate one first.'))
            return {}
        faces = self.faces()
        if not faces:
            self._say(self.tr('Pick at least one surface.'))
            return {}
        edge = None
        values = self.values()
        try:
            report = cvt_remesh.run(
                controller, faces, values,
                target_edge_length=edge, replace=self.replaceOriginals(),
                tolerance=self._tolerance.modelValue() or None)
        except cvt_remesh.CvtRefusal as error:
            self._say(str(error))
            return {}
        self._adopt(report)
        return report

    def runAsync(self) -> bool:
        """The button: the same run, off the window's thread."""
        if self._busy:
            return False
        self._busy = True
        self._run.setEnabled(False)
        self._say(self.tr('Remeshing…'))

        def work():
            try:
                self._bridge.done.emit(self.run())
            except Exception as error:                   # noqa: BLE001
                self._bridge.failed.emit(str(error))
        threading.Thread(target=work, name='gmsh-cvt', daemon=True).start()
        return True

    def _finished(self, _report) -> None:
        self._busy = False
        self._run.setEnabled(True)

    def _failed(self, message: str) -> None:
        self._busy = False
        self._run.setEnabled(True)
        self._say(message)

    def _adopt(self, report: dict) -> None:
        self._last = report
        applied = report.get('applied') or {}
        replace = report.get('replace') or {}
        refused = bool(replace.get('requested') and not replace.get('ok'))
        self._keep.setEnabled(refused and not applied)
        self._export.setEnabled(True)
        self._undo.setEnabled(bool(applied))
        self._fillSummary(report.get('summary') or {})
        self._preview.setEnabled(True)
        text = (report.get('summary') or {}).get('text', '')
        if refused:
            text = (replace.get('refusal', '') + ' ' + text).strip()
            if applied.get('note'):
                text = applied['note'] + ' ' + (report.get('summary') or {}
                                                ).get('text', '')
        self._say(text)
        if self._preview.isChecked():
            self._showPreview(True)
        self.ran.emit(report)
        if applied:
            self.meshEdited.emit()

    def _fillSummary(self, summary: dict) -> None:
        rows = summary.get('rows') or []
        self._table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate((row['name'], row['before'],
                                       row['after'])):
                text = ('-' if value is None else
                        (f'{value:,}' if isinstance(value, int)
                         else f'{value:.4g}') if j else str(value))
                self._table.setItem(i, j, QTableWidgetItem(text))
        self._table.setVisible(bool(rows))

    def summaryRows(self) -> list:
        return [[self._table.item(i, j).text() for j in range(3)]
                for i in range(self._table.rowCount())]

    def lastReport(self) -> dict:
        return dict(self._last or {})

    # -- after the run ------------------------------------------------------- #

    def keepBoth(self) -> dict:
        controller = self._controller()
        if controller is None or not self._last:
            return {}
        try:
            done = cvt_remesh.apply_run(controller, self._last['run_id'],
                                        'keep')
        except cvt_remesh.CvtRefusal as error:
            self._say(str(error))
            return {}
        self.adoptKept(done)
        return done

    def adoptKept(self, done: dict) -> None:
        """The panel after the result was kept beside the originals."""
        self._keep.setEnabled(False)
        self._undo.setEnabled(True)
        self._say(self.tr('The result is kept beside the originals as '
                          'surface %d.') % done['entity'][1])
        self.meshEdited.emit()

    def undo(self) -> dict:
        controller = self._controller()
        if controller is None:
            return {}
        try:
            done = cvt_remesh.undo(controller)
        except cvt_remesh.CvtRefusal as error:
            self._say(str(error))
            return {}
        self.adoptUndone(done, cvt_remesh.status(controller)['can_undo'])
        return done

    def adoptUndone(self, done: dict, can_undo: bool) -> None:
        """The panel after the last remesh was undone."""
        self._undo.setEnabled(bool(can_undo))
        self._say(self.tr('Back to the model before %s.') % done['undone']
                  if done['verified'] else
                  self.tr('Restored, but the model differs: %s') % '; '.join(
                      done['differences']))
        self.meshEdited.emit()

    def exportResult(self, path=None) -> dict:
        controller = self._controller()
        if controller is None or not self._last:
            return {}
        if path is None:
            path, _filter = QFileDialog.getSaveFileName(
                self, self.tr('Export remeshed surface'), '', 'STL (*.stl)')
        if not path:
            return {}
        try:
            return cvt_remesh.export_surface(controller,
                                             self._last['run_id'], path)
        except Exception as error:                       # noqa: BLE001
            self._say(str(error))
            return {}

    # -- the viewport -------------------------------------------------------- #

    def _view(self):
        if self._plugin_view is None:
            widget = self._viewport()
            if widget is None:
                return None
            from foammesh.rendering.gmsh_plugin_view import GmshPluginView
            self._plugin_view = GmshPluginView(widget)
        return self._plugin_view

    def _showPreview(self, on: bool) -> dict:
        view = self._view()
        controller = self._controller()
        if view is None or controller is None or not self._last:
            return {}
        if not on:
            for key in ('cvt:before', 'cvt:after'):
                view.hide(key)
            return {}
        try:
            shown = cvt_remesh.preview(controller, self._last['run_id'])
        except cvt_remesh.CvtRefusal as error:
            self._say(str(error))
            return {}
        out = {}
        for key, row in shown['rows'].items():
            path = row.get('host_path') or row['path']
            if Path(path).is_file():
                out[key] = view.show('cvt:' + key, path, row['name'])
        # the old surface under the new one: draw the new one last
        return out


class CvtApi:
    """What the local API does on the open Remesh surface (CVT) panel (Plan
    48 PA3); registered on the case session, called by ``pa3_routes`` and by
    the ``gmsh.cvt.*`` routes so the panel shows a run made through the API as
    it shows one made with its own buttons."""

    def __init__(self, panel: CvtRemeshPanel):
        self._panel = panel

    @staticmethod
    def _gui(function, *args):
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread
        return on_gui_thread(function, *args)

    def adopt(self, report: dict) -> dict:
        """A run finished: the summary, the buttons and the note."""
        def run():
            self._panel._adopt(report)                    # noqa: SLF001
            return {'note': self._panel.note(),
                    'summary': self._panel.summaryRows()}
        return self._gui(run)

    def kept(self, done: dict) -> None:
        self._gui(self._panel.adoptKept, done)

    def undone(self, done: dict, can_undo: bool) -> None:
        self._gui(self._panel.adoptUndone, done, can_undo)

    def show(self, on: bool, run_id: str = '') -> dict:
        """Show or hide the before and after surfaces of a run."""
        def run():
            panel = self._panel
            if run_id and (panel._last or {}).get('run_id') != run_id:  # noqa: SLF001
                panel._last = {'run_id': run_id}          # noqa: SLF001
            if not panel._last:                           # noqa: SLF001
                raise ValueError('run a remesh first')
            panel._preview.setEnabled(True)               # noqa: SLF001
            blocked = panel._preview.blockSignals(True)   # noqa: SLF001
            try:
                panel._preview.setChecked(bool(on))       # noqa: SLF001
            finally:
                panel._preview.blockSignals(blocked)      # noqa: SLF001
            layers = panel._showPreview(bool(on))         # noqa: SLF001
            if on and not layers:
                raise ValueError(panel.note() or 'there is no viewport to '
                                 'show the surfaces in')
            return {'shown': bool(on), 'run_id': panel._last['run_id'],  # noqa: SLF001
                    'layers': sorted(layers)}
        return self._gui(run)
