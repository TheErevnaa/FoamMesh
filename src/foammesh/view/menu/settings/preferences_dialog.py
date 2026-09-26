"""Settings > Preferences: the application-wide choices in one dialog.

DP-759 (gui review 0925, top-bar-and-settings P1 item 12). The Settings menu
used to offer only the theme, while the OpenFOAM runtime, the diagnostic
budget and the geometry qualification mode had setters nothing called. Each
page here reads its current value from ``AppSettings`` and writes it back
through the same setter the rest of the application reads from, so nothing
new is stored and nothing is read from a second place.

Scale is not offered: ``AppSettings.getScale`` has no reader, so a control
for it would change nothing.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
    QLineEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)

from foammesh.core.geometry.diagnostics.budget import BudgetPolicy
from foammesh.core.naming import humanise_option
from foammesh.core.quality.qualification import QualificationMode
from foammesh.view.theming.metrics import (
    CompactDoubleSpinBox, CompactSpinBox, unit_cell,
)
from foammesh.view.theming.theme_manager import ThemeMode

_BUDGET_LABELS = {
    BudgetPolicy.ABORT: 'Stop the check',
    BudgetPolicy.WARN_ONLY: 'Warn and let it finish',
    BudgetPolicy.NEVER_LIMIT: 'No time limit',
}
_BUDGET_NOTES = {
    BudgetPolicy.ABORT: 'A surface check that runs past the limit stops and is reported as not evaluated.',
    BudgetPolicy.WARN_ONLY: 'A surface check that runs past the limit is reported, then allowed to finish.',
    BudgetPolicy.NEVER_LIMIT: 'Surface checks run for as long as they take.',
}
_MODE_NOTES = {
    QualificationMode.REPORT_ONLY: 'Geometry checks are run and reported, and block nothing.',
    QualificationMode.ENFORCING: 'A failing geometry check can stop a meshing run or an export.',
}


def _note(text: str = '') -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setProperty('foammeshTone', 'muted')
    return label


class PreferencesDialog(QDialog):
    """One page per preference; OK and Apply write, Cancel leaves them."""

    #: A search for the WSL runtime finished (a DetectedRuntime, or None).
    runtimeFound = Signal(object)

    def __init__(self, parent=None, *, settings, themeManager,
                 applyRuntime=None, findRuntime=None, privacy=None):
        super().__init__(parent)
        self.setObjectName('preferencesDialog')
        self.setWindowTitle(self.tr('Preferences'))
        self._settings = settings
        self._themes = themeManager
        self._applyRuntime = applyRuntime
        self._findRuntime = findRuntime
        self.runtimeFound.connect(self._showFoundRuntime)
        self._runtime = settings.getOpenFoamRuntime()

        self.pages = QTabWidget(self)
        self.pages.setObjectName('preferencesPages')
        self.pages.addTab(self._appearancePage(), self.tr('Appearance'))
        self.pages.addTab(self._runtimePage(), self.tr('OpenFOAM runtime'))
        self.pages.addTab(self._diagnosticsPage(), self.tr('Diagnostics'))
        self.pages.addTab(self._qualificationPage(), self.tr('Geometry qualification'))
        if privacy is not None:
            self.pages.addTab(self._privacyPage(privacy), self.tr('Privacy'))

        self.problem = QLabel(self)
        self.problem.setObjectName('preferencesProblem')
        self.problem.setWordWrap(True)
        self.problem.setProperty('foammeshStatus', 'error')
        self.problem.hide()

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self.apply)

        # Every page's tab stays in sight; none hides behind scroll arrows.
        self.setMinimumWidth(self.pages.tabBar().sizeHint().width() + 48)
        layout = QVBoxLayout(self)
        layout.addWidget(self.pages)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)

    # Pages ----------------------------------------------------------------
    def _appearancePage(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.themeCombo = QComboBox(page)
        self.themeCombo.setObjectName('preferencesTheme')
        for mode in (ThemeMode.SYSTEM, ThemeMode.LIGHT, ThemeMode.DARK):
            self.themeCombo.addItem(humanise_option(mode.value), mode.value)
        self.themeCombo.setCurrentIndex(
            self.themeCombo.findData(ThemeMode(self._themes.mode).value))
        form.addRow(self.tr('Theme'), self.themeCombo)
        form.addRow(_note(self.tr('System follows the light or dark setting of the desktop.')))
        return page

    def _runtimePage(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        runtime = self._runtime
        self.distroEdit = QLineEdit(str(runtime['wsl_distro']), page)
        self.distroEdit.setObjectName('preferencesWslDistro')
        self.userEdit = QLineEdit(str(runtime['wsl_user']), page)
        self.userEdit.setObjectName('preferencesWslUser')
        self.bashrcEdit = QLineEdit(str(runtime['bashrc']), page)
        self.bashrcEdit.setObjectName('preferencesBashrc')
        self.stageTimeout = CompactSpinBox(page)
        self.stageTimeout.setObjectName('preferencesStageTimeout')
        self.stageTimeout.setRange(1, 7 * 24 * 3600)
        self.stageTimeout.setValue(int(runtime['stage_timeout']))
        form.addRow(self.tr('WSL distribution'), self.distroEdit)
        form.addRow(self.tr('WSL user'), self.userEdit)
        form.addRow(self.tr('OpenFOAM 13 bashrc'), self.bashrcEdit)
        form.addRow(self.tr('Stage time limit'), unit_cell(self.stageTimeout, 's', page))
        if self._findRuntime is not None:
            self.findRuntimeButton = QPushButton(self.tr('Find automatically'), page)
            self.findRuntimeButton.setObjectName('preferencesFindRuntime')
            self.findRuntimeButton.setToolTip(self.tr(
                'Look through the WSL distributions for OpenFOAM 13 and Gmsh, '
                'trying the user foamuser first'))
            self.findRuntimeButton.clicked.connect(self.findRuntime)
            self._findNote = _note()
            self._findNote.setObjectName('preferencesFindRuntimeNote')
            form.addRow(self.findRuntimeButton, self._findNote)
        form.addRow(_note(self.tr(
            'The OpenFOAM 13 environment is checked again as soon as these are applied.')))
        return page

    def _diagnosticsPage(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        policy, seconds = self._settings.getDiagnosticBudget()
        self.budgetPolicy = QComboBox(page)
        self.budgetPolicy.setObjectName('preferencesBudgetPolicy')
        for option in BudgetPolicy:
            self.budgetPolicy.addItem(self.tr(_BUDGET_LABELS[option]), option.value)
        self.budgetPolicy.setCurrentIndex(self.budgetPolicy.findData(BudgetPolicy(policy).value))
        self.budgetSeconds = CompactDoubleSpinBox(page)
        self.budgetSeconds.setObjectName('preferencesBudgetSeconds')
        self.budgetSeconds.setRange(1.0, 24 * 3600.0)
        self.budgetSeconds.setDecimals(0)
        self.budgetSeconds.setValue(float(seconds))
        self._budgetNote = _note()
        form.addRow(self.tr('When a surface check runs long'), self.budgetPolicy)
        form.addRow(self.tr('Time limit per check'), unit_cell(self.budgetSeconds, 's', page))
        form.addRow(self._budgetNote)
        self.budgetPolicy.currentIndexChanged.connect(self._showBudget)
        self._showBudget()
        return page

    def _qualificationPage(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.qualificationMode = QComboBox(page)
        self.qualificationMode.setObjectName('preferencesQualificationMode')
        for mode in QualificationMode:
            self.qualificationMode.addItem(humanise_option(mode.value), mode.value)
        self.qualificationMode.setCurrentIndex(self.qualificationMode.findData(
            QualificationMode(self._settings.getQualificationMode()).value))
        self._modeNote = _note()
        form.addRow(self.tr('Geometry checks'), self.qualificationMode)
        form.addRow(self._modeNote)
        self.qualificationMode.currentIndexChanged.connect(self._showMode)
        self._showMode()
        return page

    def _privacyPage(self, privacy) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(_note(self.tr(
            'Choose whether FoamMesh may send anonymous usage statistics.')))
        self.privacyButton = QPushButton(self.tr('Usage statistics…'), page)
        self.privacyButton.setObjectName('preferencesPrivacy')
        self.privacyButton.clicked.connect(lambda: privacy())
        layout.addWidget(self.privacyButton, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        return page

    def _showBudget(self):
        policy = BudgetPolicy(self.budgetPolicy.currentData())
        self.budgetSeconds.setEnabled(policy is not BudgetPolicy.NEVER_LIMIT)
        self._budgetNote.setText(self.tr(_BUDGET_NOTES[policy]))

    def _showMode(self):
        self._modeNote.setText(self.tr(
            _MODE_NOTES[QualificationMode(self.qualificationMode.currentData())]))

    # Finding the runtime ---------------------------------------------------
    def findRuntime(self):
        """Search WSL off the GUI thread; the fields are filled when it answers."""
        self.findRuntimeButton.setEnabled(False)
        self._findNote.setText(self.tr(
            'Looking through WSL; a stopped distribution can take 20 s to start…'))

        def search():
            try:
                found = self._findRuntime()
            except Exception:                           # noqa: BLE001
                found = None
            self.runtimeFound.emit(found)

        threading.Thread(target=search, name='find-openfoam-runtime',
                         daemon=True).start()

    def _showFoundRuntime(self, found):
        self.findRuntimeButton.setEnabled(True)
        if found is None:
            self._findNote.setText(self.tr(
                'No WSL distribution with OpenFOAM 13 (/opt/openfoam13) was found.'))
            return
        self.distroEdit.setText(found.distribution)
        self.userEdit.setText(found.user)
        self.bashrcEdit.setText(found.bashrc)
        self._findNote.setText(self.tr('Found {0}. Apply or OK to use it.').format(
            found.describe()))

    # Writing --------------------------------------------------------------
    def _runtimeValues(self) -> dict:
        return {
            'profile_id': self._runtime['profile_id'],
            'wsl_distro': self.distroEdit.text().strip(),
            'wsl_user': self.userEdit.text().strip(),
            'bashrc': self.bashrcEdit.text().strip(),
            'stage_timeout': int(self.stageTimeout.value()),
        }

    def _refusal(self, runtime: dict) -> str:
        for key, name in (('wsl_distro', self.tr('WSL distribution')),
                          ('wsl_user', self.tr('WSL user')),
                          ('bashrc', self.tr('OpenFOAM 13 bashrc'))):
            if not runtime[key]:
                return self.tr('Enter the {0}.').format(name)
        return ''

    def apply(self) -> bool:
        """Write every page; return False, and say why, if one is refused."""
        runtime = self._runtimeValues()
        refusal = self._refusal(runtime)
        self.problem.setText(refusal)
        self.problem.setVisible(bool(refusal))
        if refusal:
            self.pages.setCurrentIndex(1)
            return False

        theme = self.themeCombo.currentData()
        if theme != ThemeMode(self._themes.mode).value:
            self._themes.set_mode(theme)

        current = {key: self._runtime.get(key) for key in runtime}
        current['stage_timeout'] = int(current['stage_timeout'])
        if runtime != current:
            self._settings.updateOpenFoamRuntime(**runtime)
            self._runtime = dict(runtime)
            if self._applyRuntime is not None:
                self._applyRuntime()

        self._settings.updateDiagnosticBudget(
            BudgetPolicy(self.budgetPolicy.currentData()),
            float(self.budgetSeconds.value()))
        self._settings.setQualificationMode(
            QualificationMode(self.qualificationMode.currentData()))
        return True

    def accept(self):
        if self.apply():
            super().accept()
