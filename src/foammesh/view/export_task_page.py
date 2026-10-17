"""The shared ``common.export`` task page.

Plan 30 WP-09 / F-17. ``common.export`` is declared by both engines and had a
page in neither registry: it was served by the legacy Designer widget through
``StepManager``'s hand-written table, so ``EngineBranchView._rebuild`` skipped
the last row of both workflows and the branch could not show, run or settle it.

Plan 33 EXPORT-04 and OF-09. The page that answered that used to carry a
paragraph about what exporting means, a record sentence and three buttons --
`Export mesh`, `Export as 2D (plane)`, `Export as 2D (wedge)` -- and not one
setting: every question an export asks lived in a window that opened on top of
the settings column. The body is the form now, the footer presses the act, and
the writers are still not duplicated here -- ``ExportForm`` holds the questions
and ``ExportPage`` holds the one run of them, so the application keeps one
export path rather than two that can disagree about what "Export" means.
"""
from __future__ import annotations

import html
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QPushButton, QWidget)

from foammesh.app import app
from foammesh.core.import_export.authored import (
    export_destination_missing, export_record)
from foammesh.view.export.export_form import (
    MESH_FORMATS, ExportForm, extrude_payload, register_inline_export)
from foammesh.view.facade_client import query
from foammesh.view.workflow_controls.task_page import EngineTaskPage


logger = logging.getLogger(__name__)

#: What the page says when the format registry could not be read at all, so
#: that a permanent failure of it is not indistinguishable from a case that
#: is not open yet. Plan 33 W-P: the fallback list is right and the silence
#: was not -- the step offered the OpenFOAM case and nothing else for three
#: journeys, through a 62-test package gate and an independent audit, and
#: said nothing about it anywhere.
FORMATS_UNREADABLE = ('The list of export formats could not be read, so only '
                      'the OpenFOAM case is offered.')

#: What the page says before the case has ever been exported. Not blank: a
#: blank line where a destination belongs reads as "no destination needed".
NOTHING_EXPORTED = 'Nothing has been exported from this case yet.'

#: DP-244. The record is the record of an act and is never edited; this is a
#: second sentence about the disk today. An export whose folder has since been
#: moved, renamed or deleted read as exported, naming a path where nothing is.
DESTINATION_MISSING = 'Destination missing: nothing is at that path now.'


#: UXA7 (#56). The longest path the sentence prints before its middle is
#: elided; the whole path stays in the link's tooltip and the Details route.
PATH_SHOWN = 64


def elide_middle(text: str, limit: int = PATH_SHOWN) -> str:
    """*text* cut in the middle to at most *limit* characters.

    The start of a path says which disk and project, the end says which
    file; the middle is the part nobody reads.
    """
    if len(text) <= limit:
        return text
    keep = max(limit - 1, 2)
    head = keep // 2
    return text[:head] + '…' + text[len(text) - (keep - head):]


def folder_of(destination) -> Path:
    """The folder to show for an export: the case folder, or the file's."""
    path = Path(str(destination))
    return path if path.is_dir() else path.parent


class ExportTaskPage(EngineTaskPage):
    """Write the finished mesh out, in the format the target solver reads."""

    task_id_default = 'common.export'
    #: Nothing to run: this task writes a file, it does not mesh. §7.1 keeps
    #: Run out of this page's vocabulary entirely.
    run_stage = None
    run_all_task_id = None
    #: The task whose acceptance produces the run an export can reproduce.
    #: Per engine, because the first meshing step is not the same one on both
    #: and a hard-coded name would send half the users to a row that does not
    #: exist. Empty on the base class, which serves neither engine alone.
    mesh_task_id = ''

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        #: The record the page is currently reporting, so that the Details
        #: route reads the same export the sentence does.
        self._record_now: dict = {}
        #: Why the format list could not be read last time it was asked for,
        #: or ``''``. Held rather than written straight to the label, because
        #: `_refreshReadiness` owns that label and runs after every read.
        self._entriesFailure: str = ''
        #: UXA7 (#56). The destination this page wrote and the form has not
        #: been changed since: while it is set, the footer's primary opens the
        #: folder instead of exporting again.
        self._writtenTo: str = ''
        self._section: bool = False
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id or self.engine_id)

    # -- construction ------------------------------------------------------ #

    def build_sections(self, layout) -> None:
        # EXPORT-03. The paragraph that used to open this page said what an
        # export is and that it does not re-mesh. That is what the step is
        # for, which the step's own description says; a form asks.
        self._readiness = QLabel('', self)
        self._readiness.setObjectName('exportReadiness')
        self._readiness.setWordWrap(True)
        self._readiness.setProperty('foammeshStatus', 'warning')
        layout.addWidget(self._readiness)

        # DP-542. The handoff point says what checkMesh counted. S5 and S6
        # each ended `Failed 1 mesh checks` (concave cells) and this page,
        # the last one before the mesh leaves the app, said nothing about it.
        self._quality = QLabel('', self)
        self._quality.setObjectName('exportQualityFailedChecks')
        self._quality.setWordWrap(True)
        self._quality.setProperty('foammeshStatus', 'warning')
        self._quality.setAccessibleName(self.tr(
            'Checks the last mesh check of this mesh failed'))
        self._quality.setVisible(False)
        layout.addWidget(self._quality)

        entries, recommended = self.exportEntries()
        # Plan 40 UXB8 (#6). The target solver is chosen once, on Mesh setup.
        # Export reads it back and offers the way there; the format below
        # starts on what that target reads.
        self._targetRow = QWidget(self)
        self._targetRow.setObjectName('exportTargetRow')
        targetLayout = QHBoxLayout(self._targetRow)
        targetLayout.setContentsMargins(0, 0, 0, 0)
        caption = QLabel(self.tr('Target solver'), self._targetRow)
        targetLayout.addWidget(caption)
        self._target = QLabel('', self._targetRow)
        self._target.setObjectName('exportTarget')
        # Plan 44 F4. "Not chosen: Mesh setup sets it" on one line pushed the
        # row to 274 px in the 241 px a 1280 px window's sheet gives it; the
        # words wrap beside the caption instead.
        self._target.setWordWrap(True)
        self._target.setAccessibleName(self.tr('Target solver'))
        self._target.setAccessibleDescription(self.tr(
            'The solver this mesh is for, as chosen on Mesh setup.'))
        caption.setBuddy(self._target)
        targetLayout.addWidget(self._target, 1)
        self._changeTarget = QPushButton(self.tr('Change target'),
                                         self._targetRow)
        self._changeTarget.setObjectName('exportChangeTarget')
        self._changeTarget.setFlat(True)
        # Plan 44 F4 (MF20). Going to Mesh setup is a secondary action: a
        # text link the width of its words, not a 111 px button, so the row
        # fits the 257 px a 1280 px window's sheet gives it.
        from foammesh.view.widgets.link_button import as_link
        as_link(self._changeTarget)
        self._changeTarget.setToolTip(self.tr(
            'The target solver is set on Mesh setup. Go there to change it.'))
        self._changeTarget.clicked.connect(self.changeTarget)
        targetLayout.addWidget(self._changeTarget)
        layout.addWidget(self._targetRow)
        self._showTarget()
        self._form = ExportForm(self, entries, recommended)
        # W-O1. The derived "Will be created in ..." line restates the two
        # destination fields above it and stood on the form before the reader
        # had touched anything. The dialog route keeps it; the step is in the
        # settings column, where Plan 33 section 1 allows settings and the
        # measurements a run produced, and a readout of two adjacent fields is
        # neither. The whole sentence is the location field's tooltip and the
        # Destination row of this step's Details view.
        self._form.setDestinationShown(False)
        layout.addWidget(self._form)
        self._form.pathChanged.connect(self._formWasEdited)
        self._form.formatChanged.connect(self._formWasEdited)
        self._form.findChild(QComboBox, 'exportDimensionality'
                             ).currentIndexChanged.connect(self._formWasEdited)
        self._form.findChild(QCheckBox, 'exportOverwrite'
                             ).toggled.connect(self._formWasEdited)
        # EXPORT-04. The footer's press arrives at the legacy page, which is
        # what the step manager holds; this is how it finds the form the
        # reader is actually looking at.
        register_inline_export(self)

        # DP-244. Where the mesh went, read out of the export record rather
        # than out of this page's own state.
        self._record = QLabel('', self)
        self._record.setObjectName('exportRecord')
        self._record.setWordWrap(True)
        # UXA7 (#56). The path is a link to the folder it names.
        self._record.setTextFormat(Qt.RichText)
        self._record.setOpenExternalLinks(False)
        self._record.linkActivated.connect(lambda _href: self.openFolder())
        self._record.setAccessibleName(self.tr(
            'What the last export of this case wrote'))
        layout.addWidget(self._record)

        # DP-244 again, and EXPORT-03. What became of the destination is not
        # the record and is not provenance: it is a warning, and it reads as
        # one rather than as the tail of a sentence that is otherwise good
        # news.
        self._missing = QLabel('', self)
        self._missing.setObjectName('exportDestinationMissing')
        self._missing.setWordWrap(True)
        self._missing.setProperty('foammeshStatus', 'warning')
        self._missing.setVisible(False)
        layout.addWidget(self._missing)

    def showEvent(self, event):
        """Re-ask whether this case has a mesh, every time the page is opened.

        DP-103. `refresh` runs when the page is built and when an edit lands,
        and neither of those happens when a stage finishes meshing. MEASURED
        in the twenty-leg sweep: the cyclone's Export page read `There is no
        mesh in this case yet, so there is nothing to export. Mesh it first.`
        with a 107,975-cell mesh on screen beside it and Castellation, Snap
        and Layers all ticked in the outline. The readiness sentence is a
        cached answer to a question whose answer had changed; asking it on
        arrival costs one `case.classify` and cannot be stale.
        """
        super().showEvent(event)
        register_inline_export(self)
        self.refresh()

    def refresh_status(self) -> None:
        """DP-129. The other half of DP-103: a run that finishes *here*.

        DP-103 asks the question again on arrival, which covers every way of
        walking to this page after meshing. It does not cover a run that
        lands from the toolbar while this page is already open, which is an
        ordinary way to use it. MEASURED on the snappy `elbow` leg of the
        `45db9031` sweep: one frame carries `Whole mesh: 52,004 cells` in the
        toolbar, a drawn mesh, `Mesh is runnable. Quality: marginal.` with
        three passing checkMesh metrics -- and, in the middle of it, `There
        is no mesh in this case yet, so there is nothing to export.`

        `refresh_status` is what the branch calls on every page each time the
        graph moves, which is exactly when a run has just landed.

        DP-142. It used to answer that by calling `refresh`, and `refresh`
        calls `refresh_status`: the two went round until Python gave up, and
        the branch's refresh loop swallows what a page raises so that one bad
        page cannot take the other nine down with it. So both fixes above
        were written correctly and neither ever ran. MEASURED with a live
        probe on the snappy `elbow` leg -- `RecursionError` at blockMesh,
        castellation, snap and layers, at every one of which `canExport()`
        answered True while the sentence saying there was no mesh stayed on
        screen. The recompute is its own method now, so neither entry point
        goes through the other.
        """
        super().refresh_status()
        self._refreshReadiness()

    # -- the one export implementation ------------------------------------- #

    @staticmethod
    def legacyPage():
        """The object that owns the writers.

        Reached rather than reimplemented: two copies of "which operation
        writes which format" is exactly the kind of second home this work
        package exists to remove.
        """
        window = getattr(app, 'window', None)
        manager = getattr(window, '_stepManager', None)
        accessor = getattr(manager, 'exportPage', None)
        return accessor() if accessor is not None else None

    def canExport(self) -> bool:
        page = self.legacyPage()
        ask = getattr(page, 'canExport', None)
        if ask is None:
            return True
        try:
            return bool(ask())
        except Exception:                                    # noqa: BLE001
            return True

    def aligned_forms(self):
        """The export form's own forms are this page's column (DP-154)."""
        form = getattr(self, '_form', None)
        return form.forms() if form is not None else ()

    def targetText(self) -> str:
        """The target solver as Mesh setup chose it, in words."""
        return getattr(self, '_targetName', '') or self.tr(
            'Not chosen: Mesh setup sets it')

    def _showTarget(self) -> None:
        label = getattr(self, '_target', None)
        if label is not None:
            label.setText(self.targetText())

    def changeTarget(self) -> bool:
        """Stand on Mesh setup, the one place the target is set (UXB8)."""
        from foammesh.view.main_window.meshing_method_branch import (
            METHOD_TOKEN)
        window = getattr(app, 'window', None)
        manager = getattr(window, '_stepManager', None)
        navigation = getattr(manager, '_navigation', None)
        request = getattr(navigation, 'requestBranch', None)
        if request is None:
            return False
        request(METHOD_TOKEN)
        return True

    def refresh(self) -> None:
        super().refresh()
        form = getattr(self, '_form', None)
        if form is not None:
            entries, recommended = self.exportEntries()
            self._showTarget()
            section = getattr(form, 'setSection', None)
            if section is not None:
                section(getattr(self, '_section', False))
            form.setEntries(entries, recommended)
        self._refreshReadiness()

    def exportEntries(self):
        """What this case can be exported as, from the facade's registry.

        The same read the menu route makes, so the list the step offers and
        the list the window offers cannot differ. A case that is not open
        yet, or a client that has never heard of the operation, leaves the
        form on its own default rather than leaving the step with no form.

        The read goes through `query`, not `run_sync`: `run_sync` is the
        synchronous *write* path and re-baselines the project fingerprint
        afterwards, which walks the case directory for nothing (Plan 30
        WP-08).

        Plan 33 W-P. The fallback stays; the silence does not. A failure that
        is total, permanent and on every call used to be indistinguishable
        from a case that is not open yet, so the step showed a one-item
        format list with nothing in the log and nothing on the page. Both are
        written now, and the page carries the reason until the read works.
        """
        try:
            payload = query(self._client,
                            'case.export.entries').payload or {}
        except Exception as error:                           # noqa: BLE001
            logger.warning('case.export.entries could not be read, so the '
                           'export step is offering its default format list '
                           'only: %s', error, exc_info=True)
            self._entriesFailure = str(error)
            return (), 'openfoam'
        self._entriesFailure = ''
        self._section = bool(payload.get('meshes_a_section'))
        solver = str(payload.get('target_solver') or 'unselected')
        self._targetName = (str(payload.get('target_solver_name') or '')
                            if solver != 'unselected' else '')
        entries = tuple(entry for entry in payload.get('entries', ())
                        if entry.get('entry_id') in MESH_FORMATS)
        return entries, str(payload.get('recommended_entry_id') or 'openfoam')

    # -- the press --------------------------------------------------------- #

    async def runExport(self) -> bool:
        """Write what the form says, and say whether a file was produced.

        EXPORT-04. This is the whole of the footer's press. It used to open
        the format chooser, which is a window asking the questions that are
        answered on screen now.
        """
        form = getattr(self, '_form', None)
        if form is None:                                     # pragma: no cover
            return False
        refusal = form.refusal()
        if refusal:
            # R50 over again: a press that does nothing and says nothing is
            # indistinguishable from a broken control.
            self._say(refusal)
            return False
        parameters = form.operationParameters()
        replacing = await self._confirmOverwrite(form)
        if replacing is None:
            return False
        if replacing:
            parameters['overwrite'] = True
        if form.dimensionality():
            boundaries, options = form.extrudeOptions()
            parameters.update({'boundaries': boundaries,
                               'options': extrude_payload(options)})
        written = bool(await self.runExportOperation(form.operation(),
                                                     parameters))
        if written:
            # DP-562. The folder the form named exists now; the record below
            # says what was written, and the form offers a free name.
            form.destinationWasWritten()
            self.refresh()
            # D10: an in-place entry names no destination to open.
            self._writtenTo = str(parameters.get('destination', ''))
            self._syncFooter()
        return written

    # -- after an export (UXA7, #56) ---------------------------------------- #

    def openFolderMode(self) -> bool:
        """Whether the footer's press opens the folder rather than exports.

        True from a successful export until the reader changes anything the
        form asks, and only while what was written is still there.
        """
        written = getattr(self, '_writtenTo', '')
        return bool(written) and Path(written).exists()

    def proceedOverride(self):
        """The footer's label and tooltip after an export, or ``None``."""
        if not self.openFolderMode():
            return None
        return (self.tr('Open folder'),
                self.tr('Open the folder the mesh was exported to. Change '
                        'the format or the name to export again.'))

    def openFolder(self) -> bool:
        """Show the exported mesh's folder in the file manager."""
        target = getattr(self, '_writtenTo', '') or (self._record_now or {}).get(
            'destination') or ''
        if not target:
            return False
        return bool(QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(folder_of(target)))))

    def _formWasEdited(self, *_args) -> None:
        if getattr(self, '_writtenTo', ''):
            self._writtenTo = ''
            self._syncFooter()

    def _syncFooter(self) -> None:
        window = getattr(app, 'window', None)
        manager = getattr(window, '_stepManager', None)
        update = getattr(manager, '_updateWizardActions', None)
        if update is not None:
            try:
                update()
            except Exception:                                # noqa: BLE001
                logger.debug('footer not refreshed', exc_info=True)

    async def _confirmOverwrite(self, form):
        """``True`` to replace the destination, ``False`` for nothing to
        replace, ``None`` to stop (DP-680).

        With "Overwrite existing" ticked and something at the destination,
        what is there must be an export of this kind with a mesh in it, and
        the reader is asked before it is replaced. Anything else there is
        refused, not emptied.
        """
        from foammesh.core.import_export.overwrite import holds_mesh
        target = form.projectPath()
        if not form.overwrite() or target is None or not target.exists():
            return False
        if not holds_mesh(target, form.overwriteKind()):
            self._say(self.tr('{0} exists but holds no mesh this export '
                              'wrote, so it is not replaced. Choose a new '
                              'name.').format(target))
            return None
        if not await self.askOverwrite(target):
            return None
        return True

    async def askOverwrite(self, target) -> bool:
        from widgets.async_message_box import AsyncMessageBox
        return await AsyncMessageBox().confirm(
            self, self.tr('Overwrite existing export'),
            self.tr('{0} already holds a mesh. Replace it with this export? '
                    'Only that {1} is replaced.').format(
                target, self.tr('file') if target.is_file()
                else self.tr('folder')))

    async def runExportOperation(self, operation, parameters) -> bool:
        """Run one export operation, through the one implementation of it."""
        page = self.legacyPage()
        runner = getattr(page, 'runExportOperation', None)
        if runner is not None:
            return bool(await runner(operation, parameters))
        result = await self._client.run(operation, parameters)
        return getattr(result, 'status', '') == 'accepted'

    def _say(self, sentence: str, notice: str = '') -> None:
        """Put one sentence on the label, with a standing notice in front.

        Plan 33 W-P. The notice is a second, independent thing the reader has
        to be told -- that the format box is not the list of what this case
        can be written as -- and it has to survive every recompute of the
        sentence. Joined here, where the label is written, so there is still
        one place that decides whether the label is on screen at all.
        """
        label = getattr(self, '_readiness', None)
        if label is None:                                    # pragma: no cover
            return
        if notice:
            sentence = f'{notice} {sentence}'.strip()
        label.setText(sentence)
        label.setVisible(bool(sentence))

    # -- the export record ------------------------------------------------- #

    def exportRecord(self) -> dict:
        """What the last export wrote, out of the case's own history.

        DP-244. The record is the source. A page that decided "exported" from
        anything it holds itself is deciding it from a status word that an
        unexported case can earn: `common.export` is declared without
        `run_gated`, so accepting the row settles it whether or not a file was
        ever written.
        """
        try:
            payload = query(self._client, 'history.query').payload or {}
        except Exception:                                    # noqa: BLE001
            return {}
        return export_record(payload.get('artifacts') or ())

    def meshStepTitle(self) -> str:
        """The outline's name for the step that would produce a run."""
        try:
            return str(self._task_titles().get(self.mesh_task_id) or '')
        except Exception:                                    # noqa: BLE001
            return ''

    def showExportRecord(self, record) -> None:
        """Say where the mesh went, in one sentence.

        EXPORT-03. It used to be three claims in one line -- the destination,
        the run whose layout was reproduced, and the cores it was decomposed
        over -- above a form whose own destination rows were being squeezed
        to make room for them. Which run an export reproduced is provenance:
        it is on the Details route with the rest of what the record holds.
        """
        self._record_now = dict(record or {})
        label = getattr(self, '_record', None)
        if label is None:                                    # pragma: no cover
            return
        destination = self._record_now.get('destination') or ''
        if not destination:
            # W-O1. The sentence stays authored here -- it is what the page
            # means by an empty record, and the accessible name above it says
            # the same thing -- and comes off the form. "Nothing has been
            # exported yet" is the state of the task, which the outline row
            # beside the column paints for all twenty-nine tasks at once, and
            # it was standing on a form that had not been touched. The record
            # speaks again the moment there is a record to read.
            label.setText(self.tr(NOTHING_EXPORTED))
            label.setVisible(False)
            self._showMissing(False)
            return
        shown = html.escape(elide_middle(str(destination)))
        label.setText(self.tr('Exported to {0}.').format(
            '<a href="#open-folder">%s</a>' % shown))
        tip = getattr(label, 'setToolTip', None)
        if tip is not None:
            tip(str(destination))
        label.setVisible(True)
        self._showMissing(export_destination_missing(self._record_now))

    def _showMissing(self, missing: bool) -> None:
        label = getattr(self, '_missing', None)
        if label is None:
            return
        label.setText(self.tr(DESTINATION_MISSING) if missing else '')
        label.setVisible(bool(missing))

    def derived_quantities(self) -> tuple:
        """The provenance of the last export, for the Details route.

        Nothing measured is removed. The run identifier is the whole point of
        recording one -- without it "exported" is a claim about a file with
        no way back to the result it holds -- so it is a row here rather than
        a clause of the sentence on the page.
        """
        record = getattr(self, '_record_now', None) or {}
        destination = record.get('destination') or ''
        if not destination:
            return ()
        rows = [(self.tr('Destination'), destination),
                (self.tr('Accepted run'),
                 record.get('run_id') or self.tr('none recorded'))]
        if record.get('entry_id'):
            rows.append((self.tr('Format'), record['entry_id']))
        if record.get('layout_source'):
            rows.append((self.tr('Mesh layout'), record['layout_source']))
        if record.get('decomposed'):
            rows.append((self.tr('Decomposed over'),
                         self.tr('{0} cores').format(record.get('cores') or 1)))
        if export_destination_missing(record):
            rows.append((self.tr('On disk now'),
                         self.tr('nothing is at that path')))
        return tuple(rows)

    def _refreshRecord(self) -> None:
        self.showExportRecord(self.exportRecord())

    def _refreshReadiness(self) -> None:
        """Ask once whether there is a mesh, and say so on the page.

        DP-142. Both entry points -- the full `refresh` that rebuilds the
        page, and the `refresh_status` the branch calls whenever the graph
        moves -- land here rather than on each other. Calling one from the
        other is what made this sentence unfixable twice over.
        """
        if not hasattr(self, '_readiness'):
            return
        exportable = self.canExport()
        form = getattr(self, '_form', None)
        if form is not None:
            form.setEnabled(exportable)
        # R50 over again on the new surface: a form the reader cannot use
        # and cannot explain is worse than one that fails with a message.
        # DP-244. And "mesh it first" names no row of the outline, so the
        # reason now carries the step that would produce a run, under the
        # name the outline shows for it.
        title = self.meshStepTitle()
        nothing = self.tr(
            'There is no mesh in this case yet, so there is nothing to '
            'export. ')
        reason = (self.tr('Run {0} first.').format(title) if title
                  else self.tr('Mesh it first.'))
        # Plan 33 W-P. A degraded format list is the reader's business: the
        # box in front of them is not then the list of what this case can be
        # written as. Passed from here rather than written at the point of
        # failure, because this method owns the label and runs after it.
        self._say('' if exportable else nothing + reason,
                  self.tr(FORMATS_UNREADABLE)
                  if getattr(self, '_entriesFailure', '') else '')
        warnings = [line for line in (self.thinRegionLine(),
                                      self.failedCheckLine(),
                                      self.layerShortfallLine()) if line]
        self._showFailedChecks('\n'.join(warnings) if exportable else '')
        self._refreshRecord()

    def _reportVerdict(self) -> dict:
        try:
            payload = query(self._client, 'quality.report').payload or {}
        except Exception:                                    # noqa: BLE001
            return {}
        data = payload.get('report')
        if not data:
            return {}
        from foammesh.core.quality import QualityReport, verdict_from_report
        try:
            return verdict_from_report(QualityReport.from_dict(data))
        except Exception:                                    # noqa: BLE001
            logger.debug('unreadable quality report', exc_info=True)
            return {}

    def thinRegionLine(self) -> str:
        """DP-2635. The regions the mesh did not resolve, or ''."""
        verdict = self._reportVerdict()
        line = '' if verdict.get('stale') else str(
            verdict.get('thinLine') or '')
        return (str(self.tr('{0}. See the Quality step.')).format(line)
                if line else '')

    def failedCheckLine(self) -> str:
        """DP-542. checkMesh's failed-check tally for the loaded mesh, or ''.

        Read from the stored report through the same projection the verdict
        strip uses, so the two cannot word the same tally differently. A
        report checked against a different mesh (``stale``) says nothing
        about this one and is not repeated here.
        """
        try:
            payload = query(self._client, 'quality.report').payload or {}
        except Exception:                                    # noqa: BLE001
            return ''
        data = payload.get('report')
        if not data:
            return ''
        from foammesh.core.quality import QualityReport, verdict_from_report
        try:
            verdict = verdict_from_report(QualityReport.from_dict(data))
        except Exception:                                    # noqa: BLE001
            logger.debug('unreadable quality report', exc_info=True)
            return ''
        if verdict.get('stale'):
            return ''
        line = str(verdict.get('failedCheckLine') or '')
        return (str(self.tr('{0}. See the Quality step.')).format(line)
                if line else '')

    def layerShortfallLine(self) -> str:
        """DP-664. Requested boundary layers the run did not grow, or ''.

        MEASURED on mesh campaign 0925 S8: three layers asked for on
        `elbow`, none grown, and this page said `Every metric grades good`
        -- checkMesh grades the cells that exist and a layer stage that grew
        nothing leaves none for it to find. Read from the same layer record
        the Quality step's table reads.
        """
        try:
            payload = query(self._client, 'mesh.layer_coverage').payload or {}
        except Exception:                                    # noqa: BLE001
            return ''
        from foammesh.core.quality import layer_shortfall_line
        line = layer_shortfall_line(payload)
        if not line:
            return ''
        return str(self.tr('{0}. See the Quality step.')).format(
            line[0].upper() + line[1:])

    def _showFailedChecks(self, line: str) -> None:
        label = getattr(self, '_quality', None)
        if label is None:
            return
        label.setText(line)
        label.setVisible(bool(line))


class SnappyExportPage(ExportTaskPage):
    engine_id = 'snappy'
    #: The first step that produces a mesh on snappy, and so the first that
    #: can produce a run for an export to reproduce.
    mesh_task_id = 'snappy.base_grid'


class _RunMeshHost:
    """SG15A. What the writer panel asks the page for to load the run's mesh."""

    def __init__(self, page):
        self._page = page

    def case_path(self):
        try:
            return self._page._client.case_path
        except Exception:                                    # noqa: BLE001
            return None

    def controller(self):
        """The session controller; made when none exists yet (the guided
        Generate runs through the facade, not the session)."""
        found = self._page.sessionController()
        if found is not None:
            return found
        from foammesh.view.gmsh_workflow.compute_page import (
            sharedGenerateController)
        return sharedGenerateController(self._page._client)


class GmshExportPage(ExportTaskPage):
    engine_id = 'gmsh'
    mesh_task_id = 'gmsh.compute'

    def build_sections(self, layout) -> None:
        """Plan 41 GN15. The form above, then every Gmsh writer under it.

        The form writes what the accepted run produced, through the facade.
        The panel writes what the resident session holds, in any format Gmsh
        writes, with that format's options, and refuses with the reason a
        format that cannot carry the mesh (GN15, section 4.4).
        """
        super().build_sections(layout)
        from PySide6.QtWidgets import QLabel

        from foammesh.view.export.gmsh_writer_panel import GmshWriterPanel

        heading = QLabel(self.tr('Gmsh formats'), self)
        heading.setObjectName('gmshWriterHeading')
        # Plan 44 GFIT (DP-2668, user decision 2026-10-16). The extra Gmsh
        # writers are a secondary section -- the form above exports the
        # accepted run, so the defaults let the user proceed -- and stand
        # behind a fold that opens closed; its header names the format now
        # chosen. The heading label stays (hidden) for what finds it.
        heading.setVisible(False)
        layout.addWidget(heading)
        self._writerPanel = GmshWriterPanel(
            self.sessionController, self, default_folder=self._exportFolder,
            run_mesh=_RunMeshHost(self))
        from foammesh.view.gmsh_workflow.folds import add_fold, clip_header

        self._writerFold = add_fold(
            self, layout, name='Writers', title=self.tr('Gmsh formats'),
            description=self.tr(
                'Write the model open in the Gmsh session, or the accepted '
                'run, in any format Gmsh writes, with that format options.'),
            panel=self._writerPanel)
        clip_header(self._writerFold)
        self._writerFold.toggled.connect(self._writerFoldToggled)
        self._writerPanel._format.currentIndexChanged.connect(
            self._writerFoldSummary)
        self._writerFoldSummary()
        self._writerPanel.setRunAvailable(self.canExport())
        # DP-154: the writer's rows are rebuilt with each format; their
        # labels go back into the page's one column each time.
        self._writerPanel.formsChanged.connect(self._align_field_columns)

        # Plan 41 GN19: the script of the model (Python / .geo), Run script
        # and the ONELAB parameters of an opened .geo; a closed tier.
        from PySide6.QtWidgets import QPushButton

        from foammesh.view.gmsh_workflow.script_panel import GmshScriptPanel

        self._scriptHeader = QPushButton(self.tr('Script of the model'), self)
        self._scriptHeader.setObjectName('gmshScriptHeader')
        self._scriptHeader.setCheckable(True)
        self._scriptHeader.setFlat(True)
        self._scriptHeader.setAccessibleDescription(self.tr(
            'The Python and .geo script that rebuilds the model in bare '
            'Gmsh; edit and reload a .geo; run a script on the model; '
            'the parameters of an opened .geo.'))
        self._scriptPanel = GmshScriptPanel(self.sessionController, self)
        self._scriptPanel.setVisible(False)
        self._scriptHeader.toggled.connect(self._scriptPanel.setVisible)
        layout.addWidget(self._scriptHeader)
        layout.addWidget(self._scriptPanel)

    def _writerFoldSummary(self, *_args) -> None:
        fold = getattr(self, '_writerFold', None)
        panel = getattr(self, '_writerPanel', None)
        if fold is None or panel is None:
            return
        title = str(fold.property('foammeshFoldTitle') or '')
        said = panel._format.currentText()
        fold.setText(f'{title}: {said}' if said else title)

    def _writerFoldToggled(self, opened: bool) -> None:
        # `refresh` skips a hidden panel, so opening the fold brings it up to
        # date at once.
        panel = getattr(self, '_writerPanel', None)
        if opened and panel is not None:
            panel.setRunAvailable(self.canExport())
            panel.refresh()
            panel.loadRunMesh()

    def aligned_forms(self):
        """The writer panel's forms join the export form's column (DP-154)."""
        panel = getattr(self, '_writerPanel', None)
        own = tuple(super().aligned_forms())
        return own + (panel.forms() if panel is not None else ())

    def _exportFolder(self):
        try:
            return str(Path(self._client.case_path) / 'exports')
        except Exception:                                    # noqa: BLE001
            return ''

    def sessionController(self):
        """The Generate page's session controller, once a stage has run."""
        from foammesh.view.gmsh_workflow.compute_page import GmshComputePage

        window = getattr(app, 'window', None)
        if window is None:
            return None
        for page in window.findChildren(GmshComputePage):
            controller = getattr(page, '_generateController', None)
            if controller is not None:
                return controller
        # SG15A: the controller the Export page made to load the run's mesh.
        from foammesh.view.gmsh_workflow.compute_page import (
            residentGenerateController)
        return residentGenerateController(self._client)

    def refresh(self) -> None:
        super().refresh()
        panel = getattr(self, '_writerPanel', None)
        if panel is not None:
            panel.setRunAvailable(self.canExport())
            if panel.isVisible():
                panel.refresh()
                panel.loadRunMesh()
