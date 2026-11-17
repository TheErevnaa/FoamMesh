"""Snappy workflow page for the *Refinement* row.

Plan 50 IP25 (D1, D2). Surface features, Castellation and Snap were three
outline rows, three pages and three presses for one question: how does the
background grid become a body-fitted mesh? The decisions are the feature angle
and the refinement tables; the snap settings are mostly left as they are. The
three are one row now, and this is its page:

1. the included angle and the extracted feature edges (``snappy.surface_features``,
   this page's own task);
2. the refinement tables, the ceilings and the gap controls -- the
   Castellation page, embedded whole;
3. the snap settings, the Snap page embedded whole behind one closed
   *Snapping* fold, with the mesher-limits fold it already had.

DP-6381. Each embedded page is still the task page of its own task: it reads
and writes its own task's fields, the branch files it under its own task id
(so ``window.page('snappy.castellation')`` and every field id and local-API
route reach the same widgets as before), and it locks with its own task. What
an embedded page loses is its own scroller and its own filter box; the host
page scrolls and filters for all three.

DP-6383. *Stop after castellation* stays in the footer drop-down, and the kept
castellation snapshot (``core/jobs/stage_snapshots.py``) is offered there as
*Show castellated mesh* whenever one is on disk.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .castellation_page import SnappyCastellationPage
from .sheet_folds import fold_behind, open_on_filter
from .snap_page import SnappySnapPage
from .surface_features_page import SnappySurfaceFeaturesPage

#: The stage the kept snapshot is read for and the label the viewport gets.
CASTELLATION_STAGE = 'castellation'

REFINEMENT_CONTROLS_TOOLTIP = (
    'The castellation settings beside the refinement table: cells between '
    'levels, the feature refinement angle, the per-process cell ceiling and '
    'gap detection.')

SNAPPING_FOLD_TOOLTIP = (
    'How the castellated mesh is pulled onto the surfaces: the snap '
    'iterations, the feature snapping and the mesher limits a snap step is '
    'checked against.')


def _embed(page: QWidget, host) -> None:
    """Strip ``page`` of the scroller and filter box the host now provides.

    The page's body is lifted out of its ``QScrollArea`` into the page's own
    layout, so the page is as tall as its settings and the host's scroller
    moves it. The filter row -- the filter box and the lock chip -- goes
    into a hidden holder: the host's box types into it
    (``SnappyRefinementPage._forward_filter``), and the host's chip is the
    row's one lock, because a row unlocks from its first task (DP-6382). The
    body's own top and bottom inset goes too: the host's body has one.
    """
    scroll = page._scroll
    outer = page.layout()
    index = outer.indexOf(scroll)
    body = scroll.takeWidget()
    outer.removeWidget(scroll)
    scroll.hide()
    outer.insertWidget(max(0, index), body, 1)
    body.show()
    holder = QWidget(page)
    holder.setObjectName('embeddedFilterHolder')
    holder.hide()
    page._filterRow.setParent(holder)
    outer.setContentsMargins(0, 0, 0, 0)
    page._body_layout.setContentsMargins(0, 0, 0, 0)
    # A wheel over a locked spin box scrolls the page that scrolls.
    page._readOnly._wheel_target = lambda: host._scroll.verticalScrollBar()


class SnappyRefinementPage(SnappySurfaceFeaturesPage):
    """Feature angle, refinement tables and snap settings: one row's page."""

    #: The tasks whose pages this one carries. `EngineBranchView` files them
    #: under their own ids and opens this page for any of them.
    hosted_task_ids = ('snappy.castellation', 'snappy.snap')

    #: ``task_id`` -- run the row's stages up to and including this one.
    stopAfterRequested = Signal(str)
    #: ``(stage, root)`` -- draw the kept mesh of ``stage`` from ``root``.
    stageMeshRequested = Signal(str, str)

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        self._hosted: dict = {}
        super().__init__(facade_client, parent, engine_id=engine_id)
        # After the base constructor: its `guard_cards` and first `refresh`
        # are this page's business, and the embedded pages run their own.
        area = self._hostedArea
        layout = area.layout()
        engine = engine_id or self.engine_id
        castellation = SnappyCastellationPage(
            self._client, area, engine_id=engine)
        snap = SnappySnapPage(self._client, area, engine_id=engine)
        for task_id, page in (('snappy.castellation', castellation),
                              ('snappy.snap', snap)):
            _embed(page, self)
            page.setObjectName(f'refinementHosted_{task_id.split(".")[-1]}')
            page.dirtyChanged.connect(self._on_hosted_dirty)
            self._hosted[task_id] = page
        layout.addWidget(castellation)
        layout.addWidget(snap)
        self._refinementControlsFold = self._fold_guided(castellation)
        self._snappingFold = fold_behind(
            layout, snap, self.tr('Snapping'), 'snappingFold',
            tooltip=self.tr(SNAPPING_FOLD_TOOLTIP))
        open_on_filter(self, self._snappingFold)
        open_on_filter(castellation, self._refinementControlsFold)
        for page in self._hosted.values():
            # One label column down the whole page (DP-154): an embedded
            # page measures with the host, not with its own forms alone.
            page._align_field_columns = (
                lambda page=page: self._align_hosted(page))
            watcher = getattr(page, '_labelWidths', None)
            if watcher is not None:
                # Its resize re-measure would cap the column at 44 % of the
                # embedded body alone; the host's watcher measures for all.
                page._body.removeEventFilter(watcher)
        self._align_field_columns()
        if self.filterText():
            self._forward_filter()

    # -- the page body ----------------------------------------------------- #

    def build_sections(self, layout) -> None:
        super().build_sections(layout)
        area = QWidget(self._body)
        area.setObjectName('refinementHostedSections')
        inner = QVBoxLayout(area)
        inner.setContentsMargins(0, 0, 0, 0)
        self._hostedArea = area
        layout.addWidget(area)

    def _fold_guided(self, castellation):
        """The Castellation page's own settings behind one closed fold.

        DP-6380. The page must fit the 300 px sheet (Plan 44 MF27); the
        refinement table is the decision this row is opened for, and the
        settings around it -- cells between levels, the feature refinement
        angle, the cell ceiling, gap detection -- are opened on purpose. The
        group sits in a holder, because the page shows and hides the group
        itself as its rows apply, and the fold must hold over that.
        """
        guided = castellation._guided
        body = castellation._body_layout
        holder = QWidget(guided.parentWidget())
        holder.setObjectName('refinementControls')
        inner = QVBoxLayout(holder)
        inner.setContentsMargins(0, 0, 0, 0)
        body.insertWidget(max(0, body.indexOf(guided)), holder)
        inner.addWidget(guided)
        return fold_behind(
            body, holder, self.tr('Refinement controls'),
            'refinementControlsFold',
            tooltip=self.tr(REFINEMENT_CONTROLS_TOOLTIP))

    def aligned_forms(self):
        forms = list(super().aligned_forms())
        for page in self._hosted_pages():
            forms += [page._guided.layout(), page._advanced.layout(),
                      page._expert.layout()]
            forms += list(page.field_forms())
            forms += list(page.aligned_forms())
        return [form for form in forms if form is not None]

    def _align_hosted(self, page) -> None:
        from foammesh.view.workflow_controls.task_page import EngineTaskPage
        self._align_field_columns()
        EngineTaskPage._dedupe_expert_toggles(page)

    def hostedPages(self) -> dict:
        """``task_id -> page`` for the pages this one carries."""
        return dict(self._hosted)

    def _hosted_pages(self):
        return tuple(getattr(self, '_hosted', {}).values())

    # -- one filter, one dirty flag, one save ------------------------------ #

    def _on_filter_text(self, _text: str = '') -> None:
        super()._on_filter_text(_text)
        self._forward_filter()

    def _forward_filter(self) -> None:
        text = self.filterText()
        for page in self._hosted_pages():
            page.setFilterText(text)

    def visibleFieldIds(self) -> tuple:
        found = list(super().visibleFieldIds())
        for page in self._hosted_pages():
            if page.isVisibleTo(self):
                found.extend(page.visibleFieldIds())
        return tuple(found)

    def _own_dirty(self) -> bool:
        return bool(self._pending) or any(
            panel.is_dirty for panel in self._panels)

    @property
    def is_dirty(self) -> bool:
        return self._own_dirty() or any(
            page.is_dirty for page in self._hosted_pages())

    def _on_hosted_dirty(self, _dirty: bool = False) -> None:
        self._set_dirty(bool(self._pending))

    def _set_dirty(self, dirty: bool) -> None:
        dirty = bool(dirty) or any(
            page.is_dirty for page in self._hosted_pages())
        super()._set_dirty(dirty)

    def apply(self, *, proposal: bool = False):
        """Update preview: write every page of the row that has an edit."""
        for page in self._hosted_pages():
            if page.is_dirty:
                page.apply(proposal=proposal)
        if self._own_dirty():
            return super().apply(proposal=proposal)
        return None

    async def save(self) -> bool:
        """Save this page's edits and each embedded page's, in row order.

        Proceed saves the page on screen (``commit_page_edits``); with three
        tasks on one page that is all three, so no edit on the Snapping fold
        is left behind by a press made from the feature angle.
        """
        refusals = []
        accepted = True
        if self._own_dirty() or (self._applying is not None
                                 and not self._applying.done()):
            if not await super().save():
                accepted = False
                refusals.append(self.last_save_refusal)
        for page in self._hosted_pages():
            if not page.is_dirty:
                continue
            if not await page.save():
                accepted = False
                refusals.append(getattr(page, 'last_save_refusal', ''))
        self.last_save_refusal = ' '.join(text for text in refusals if text)
        return accepted

    def revert(self):
        if self.is_dirty:
            for page in self._hosted_pages():
                if page.is_dirty:
                    page.revert()
            if not self._own_dirty():
                return None
        return super().revert()

    # -- locks: each task its own ------------------------------------------ #

    def _stays_live_when_locked(self, widget) -> bool:
        # An embedded page locks with its own task, not with this one.
        return (widget is getattr(self, '_hostedArea', None)
                or super()._stays_live_when_locked(widget))

    def _lockChildTables(self, locked: bool) -> None:
        from foammesh.view.workflow_controls.child_controls import (
            ChildControlPanel,
        )
        from foammesh.view.workflow_controls.lock_refusal import (
            locked_sentence,
        )

        task = self._task if isinstance(getattr(self, '_task', None),
                                        dict) else {}
        title = str(task.get('title') or self.task_id or '')
        note = locked_sentence([title] if title else [])
        area = getattr(self, '_hostedArea', None)
        for panel in self.findChildren(ChildControlPanel):
            if area is not None and area.isAncestorOf(panel):
                continue
            try:
                panel.setRowsLocked(locked, note)
            except Exception:                                # noqa: BLE001
                pass

    # -- the footer drop-down ---------------------------------------------- #

    def footerActions(self) -> list:
        actions = list(super().footerActions())
        actions.append((
            'footerStopAfterCastellation', self.tr('Stop after castellation'),
            self.tr('Extract the feature edges if they are not, run the '
                    'castellation pass and stop before the snap, so the '
                    'castellated mesh can be inspected.'),
            lambda: self.stopAfterRequested.emit('snappy.castellation')))
        root = self._castellation_snapshot()
        if root:
            actions.append((
                'footerShowCastellatedMesh', self.tr('Show castellated mesh'),
                self.tr('Draw the mesh the last castellation pass made, '
                        'before it was snapped.'),
                lambda root=root: self.stageMeshRequested.emit(
                    CASTELLATION_STAGE, root)))
        return actions

    def _castellation_snapshot(self) -> str:
        """The case root of the kept castellated mesh, or ``''``."""
        from foammesh.core.jobs import stage_snapshots

        case = getattr(self._client, 'case_path', None)
        if not case:
            return ''
        try:
            mesh = stage_snapshots.stage_mesh_path(case, CASTELLATION_STAGE)
        except Exception:                                    # noqa: BLE001
            return ''
        if mesh is None:
            return ''
        return str(mesh.parent.parent)


__all__ = ['SnappyRefinementPage']
