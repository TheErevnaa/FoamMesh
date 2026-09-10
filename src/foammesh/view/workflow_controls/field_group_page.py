"""A page built from a set of registry field ids, and nothing else.

Plan 26 WP4. Eleven fields -- five ``mesh/execution/*`` and six
``mesh/intent`` -- declare a ``ui_location`` and no view module renders any of
them. ``maxCpuCores`` was the field the meshing run read for its rank count,
so until Plan 25 rewired it every mesh ran on one core whatever the Parallel
Environment dialog said: a validated, serialised, *consumed* field that no
human could reach.

The page is deliberately generic. Hand-placing eleven widgets would put the
schema and the desktop back out of step the moment a field is added, which is
the failure mode the AF2 registry exists to prevent -- so the page takes field
ids, asks the registry what they are, and refuses to render anything the schema
does not publish.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from foammesh.core.facade.errors import ValidationFailedError
from foammesh.view.facade_client import query, submit

from .conditional_fields import refresh_applicability
from .field_widgets import FieldEditor


class FieldGroupPage(QWidget):
    """One titled group of registry-backed fields, applied through the facade."""

    #: Field ids this page renders. Subclasses name their own.
    field_ids: tuple[str, ...] = ()
    #: Navigation token that routes to this page.
    token: str = ''
    heading: str = ''
    #: Shown under the heading. Say what the settings do and, where it matters,
    #: what does *not* read them -- a page that implies more than it delivers
    #: is the defect this plan is named after.
    purpose: str = ''
    #: Optional caveat rendered distinctly from the purpose.
    caveat: str = ''

    dirtyChanged = Signal(bool)

    def __init__(self, facade_client, parent=None):
        super().__init__(parent)
        self._client = facade_client
        self._editors: dict[str, FieldEditor] = {}
        self._pending: dict[str, object] = {}
        self.setObjectName(self.token.replace('.', '_') + 'Page')
        self.setAccessibleName(self.heading or self.token)

        outer = QVBoxLayout(self)
        title = QLabel(self.heading or self.token, self)
        title.setObjectName('fieldGroupHeading')
        outer.addWidget(title)
        if self.purpose:
            purpose = QLabel(self.purpose, self)
            purpose.setWordWrap(True)
            outer.addWidget(purpose)
        if self.caveat:
            caveat = QLabel(self.caveat, self)
            caveat.setObjectName('fieldGroupCaveat')
            caveat.setWordWrap(True)
            caveat.setProperty('foammeshStatus', 'warning')
            outer.addWidget(caveat)

        body = QWidget(self)
        self._form = QGroupBox(self.tr('Settings'), body)
        QFormLayout(self._form)
        body_layout = QVBoxLayout(body)
        body_layout.addWidget(self._form)
        body_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        # C5. The page scrolled sideways and so did the table inside it:
        # two horizontal scrollbars stacked a few pixels apart, and the
        # outer one moved the headings along with the row it was meant to
        # reveal. Only the innermost scrollable thing should scroll.
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._apply = QPushButton(self.tr('Apply'), self)
        self._revert = QPushButton(self.tr('Revert'), self)
        self._apply.clicked.connect(self.apply)
        self._revert.clicked.connect(self.revert)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        for button in (self._revert, self._apply):
            buttons.addWidget(button)
        outer.addLayout(buttons)

        self.build()

    # -- construction ------------------------------------------------------ #

    def build(self) -> None:
        layout = self._form.layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._editors.clear()
        for field_id in self.field_ids:
            try:
                descriptor = self._client.descriptor(field_id)
            except (KeyError, ValidationFailedError, AttributeError):
                # Gate 10 forbids a control the schema cannot back. Say so
                # rather than render an editor that writes nowhere.
                notice = QLabel(
                    self.tr('%s has no schema descriptor and cannot be '
                            'edited here.') % field_id, self)
                notice.setWordWrap(True)
                notice.setObjectName('unbackedFieldNotice')
                layout.addRow(notice)
                continue
            editor = FieldEditor(descriptor, self,
                                 choices=self.choices_for(field_id))
            editor.valueChanged.connect(self._on_changed)
            row = QHBoxLayout()
            row.addWidget(editor.editor, 1)
            if descriptor.unit:
                row.addWidget(editor.unit_label)
            container = QWidget(self._form)
            container.setLayout(row)
            layout.addRow(editor.label, container)
            self._editors[field_id] = editor
        self.reload()

    def choices_for(self, field_id: str):
        """Runtime-narrowed rows for one field, or ``None`` for the schema's.

        A subclass overrides this when the schema's vocabulary is wider than
        what the machine in front of the user can do.
        """
        return None

    # -- values ------------------------------------------------------------ #

    def reload(self) -> None:
        self._pending.clear()
        if self._editors:
            values = self._client.field_values(tuple(self._editors))
            for field_id, editor in self._editors.items():
                editor.set_value(
                    values.get(field_id, editor.descriptor.default))
            # CP-09 item 4. A setting the configuration cannot reach stays on
            # the page, greyed and explained, and out of the patch.
            self._inactive = refresh_applicability(
                self._client, self._editors, self._pending)
        self._set_dirty(False)

    @property
    def is_dirty(self) -> bool:
        return bool(self._pending)

    def pending_patch(self) -> dict:
        return dict(self._pending)

    def _on_changed(self, field_id: str, value) -> None:
        self._pending[field_id] = value
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._apply.setEnabled(dirty)
        self._revert.setEnabled(dirty)
        self.dirtyChanged.emit(dirty)

    def apply(self):
        if not self._pending:
            return None

        def applied(result):
            if getattr(result, 'status', 'accepted') == 'accepted':
                self.reload()
                return
            QMessageBox.warning(
                self, self.tr('Update failed'),
                str(getattr(result, 'message', '')
                    or self.tr('The facade rejected the edit.')))

        # C31-12. Scheduled, not run on the GUI thread; `applied` is the tail
        # of the old body and still runs after the write lands.
        return submit(self._client, 'configuration.patch',
                      {'patch': self.pending_patch()}, then=applied)

    def revert(self) -> None:
        self.reload()


class ExecutionPreferencesPage(FieldGroupPage):
    """The thirteen ``mesh/execution/*`` settings that govern every run.

    WP-13 / F-34. It said "five" from the day it was written and grew three
    decomposition fields since; the count is in the docstring because the page
    is the answer to "where do I change how this case uses the machine", and a
    reader who counts five and finds thirteen stops trusting the rest of it.
    Plan 31 (parallel.decompose_extras) added the last five: the ``constraints``
    block of ``decomposeParDict`` and its weight field.
    """

    #: How long the page is willing to wait for the runtime probe before
    #: rendering. The probe boots a WSL distribution -- about half a minute
    #: cold, measured in Plan 30 WP-08 -- and a page that waits for it is the
    #: freeze that plan was written to remove. So it renders un-narrowed and
    #: picks the answer up on the next visit.
    probe_wait_seconds = 2.0

    #: False until availability rests on something the runtime said.
    _runtime_answered = False
    _rebuilding = False

    token = 'preferences.execution'
    field_ids = (
        'mesh.execution.mode',
        'mesh.execution.max_cpu_cores',
        'mesh.execution.max_memory_bytes',
        'mesh.execution.allow_distributed',
        'mesh.execution.preferred_backend',
        # Plan 26 WP9.2. The decomposition method was a source constant in two
        # writers. Given that snappy's refinement is decomposition-sensitive it
        # is a meshing input, and it belongs beside the core count that decides
        # how many cuts there are.
        'mesh.execution.decomposition_method',
        'mesh.execution.decomposition_order',
        'mesh.execution.decomposition_cells',
        # Plan 31 (parallel.decompose_extras). `decomposeParDict` has a
        # `constraints {}` block that says what the partitioner may not cut,
        # and this product wrote none of it -- so a case with a faceZone or a
        # baffle could be split straight through the thing that had to stay
        # whole, with the failure surfacing later in the solver rather than
        # here. Every one is off or empty by default, so a case that never
        # opens this page writes the dictionary it always wrote.
        'mesh.execution.preserve_face_zones',
        'mesh.execution.preserve_baffles',
        'mesh.execution.preserve_patches',
        'mesh.execution.preserve_refinement_history',
        'mesh.execution.decomposition_weight_field',
    )
    heading = 'Execution'

    def choices_for(self, field_id: str):
        """Offer only decomposition methods the selected runtime can run.

        Plan 31 CP-07 item 4. The schema's enum is what the writer can write;
        it is not what the installed OpenFOAM has. Measured on this machine's
        OpenFOAM 13, ``libmetisDecomp.so`` exists only under
        ``platforms/linux64GccDPInt32Opt/lib/dummy`` -- the directory OpenFOAM
        builds stubs in that abort when a case asks for them -- so ``metis``
        was a row the page offered and the run could not honour.

        Refused rows stay visible and disabled with the reason attached: a
        method that vanishes from the list reads as a feature this product
        never had, and the user has no way to learn that the fix is on the
        runtime side. When the probe has not answered yet nothing is narrowed,
        because "not asked" is not "not there".
        """
        if field_id != 'mesh.execution.decomposition_method':
            return None
        try:
            result = query(self._client,
                           'mesh.execution.decomposition_methods',
                           {'timeout_seconds': self.probe_wait_seconds})
            document = getattr(result, 'payload', None) or {}
            methods = document.get('methods') or ()
        except Exception:  # noqa: BLE001 - a page must still render
            self._runtime_answered = True
            return None
        if not methods:
            self._runtime_answered = True
            return None
        probed = bool(document.get('probed'))
        self._runtime_answered = probed
        return tuple(
            (item['name'], item['name'],
             item.get('reason')
             or ('available in the selected runtime' if probed
                 else 'the runtime has not been probed'),
             bool(item.get('available')))
            for item in methods)

    def reload(self) -> None:
        """Re-read the values, and take the runtime's answer if it arrived.

        The first render happens before a cold runtime can answer, so the
        combo is un-narrowed then. The probe keeps running on its worker
        thread; the next time the page is shown the answer is in the cache and
        the rows are rebuilt against it.
        """
        super().reload()
        if self._runtime_answered or self._rebuilding or not self._editors:
            return
        self.choices_for('mesh.execution.decomposition_method')
        if not self._runtime_answered:
            return
        self._rebuilding = True
        try:
            self.build()
        finally:
            self._rebuilding = False

    purpose = (
        'How this case is allowed to use the machine. These bound every '
        'meshing run: the core ceiling here is what the run reads for its '
        'rank count, so a Parallel Environment asking for more than this '
        'gets this.')
    caveat = (
        'Changing the core count can change the mesh. Snappy\'s refinement '
        'is decomposition-sensitive: a measured duct case moved +12.4% in '
        'cell count between serial and 16 ranks, while a 609k-cell annulus '
        'moved -0.04% on the same ranks. Only a serial mesh is independent '
        'of the core count.')


class MeshIntentPage(FieldGroupPage):
    """What this mesh is for: the target solver, and nothing else.

    Plan 30 WP-09 (F-23). This page used to carry six ``mesh/intent`` sizing
    fields -- target size, minimum size, cell ceiling, growth rate and two
    policies -- that were validated, serialised and built into the engine
    contract, and that neither shipped engine ever read: Gmsh sizes from its
    own ``gmsh/globalSizing`` block and snappy from its base grid and
    refinement levels. The page said so in a caveat, which made it a page
    whose own text told the reader not to use it. The fields are gone from
    the schema now, and a load-time migration drops them from cases that have
    them, so there is nothing left to caveat.

    What remains is the one genuinely engine-agnostic intent there is: which
    solver will read the mesh. It decides which engines are offered and which
    quality operation qualifies the result, so it is a statement of intent
    rather than a setting, and it belongs here rather than only halfway down
    the Meshing Method page.

    It is not a ``FieldEditor`` row: ``mesh.target_solver`` is read-only in
    the registry precisely because changing it invalidates published verdicts
    and can strand the selected engine, so it is written through
    ``mesh.target_solver.set`` -- the operation that does that work -- and not
    through ``configuration.patch``.
    """

    token = 'workflow.mesh.intent'
    #: No schema fields. The solver is set through its own operation.
    field_ids: tuple[str, ...] = ()
    #: The two solvers a case can be meshed for. ``unselected`` is a state,
    #: not an option, and is shown by leaving both unchecked.
    SOLVER_LABELS = {
        'openfoam': 'OpenFOAM',
        'su2': 'SU2',
    }
    heading = 'Mesh Intent'
    purpose = (
        'Which solver this mesh is for. It decides which meshing engines are '
        'offered and which quality check qualifies the result, and it is '
        'recorded with the case so the plan states what the mesh was made '
        'for. Changing it after a mesh exists marks the published quality '
        'verdict stale.')
    caveat = ''

    def __init__(self, facade_client, parent=None):
        self._solverButtons: dict[str, QRadioButton] = {}
        self._solverGroup = None
        self._solverReason = None
        super().__init__(facade_client, parent)

    def build(self) -> None:
        super().build()
        # Nothing renders in "Settings" and an empty titled box reads as a
        # section that failed to load.
        self._form.setVisible(False)
        self._apply.setVisible(False)
        self._revert.setVisible(False)

        box = QGroupBox(self.tr('Mesh for'), self)
        box.setObjectName('meshIntentTargetSolverBox')
        box_layout = QVBoxLayout(box)
        self._solverGroup = QButtonGroup(self)
        for solver_id, label in self.SOLVER_LABELS.items():
            button = QRadioButton(label, self)
            button.setObjectName(solver_id + 'IntentTargetSolver')
            button.setProperty('targetSolver', solver_id)
            button.clicked.connect(
                lambda _checked=False, chosen=solver_id:
                self.chooseTargetSolver(chosen))
            self._solverGroup.addButton(button)
            self._solverButtons[solver_id] = button
            box_layout.addWidget(button)
        self._solverReason = QLabel(self)
        self._solverReason.setObjectName('meshIntentTargetSolverReason')
        self._solverReason.setWordWrap(True)
        self._solverReason.setProperty('foammeshTone', 'muted')
        self._solverReason.setMinimumHeight(
            2 * self._solverReason.fontMetrics().lineSpacing())
        box_layout.addWidget(self._solverReason)
        # Above the (hidden) settings box, where the scroll area is.
        self.layout().insertWidget(self.layout().count() - 2, box)
        self.reload()

    # -- target solver ----------------------------------------------------- #

    def targetSolver(self) -> str:
        return self._targetSolver()

    def _targetSolver(self) -> str:
        try:
            result = query(self._client, 'mesh.target_solver.get', {})
        except Exception:
            return 'unselected'
        payload = getattr(result, 'payload', None) or {}
        return str(payload.get('target_solver') or 'unselected')

    def reload(self) -> None:
        super().reload()
        if self._solverGroup is None:
            return
        current = self._targetSolver()
        self._solverGroup.setExclusive(False)
        for solver_id, button in self._solverButtons.items():
            button.setChecked(solver_id == current)
        self._solverGroup.setExclusive(True)
        self._solverReason.setText(self.tr(
            'Choose the solver this mesh is for. Every meshing method is '
            'offered until you do.') if current == 'unselected' else '')

    def chooseTargetSolver(self, solver: str) -> None:
        # C31-12. Scheduled, not blocking: choosing a solver re-derives which
        # meshing methods are offered, and the old synchronous call froze the
        # radio group mid-click while the facade wrote and re-fingerprinted the
        # case. Everything that used to follow the call is `chosen`, so the
        # order (write, then reason text, then reload, then the warning) is
        # unchanged; a refusal arrives as a FailedResult carrying its message.
        def chosen(result) -> None:
            if getattr(result, 'status', 'accepted') != 'accepted':
                self._solverReason.setText(
                    str(getattr(result, 'message', '')
                        or self.tr('The target solver could not be set.')))
                self.reload()
                return
            stranded = (getattr(result, 'payload', None) or {}).get(
                'engine_incompatible_reason')
            self.reload()
            if stranded:
                # Not repaired here: which engine to move to is the user's call.
                QMessageBox.warning(
                    self, self.tr('Mesh Intent'),
                    self.tr('The selected meshing method cannot serve this '
                            'solver.\n\n{0}\n\n'
                            'Choose another method before meshing.'
                            ).format(stranded))

        submit(self._client, 'mesh.target_solver.set',
               {'target_solver': str(solver)}, then=chosen)
