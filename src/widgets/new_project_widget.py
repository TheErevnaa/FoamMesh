#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
from pathlib import Path

from PySide6.QtCore import QEvent, QRegularExpression, Qt, Signal
from PySide6.QtGui import QFontMetrics, QRegularExpressionValidator
from PySide6.QtWidgets import QFileDialog, QSizePolicy, QWidget

from .new_project_widget_ui import Ui_NewProjectWidget


class NewProjectWidget(QWidget):
    pathChanged = Signal(Path)

    #: `setupUi` resizes this widget before it has built the labels, and Qt
    #: delivers that resize synchronously, so `resizeEvent` can run before
    #: `__init__` has set anything up. Empty here means "nothing to draw yet".
    _destinationText = ''
    #: See `setDestinationShown`. A class attribute for the same reason.
    _destinationShown = True

    def __init__(self, parent, path: Path = None, suffix=None, name: str = ''):
        super().__init__(parent)

        self._ui = Ui_NewProjectWidget()
        self._ui.setupUi(self)

        self._suffix = ''

        self._validatedPath = None

        self._dialog = None

        # R125. Whether the destination is a folder to create or a single
        # file to write, and the extension that file carries. The widget
        # cannot know; whoever put it in a dialog does. See setDestination().
        self._destinationIsFile = False
        self._destinationSuffix = ''
        self._destinationText = ''

        # G7. These labels carry whatever the user is typing plus the whole
        # destination path, and an un-wrapped QLabel asks its layout for the
        # width of its text. So the Export dialog grew a little with every
        # keystroke in Project Name and the OK/Cancel buttons walked sideways
        # under the cursor. An ignored width hint means the labels take the
        # width they are given instead of demanding their own.
        for label in (self._ui.locationDescription,
                      self._ui.validationMessage):
            label.setSizePolicy(QSizePolicy.Policy.Ignored,
                                QSizePolicy.Policy.Minimum)
        self._ui.validationMessage.setWordWrap(True)
        # R43/R105/R125. The destination used to be told in two labels sitting
        # side by side in one row: "Will be created in <location>/" wrapped to
        # four lines inside the group box, and the folder-name label beside it
        # was drawn outside the box entirely, over the dialog background. Both
        # halves now go into one label, on one line, elided in the middle so
        # the tail of the path -- the part being checked -- stays readable and
        # the dialog does not grow a line every time the path gets longer.
        self._ui.locationDescription.setWordWrap(False)

        # W-O1. The two row labels were built by Designer without a buddy, so
        # nothing told Qt -- or a screen reader, or a census of the settings
        # column -- which control each one names. They are names, and now say
        # so.
        self._ui.label.setBuddy(self._ui.projectName)
        self._ui.label_2.setBuddy(self._ui.projectLocation)

        #: W-O1. Whether the derived destination sentence is drawn on the
        #: form. It is a readout of the two fields above it rather than a
        #: setting, so the export *step* -- which lives in the settings
        #: column, where Plan 33 section 1 allows settings and nothing else --
        #: turns it off and reads the same sentence off the location field's
        #: tooltip and off the step's Details route. The dialogs keep it:
        #: a dialog is not the settings column.
        self._destinationShown = True

        #: DP-680. Whether a destination that is already on disk is accepted,
        #: to be replaced. Off unless a host with an overwrite control turns
        #: it on; the host confirms and the export checks what it replaces.
        self._overwriteAllowed = False

        # DP-202. R43 folded the folder name into the line above, and
        # left the label it came out of on the form, hidden. Every
        # keystroke in Project Name then wrote the folder name twice:
        # once into the line the reader reads and once into a label
        # nobody could see. The hidden half is gone from the form.

        self._ui.projectName.setValidator(
            QRegularExpressionValidator(QRegularExpression(r'[^\s\\/:*?"<>|][^\\/:*?"<>|]*')))

        # R17. The field that names the folder the case lands in was too
        # narrow to show it: the form gave it whatever was left after the
        # `Project Location` label and the `Browse...` button, which on the
        # Save case dialog was a few characters. A real case path is what has
        # to fit, so the field asks for a width measured in the font it draws
        # with rather than accepting the remainder.
        self._ui.projectLocation.setMinimumWidth(
            QFontMetrics(self._ui.projectLocation.font()).averageCharWidth()
            * 48)

        location = str(path.resolve() if path and path.exists() else Path.home())
        self._ui.projectLocation.setText(location)
        self._showLocationTail()
        if name:
            self._ui.projectName.setText(name)
        self._updateProjectPath()

        self._connectSignalsSlots()

        # R42. The folder a user types into Project Location is usually made
        # in a file manager, i.e. while this window is in the background, so
        # the moment to look at the disk again is when it comes back to the
        # front.
        window = self.window()
        if window is not None:
            window.installEventFilter(self)

        if suffix is None:
            self._ui.formLayout.removeRow(self._ui.suffixField)
        else:
            self._ui.suffix.addItem(suffix, suffix)
            self._ui.suffix.addItem(self.tr('<no suffix>'), '')

    def formLayout(self):
        """The form the location and name rows sit in, for a host that
        aligns its label column with the forms around it (DP-154)."""
        return self._ui.formLayout

    def projectPath(self):
        return self._validatedPath

    def validationMessage(self):
        return self._ui.validationMessage.text()

    def setFixedProjectPath(self, path):
        self._ui.projectName.setText(path.name)
        self._ui.projectLocation.setText(str(path.parent.resolve()))
        self._ui.validationMessage.hide()
        self.setEnabled(False)

    def hideValidationMessage(self):
        self._ui.validationMessage.hide()

    def setDestinationShown(self, shown: bool) -> None:
        """Draw the derived destination sentence, or keep it off the form.

        W-O1. The sentence restates the two fields above it and stands on the
        form before the reader has touched anything, which is what Plan 33
        section 1 takes out of the settings column. Nothing is lost by turning
        it off: `_renderDestination` puts the whole sentence on the location
        field as its tooltip either way, and the export step already carries
        the destination as a row of its Details view.
        """
        self._destinationShown = bool(shown)
        self._ui.locationDescription.setVisible(self._destinationShown)
        self._renderDestination()

    def setDestination(self, kind: str, suffix: str = ''):
        """Say whether this destination is a folder or a single file (R125).

        The SU2 export dialog's preview read "Will be created in
        ...\\run6\\exports\\" with `venturi_gmsh_su2` beside it as a folder
        segment; what appeared on disk was one file,
        `exports/venturi_gmsh_su2.su2`. The preview described a directory
        that is never made, because this widget was only ever told about the
        new-case route, where a folder really is what gets created.
        """
        self._destinationIsFile = (kind == 'file')
        self._destinationSuffix = suffix or ''
        self._updateDestinationText()
        # DP-639. The format does decide whether the typed name is free: an
        # SU2 export of `duct` writes `duct.su2`, so that is the name to look
        # for on disk. Re-checked here without re-emitting `pathChanged`
        # behind the dialog that is asking; it reads `projectPath()` itself.
        self._validate()

    def setOverwriteAllowed(self, allowed: bool) -> None:
        """Accept an existing destination of the right kind (DP-680).

        Only the kind this export writes is accepted -- a folder for a case,
        a file for a file format -- and the row says it will be replaced.
        """
        self._overwriteAllowed = bool(allowed)
        self._updateProjectPath()

    def revalidate(self):
        """Re-check the typed location against the disk (R42).

        Validation only ever ran on `textChanged`, so a Project Location that
        did not exist at the moment it was typed stayed invalid after the
        folder was created: OK was disabled, still looked enabled, and two
        clicks on it did nothing at all -- no message, nothing written,
        nothing logged. Typing one character into Project Name and deleting
        it again was the only way to make the next identical click export.
        """
        self._updateProjectPath()

    def eventFilter(self, watched, event):
        # R42. See revalidate(): coming back from the file manager is the
        # event that says "look at the disk again".
        if event.type() == QEvent.Type.WindowActivate:
            self.revalidate()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # R43/R105. The elision has to follow the width the layout hands out.
        if self._destinationText:
            self._renderDestination()

    def _connectSignalsSlots(self):
        self._ui.projectName.textChanged.connect(self._updateProjectPath)
        # E13. The location read-only but framed exactly like the name beside
        # it, so typing into it went nowhere visible -- the keystrokes landed
        # in Project Name and produced `pipe_gmsh_exportDFoamMeshDemo`. It is
        # a path field; let it take a path.
        self._ui.projectLocation.textChanged.connect(self._updateProjectPath)
        self._ui.suffix.currentIndexChanged.connect(self._suffixChanged)
        self._ui.select.clicked.connect(self._selectLocation)

    def _suffixChanged(self):
        self._suffix = self._ui.suffix.currentData()
        self._updateProjectPath()

    def _selectLocation(self):
        self._dialog = QFileDialog(self, self.tr('Select location'), self._ui.projectLocation.text())
        self._dialog.setFileMode(QFileDialog.FileMode.Directory)
        self._dialog.fileSelected.connect(self._locationSelected)
        self._dialog.open()

    def _typedLocation(self):
        """The typed location as an absolute path, or `''` if none is typed.

        Plan 33 W-P. This is the one place where two typed strings become a
        path, and it serves the Save case dialog as well as every export, so
        it is the seam where the destination is made absolute. A location
        typed as `exports` used to travel as `exports`: the record named it,
        the sentence under these rows named it, and the runtime was handed
        it -- three different directories the moment anything ran from
        somewhere else, and nothing the WSL profile could translate at all.
        `abspath` rather than `resolve`, because the reader is owed the path
        they typed anchored to a root, not a symlink chased to its target.
        """
        location = self._ui.projectLocation.text()
        return os.path.abspath(location) if location else ''

    def _updateProjectPath(self):
        self._updateDestinationText()
        self._validate()
        self.pathChanged.emit(self._validatedPath)

    def _validate(self):
        location = self._typedLocation()
        folderName = f'{self._ui.projectName.text()}{self._suffix}' if self._ui.projectName.text() else ''
        path = Path(location) / folderName
        # DP-639 (field audit 0924 D-SH-07). A file destination writes the
        # name with its suffix, appended exactly as the destination sentence
        # appends it; checking the bare name let an existing `duct.su2`
        # through (and refused `duct` for a folder the export never touches).
        written = path
        if (self._destinationIsFile and self._destinationSuffix
                and not folderName.endswith(self._destinationSuffix)):
            written = path.with_name(path.name + self._destinationSuffix)

        self._validatedPath = None

        if not self._ui.projectName.text():
            self._ui.validationMessage.clear()
        elif not Path(location).is_dir():
            self._ui.validationMessage.setText(
                self.tr('{0} is not a folder.').format(location))
        elif (written.exists() and self._overwriteAllowed
              and (written.is_file() if self._destinationIsFile
                   else written.is_dir())):
            # DP-680. The host's "Overwrite existing" is on: the destination
            # is accepted and the row says what will happen to it.
            self._ui.validationMessage.setText(
                self.tr('{0} exists and will be replaced.').format(written))
            self._validatedPath = path
        elif written.exists():
            # Without the overwrite control an existing destination is
            # refused here, and the export form offers the next free name
            # (DP-562).
            self._ui.validationMessage.setText(
                self.tr('{0} already exists.').format(written))
        else:
            self._ui.validationMessage.clear()
            self._validatedPath = path

    def _updateDestinationText(self):
        # Plan 33 W-P. The same absolute location the path is built from, so
        # the sentence and the emitted path cannot name different places.
        location = self._typedLocation()
        folderName = f'{self._ui.projectName.text()}{self._suffix}' if self._ui.projectName.text() else ''
        # E14. This line read `Project will be created in <Project Location>`
        # and went on reading it after a location had been chosen, so the one
        # sentence that says where the case lands never said where.
        #
        # R125. And it named a folder whatever the format was writing, so an
        # SU2 export announced a directory and produced a file. The whole
        # destination is spelled out once, here, including the extension the
        # file will carry. The path goes last in every one of these sentences
        # so that the middle elision in `_renderDestination` eats the sentence
        # and the middle of the path, never the tail being checked.
        if not location:
            self._destinationText = self.tr('Choose a location.')
        elif not folderName:
            self._destinationText = self.tr(
                'Will be created in {0}{1}').format(location, os.sep)
        elif self._destinationIsFile:
            # The suffix is only appended when the typed name does not carry
            # it already, exactly as the export dialog appends it to the path
            # it hands to the writer; otherwise the preview promises
            # `duct.su2.su2`.
            fileName = folderName
            if (self._destinationSuffix
                    and not fileName.endswith(self._destinationSuffix)):
                fileName += self._destinationSuffix
            self._destinationText = self.tr('Will write the file {0}') \
                .format(f'{location}{os.sep}{fileName}')
        else:
            self._destinationText = self.tr('Will create the folder {0}') \
                .format(f'{location}{os.sep}{folderName}')
        self._renderDestination()

    def _renderDestination(self):
        """Put the destination sentence on one line, whatever its length.

        R43/R105/R125. Wrapping it produced four lines inside a group box
        sized for one, grew the dialog as the path was typed, and cut the
        sentence off at `...\\scratch` -- the tail of the path is the half
        the user is checking. Middle elision keeps both ends visible, and
        the whole path stays available as the tooltip.
        """
        label = self._ui.locationDescription
        text = self._destinationText
        label.setToolTip(text)
        # W-O1. The same sentence, on the control it is derived from, so a
        # host that keeps the line off the form keeps the fact.
        self._ui.projectLocation.setToolTip(text)
        if not self._destinationShown:
            label.setVisible(False)
            return
        width = label.contentsRect().width()
        rendered = (QFontMetrics(label.font()).elidedText(
            text, Qt.TextElideMode.ElideMiddle, width) if width > 0 else text)
        if label.text() != rendered:
            label.setText(rendered)

    def _showLocationTail(self):
        """Scroll the location field to its end (R17).

        A QLineEdit shows the head of a path that does not fit, and the head
        of `D:/OneDrive/Documents/FoamMesh/cases` is the half the user
        already knows. The last segment is the folder the case is created
        inside, so that is the end that must be on screen after the path is
        set for them.
        """
        self._ui.projectLocation.setCursorPosition(
            len(self._ui.projectLocation.text()))

    def _locationSelected(self, dir):
        self._ui.projectLocation.setText(str(Path(dir).resolve()))
        self._showLocationTail()
        self._updateProjectPath()
