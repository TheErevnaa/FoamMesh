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
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from foammesh.core.facade import applicability
from foammesh.core.facade.errors import ValidationFailedError
from foammesh.core.facade.fields import REGISTRY
from foammesh.view.facade_client import query, query_async, submit

from .conditional_fields import refresh_applicability
from foammesh.view.theming.metrics import (MARGIN_NONE, align_unit_column,
                                           apply_form_metrics)
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
        # Plan 33 SETUP-01. Two paragraphs above the first control, on a page
        # whose whole job is five spin boxes: MEASURED on Execution
        # Preferences at a 520 px panel, the purpose and the caveat came to
        # six wrapped lines and pushed the first setting below them. They are
        # still the page's own words and still class attributes, so anything
        # that reads `page.purpose` or `page.caveat` reads the same string --
        # they are the heading's tooltip and the page's accessible
        # description now, which is where an explanation belongs when the
        # reader did not ask for one.
        explained = ' '.join(part for part in (self.purpose, self.caveat)
                             if part)
        title.setToolTip(explained)
        self.setAccessibleDescription(explained)
        # W-O1. A heading over a group of controls belongs on the group, and
        # this one had a group already: the box below was headed `Settings`,
        # which names the column rather than the settings in it. The box now
        # carries the heading and the label carries nothing but the words,
        # for whatever reads `page.heading` off the widget tree.
        title.setVisible(False)
        self._heading = title
        outer.addWidget(title)

        body = QWidget(self)
        self._form = QGroupBox(self.heading or self.token, body)
        apply_form_metrics(QFormLayout(self._form))
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
        self._outer = outer
        self._body = body
        self._scroll = scroll
        self._embedded = False

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
            # DP-1250. Zero, as `unit_cell` has been since DP-198: this cell
            # sits inside a form field that already has the form's margin.
            # MEASURED on Surface features under the real theme: the layout
            # default of 8 above and below put a 30 px spin box in a 46 px
            # cell, so every registry row stood ~50 px apart against the
            # ~34 px of a row built with `unit_cell` on the same page.
            row.setContentsMargins(
                MARGIN_NONE, MARGIN_NONE, MARGIN_NONE, MARGIN_NONE)
            row.addWidget(editor.editor, 1)
            # DP-156, as on the task page: the cell always ends with the unit
            # label so the column can be given one width.
            row.addWidget(editor.unit_label)
            container = QWidget(self._form)
            container.setLayout(row)
            layout.addRow(editor.label, container)
            # DP-1250. Hand the editor its row, so an inapplicable field
            # takes the row off the form (`setRowVisible`) and hides this
            # cell with it. Only `ExecutionPreferencesPage` did this, so on
            # every other panel the label and editor hid and the form kept
            # the line -- MEASURED, the six subset-plane rows of Surface
            # features left ~124 px of blank between "Keep feature edges
            # crossing a plane" and "Add feature edges from file".
            editor.setRow(layout, container)
            self._editors[field_id] = editor
        # DP-339. A rebuild replaces the editors; it is not a write, so an
        # edit that has not reached the case yet is carried onto the new ones.
        self.reload(discard_pending=False)

    def form_layout(self) -> QFormLayout:
        """The form this panel's editors are drawn into.

        DP-154. A task page stacking two of these panels aligns their label
        columns, which it can only do if it can reach the layouts.
        """
        return self._form.layout()

    def setEmbedded(self, embedded: bool = True) -> None:
        """Stop being a page in its own right and become a section of one.

        DP-155. The class was written as a standalone page -- heading,
        purpose, its own ``QScrollArea``, its own Apply -- and the snappy
        workflow then mounted four of them as panels *inside* task pages that
        already scroll. So the twenty-seven mesh quality limits were drawn
        into 1477px of content inside a 360px window, itself inside the page's
        own scroller: MEASURED, 1117px of the limits could only be reached by
        landing the pointer on the inner panel and scrolling there, with two
        vertical scrollbars a few pixels apart and no sign of which one the
        wheel would move. The inner bar also took 12px off its panel's width,
        so the panel with the bar put its editors 12px left of the panel
        without one -- the same column split DP-154 had just closed, reopened
        one level down.

        Embedding lifts the body straight out of the scroll area and drops the
        scroll area, so the panel is exactly as tall as its rows and the page
        it sits on does all the scrolling. It is deliberately not the
        default: a subclass reached as a page in its own right does need to
        scroll. Plan 32 W1 made `ExecutionPreferencesPage` and
        `GmshHealingPanel` sections of Mesh setup and Preparation rather than
        rows of the outline, so both are embedded by their hosts.

        DP-157. Embedding also takes the panel's own Apply and Revert
        away. A panel is a section of a page, and the page already
        carries Preview, Update and Revert for everything on it; leaving
        the panel's pair in place put three commit controls on the QA
        page, one of which committed the twenty-seven mesh quality limits
        and two of which did not, with nothing on screen to say which was
        which. The page adopts the panel -- `EngineTaskPage.adoptPanel`
        -- and submits the panel's edits with its own.
        """
        embedded = bool(embedded)
        if embedded == self._embedded or not embedded:
            # Un-embedding is not supported: nothing needs it, and putting the
            # body back would have to rebuild a scroll area to put it in.
            return
        self._embedded = True
        body = self._scroll.takeWidget()
        index = self._outer.indexOf(self._scroll)
        self._outer.removeWidget(self._scroll)
        self._scroll.setParent(None)
        self._scroll.deleteLater()
        self._scroll = None
        self._outer.insertWidget(index, body)
        body.setParent(self)
        body.show()
        # Plan 33 section 6 check 4, W-O2. A panel standing on its own needs
        # its own left margin; a panel embedded in a page is inside the page's
        # margin already, and these two default 9 px layout margins -- this
        # one and the body's -- were adding 18 px on top of it. MEASURED on
        # Surface features at a 635 px settings column: the page's own form
        # put its labels at x=31 and the two embedded panels put theirs at
        # x=44, one page, two label columns.
        #
        # DP-1250. The vertical margins went the same way. W-O2 kept them on
        # the reasoning that they separate the panel from its neighbours --
        # but both layouts kept them, so each embedded group box had 16 px
        # above and 16 px below it on top of the page's own spacing. MEASURED
        # on Surface features: 22 px from the page's own "Feature
        # extraction" box to the first panel's box and 38 px between the two
        # stacked panels, against 6 px between the page's own boxes. The
        # page's layout spacing is what separates sections on a page, so an
        # embedded panel adds none of its own.
        for layout in (self._outer, body.layout()):
            if layout is None:
                continue
            layout.setContentsMargins(
                MARGIN_NONE, MARGIN_NONE, MARGIN_NONE, MARGIN_NONE)
        for button in (self._apply, self._revert):
            button.hide()

    def isEmbedded(self) -> bool:
        """True when this panel is a section of a task page (DP-155)."""
        return self._embedded

    def choices_for(self, field_id: str):
        """Runtime-narrowed rows for one field, or ``None`` for the schema's.

        A subclass overrides this when the schema's vocabulary is wider than
        what the machine in front of the user can do.
        """
        return None

    # -- values ------------------------------------------------------------ #

    def reload(self, *, discard_pending: bool = True) -> None:
        """Re-read every editor from the case.

        DP-339. `discard_pending` says what to do with an edit nobody has
        committed. After a patch the facade accepted, and after a revert,
        the edit is spent and goes; that is what a reload always did. But a
        reload is also how a page refreshes itself, and a refresh arrives
        for reasons that have nothing to do with the person typing: the
        method page reloads this band on every revisit, and the execution
        band rebuilds itself when a cold runtime probe finally answers.
        MEASURED on all three journeys of the settings-column campaign:
        `serial` was typed into the execution band, the target solver and
        the mesher were chosen next, each choice refreshed the method page,
        the refresh reloaded the band -- and the press that followed found
        `_pending` empty, sent no patch at all (the journal holds no
        `mesh.execution.*` transaction) and left the case on `auto`. So a
        refresh now keeps the typed value on screen and in the patch, and
        only a write or a revert clears it.
        """
        kept = {} if discard_pending else dict(self._pending)
        self._pending.clear()
        if self._editors:
            values = self._client.field_values(tuple(self._editors))
            for field_id, editor in self._editors.items():
                if field_id in kept:
                    # The store has not been told about this one yet, so what
                    # the person typed is the truer value of the two.
                    editor.set_value(kept[field_id])
                    continue
                editor.set_value(
                    values.get(field_id, editor.descriptor.default))
            self._pending.update(
                (field_id, value) for field_id, value in kept.items()
                if field_id in self._editors)
            # CP-09 item 4. A setting the configuration cannot reach stays on
            # the page, greyed and explained, and out of the patch.
            self._inactive = refresh_applicability(
                self._client, self._editors, self._pending,
                overrides=self._conditionOverrides())
        # DP-156. A page reached on its own aligns its own unit column; a
        # panel embedded on a task page has this done again, across both
        # panels, after the page's refresh has reloaded them.
        align_unit_column((self.form_layout(),))
        self._set_dirty(bool(self._pending))

    def _conditionOverrides(self) -> dict:
        """Values the applicability clauses judge in place of the stored ones.

        None here: a page's rows follow the case as it is written. A page
        whose rows follow a choice as it is made returns its pending edits.
        """
        return {}

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

    async def save(self) -> bool:
        """Write the pending edits and say whether they were accepted.

        DP-227. `apply` is a button handler: it schedules the write and
        reports a refusal in a modal of its own, and the task it returns
        completes *before* its continuation runs, so awaiting it tells a
        caller nothing about the outcome. A wizard needs an answer it can
        branch on before it moves the outline, so this awaits the same
        command and answers True only when the facade accepted it. The
        message belongs to whoever asked, which is why there is no dialog
        here.

        Plan 30 WP-08. There is no synchronous fallback: a write from a view
        module goes on the scheduler or it does not happen, and a client with
        no awaitable `run` is answered False rather than served from the GUI
        thread. Every caller already has to handle a refusal.
        """
        if not self._pending:
            return True
        parameters = {'patch': self.pending_patch()}
        runner = getattr(self._client, 'run', None)
        if runner is None:
            return False
        try:
            result = await runner('configuration.patch', parameters)
        except Exception:
            return False
        if getattr(result, 'status', 'accepted') != 'accepted':
            return False
        self.reload()
        return True

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
    heading = 'Meshing resources'

    #: The route the band is filtering for: the engine id of the checked
    #: meshing method, as the host page reports it.
    _engine_id = ''

    #: Which field each partitioner consumes, read off the writer
    #: (`foammesh/openfoam/decomposition.py`): `NEEDS_COEFFICIENTS` takes the
    #: cell vector for `hierarchical` and `simple`, and the order belongs to
    #: `hierarchical` alone. A method the writer stops consuming a field for
    #: is one edit here, beside the rest of the rule.
    METHOD_FIELDS = {
        'hierarchical': ('mesh.execution.decomposition_order',
                         'mesh.execution.decomposition_cells'),
        'simple': ('mesh.execution.decomposition_cells',),
    }

    #: Every field a parallel OpenFOAM run reads, whatever the partitioner.
    DECOMPOSITION_FIELDS = (
        'mesh.execution.max_cpu_cores',
        'mesh.execution.decomposition_method',
        'mesh.execution.preserve_face_zones',
        'mesh.execution.preserve_baffles',
        'mesh.execution.preserve_patches',
        'mesh.execution.preserve_refinement_history',
        'mesh.execution.decomposition_weight_field',
    )

    #: Why a setting no run reads is not on the page. Keyed by field id, and
    #: carried on the editor that is still holding the stored value.
    UNREAD_FIELDS = {
        'mesh.execution.max_memory_bytes':
            'No meshing runtime enforces a memory ceiling, so this number '
            'bounds nothing that runs.',
        'mesh.execution.allow_distributed':
            'Meshing runs on this machine, so there is nothing to '
            'distribute.',
        'mesh.execution.preferred_backend':
            'Each mesher has one runtime, and the run takes it from the '
            'meshing method.',
    }

    def visible_field_ids(self, engine_id, mode, method) -> tuple[str, ...]:
        """The fields the chosen mesher reads, in page order.

        Plan 33 SETUP-02. This is the whole rule, in one readable place,
        because "which fields does this mesher read" is the question the page
        answered wrongly: it offered all thirteen on every route, so a Gmsh
        case -- one process, threads, nothing decomposed -- was asked for a
        decomposition method, an order, a cell vector, four partitioner
        constraints and a weight field, none of which a Gmsh run reads.

        A hidden field keeps its editor and its stored value: this narrows
        what is asked, it does not clear what the case holds.
        """
        engine = str(engine_id or '').rsplit('.', 1)[-1].lower()
        mode = str(mode or '').rsplit('.', 1)[-1].lower()
        method = str(method or '').rsplit('.', 1)[-1].lower()
        shown = {'mesh.execution.mode'}
        if engine == 'gmsh':
            # Gmsh meshes in one process; the count is threads, not ranks.
            shown.add('mesh.execution.max_cpu_cores')
        elif engine == 'snappy':
            if mode == 'parallel':
                shown.update(self.DECOMPOSITION_FIELDS)
                shown.update(self.METHOD_FIELDS.get(method, ()))
            elif mode != 'serial':
                # DP-1260. Automatic mode reads the count too: a count typed
                # here is what `requested_cpu_count` hands the run, and zero is
                # Auto. The row also carries the count Auto came out as, and
                # hiding it in Auto mode left the page silent on the one
                # number that mode decides.
                shown.add('mesh.execution.max_cpu_cores')
        else:
            # No mesher is chosen yet: ask only what both of them read, and
            # let the rest arrive with the answer to which mesher this is.
            shown.add('mesh.execution.max_cpu_cores')
        return tuple(field_id for field_id in self.field_ids
                     if field_id in shown)

    def __init__(self, facade_client, parent=None):
        super().__init__(facade_client, parent)
        self._mountAutoBasis()
        self._mountRedistribute()

    def setEngine(self, engine_id: str) -> None:
        """Say which mesher the page is standing on, and ask the rule again."""
        engine = str(engine_id or '').rsplit('.', 1)[-1].lower()
        if engine == self._engine_id:
            return
        self._engine_id = engine
        self._applyRoute()
        self._refreshEffectiveCount()
        self._refreshRedistribute()

    # -- Plan 37 UF17: change the core count of a decomposed mesh ----------- #

    def _mountRedistribute(self) -> None:
        """A button under the settings that re-splits the processor cases.

        Editing the core ceiling changes what the *next* decomposition asks
        for; it does nothing to processor cases already on disk, which a
        stage reuses only while their count matches. This moves the mesh
        that is there onto the new count (``mesh.redistribute``), after a
        preview says what it will do and the user confirms it. It lives
        below the group box, not in the form, because the form is rebuilt.
        """
        button = QPushButton(self.tr('Change core count of the mesh…'),
                             self._body)
        button.setObjectName('executionRedistributeButton')
        button.setAccessibleName(self.tr('Change core count of the mesh'))
        button.setAccessibleDescription(self.tr(
            'Split the decomposed mesh already on disk over a different '
            'core count, without meshing again.'))
        button.setToolTip(button.accessibleDescription())
        # `clicked` carries a bool; the count is asked, not taken from it.
        button.clicked.connect(lambda _checked=False: self.redistribute())
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button)
        layout = self._body.layout()
        layout.insertLayout(max(layout.count() - 1, 0), row)
        self._redistribute_button = button
        self._refreshRedistribute()

    def _refreshRedistribute(self) -> None:
        button = getattr(self, '_redistribute_button', None)
        if button is None:
            return
        # Only snappy decomposes; a Gmsh case has no processor cases.
        button.setVisible(self._engine_id == 'snappy')

    def _askTarget(self, current: int) -> int | None:
        from PySide6.QtWidgets import QInputDialog
        value, accepted = QInputDialog.getInt(
            self, self.tr('Change core count'),
            self.tr('Spread the decomposed mesh over (cores)'),
            self.redistributeDefault(current), 2, 4096, 1)
        return int(value) if accepted else None

    def redistributeDefault(self, current: int = 0) -> int:
        """The count the redistribute dialog opens on.

        DP-1261. This was ``max(int(current or 2), 2)``: with the ceiling at
        zero (Auto) the dialog offered 2 on a host whose Auto is fifteen
        ranks, so the obvious OK moved a mesh onto two cores. Zero now opens
        on the count Auto resolves to, the number the row above shows; two is
        only the floor, because a redistribute onto one rank is a
        reconstruct, not a split.
        """
        try:
            current = int(current or 0)
        except (TypeError, ValueError):
            current = 0
        if current <= 0:
            policy = dict(self._policyNow())
            if policy['mode'] == 'serial':
                policy['mode'] = 'parallel'
            host, _measured = self._hostReading()
            current = self._snappyCount(policy, host)[0]
        return max(int(current), 2)

    def _confirm(self, text: str) -> bool:
        answer = QMessageBox.question(
            self, self.tr('Change core count'), text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def _tell(self, text: str, warning: bool = False) -> None:
        box = QMessageBox.warning if warning else QMessageBox.information
        box(self, self.tr('Change core count'), text)

    def redistributeSummary(self, report: dict) -> str:
        """The confirmation text: what moves, what is dropped, what goes stale."""
        census = report.get('census') or {}
        lines = [self.tr('Move the decomposed mesh from {0} to {1} cores.').format(
            report.get('source_ranks'), report.get('target_ranks'))]
        if census.get('cells') is not None:
            lines.append(self.tr('{0:,} cells and every zone and field are kept; '
                                 'the case-root mesh is not touched.').format(
                int(census['cells'])))
        lines.append(self.tr('redistributePar runs on {0} processes on a copy; '
                             'the processor cases are swapped only after the '
                             'copy passes checkMesh.').format(report.get('np')))
        # Plan 37 UF20, MEASURED live: the preview sends ``unmapped`` as a
        # list of ``{file, what}`` (``redistribute_transaction.assess``); this
        # read it as a dict, and on a case with any unmapped file the
        # confirmation died on ``'list' object has no attribute 'values'``.
        unmapped = report.get('unmapped') or []
        if isinstance(unmapped, dict):
            unmapped = list(unmapped.values())
        names = sorted({str(item.get('what') or item.get('file'))
                        if isinstance(item, dict) else str(item)
                        for item in unmapped})
        if names:
            lines.append(self.tr('Not carried over: {0}.').format(
                ', '.join(names)))
        lines.append(self.tr('The quality report goes stale, and the core '
                             'count setting becomes {0}.').format(
            report.get('target_ranks')))
        return '\n\n'.join(lines)

    def redistribute(self, target: int | None = None):
        """Preview, confirm, then run ``mesh.redistribute``."""
        if target is None:
            try:
                current = int(self._currentValue('mesh.execution.max_cpu_cores') or 0)
            except (TypeError, ValueError):
                current = 0
            target = self._askTarget(current)
            if target is None:
                return None
        # Plan 37 UF20, MEASURED live: the preview's handler awaits (it
        # settles an interrupted change and counts the processor cases in a
        # worker), so the synchronous ``query`` refused it on every click with
        # "mesh.redistribute.preview requires the async execute() path" and
        # the button never got past its first dialog. It is awaited on the
        # running loop; what follows -- a modal confirmation -- is handed
        # back through ``call_soon`` so its nested loop does not run inside
        # this task (DP-34).
        import asyncio

        parameters = {'ranks': int(target)}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None:
            try:
                preview = query(self._client, 'mesh.redistribute.preview',
                                parameters)
            except Exception as error:  # noqa: BLE001 - said, not raised
                self._tell(str(error), warning=True)
                return None
            return self._redistributeFromPreview(preview, target)

        async def ask():
            try:
                preview = await query_async(
                    self._client, 'mesh.redistribute.preview', parameters)
            except Exception as error:  # noqa: BLE001 - said, not raised
                loop.call_soon(self._tell, str(error), True)
                return
            loop.call_soon(self._redistributeFromPreview, preview, target)

        return loop.create_task(ask())

    def _redistributeFromPreview(self, preview, target: int):
        """Confirm what the preview said, then run ``mesh.redistribute``."""
        report = getattr(preview, 'payload', None) or {}
        refusal = report.get('refusal')
        if refusal:
            self._tell(str(refusal.get('reason') or refusal.get('code')))
            return None
        if not self._confirm(self.redistributeSummary(report)):
            return None
        button = self._redistribute_button
        button.setEnabled(False)

        def done(result):
            button.setEnabled(True)
            if getattr(result, 'status', '') == 'accepted':
                payload = getattr(result, 'payload', None) or {}
                self.reload()
                self._tell(self.tr('The mesh is now split over {0} cores.').format(
                    payload.get('target_ranks', target)))
                return
            details = getattr(result, 'payload', None) or {}
            self._tell(self.tr('The core count was not changed; the processor '
                               'cases are as they were.\n\n{0}').format(
                getattr(result, 'message', '') or details.get('reason', '')),
                warning=True)

        return submit(self._client, 'mesh.redistribute',
                      {'ranks': int(target),
                       'expected_revision': report.get('revision')},
                      then=done)

    def engineId(self) -> str:
        return self._engine_id

    def effectiveCountText(self) -> str:
        """What the run will use, in the words beside the ceiling."""
        label = getattr(self, '_effective', None)
        return '' if label is None else label.text()

    # -- construction ------------------------------------------------------ #

    def build(self) -> None:
        """Build the rows, then add the count the run will use.

        The generic page hands each editor its form row (DP-1250), so an
        inapplicable field here leaves no empty line either.
        """
        super().build()
        self._mountEffectiveCount()
        self._applyRoute()

    def _cellFor(self, field_id: str):
        editor = self._editors.get(field_id)
        if editor is None:
            return None
        layout = self.form_layout()
        row, _role = layout.getWidgetPosition(editor.label)
        if row < 0:
            return None
        item = layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
        return item.widget() if item is not None else None

    def _mountEffectiveCount(self) -> None:
        """Put the count the run will use beside the ceiling that bounds it.

        Plan 33 SETUP-03. Zero reads as `Automatic`, and a reader cannot be
        left to guess what automatic came out as, so the number the run asks
        for is the number on the page -- both from one function.
        """
        self._effective = None
        cell = self._cellFor('mesh.execution.max_cpu_cores')
        row = None if cell is None else cell.layout()
        if row is None:
            return
        label = QLabel(cell)
        label.setObjectName('executionEffectiveCores')
        # DP-186 and the castellation estimate before it: a label whose text
        # is rewritten on every edit must not carry a fixed accessible name,
        # or an assistive reader is handed the heading in place of the
        # number. The sentence belongs in the description.
        label.setAccessibleDescription(
            self.tr('The count the run asks for, after this limit and what '
                    'the machine has.'))
        label.setToolTip(label.accessibleDescription())
        # DP-156: the cell still ends with the unit label.
        row.insertWidget(max(row.count() - 1, 0), label)
        self._effective = label
        self._refreshEffectiveCount()

    # -- the route --------------------------------------------------------- #

    def _currentValue(self, field_id: str):
        if field_id in self._pending:
            return self._pending[field_id]
        editor = self._editors.get(field_id)
        return None if editor is None else editor.value()

    def _applyRoute(self) -> None:
        """Ask the rule again and take the rows it does not name away.

        Run after `refresh_applicability`, never instead of it: a field the
        configuration itself has ruled out stays ruled out with the reason its
        clause gave, and this adds the route's own reasons on top.
        """
        if not self._editors or not hasattr(self, '_effective'):
            return
        shown = set(self.visible_field_ids(
            self._engine_id,
            self._currentValue('mesh.execution.mode'),
            self._currentValue('mesh.execution.decomposition_method')))
        inactive = getattr(self, '_inactive', None) or {}
        for field_id, editor in self._editors.items():
            if field_id in shown:
                if field_id not in inactive:
                    editor.setApplicability(True, '')
                continue
            editor.setApplicability(False, self._routeReason(field_id))

    def _routeReason(self, field_id: str) -> str:
        unread = self.UNREAD_FIELDS.get(field_id)
        if unread:
            return unread
        if self._engine_id == 'gmsh':
            return self.tr('Gmsh meshes in one process and decomposes '
                           'nothing, so a Gmsh run never reads this.')
        mode = str(self._currentValue('mesh.execution.mode') or '')
        if mode.rsplit('.', 1)[-1].lower() == 'auto':
            # DP-593. Automatic mode may still run on several ranks, so "a
            # serial run" was the wrong excuse; what it does is decompose
            # with the defaults and leave this setting unread.
            return self.tr('Automatic mode picks the rank count and '
                           'decomposes with scotch and no constraints, so '
                           'this is read only in Parallel mode.')
        if mode.rsplit('.', 1)[-1].lower() != 'parallel':
            return self.tr('A serial run decomposes nothing, so nothing '
                           'reads this.')
        return self.tr('The chosen decomposition method does not read this.')

    def _refreshEffectiveCount(self) -> None:
        """Say what each mesher will really run on, in its own unit.

        DP-691. This asked `effective_cpu_count` with the ceiling alone and
        read `Automatic` for zero, while the snappy launcher also read the
        Parallel Environment dialog and started one rank for an automatic
        case, and Gmsh took every thread the machine had.

        DP-1260. The snappy half still asked `effective_cpu_count(unasked=1)`
        and said `1 rank` for Auto while the run (DP-1231) meshed on the WSL
        host's cores less one -- MEASURED on the stand-in 16-core host: page
        `1 rank`, run 15 ranks. It now asks `DomainOperations._snappy_cpu`,
        the function the stage launcher and the plan preview ask, with the
        cached host reading (never the probe itself: that boots WSL). Gmsh
        asks `resolve_parallel_threads`, which its job asks. How Auto got
        there is said in a line under the group.
        """
        from foammesh.core.gmsh.plan_derivation import resolve_parallel_threads

        label = getattr(self, '_effective', None)
        if label is None:
            return
        policy = self._policyNow()
        host, measured = self._hostReading()
        ranks, cpu = self._snappyCount(policy, host)
        threads = resolve_parallel_threads(policy)
        rank_text = (self.tr('1 rank') if ranks == 1
                     else self.tr('{0} ranks').format(ranks))
        thread_text = (self.tr('1 thread') if threads == 1
                       else self.tr('{0} threads').format(threads))
        if self._engine_id == 'snappy':
            label.setText(rank_text)
        elif self._engine_id == 'gmsh':
            label.setText(thread_text)
        else:
            label.setText(self.tr('snappy: {0} \u00b7 Gmsh: {1}').format(
                rank_text, thread_text))
        if self._engine_id == 'gmsh':
            from foammesh.core.execution.resources import meshing_cpu_count
            cpu = meshing_cpu_count(policy, engine='gmsh', host=host)
            count = threads
        else:
            count = ranks
        basis = self._basisText(cpu, count, host, measured)
        if cpu is not None and cpu.auto and not measured:
            self._warmHost()
        self._setBasis(basis)
        label.setToolTip(' '.join(part for part in (
            label.accessibleDescription(), basis) if part))

    # -- DP-1260: how Auto came to its count ------------------------------- #

    def _policyNow(self) -> dict:
        """The policy the run would read, from what is typed on the page."""
        def number(field_id):
            try:
                return max(int(self._currentValue(field_id) or 0), 0)
            except (TypeError, ValueError):
                return 0
        mode = str(self._currentValue('mesh.execution.mode') or 'auto')
        return {'mode': mode.rsplit('.', 1)[-1].lower(),
                'max_cpu_cores': number('mesh.execution.max_cpu_cores') or None,
                'max_memory_bytes':
                    number('mesh.execution.max_memory_bytes') or None}

    @staticmethod
    def _hostReading():
        """``(MeshingHost, measured)`` without waiting for anything.

        ``meshing_host()`` answers from the cache and falls back to this
        PC's own facts until the WSL distribution has been read; *measured*
        is False for that fallback, which is not the machine the mesher runs
        on.
        """
        from foammesh.core.execution import resources

        host = resources.meshing_host()
        if str(host.source) != 'local':
            return host, True
        try:
            return host, resources._runtime_target() is None
        except Exception:                                   # noqa: BLE001
            return host, False

    def _casePath(self):
        try:
            return self._client.case_path
        except Exception:                                   # noqa: BLE001
            return None

    def _snappyCount(self, policy: dict, host):
        """``(ranks, CpuCount | None)`` the next snappy stage would run on."""
        from foammesh.core.execution.resources import (
            effective_cpu_count, meshing_cpu_count,
        )

        if policy['mode'] == 'serial':
            return 1, None
        case_path = self._casePath()
        try:
            if case_path is not None:
                from foammesh.core.facade.domain_operations import (
                    DomainOperations,
                )
                # ``stage='snap'``: the count Snap and Layers will run on,
                # which is Castellation's own when it ran on Auto (DP-1232).
                cpu, host = DomainOperations._snappy_cpu(
                    case_path, policy, stage='snap', host=host)
            else:
                cpu = meshing_cpu_count(policy, engine='snappy', host=host)
        except (TypeError, ValueError, OSError):
            return 1, None
        if cpu.auto:
            return max(1, int(cpu.count)), cpu
        return max(1, int(effective_cpu_count(
            policy, requested=cpu.count, facts=host.resource_facts()))), cpu

    def _basisText(self, cpu, count: int, host, measured: bool) -> str:
        """One sentence on where the count came from, or '' when it is
        simply the number typed."""
        if cpu is None:
            return ''
        unit = cpu.unit
        if not cpu.auto:
            cores = int(host.physical_cores or 0)
            if measured and unit == 'ranks' and cores and count > cores:
                # DP-1263. Open MPI gives one slot per physical core.
                return self.tr(
                    'More ranks than the host\'s {0} physical cores: the '
                    'run starts Open MPI with --oversubscribe, so ranks '
                    'share cores.').format(cores)
            return ''
        if cpu.source == 'recorded':
            return self.tr(
                'Auto: {0} {1}, the count Castellation of this mesh ran on. '
                'It stays fixed for Snap and Layers, so the mesh is not '
                're-split midway.').format(count, unit)
        if not measured:
            return self.tr(
                'Auto: measuring the WSL host\u2026 Until it answers this is '
                'this PC\'s count, {0} {1}.').format(count, unit)
        where = (self.tr('the WSL host') if str(host.source).startswith('wsl')
                 else self.tr('this machine'))
        text = self.tr('Auto: {0} {1} cores on {2}, less {3}').format(
            cpu.cores, cpu.core_kind, where, cpu.headroom)
        free = host.memory_available_bytes
        if free:
            text += self.tr('; {0:.0f} GiB free').format(free / (1 << 30))
        if cpu.limited_by == 'memory':
            text += self.tr(', which holds {0} {1}').format(
                cpu.memory_limit, unit)
        return text + '.'

    def _mountAutoBasis(self) -> None:
        """A wrapped line under the group: how Auto reached its count."""
        label = QLabel(self._body)
        label.setObjectName('executionAutoBasis')
        label.setWordWrap(True)
        label.setAccessibleDescription(self.tr(
            'How the automatic core count was reached.'))
        label.setVisible(False)
        layout = self._body.layout()
        layout.insertWidget(max(layout.count() - 1, 0), label)
        self._basis = label
        self._refreshEffectiveCount()

    def _setBasis(self, text: str) -> None:
        label = getattr(self, '_basis', None)
        if label is None:
            return
        label.setText(text)
        editor = self._editors.get('mesh.execution.max_cpu_cores')
        label.setVisible(bool(text) and editor is not None
                         and editor.applies())

    def autoBasisText(self) -> str:
        """What the page says about how Auto reached its count."""
        label = getattr(self, '_basis', None)
        return '' if label is None or label.isHidden() else label.text()

    #: Set while a WSL host reading is out, so a refresh does not start a
    #: second one.
    _hostWarming = False

    def _warmHost(self) -> None:
        """Read the WSL host off the GUI thread, then say the real count.

        Only inside the running loop, and only once the OpenFOAM runtime has
        answered this window (`utility_if_known`): the distribution is then
        already up and the reading costs a second, and a page built without
        an application -- a test's -- never boots WSL to draw a label.
        """
        if self._hostWarming:
            return
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            from foammesh.app import app
            known = app.capabilities.utility_if_known('snappyHexMesh')
        except Exception:                                   # noqa: BLE001
            known = None
        if known is None or not getattr(known, 'available', False):
            return
        from foammesh.core.execution.resources import warm_meshing_host
        self._hostWarming = True

        async def warm():
            try:
                await warm_meshing_host()
            except Exception:                               # noqa: BLE001
                pass
            finally:
                self._hostWarming = False
            try:
                self._refreshEffectiveCount()
            except RuntimeError:
                pass                    # the page was closed meanwhile

        loop.create_task(warm())

    def _on_changed(self, field_id: str, value) -> None:
        super()._on_changed(field_id, value)
        if field_id in ('mesh.execution.mode',
                        'mesh.execution.decomposition_method'):
            self._applyRoute()
        if field_id in ('mesh.execution.mode',
                        'mesh.execution.max_cpu_cores'):
            self._refreshEffectiveCount()

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

    def reload(self, *, discard_pending: bool = True) -> None:
        """Re-read the values, and take the runtime's answer if it arrived.

        The first render happens before a cold runtime can answer, so the
        combo is un-narrowed then. The probe keeps running on its worker
        thread; the next time the page is shown the answer is in the cache and
        the rows are rebuilt against it.
        """
        super().reload(discard_pending=discard_pending)
        # `refresh_applicability` hands every clause-free field back its row,
        # so the route has to be asked again after each reload rather than
        # only when the mesher changes.
        self._applyRoute()
        self._refreshEffectiveCount()
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
        'rank count, so a Parallel environment asking for more than this '
        'gets this.')
    caveat = (
        'Changing the core count can change the mesh. Snappy\'s refinement '
        'is decomposition-sensitive: a measured duct case moved +12.4% in '
        'cell count between serial and 16 ranks, while a 609k-cell annulus '
        'moved -0.04% on the same ranks. Only a serial mesh is independent '
        'of the core count.')



class GmshHealingPanel(FieldGroupPage):
    """The import and healing questions Gmsh asks, hosted on Preparation.

    Plan 32 §4.4. These nineteen settings were the `gmsh.describe_geometry`
    task page: a child row of the engine branch, so they were reached only
    after a solver was chosen, a method was chosen and the method was applied
    -- which is to say after `1. Geometry` had already imported the file the
    tolerances describe. They are statements about how to read the CAD, so
    they belong beside the readiness report that says what is wrong with it.

    The field list is read from the registry rather than typed here: the
    fields are exactly those whose `ui_location` is
    `workflow.gmsh.describe_geometry`, and a hand list is a second place for
    that answer to be wrong.
    """

    token = 'preparation.gmsh_healing'
    heading = 'Import and healing'
    # DP-630 (field audit 0924 D-SH-06). This said the settings "describe the
    # geometry the rest of this page reports on". Nothing but the Gmsh job
    # reads them: the readiness report above never changes with them, and
    # nothing is re-imported when they do.
    purpose = (
        'How Gmsh reads the geometry when it meshes it: the tolerances, the '
        'sewing and the fixes its CAD importer applies, and the far field — '
        'a box, sphere or cylinder — it can build around the result. They '
        'take effect in the Gmsh run '
        'only; the readiness report on this page is not re-run with them.')
    caveat = ''
    field_ids: tuple[str, ...] = tuple(
        field_id for field_id in REGISTRY.ids()
        if REGISTRY.get(field_id).ui_location
        == 'workflow.gmsh.describe_geometry')

    #: DP-629 (field audit 0924 D-SH-02). Settings of Gmsh's CAD importer. A
    #: surface (STL/OBJ) is read without it, so on a case whose every source
    #: is a surface they change nothing, and the run records them so.
    CAD_IMPORTER_FIELDS = tuple(
        f'gmsh.describe_geometry.{name}' for name in (
            'sew_faces', 'fix_degenerated', 'make_solids', 'fix_small_edges',
            'fix_small_faces', 'auto_fix', 'union_unify', 'import_labels',
            'occ_parallel', 'heal_shapes', 'import_tolerance',
            'remove_duplicate_faces'))
    SURFACE_ONLY_REASON = (
        'Every geometry in this case is a surface (STL/OBJ), which Gmsh reads '
        'without its CAD importer, so this setting would change nothing.')
    #: DP-631 (field audit 0924 D-SH-01). A second unit knob: it multiplied
    #: a CAD import already in metres and did nothing to a surface.
    IMPORT_SCALING = 'gmsh.describe_geometry.import_scaling'
    IMPORT_SCALING_REASON = (
        'Held at 1. The unit is chosen when the file is imported; a second '
        'factor here would put the Gmsh mesh at a different size from the '
        'viewport and from every size on these pages.')

    def surfaceOnly(self) -> bool:
        """True when the case has geometry and none of it is read as CAD."""
        try:
            from foammesh.app import app
            from foammesh.core.geometry import GeometryArtifactStore
            from foammesh.core.geometry.store import is_cad_entry
            project = getattr(app, 'project', None)
            path = getattr(project, 'path', None)
            if path is None:
                return False
            entries = GeometryArtifactStore(path).entries()
        except Exception:                                    # noqa: BLE001
            # Unknown is not "surface": leave the switches offered.
            return False
        return bool(entries) and not any(is_cad_entry(item)
                                         for item in entries)

    #: Plan 37. The farfield these fields describe is one record, edited here
    #: and from Geometry > Farfield...; the line says where the other is.
    FARFIELD_POINTER = ('The far field can also be set up from '
                        'Geometry → Farfield…; both edit the '
                        'same settings.')

    def build(self) -> None:
        super().build()
        layout = self.form_layout()
        editor = self._editors.get('gmsh.describe_geometry.enabled')
        if editor is None:
            return
        row, _role = layout.getWidgetPosition(editor.label)
        if row < 0:
            return
        pointer = QLabel(self.tr(self.FARFIELD_POINTER), self._form)
        pointer.setObjectName('gmshFarfieldPointer')
        pointer.setWordWrap(True)
        layout.insertRow(row, pointer)
        # Plan 37 UF13. Said under the shape when a sphere or cylinder was
        # sized to hold the geometry, as Geometry > Farfield... says it.
        shape = self._editors.get('gmsh.describe_geometry.shape')
        fit_row = (layout.getWidgetPosition(shape.label)[0]
                   if shape is not None else -1)
        note = QLabel(self._form)
        note.setObjectName('gmshFarfieldFitNote')
        note.setWordWrap(True)
        note.hide()
        if fit_row >= 0:
            layout.insertRow(fit_row + 1, note)
        else:
            layout.addRow(note)
        self._fitNote = note

    def pinnedReasons(self) -> dict[str, str]:
        """The fields this case cannot use, beyond their own conditions."""
        pinned = {self.IMPORT_SCALING: self.IMPORT_SCALING_REASON}
        if self.surfaceOnly():
            pinned.update((field_id, self.SURFACE_ONLY_REASON)
                          for field_id in self.CAD_IMPORTER_FIELDS)
        return pinned

    def reload(self, *, discard_pending: bool = True) -> None:
        super().reload(discard_pending=discard_pending)
        note = getattr(self, '_fitNote', None)
        if note is not None and discard_pending:
            note.hide()
        if self._applyPinned():
            align_unit_column((self.form_layout(),))
            self._set_dirty(bool(self._pending))

    def _applyPinned(self) -> bool:
        """Take the pinned fields off the form; True when any row went."""
        inactive = getattr(self, '_inactive', None)
        if inactive is None:
            inactive = self._inactive = {}
        changed = False
        for field_id, reason in self.pinnedReasons().items():
            editor = self._editors.get(field_id)
            if editor is None or not editor.applies():
                continue
            editor.setApplicability(False, reason)
            inactive[field_id] = reason
            self._pending.pop(field_id, None)
            changed = True
        return changed

    # -- the farfield rows follow the shape as it is chosen ---------------- #

    #: Plan 37 UF13. The settings the farfield rows' clauses read on this
    #: page: the radius, length and axis follow the shape, the explicit
    #: centre follows the centre mode.
    FARFIELD_SWITCHES = ('gmsh.describe_geometry.shape',
                         'gmsh.describe_geometry.centre_mode')

    def _conditionOverrides(self) -> dict:
        """The shape and centre mode as chosen, applied or not.

        Plan 37 UF13. The rows were judged on the stored shape only, so a
        sphere's radius appeared after Apply. Geometry → Farfield… swaps them
        as the shape is chosen; this panel edits the same record and now
        does the same.
        """
        return {field_id: self._pending[field_id]
                for field_id in self.FARFIELD_SWITCHES
                if field_id in self._pending}

    #: Choosing these sizes a sphere or cylinder that would not hold the
    #: geometry, as Geometry > Farfield... does (``FarfieldDialog._fit``).
    FARFIELD_FIT_TRIGGERS = FARFIELD_SWITCHES + (
        'gmsh.describe_geometry.enabled',)
    FIT_NOTE = ('Sized to hold the geometry with room around it; change it '
                'if you need a different size.')

    def _on_changed(self, field_id: str, value) -> None:
        super()._on_changed(field_id, value)
        if field_id in self.FARFIELD_FIT_TRIGGERS:
            self._fitFarfield()
        if field_id in self.FARFIELD_SWITCHES:
            self._refreshFarfieldRows()

    def modelBounds(self):
        """The model's extent in Gmsh order, or ``None`` with no geometry.

        Read as Geometry > Farfield... reads it, from the geometry manager's
        surface bounds.
        """
        try:
            from foammesh.app import app
            from foammesh.core.mesh import snappy_farfield
            manager = getattr(getattr(app, 'window', None),
                              'geometryManager', None)
            surfaces = getattr(manager, 'getSurfaceBounds', None)
            extent = surfaces() if surfaces is not None else None
            return (None if extent is None
                    else snappy_farfield.gmsh_bounds(extent.toTuple()))
        except Exception:                                    # noqa: BLE001
            return None

    def fitNote(self) -> QLabel | None:
        return getattr(self, '_fitNote', None)

    def _fitFarfield(self) -> bool:
        """A sphere or cylinder chosen too small is sized to hold the model.

        Plan 37 UF13. The same rule as ``FarfieldDialog._fit``: only with an
        automatic centre, and only when the size on screen would be refused
        for containment. The new radius (and a cylinder's length) is put in
        the edit, so Apply writes it. True when anything was sized.
        """
        from foammesh.core.mesh import farfield_spec
        from foammesh.view.geometry.farfield_dialog import (FIELDS,
                                                            spec_from_values)

        note = self.fitNote()
        if note is not None:
            note.hide()
        values = {field_id: (self._pending[field_id]
                             if field_id in self._pending
                             else self._editors[field_id].value())
                  for field_id, _leaf in FIELDS if field_id in self._editors}
        try:
            spec = spec_from_values(values)
        except Exception:                                    # noqa: BLE001
            return False
        bounds = self.modelBounds()
        if (bounds is None or not spec.enabled or spec.shape == 'box'
                or spec.centre_mode != 'auto' or spec.problems()
                or not farfield_spec.check(spec, bounds)):
            return False
        sized = farfield_spec.fitted(spec, bounds)
        fits = {'gmsh.describe_geometry.radius': sized.radius}
        if spec.shape == 'cylinder':
            fits['gmsh.describe_geometry.length'] = sized.length
        for field_id, value in fits.items():
            editor = self._editors.get(field_id)
            if editor is None:
                continue
            editor.set_value(value)
            self._pending[field_id] = editor.value()
        self._set_dirty(True)
        if note is not None:
            note.setText(self.tr(self.FIT_NOTE))
            note.show()
        return True

    def _refreshFarfieldRows(self) -> None:
        """Offer the rows the chosen shape and centre mode read, now.

        Nothing is dropped from the edit: a radius typed for a sphere is
        kept if the shape goes back to sphere before Apply, and
        `pending_patch` leaves out whatever the shape on screen does not read.
        """
        self._inactive = refresh_applicability(
            self._client, self._editors, None,
            overrides=self._conditionOverrides())
        self._applyPinned()
        align_unit_column((self.form_layout(),))

    def pending_patch(self) -> dict:
        """The edit, less the farfield rows the chosen shape does not read.

        Only the rows whose clauses name the shape or the centre mode are
        held back here; every other inactive row is dropped by the reload,
        as on every field page.
        """
        inactive = getattr(self, '_inactive', None) or {}
        held = set()
        for field_id in self._pending:
            editor = self._editors.get(field_id)
            if field_id not in inactive or editor is None:
                continue
            clauses = getattr(editor.descriptor, 'applies_when', ()) or ()
            if set(applicability.referenced_fields(clauses)) & set(
                    self.FARFIELD_SWITCHES):
                held.add(field_id)
        return {field_id: value for field_id, value in self._pending.items()
                if field_id not in held}
