"""Write the Gmsh session mesh in any format Gmsh writes (Plan 41 GN15).

The panel sits on the Export page of the Gmsh route.  It lists every writer of
:mod:`foammesh.core.gmsh.export_writers`, draws that writer's options from the
option catalogue, says before anything is written why a writer cannot take the
mesh in hand, and writes through :class:`MeshExporter`.  It reads nothing from
the target-solver choice.

The write runs on a worker thread: the session call blocks for as long as
Gmsh takes, and the window must not freeze meanwhile.

Plan 48 GM4 (output honesty).  The version list is the five versions Gmsh
4.15.2 writes; a format the measured target matrix (section 4.9.1) marks
CONVERTED stays selectable and offers its conversion as a choice, a REFUSED
one is greyed with the measured reason, and what a write loses is said before
it is written.  The unit pins (D2) are shown with who set them and can be
unlocked.
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QVBoxLayout, QWidget)

from foammesh.core.gmsh import export_writers as writers
from foammesh.core.gmsh import option_catalogue as catalogue
from foammesh.core.gmsh.option_catalogue import (
    OVERRIDES_FIELD, overrides_from_text, overrides_to_text)
from foammesh.core.gmsh.export_service import ExportRefused, MeshExporter
from foammesh.view.combo_items import item_enabled, set_item_enabled
from foammesh.view.theming.metrics import apply_form_metrics
from widgets.fit_to_text import FlowLayout

#: Version choices the .msh writer takes (Mesh.MshFileVersion is a float).
MSH_VERSIONS = tuple((name, number) for number, name in writers.MSH_VERSIONS)

#: Plan 42 SG15 (D10). Formats the shared export form above already writes
#: from the accepted run; while the source is the accepted run the panel
#: lists the others only, so every format is written once.
#: Plan 48 GM4: ``msh`` is no longer one of them. The shared form writes the
#: accepted run's mesh as MSH 2.2 for the solvers; the user's own ``.msh`` (any
#: version, binary or not) is this panel's.
FORM_FORMATS = frozenset({'openfoam', 'su2', 'vtk', 'cgns', 'med', 'unv'})
SOURCE_RUN, SOURCE_MODEL = 'run', 'model'

NO_MESH = 'Generate a mesh on the Generate page first; there is nothing to write yet.'
#: DP-1448. The tooltip of a format while no mesh is open in the session.
NO_SESSION_MESH = ('No mesh is open in the Gmsh session, so no format can be '
                   'written from this section yet.')
#: DP-1448. Said in place of the format list when the accepted run is the
#: source and the session holds no mesh (a mesh made by the guided run is
#: written by the form above; these formats read the open session).
LOADING_RUN_MESH = "Loading the accepted run's mesh into the Gmsh session..."
RUN_NOT_IN_SESSION = ('These formats are written from the mesh open in the '
                      'Gmsh session, and none is open. The form above writes '
                      'the accepted run.')


class GmshWriterPanel(QWidget):
    """Pick a writer, set its options, name a file, write."""

    #: A write finished: the :meth:`ExportResult.to_dict` of it.
    written = Signal(dict)
    #: A write was refused or failed; the sentence.
    failed = Signal(str)
    #: The option rows were rebuilt for another writer (DP-154: the page
    #: puts their labels back in its one column).
    formsChanged = Signal()
    _done = Signal(object)
    _runMeshLoaded = Signal(object)

    def __init__(self, controller_getter, parent=None, *, default_folder=None,
                 profile=None, run_mesh=None, confirm=None, client=None):
        super().__init__(parent)
        #: Plan 48 GM4. The facade client the unit pins are read from and
        #: written through; without one the pins are shown and locked. The
        #: Export page's own client is found through ``run_mesh`` when none
        #: is given.
        self._client = client or _client_of(run_mesh)
        self._convert: set = set()
        #: SG15A. ``run_mesh`` has ``case_path()`` and ``controller()`` (the
        #: session controller, made when none exists); without it the
        #: Accepted-run source loads nothing. ``confirm(sentence) -> bool``
        #: asks before a model the session holds is replaced.
        self._runMesh = run_mesh
        self._confirm = confirm or self._askReplace
        self._loading = False
        self._loadProblem = ''
        self._declined = None
        self._failed = None
        self._loadHandle = None
        self._controller_getter = controller_getter
        self._default_folder = default_folder
        self._profile = profile
        self._facts = writers.MeshFacts()
        self._values: dict = {}
        self._controls: dict = {}
        self._busy = False
        self.last_result: dict = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        # DP-154: the house form metrics, as the export form above has, so
        # the two stand in one column with one spacing.
        apply_form_metrics(form)
        self._form = form
        layout.addLayout(form)
        self._format = QComboBox(self)
        self._format.setObjectName('gmshWriterFormat')
        # DP-573: the longest format name no longer sets the panel's width;
        # the combo narrows to the settings column and its list stays wide.
        self._format.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._format.setMinimumContentsLength(12)
        self._format.setAccessibleName(self.tr('Gmsh format'))
        self._format.setAccessibleDescription(self.tr(
            'The file format the session mesh is written in.'))
        self._runAvailable = False
        self._source = QComboBox(self)
        self._source.setObjectName('gmshWriterSource')
        self._source.setAccessibleName(self.tr('Export source'))
        self._source.setAccessibleDescription(self.tr(
            'Where the mesh comes from: the accepted run, written by the '
            'form above for its formats, or the model open in the Gmsh '
            'session, written by this section in every format.'))
        self._source.addItem(self.tr('Accepted run'), SOURCE_RUN)
        self._source.addItem(self.tr('Open model'), SOURCE_MODEL)
        self._source.setCurrentIndex(1)
        sourceLabel = QLabel(self.tr('Source'), self)
        sourceLabel.setBuddy(self._source)
        form.addRow(sourceLabel, self._source)
        self._fillFormats()
        label = QLabel(self.tr('Format'), self)
        label.setBuddy(self._format)
        form.addRow(label, self._format)

        self._conversionHost = QWidget(self)
        self._conversionHost.setObjectName('gmshWriterConversions')
        self._conversionLayout = QVBoxLayout(self._conversionHost)
        self._conversionLayout.setContentsMargins(0, 0, 0, 0)
        self._conversionBoxes: dict = {}
        self._conversionHost.setVisible(False)
        layout.addWidget(self._conversionHost)

        self._drops = QLabel('', self)
        self._drops.setObjectName('gmshWriterDrops')
        self._drops.setWordWrap(True)
        self._drops.setVisible(False)
        layout.addWidget(self._drops)

        self._refusal = QLabel('', self)
        self._refusal.setObjectName('gmshWriterRefusal')
        self._refusal.setWordWrap(True)
        self._refusal.setProperty('foammeshStatus', 'warning')
        self._refusal.setVisible(False)
        layout.addWidget(self._refusal)

        self._optionsHost = QWidget(self)
        self._optionsHost.setObjectName('gmshWriterOptions')
        self._optionsForm = QFormLayout(self._optionsHost)
        apply_form_metrics(self._optionsForm)
        layout.addWidget(self._optionsHost)

        row = QHBoxLayout()
        self._path = QLineEdit(self)
        self._path.setObjectName('gmshWriterPath')
        self._path.setAccessibleName(self.tr('File to write'))
        self._browse = QPushButton(self.tr('Browse…'), self)
        self._browse.setObjectName('gmshWriterBrowse')
        row.addWidget(self._path, 1)
        row.addWidget(self._browse)
        pathLabel = QLabel(self.tr('Write to'), self)
        pathLabel.setBuddy(self._path)
        wrapper = QWidget(self)
        self._pathRow = wrapper
        wrapper.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        form.addRow(pathLabel, wrapper)

        self._overwrite = QCheckBox(self.tr('Overwrite existing'), self)
        self._overwrite.setObjectName('gmshWriterOverwrite')
        layout.addWidget(self._overwrite)

        # DP-573: a row of controls wraps rather than standing wider than
        # the settings column (the compact column is 314 px).
        buttons = FlowLayout()
        self._write = QPushButton(self.tr('Write mesh'), self)
        self._write.setObjectName('gmshWriterWrite')
        self._saveOptions = QPushButton(self.tr('Save options as .opt…'), self)
        self._saveOptions.setObjectName('gmshWriterSaveOptions')
        self._restore = QPushButton(self.tr('Restore defaults'), self)
        self._restore.setObjectName('gmshWriterRestoreDefaults')
        buttons.addWidget(self._write)
        buttons.addWidget(self._saveOptions)
        buttons.addWidget(self._restore)
        layout.addLayout(buttons)

        self._pins = UnitPinsBox(self._client, self)
        layout.addWidget(self._pins)

        self._result = QLabel('', self)
        self._result.setObjectName('gmshWriterResult')
        self._result.setWordWrap(True)
        self._result.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._result)

        self._format.currentIndexChanged.connect(self._formatChanged)
        self._source.currentIndexChanged.connect(self._sourceChanged)
        self._browse.clicked.connect(self._choosePath)
        self._write.clicked.connect(self.writeNow)
        self._saveOptions.clicked.connect(self._chooseOptionsFile)
        self._restore.clicked.connect(self.restoreDefaults)
        self._done.connect(self._finished)
        self._runMeshLoaded.connect(self._runMeshFinished)
        self._formatChanged()

    # -- state ------------------------------------------------------------- #

    def forms(self) -> tuple:
        """Every form on this panel, top to bottom, for one label column.

        DP-154. The source, format and destination rows are one
        `QFormLayout` and the writer's options another, and each sized its
        label column from its own longest label: on the Gmsh export page
        their editors stood at x=72 and x=170 under the export form's 124.
        The page aligns them with its own forms through this tuple.
        """
        return (self._form, self._optionsForm)

    def source(self) -> str:
        return str(self._source.currentData())

    def setRunAvailable(self, available: bool) -> None:
        """The accepted run exists: it is the source until the switch moves.

        Without a run the switch stays on the open model and the run choice
        is disabled, so the reader sees why.
        """
        available = bool(available)
        if available == self._runAvailable:
            return
        self._runAvailable = available
        set_item_enabled(self._source, 0, available)
        self._source.blockSignals(True)
        self._source.setCurrentIndex(0 if available else 1)
        self._source.blockSignals(False)
        self._sourceChanged()

    def setSource(self, source: str) -> None:
        index = self._source.findData(source)
        if index >= 0 and (source == SOURCE_MODEL or self._runAvailable):
            self._source.setCurrentIndex(index)

    def _fillFormats(self) -> None:
        keep = self._format.currentData() if self._format.count() else None
        self._format.blockSignals(True)
        self._format.clear()
        for item in writers.WRITERS:
            if self.source() == SOURCE_RUN and item.id in FORM_FORMATS:
                continue
            self._format.addItem(item.label, item.id)
        index = self._format.findData(keep) if keep else -1
        self._format.setCurrentIndex(max(index, 0))
        self._format.blockSignals(False)

    def _sourceChanged(self, *_args) -> None:
        self._declined = None
        self._failed = None
        self._loadProblem = ''
        self._fillFormats()
        self._formatChanged()
        self.loadRunMesh()

    def writerId(self) -> str:
        return str(self._format.currentData())

    def selectWriter(self, writer_id: str) -> None:
        index = self._format.findData(writer_id)
        if index >= 0:
            self._format.setCurrentIndex(index)

    def setOption(self, name: str, value) -> None:
        """Set one option the way the control would; the value is checked."""
        self._values[name] = value
        control = self._controls.get(name)
        if isinstance(control, QCheckBox):
            control.setChecked(bool(float(value)))
        elif isinstance(control, QComboBox):
            index = control.findData(float(value))
            if index >= 0:
                control.setCurrentIndex(index)
        elif isinstance(control, QLineEdit):
            control.setText(str(value))
        self.refresh()

    def restoreDefaults(self) -> None:
        """Put every option of this writer back to its catalogue default.

        The destination the user typed stays as typed.
        """
        typed = self._path.text()
        self._values = {}
        self._formatChanged()
        self._path.setText(typed)

    def options(self) -> dict:
        return dict(self._values)

    def exporter(self):
        controller = self._controller_getter()
        if controller is None:
            return None
        return MeshExporter(lambda: controller.session, profile=self._profile,
                            overrides=self._pins.stored)

    def pins(self):
        """The unit pins box (Plan 48 GM4, D2)."""
        return self._pins

    def convert(self) -> tuple:
        """The conversions the user has chosen for this write."""
        return tuple(sorted(self._convert))

    def setConvert(self, name: str, on: bool = True) -> None:
        if on:
            self._convert.add(name)
        else:
            self._convert.discard(name)
        self.refresh()

    def _available_conversions(self, wid=None) -> list:
        if not self._facts.nodes:
            return []
        own = wid is None or wid == self.writerId()
        return writers.conversions(
            writers.writer(wid or self.writerId()), self._facts,
            self._values if own else None)

    def refresh(self) -> None:
        """Re-ask the session what the mesh is, then what the writer says."""
        exporter = None if self._loading else self.exporter()
        self._facts = writers.MeshFacts()
        if exporter is not None:
            try:
                self._facts = exporter.facts()
            except Exception:                                # noqa: BLE001
                self._facts = writers.MeshFacts()
        self._markUnwritable()
        offered = [item['id'] for item in self._available_conversions()]
        self._convert &= set(offered)
        reason = writers.refusal(self.writerId(), self._facts, self._values,
                                 convert=self.convert())
        self._showConversions()
        if not self._facts.nodes and self._facts.entities == 0:
            reason = self.tr(
                RUN_NOT_IN_SESSION
                if self._runAvailable and self.source() == SOURCE_RUN
                else NO_MESH)
            if self._loading:
                reason = self.tr(LOADING_RUN_MESH)
            elif self._loadProblem and self.source() == SOURCE_RUN:
                reason = self._loadProblem
        self._refusal.setText(reason)
        self._refusal.setVisible(bool(reason))
        self._showDrops(reason)
        self._write.setEnabled(not reason and not self._busy)
        self._saveOptions.setEnabled(bool(self.writerOptionNames()))

    def _showConversions(self) -> None:
        """One check box per conversion the matrix names for this write."""
        wanted = {item['id']: item for item in self._available_conversions()}
        for name in list(self._conversionBoxes):
            if name not in wanted:
                box = self._conversionBoxes.pop(name)
                self._conversionLayout.removeWidget(box)
                box.deleteLater()
        for name, item in wanted.items():
            box = self._conversionBoxes.get(name)
            if box is None:
                box = QCheckBox(item['label'], self._conversionHost)
                box.setObjectName('gmshWriterConvert_' + name)
                box.setToolTip(item['description'])
                box.setAccessibleName(item['label'])
                box.setAccessibleDescription(item['description'])
                box.toggled.connect(
                    lambda on, n=name: self.setConvert(n, on))
                self._conversionBoxes[name] = box
                self._conversionLayout.addWidget(box)
            box.blockSignals(True)
            box.setChecked(name in self._convert)
            box.blockSignals(False)
        self._conversionHost.setVisible(bool(wanted))

    def _showDrops(self, reason) -> None:
        """What this write leaves out, said before it is written."""
        drops = [] if reason or not self._facts.nodes else writers.stated_drops(
            writers.writer(self.writerId()), self._facts, self._values)
        self._drops.setText(
            self.tr('This write leaves out: ') + '; '.join(drops) + '.'
            if drops else '')
        self._drops.setVisible(bool(drops))

    def _markUnwritable(self) -> None:
        """FIX-K (SG15): a format this session cannot write is greyed with
        its reason, so what is offered is what can be written."""
        known = bool(self._facts.nodes or self._facts.entities)
        # DP-1448. With no mesh in the session (none generated, or its
        # session ended) the panel used to list every format as available
        # while Write was refused: formats the reader cannot use. They are
        # all greyed, with the one reason, and the list does not take focus.
        self._format.setEnabled(known)
        self._showMeshControls(known)
        for index in range(self._format.count()):
            wid = self._format.itemData(index)
            reason = ''
            if known and self._facts.nodes:
                # A CONVERTED cell stays selectable: its conversion is the
                # user's choice, offered once the format is picked.
                reason = writers.refusal(
                    wid, self._facts, _hint=False,
                    convert=[c['id'] for c in self._available_conversions(wid)])
            elif known and writers.writer(wid).kind == writers.GEOMETRY:
                reason = writers.refusal(wid, self._facts, _hint=False)
            if not known:
                reason = self.tr(NO_SESSION_MESH)
            set_item_enabled(self._format, index, not reason)
            self._format.setItemData(index, reason or None,
                                     Qt.ItemDataRole.ToolTipRole)

    def _meshKnown(self) -> bool:
        return bool(self._facts.nodes or self._facts.entities)

    def _showMeshControls(self, known: bool) -> None:
        """DP-1448. With no mesh in the session there is no format list, no
        options, no destination and no Write: only the sentence that says
        why. They come back when a mesh is open."""
        self._form.setRowVisible(self._format, known)
        self._form.setRowVisible(self._pathRow, known)
        self._optionsHost.setVisible(known and bool(self._controls))
        for widget in (self._overwrite, self._write, self._saveOptions,
                       self._restore):
            widget.setVisible(known)

    def writerOptionNames(self) -> tuple:
        return tuple(option.name for option in
                     writers.writer_options(self.writerId()))

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self.loadRunMesh()

    # -- the accepted run's mesh in the session (SG15A) ----------------------- #

    def loading(self) -> bool:
        return self._loading

    def _askReplace(self, sentence: str) -> bool:
        from PySide6.QtWidgets import QMessageBox
        from widgets.async_message_box import box as messageBox
        # The one helper module builds the box; the buttons are this panel's.
        box = messageBox(self)
        box.setObjectName('gmshRunMeshReplaceDialog')
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr('Replace the model in the Gmsh session?'))
        box.setText(sentence)
        replace = box.addButton(self.tr('Replace'),
                                QMessageBox.ButtonRole.AcceptRole)
        replace.setObjectName('gmshRunMeshReplace')
        keep = box.addButton(self.tr('Keep the open model'),
                             QMessageBox.ButtonRole.RejectRole)
        keep.setObjectName('gmshRunMeshKeep')
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is replace

    def loadRunMesh(self) -> bool:
        """Open the accepted run's mesh in the session, off the GUI thread.

        Returns True when a load was started. A session that already holds a
        different model is never replaced without the confirm answering yes.
        """
        if (self._runMesh is None or self._loading or not self._runAvailable
                or self.source() != SOURCE_RUN or not self.isVisible()):
            return False
        from foammesh.core.gmsh import run_mesh as rm
        try:
            run = rm.accepted_mesh(self._runMesh.case_path())
        except rm.RunMeshRefused as error:
            self._loadProblem = str(error)
            self.refresh()
            return False
        if run.key in (self._declined, self._failed):
            return False
        try:
            controller = self._runMesh.controller()
        except Exception as error:                           # noqa: BLE001
            self._loadProblem = str(error)
            self.refresh()
            return False
        if not rm.needs_load(controller, run):
            self._loadProblem = ''
            self.refresh()
            return False
        sentence = rm.question(controller, run)
        if sentence and not self._confirm(sentence):
            self._declined = run.key
            self.setSource(SOURCE_MODEL)
            self._result.setText(self.tr(
                'The session keeps its model; the Open model source is '
                'shown.'))
            return False
        self._loadProblem = ''
        self._loading = True
        self._result.setText('')
        from foammesh.view.gmsh_workflow.generate_panel import (
            beginSessionAction)
        handle = beginSessionAction(
            controller, self.tr('Load the mesh of run %s') % run.run_id)
        self._loadHandle = handle

        def progress(text, fraction):
            if handle is not None:
                handle.progress('load', text, fraction)

        def work():
            try:
                rm.load(controller, run, progress)
                self._runMeshLoaded.emit((run, None))
            except Exception as error:                       # noqa: BLE001
                if handle is not None and handle.cancel_requested:
                    try:
                        controller.recover()
                    except Exception:                        # noqa: BLE001
                        pass
                self._runMeshLoaded.emit((run, error))

        threading.Thread(target=work, name='gmsh-run-mesh-load',
                         daemon=True).start()
        self.refresh()
        return True

    def _runMeshFinished(self, outcome) -> None:
        run, error = outcome
        self._loading = False
        handle, self._loadHandle = self._loadHandle, None
        cancelled = bool(handle is not None and handle.cancel_requested)
        if handle is not None:
            handle.finish(error is None, str(error or ''))
        if error is not None:
            self._failed = run.key
            self._loadProblem = (
                self.tr('Loading the mesh of run %s was cancelled; choose '
                        'the source again to load it.') % run.run_id
                if cancelled else str(error))
        else:
            self._loadProblem = ''
        self.refresh()

    # -- options ------------------------------------------------------------ #

    def _formatChanged(self, *_args) -> None:
        item = writers.writer(self.writerId())
        while self._optionsForm.rowCount():
            self._optionsForm.removeRow(0)
        self._controls.clear()
        self._convert = set()
        self._values = {name: value for name, value in self._values.items()
                        if name in item.options or name in writers.READ_OPTIONS}
        for option in writers.writer_options(item.id):
            control = self._control(option)
            self._controls[option.name] = control
            caption = QLabel(option.label, self._optionsHost)
            caption.setToolTip(option.help)
            caption.setBuddy(control)
            self._optionsForm.addRow(caption, control)
        self._optionsHost.setVisible(
            bool(self._controls) and self._meshKnown())
        self._path.setText(self._offer(item))
        self._write.setText(self.tr('Write folder') if item.kind == writers.FOAM
                            else self.tr('Write file'))
        self.refresh()
        self.formsChanged.emit()

    def _control(self, option):
        name = option.name
        current = self._values.get(name, option.default)
        if name == 'Mesh.MshFileVersion':
            box = QComboBox(self._optionsHost)
            for text, number in MSH_VERSIONS:
                box.addItem(text, number)
            box.setCurrentIndex(max(box.findData(float(current)), 0))
            box.currentIndexChanged.connect(
                lambda _i, n=name, b=box: self._store(n, b.currentData()))
        elif option.kind == 'bool':
            box = QCheckBox(self._optionsHost)
            box.setChecked(bool(float(current)))
            box.toggled.connect(
                lambda on, n=name: self._store(n, 1.0 if on else 0.0))
        elif option.kind == 'enum':
            box = QComboBox(self._optionsHost)
            for key, text in option.choices:
                box.addItem(str(text), float(key))
            box.setCurrentIndex(max(box.findData(float(current)), 0))
            box.currentIndexChanged.connect(
                lambda _i, n=name, b=box: self._store(n, b.currentData()))
        else:
            box = QLineEdit(self._optionsHost)
            box.setText(str(current))
            box.editingFinished.connect(
                lambda n=name, b=box: self._store(n, b.text()))
        box.setObjectName('gmshWriterOption_' + name.split('.', 1)[1])
        box.setAccessibleName(option.label)
        box.setToolTip(option.help)
        return box

    def _store(self, name: str, value) -> None:
        self._values[name] = value
        self.refresh()

    # -- destination --------------------------------------------------------- #

    def _offer(self, item) -> str:
        folder = self._default_folder() if callable(self._default_folder) \
            else self._default_folder
        if not folder:
            return ''
        base = Path(folder)
        if item.kind == writers.FOAM:
            return str(base / 'openfoam-case')
        return str(base / ('mesh' + item.suffix))

    def _choosePath(self) -> None:
        item = writers.writer(self.writerId())
        if item.kind == writers.FOAM:
            chosen = QFileDialog.getExistingDirectory(
                self, self.tr('Folder for the OpenFOAM case'),
                self._path.text())
        else:
            chosen, _ = QFileDialog.getSaveFileName(
                self, self.tr('Write mesh'), self._path.text(),
                f'{item.label} (*{item.suffix})' if item.suffix else '')
        if chosen:
            self._path.setText(chosen)

    def _chooseOptionsFile(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self, self.tr('Save the options of this export'), '',
            'Gmsh options (*.opt)')
        if chosen:
            self.saveOptionsTo(chosen)

    def saveOptionsTo(self, path) -> bool:
        """Write the options set for this writer as a Gmsh ``.opt`` file."""
        try:
            MeshExporter.write_options_file(path, self.writerId(), self._values)
        except (ExportRefused, catalogue.OptionRefusal, ValueError) as error:
            self._result.setText(str(error))
            return False
        self._result.setText(self.tr('Options saved to {0}.').format(path))
        return True

    # -- the write ----------------------------------------------------------- #

    def writeNow(self) -> None:
        """Write on a worker thread; the answer arrives on ``written``/``failed``."""
        if self._busy:
            return
        exporter = self.exporter()
        if exporter is None:
            self._fail(self.tr(NO_MESH))
            return
        destination = self._path.text().strip()
        if not destination:
            self._fail(self.tr('Name the file to write.'))
            return
        # DP-1457: WSL Gmsh needs an absolute Windows path; a destination
        # typed relative to the working folder is made absolute here.
        destination = str(Path(destination).expanduser().resolve())
        writer_id, options = self.writerId(), dict(self._values)
        convert = self.convert()
        overwrite = self._overwrite.isChecked()
        self._busy = True
        self._write.setEnabled(False)
        self._result.setText(self.tr('Writing…'))
        # Plan 42 SG16 (D9): a write is a run like any other.
        from foammesh.view.gmsh_workflow.generate_panel import (
            beginSessionAction)
        self._handle = beginSessionAction(
            self._controller_getter(), self.tr('Write %s') % writer_id)

        def work():
            try:
                extra = {'convert': convert} if convert else {}
                outcome = exporter.write(writer_id, destination, options,
                                         overwrite=overwrite, **extra)
                self._done.emit(outcome)
            except Exception as error:                       # noqa: BLE001
                self._done.emit(error)

        threading.Thread(target=work, name='gmsh-export', daemon=True).start()

    def _finished(self, outcome) -> None:
        self._busy = False
        handle, self._handle = getattr(self, '_handle', None), None
        if handle is not None:
            handle.finish(
                not isinstance(outcome, Exception)
                and not outcome.to_dict().get('problems'),
                str(outcome) if isinstance(outcome, Exception) else '')
        if isinstance(outcome, Exception):
            self._fail(str(outcome))
        else:
            data = outcome.to_dict()
            self.last_result = data
            text = self.tr('Wrote {0} ({1:,} bytes).').format(
                data['path'], data['bytes'])
            if data.get('files') and len(data['files']) > 1:
                text += ' ' + self.tr('{0} files.').format(len(data['files']))
            if data['problems']:
                text = '; '.join(data['problems'])
            elif data['verified']:
                text += ' ' + self.tr('Read back: counts agree.')
            if data['notes']:
                text += ' ' + ' '.join(data['notes'])
            self._result.setText(text)
            (self.written if not data['problems'] else self.failed).emit(
                data if not data['problems'] else text)
        self.refresh()

    def _fail(self, sentence: str) -> None:
        self._result.setText(sentence)
        self.failed.emit(sentence)


def _client_of(run_mesh):
    """The Export page's facade client, through the run-mesh host it gave."""
    if run_mesh is None:
        return None
    page = getattr(run_mesh, '_page', None)
    return getattr(page, '_client', None) or getattr(run_mesh, 'client', None)


class UnitPinsBox(QWidget):
    """The two import-unit pins, with who set them (Plan 48 GM4, D2).

    FoamMesh pins ``Geometry.OCCTargetUnit`` to metres and
    ``Geometry.OCCScaling`` to 1 before every CAD import, so a model in
    millimetres arrives in metres. Both are shown here as *set by FoamMesh*
    and can be unlocked: unlocking stores the two options in the project's
    Gmsh options (the Options list shows them like any other) and they read
    *set by you* until locked again, which removes them.
    """

    changed = Signal()

    def __init__(self, client=None, parent=None):
        super().__init__(parent)
        self._client = client
        #: What the facade said when it refused the last change, or ''.
        self._refusal = ''
        self.setObjectName('gmshUnitPins')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._title = QLabel(self.tr('Import units'), self)
        self._title.setObjectName('gmshUnitPinsTitle')
        layout.addWidget(self._title)
        form = QFormLayout()
        apply_form_metrics(form)
        self._form = form
        layout.addLayout(form)
        self._unit = QLineEdit(self)
        self._unit.setObjectName('gmshPinUnit')
        self._unit.setAccessibleName(self.tr('Target unit'))
        self._unit.setToolTip(self.tr(
            'Geometry.OCCTargetUnit: the unit a CAD file is converted to on '
            'import. M keeps the model in metres.'))
        self._scale = QLineEdit(self)
        self._scale.setObjectName('gmshPinScaling')
        self._scale.setAccessibleName(self.tr('Import scaling'))
        self._scale.setToolTip(self.tr(
            'Geometry.OCCScaling: a factor applied to the geometry on import'))
        unitLabel = QLabel(self.tr('Target unit'), self)
        unitLabel.setBuddy(self._unit)
        scaleLabel = QLabel(self.tr('Import scaling'), self)
        scaleLabel.setBuddy(self._scale)
        form.addRow(unitLabel, self._unit)
        form.addRow(scaleLabel, self._scale)
        row = QHBoxLayout()
        self._state = QLabel('', self)
        self._state.setObjectName('gmshPinState')
        self._state.setWordWrap(True)
        self._toggle = QPushButton(self)
        self._toggle.setObjectName('gmshPinUnlock')
        row.addWidget(self._state, 1)
        row.addWidget(self._toggle)
        layout.addLayout(row)
        self._note = QLabel('', self)
        self._note.setObjectName('gmshPinNote')
        self._note.setWordWrap(True)
        layout.addWidget(self._note)
        self._toggle.clicked.connect(self._toggled)
        self._unit.editingFinished.connect(self._edited)
        self._scale.editingFinished.connect(self._edited)
        self.reload()

    # -- the stored options ------------------------------------------------- #

    def stored(self) -> dict:
        """The project's stored Gmsh options (``{}`` without a client)."""
        if self._client is None:
            return {}
        try:
            values = self._client.field_values((OVERRIDES_FIELD,))
            raw = values.get(OVERRIDES_FIELD)
            return overrides_from_text(getattr(raw, 'value', raw))
        except Exception:                                    # noqa: BLE001
            return {}

    def _store(self, mapping: dict) -> bool:
        if self._client is None:
            return False
        from foammesh.view import facade_client
        facade_client.submit(
            self._client, 'configuration.patch',
            {'patch': {OVERRIDES_FIELD: overrides_to_text(mapping)}},
            then=self._stored)
        return True

    def _stored(self, result) -> None:
        """Plan 48 LW (DP-5600). The write is answered after the click: a
        refusal (the Generate step is locked once the mesh is accepted) is
        said in the note instead of leaving a button that does nothing."""
        failed = getattr(result, 'status', '') == 'failed'
        self._refusal = str(getattr(result, 'message', '') or self.tr(
            'The change was refused.')) if failed else ''
        self.reload()

    def rows(self) -> list:
        return writers.unit_pin_rows(self.stored())

    def unlocked(self) -> bool:
        return any(row['unlocked'] for row in self.rows())

    def reload(self) -> None:
        stored = self.stored()
        rows = writers.unit_pin_rows(stored)
        unit, scale = rows[0], rows[1]
        unlocked = unit['unlocked'] or scale['unlocked']
        for edit, row in ((self._unit, unit), (self._scale, scale)):
            edit.blockSignals(True)
            edit.setText(str(row['value']))
            edit.blockSignals(False)
            edit.setReadOnly(not unlocked)
        self._state.setText(unit['state'] if unit['state'] == scale['state']
                            else '%s / %s' % (unit['state'], scale['state']))
        self._toggle.setText(self.tr('Lock again') if unlocked
                             else self.tr('Unlock'))
        self._toggle.setEnabled(self._client is not None)
        self._toggle.setToolTip(
            '' if self._client is not None else self.tr(
                'Open a project to change the import units.'))
        factor = writers.metres_per_unit(stored)
        if factor == 1.0:
            note = self.tr('The model is in metres.')
        elif factor:
            note = self.tr('One model unit is %g m; the SU2 file is scaled '
                           'to metres on writing.') % factor
        else:
            note = self.tr('The target unit is not one FoamMesh knows; files '
                           'are written in the model unit.')
        if self._refusal:
            note = self._refusal + ' ' + note
        self._note.setText(note)
        self.changed.emit()

    def unlock(self, unit=None, scaling=None) -> bool:
        """Store both pins (their current values, or the ones given)."""
        try:
            mapping = writers.with_unit_pins(
                self.stored(), unlock=True, unit=unit, scaling=scaling)
        except catalogue.OptionRefusal as error:
            self._note.setText(str(error))
            return False
        return self._store(mapping)

    def lock(self) -> bool:
        return self._store(writers.with_unit_pins(self.stored(), unlock=False))

    def _toggled(self) -> None:
        if self.unlocked():
            self.lock()
        else:
            self.unlock()

    def _edited(self) -> None:
        if self.unlocked():
            self.unlock(self._unit.text(), self._scale.text())
