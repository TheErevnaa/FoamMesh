#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QComboBox
from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor

from widgets.progress_dialog import ProgressDialog

from foammesh.app import app
from foammesh.core.quality import inspector as limits
from foammesh.core.quality.cell_fields import DEFAULT_RANK_COUNT, histogram
from foammesh.rendering.actor_info import (
    QUALITY_WORST_IS_LOW, MeshActor, MeshQualityIndex)
from foammesh.rendering import quality_scale
from foammesh.rendering.cell_focus import CellFocus
from foammesh.view.main_window.main_window_ui import Ui_MainWindow
from foammesh.support import colour_standard
from widgets.rendering.rendering_widget import RenderingWidget
from foammesh.view.theming.vtk_theme import apply_scalar_bar_theme



async def rankingFromFile(path, count: int):
    """The worst *count* elements of every Gmsh measure of a mesh file.

    Plan 48 GM12 (R7): quality on a bare ``.msh`` needs no case. Gmsh measures
    the file in the qualified runtime, off the GUI thread, and the answer has
    the shape ``quality.cell_fields`` gives for an OpenFOAM case.
    """
    import asyncio

    from foammesh.core.gmsh import element_quality

    def measure():
        arrays = element_quality.measure_file(path)
        return element_quality.rank_worst(arrays, int(count))

    return await asyncio.get_running_loop().run_in_executor(None, measure)


class MeshQualityInfo(QObject):
    #: What the user should be told about a colouring that could not happen.
    #: A signal rather than a direct status-bar call so the panel stays
    #: drivable without a main window.
    statusMessage = Signal(str)
    #: Plan 39 VI9. A mesh is there to inspect (True) or is gone (False); the
    #: Quality inspector enables itself on this.
    availabilityChanged = Signal(bool)
    #: Plan 39 VI9. The colouring a section shares (its metric band) changed.
    scaleChanged = Signal()
    #: Plan 39 VI9. The worst-cell / beyond-limit colouring went on or off.
    highlightingChanged = Signal(bool)
    #: Plan 39 VI9. The limit in force for a metric changed (field name, value
    #: or None), so the inspector's field and the histogram's line follow.
    limitChanged = Signal(str, object)
    #: Plan 41 GN13. An element of the Gmsh mesh was inspected: the rows of
    #: its read-out (``cell_inspect.lines``), empty when nothing is.
    elementInspected = Signal(object)

    def __init__(self, ui: Ui_MainWindow):
        super().__init__()

        # Plan 39 VI9. The metric is held here and shown by the Quality
        # inspector; the Display Control group that used to own the combo, the
        # two-handle band and Apply is gone (Plan 39 Q3).
        self._index = QComboBox()
        self._shown = False
        self._band = (0.0, 1.0)
        self._range = (0.0, 1.0)
        #: The limit the user (or a prefill) set per metric.
        self._limits: dict = {}
        self._view: RenderingWidget = ui.renderingView

        # Plan 39 VI8 (F5d). Non-orthogonality first and the default: it is
        # what OpenFOAM meshes most often fail on, and a toolbar button that
        # defaulted to Volume told nobody that the other metrics existed.
        # Volume is last.
        for index in (*self.METRIC_ORDER, *self.GMSH_METRIC_ORDER):
            self._index.addItem(self.METRIC_LABELS[index], index)

        defaultIndex = self._index.findData(MeshQualityIndex.NON_ORTHO_ANGLE)
        self._index.setCurrentIndex(defaultIndex)
        #: How "worst cells" is cut: ('range', None) the worst tenth of the
        #: metric's range, ('count', N) the N worst cells, ('limit', None) the
        #: cells beyond the check limit. Remembered with the metric, so the
        #: toolbar button repeats the last choice.
        self._worstMode = ('range', None)
        self._worstKind = None
        self._focus = None
        self._inspector = None
        self._inspected = {}

        self._legend = None
        self._computing = False
        #: Who to tell when the colouring goes on or comes off. The viewport
        #: toolbar has to follow the picture rather than the press: a
        #: measurement that arrives seconds later is what actually paints.
        self._observer = None
        self._highlighting = False
        #: Bumped whenever the colouring is taken off, so a measurement that
        #: was started for a request nobody wants any more can tell.
        self._generation = 0

        self._connectSignalsSlots(ui)

    #: Metrics where a large number is the bad one. Volume is the exception:
    #: the cells that hurt are the tiny ones.
    WORST_IS_HIGH = frozenset({
        MeshQualityIndex.ASPECT_RATIO,
        MeshQualityIndex.NON_ORTHO_ANGLE,
        MeshQualityIndex.SKEWNESS,
    })
    #: "Show me the bad cells" means the tail, not a band a user has to find by
    #: dragging a slider until something turns red. A tenth of the *range*
    #: between the smallest and largest value on the mesh, not a tenth of the
    #: cells -- the control says which, because the two are different pictures
    #: and the slider the user then drags shows this one.
    WORST_FRACTION = 0.1
    #: DP-714. The unit each metric is shown in on the legend; '' for a
    #: dimensionless one.
    METRIC_UNITS = {
        MeshQualityIndex.ASPECT_RATIO: '',
        MeshQualityIndex.NON_ORTHO_ANGLE: '\u00b0',
        MeshQualityIndex.SKEWNESS: '',
        MeshQualityIndex.VOLUME: 'm\u00b3',
    }

    #: Plan 41 GN13. Gmsh's measures, offered when the mesh on screen is the
    #: session's (its cells carry them).
    GMSH_METRIC_ORDER = (
        MeshQualityIndex.GMSH_SICN,
        MeshQualityIndex.GMSH_SIGE,
        MeshQualityIndex.GMSH_GAMMA,
        MeshQualityIndex.GMSH_ISOTROPY,
        MeshQualityIndex.GMSH_MIN_JACOBIAN,
        MeshQualityIndex.GMSH_VOLUME,
    )
    #: Plan 39 VI8. The order the metrics are offered in, everywhere.
    METRIC_ORDER = (
        MeshQualityIndex.NON_ORTHO_ANGLE,
        MeshQualityIndex.SKEWNESS,
        MeshQualityIndex.ASPECT_RATIO,
        MeshQualityIndex.VOLUME,
    )
    METRIC_LABELS = {
        MeshQualityIndex.NON_ORTHO_ANGLE: 'Non-orthogonal angle',
        MeshQualityIndex.SKEWNESS: 'Skewness',
        MeshQualityIndex.ASPECT_RATIO: 'Aspect ratio',
        MeshQualityIndex.VOLUME: 'Volume',
        MeshQualityIndex.GMSH_SICN: 'SICN',
        MeshQualityIndex.GMSH_SIGE: 'SIGE',
        MeshQualityIndex.GMSH_GAMMA: 'Gamma',
        MeshQualityIndex.GMSH_ISOTROPY: 'Isotropy',
        MeshQualityIndex.GMSH_MIN_JACOBIAN: 'Smallest Jacobian',
        MeshQualityIndex.GMSH_VOLUME: 'Element volume',
    }
    #: The two letters the toolbar button wears for each metric.
    METRIC_BADGES = {
        MeshQualityIndex.NON_ORTHO_ANGLE: 'NO',
        MeshQualityIndex.SKEWNESS: 'SK',
        MeshQualityIndex.ASPECT_RATIO: 'AR',
        MeshQualityIndex.VOLUME: 'VO',
        MeshQualityIndex.GMSH_SICN: 'SI',
        MeshQualityIndex.GMSH_SIGE: 'SG',
        MeshQualityIndex.GMSH_GAMMA: 'GA',
        MeshQualityIndex.GMSH_ISOTROPY: 'IS',
        MeshQualityIndex.GMSH_MIN_JACOBIAN: 'JA',
        MeshQualityIndex.GMSH_VOLUME: 'EV',
    }
    #: Counts the Worst cells menu offers.
    WORST_COUNTS = (10, 100, 1000)

    def isVisible(self):
        return self._shown

    def hide(self):
        was = self._shown
        self._shown = False
        self._clean()
        self._syncStatistics()
        if was:
            self.availabilityChanged.emit(False)

    def show(self):
        was = self._shown
        self._shown = True
        self._updateAvailability()
        self.syncMetrics()
        self._syncStatistics()
        if not was:
            self.availabilityChanged.emit(True)

    def activeIndex(self) -> MeshQualityIndex:
        return self._index.currentData()

    def metricOrder(self) -> list:
        """The metrics the mesh on screen can be coloured by, in order.

        Plan 41 GN13. A mesh from the Gmsh session is measured by Gmsh's
        numbers; every other mesh by OpenFOAM's four.
        """
        manager = getattr(app.window, 'meshManager', None)
        reader = getattr(manager, 'qualityMetrics', None)
        order = list(reader()) if reader is not None else []
        if not order:
            return list(self.METRIC_ORDER)
        return [index for index in
                (*self.METRIC_ORDER, *self.GMSH_METRIC_ORDER)
                if index in order]

    def syncMetrics(self) -> None:
        """Offer the inspector the metrics the mesh on screen can be judged by."""
        self.adoptAvailableMetric()
        tab = getattr(self, '_rankTab', None)
        setter = getattr(tab, 'set_rank_metrics', None)
        if setter is not None:
            setter([(index.value, self.METRIC_LABELS[index])
                    for index in self.metricOrder()],
                   self.activeIndex().value)

    def adoptAvailableMetric(self) -> None:
        """Move the metric to one the mesh on screen has, if it has not."""
        order = self.metricOrder()
        if order and self.activeIndex() not in order:
            self.setWorstChoice(order[0])
            self._syncBandToRange(order[0])

    def band(self):
        return self._band

    # -- the remembered choice --------------------------------------------- #

    def worstChoice(self) -> dict:
        """What Worst cells will do when pressed: metric, and how it is cut."""
        mode, count = self._worstMode
        return {'metric': self.activeIndex(), 'mode': mode, 'count': count}

    def setWorstChoice(self, index: MeshQualityIndex | None = None,
                       mode: str | None = None, count: int | None = None):
        """Remember the metric and the cut. Paints nothing by itself.

        One memory for the toolbar button, the Display Control combo and the
        Quality tab: the combo *is* the metric, so expanding or collapsing the
        group can no longer put it back on its first entry (F5d).
        """
        if index is not None:
            position = self._index.findData(index)
            if position >= 0 and position != self._index.currentIndex():
                self._index.setCurrentIndex(position)
        tab = getattr(self, '_rankTab', None)
        if tab is not None:
            tab.set_rank_metric(self.activeIndex().value)
        if mode is not None:
            self._worstMode = (mode, count if mode == 'count' else None)
        # A limit exists for two of the four metrics unless one was typed.
        # Carrying "beyond the limit" over to a metric that has none would
        # leave the button promising a cut it cannot make.
        if self._worstMode[0] == 'limit' and self.limitFor(
                self.activeIndex()) is None:
            self._worstMode = ('range', None)

    def limitFor(self, index: MeshQualityIndex) -> float | None:
        """The limit in force for this metric, or None if it has none.

        Plan 39 VI9. A limit the user typed (or dragged, or took from the
        mesher) stands; otherwise the check's own limit, for the two metrics
        checkMesh judges. A proposed value never overwrites a typed one.
        """
        if index in self._limits:
            return self._limits[index]
        return self.checkLimitFor(index)

    def checkLimitFor(self, index: MeshQualityIndex) -> float | None:
        """The limit checkMesh judges this metric by, or None if it has none."""
        return self._projectLimit(index, limits.SOURCE_CHECK)

    def mesherLimitFor(self, index: MeshQualityIndex) -> float | None:
        """The limit the mesher aims for (snappy ``meshQuality``), or None."""
        return self._projectLimit(index, limits.SOURCE_MESHER)

    def _projectLimit(self, index, source):
        db = getattr(app, 'db', None)

        def read(path):
            return None if db is None else db.getValue(path)

        return limits.limit_value(read, index.value, source)

    def setLimit(self, index: MeshQualityIndex, value) -> None:
        """Hold *value* as the limit for a metric (None drops it)."""
        if value is None:
            self._limits.pop(index, None)
        else:
            self._limits[index] = float(value)
        self.limitChanged.emit(index.value, self._limits.get(index))

    def applyThresholds(self) -> list:
        """Plan 43 LY13 (D8, DP-1470). The Quality thresholds the user just
        applied (the project's ``meshQuality`` record) become the limit of
        every metric they judge, so the legend's limit line, the cells beyond
        the limit and the histogram all follow them. Returns the metrics set.
        """
        applied = []
        for index in MeshQualityIndex:
            value = self.mesherLimitFor(index)
            if value is None:
                continue
            self.setLimit(index, value)
            applied.append(index)
        if applied and self._highlighting:
            # The colouring on screen is repainted with the remembered cut.
            self.highlightWorst()
        else:
            self.scaleChanged.emit()
        return applied

    def limitIsSet(self, index: MeshQualityIndex) -> bool:
        return index in self._limits

    def highlightWorst(self, index: MeshQualityIndex | None = None, *,
                       mayCompute: bool = True,
                       mode: str | None = None,
                       count: int | None = None) -> bool:
        """Colour the worst tenth of a metric range in one call.

        Plan 39 VI8. ``mode`` and ``count`` cut it another way for this and
        later presses: the ``count`` worst cells, or the cells beyond the
        check limit.

        Finding the bad cells used to mean guessing a band and dragging until
        something appeared. Returns False -- without pretending otherwise --
        when there is no mesh to measure; when the mesh simply has not had its
        quality fields computed yet, it computes them and then colours, because
        "not computed yet" is not the same answer as "cannot".
        """
        self.setWorstChoice(index, mode, count)
        self.adoptAvailableMetric()
        index = self.activeIndex()
        if not self._metricAvailable(index):
            # `mayCompute` bounds this to one attempt per user action. Without
            # it the resumption re-enters here, finds the metric still absent,
            # and schedules another measurement -- forever.
            if mayCompute:
                return self._computeThen(
                    lambda: self.highlightWorst(index, mayCompute=False))
            self.statusMessage.emit(self.tr(
                'This mesh could not be measured for {0}.').format(
                    self._index.currentText()))
            return False

        manager = app.window.meshManager
        low, high = manager.getScalarRange(index)
        span = high - low
        if span <= 0:
            return False

        mode, count = self._worstMode
        band, kind = None, ('range', None)
        if mode == 'count':
            band = self._countBand(index, count, low, high)
            kind = ('count', count)
        elif mode == 'limit':
            limit = self.limitFor(index)
            beyond = (None if limit is None else
                      limits.beyond_band(limit, low, high, index.value))
            if limit is not None and beyond is None:
                # Nothing to show is an answer, and it is not "no bad cells"
                # painted as an empty picture.
                self.clearHighlight()
                worst = low if index in QUALITY_WORST_IS_LOW else high
                self.statusMessage.emit(self.tr(
                    'No cell is beyond the {0} limit of {1:g}{2}; the worst '
                    'is {3:.4g}.').format(
                        self._index.currentText(), limit,
                        self.METRIC_UNITS.get(index, ''), worst))
                return True
            if beyond is not None:
                band = beyond
                kind = ('limit', limit)
        if band is None:
            kind = ('range', None)
            if index in self.WORST_IS_HIGH:
                band = (high - span * self.WORST_FRACTION, high)
            else:
                band = (low, low + span * self.WORST_FRACTION)

        self._range = (low, high)
        self._band = (float(band[0]), float(band[1]))
        self._worstBand = band
        self._worstKind = kind
        self._apply()
        return True

    def _countBand(self, index, count, low, high):
        """The band holding the ``count`` worst cells, or None if unknowable.

        The Nth worst value is where the band starts; cells tied with it come
        along, so it is "at least N" only when the mesh has ties there.
        """
        manager = getattr(app.window, 'meshManager', None)
        volume = (manager.actorInfo('internalMesh')
                  if manager is not None and hasattr(manager, 'actorInfo')
                  else None)
        ranked = (volume.rankedCells(index, count)
                  if isinstance(volume, MeshActor) else [])
        if not ranked:
            return None
        edge = ranked[-1]['value']
        return ((edge, high) if index in self.WORST_IS_HIGH else (low, edge))

    def legendTitle(self) -> str:
        """DP-714. What the legend is a scale of: metric, unit, and band.

        Viewport audit 0925 F10: it read 0 to 1 with no title, whatever was
        coloured. The band says "worst 10% of range" when it is the one
        Poor cells chose, and its own ends when the user dragged another.
        """
        index = self.activeIndex()
        name = self._index.currentText()
        unit = self.METRIC_UNITS.get(index, '')
        metric = f'{name} ({unit})' if unit else name
        band = tuple(self.band() or ())
        worst = getattr(self, '_worstBand', None)
        if worst is not None and len(band) == 2 and all(
                abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
                for a, b in zip(band, worst)):
            kind, argument = self._worstKind or ('range', None)
            if kind == 'count':
                which = self.tr('the worst {0:,} cells').format(argument)
            elif kind == 'limit':
                which = self.tr('beyond the limit of {0:g}').format(argument)
            else:
                which = self.tr('worst {0:.0f}% of range').format(
                    100 * self.WORST_FRACTION)
        elif len(band) == 2:
            which = self.tr('cells {0:.3g} to {1:.3g}').format(*band)
        else:
            which = ''
        return f'{metric}\n{which}' if which else metric

    def bandLabel(self) -> str:
        """Plan 44 FX3 (L2). The legend title's second line: which band.

        "worst 10% of range", "the worst 1,000 cells", "beyond the limit of
        4", "cells 0.1 to 0.4" -- what the picture-only scalar bar's title
        said, for the themed legend to carry on screen.
        """
        title = self.legendTitle()
        return title.split('\n', 1)[1] if '\n' in title else ''

    def setHighlightObserver(self, observer):
        """Tell `observer(bool)` whenever the worst-cell colouring changes.

        The press is not the event worth reporting. A mesh that has never
        been measured paints only once the measurement lands, and a
        measurement can land after the user has already turned the colouring
        off -- so the control has to follow the viewport, not the click.
        """
        self._observer = observer

    def isHighlighting(self) -> bool:
        """Whether the worst-cell colouring is the picture on screen."""
        return self._highlighting

    def clearHighlight(self):
        """Take the worst-cell colouring off and abandon any measurement."""
        self._clean()
        if self._focus is not None:
            self._focus.clear()
        self._view.refresh()

    def _setHighlighting(self, active: bool):
        active = bool(active)
        if active == self._highlighting:
            return
        self._highlighting = active
        self.highlightingChanged.emit(active)
        if self._observer is not None:
            self._observer(active)

    def _artifactId(self):
        """Which mesh is on screen, or None when nothing can say."""
        manager = getattr(app.window, 'meshManager', None)
        reader = getattr(manager, 'artifactId', None)
        return reader() if reader is not None else None

    def _guarded(self, resume):
        """Wrap a resume so it declines to paint a picture nobody asked for.

        MEASURED, VIEW-06: with the colouring already taken off, the resume
        for the abandoned request still ran `applyCellFilter` and put the
        legend back, over a control reading off. Replacing the mesh between
        the request and the result did the same thing to the new mesh.
        """
        generation = self._generation
        identity = self._artifactId()

        def guarded():
            if generation != self._generation:
                return
            if identity != self._artifactId():
                return
            resume()

        return guarded

    def _metricAvailable(self, index) -> bool:
        manager = getattr(app.window, 'meshManager', None)
        if manager is None:
            return False
        hasScalar = getattr(manager, 'hasScalar', None)
        return bool(hasScalar(index)) if hasScalar is not None else True

    def _hasMesh(self) -> bool:
        manager = getattr(app.window, 'meshManager', None)
        return manager is not None and not manager.isEmpty()

    def _computeThen(self, resume) -> bool:
        """Compute the per-cell quality fields, then do what was asked.

        Every control here that needs a field it does not have comes through
        this one place, so there is a single answer to "how do these arrays get
        made" rather than one per button.
        """
        if not self._hasMesh():
            return False
        if self._computing:
            return True
        self._computing = True
        # VIEW-06. The guard goes on here, where the request is made, so
        # every caller that needs a field it does not have gets it -- there
        # is no second place to forget.
        resume = self._guarded(resume)
        # `create_task`, the same call the verdict strip's scheduler uses. An
        # `@asyncSlot` here would be the more obvious spelling and in this
        # environment it has silently scheduled nothing at all -- the strip sat
        # dormant over a live mesh for exactly that reason.
        asyncio.create_task(self._computeFields(resume))
        return True

    async def _computeFields(self, resume):
        manager = app.window.meshManager
        # Plan 39 VI1 (F6b). The measurement runs in a worker process; Cancel
        # stops the task awaiting it, which kills that worker's tree, and asks
        # the case to stop anything else it holds, as every progress surface does.
        progress = ProgressDialog(app.window, self.tr('Mesh quality'),
                                  cancelable=True, autoCloseOnCancel=False,
                                  onViewport=True)
        progress.setLabelText(self.tr(
            'Measuring cell quality over {0:,} cells…').format(
                manager.getNumberOfDisplayedCells()))
        progress.open()
        measuring = asyncio.ensure_future(manager.ensureQualityFields())
        cancelled = []

        def _cancelMeasuring():
            cancelled.append(True)
            progress.setLabelText(self.tr('Cancelling the measurement…'))
            measuring.cancel()
            self._cancelTask = asyncio.ensure_future(
                app.facadeClient.cancel_active_jobs())

        progress.cancelClicked.connect(_cancelMeasuring)
        try:
            outcome = await measuring
        except asyncio.CancelledError:
            if not cancelled:
                raise
            outcome = 'cancelled'
        finally:
            progress.close()
            self._computing = False

        self._updateAvailability()
        if outcome == 'cancelled':
            self.statusMessage.emit(self.tr(
                'Cell quality measurement cancelled.'))
            return
        if outcome == 'ready':
            self._redrawInspector()
            resume()
            return
        self.statusMessage.emit({
            'no-mesh': self.tr('No mesh is loaded.'),
            'mismatch': self.tr(
                'The cell measurements do not describe the mesh on screen, so '
                'nothing was coloured.'),
        }.get(outcome, self.tr(
            'Cell quality could not be measured for this mesh.')))

    def _updateAvailability(self):
        """Say what the control will do, rather than refusing without recourse.

        This used to disable Apply and explain that the mesh "carries no field"
        and that the user should run a mesh check that writes cell fields.
        Both halves were wrong. The arrays do not come from `checkMesh` --
        Foundation v13 has no ``-writeAllFields`` to run -- they are computed
        from the polyMesh by `quality.cell_fields`, and nothing was asking it
        for them. So the control disabled itself and sent the user after a
        command that does not exist, on a mesh that could have been coloured
        all along.
        """
        return self._metricAvailable(self.activeIndex())

    def applyTheme(self, tokens):
        if self._legend is not None:
            apply_scalar_bar_theme(self._legend, tokens)
            self._view.refresh()

    def _connectSignalsSlots(self, ui):
        self._index.currentIndexChanged.connect(self._meshQualityIndexChanged)

    def _meshQualityIndexChanged(self, index: int):
        qualityIndex: MeshQualityIndex = self._index.itemData(index)
        self._updateAvailability()
        self._syncBandToRange(qualityIndex)

    def _syncBandToRange(self, qualityIndex: MeshQualityIndex):
        """Put the band on the metric's real range."""
        if not app.window.meshManager:
            return
        left, right = app.window.meshManager.getScalarRange(qualityIndex)

        # A band with no width cannot be thresholded or drawn.
        if left == right:
            if left == 0:  # (0, 0) defaults to (0, 1)
                right = 1
            else:
                left = left * 0.99  # 1% Reduction
                right = right * 1.01  # 1% Addition

        self._range = (left, right)
        self._band = (float(left), float(right))

    def _applyAfterCompute(self):
        """Apply once the numbers exist -- on their range, not the placeholder.

        Until the field is computed `getScalarRange` answers (0, 1), so the
        band the user is looking at describes nothing. Applying it unchanged
        would threshold a real metric against a placeholder interval and show a
        confident, meaningless picture.
        """
        self._syncBandToRange(self.activeIndex())
        self._apply(mayCompute=False)

    def _apply(self, *, mayCompute: bool = True):
        self.adoptAvailableMetric()
        qualityIndex: MeshQualityIndex = self._index.currentData()

        # Thresholding an array that is not there colours nothing, and nothing
        # coloured reads as "no bad cells" -- the one answer this control must
        # never give by accident.
        if not self._metricAvailable(qualityIndex):
            if mayCompute:
                self._computeThen(self._applyAfterCompute)
            else:
                self.statusMessage.emit(self.tr(
                    'This mesh could not be measured for {0}.').format(
                        self._index.currentText()))
            return

        if app.window.meshManager:
            app.window.meshManager.setScalar(qualityIndex)
            app.window.meshManager.setScalarBand(*self._band)
            app.window.meshManager.applyCellFilter()

        if self._legend is None:
            self._legend = vtkScalarBarActor()
            self._legend.SetObjectName('qualityLegend')
            self._legend.UnconstrainedFontSizeOn()
            self._legend.SetLabelFormat('%.3g')
            self._legend.SetTitleRatio(0.5)
            self._legend.SetBarRatio(0.25)
            # DP-714. Wide enough that the centred title stays on screen,
            # and above the deviation legend, which has the corner below.
            # Plan 39 VI15 (I4). The overlay layout stacks it with the
            # other legends; it no longer picks a corner of its own.
            # Plan 43 LY11. The one Qt legend draws the bar; this actor stays
            # for the picture-taking path and is not shown on screen.
            # Plan 44 FX3 (L2). Hiding it here was not enough: addLegend (and
            # every Show legend / colour standard change) set it visible
            # again, so a second "worst 10% of range" bar drew at the left
            # of the clear area. The view keeps it picture-only now.
            self._legend.SetVisibility(False)
            self._view.addLegend('quality', self._legend)
            pictureOnly = getattr(self._view, 'setLegendOnScreen', None)
            if pictureOnly is not None:
                pictureOnly('quality', False)
            else:
                self._legend.SetVisibility(False)
            if app.themeManager is not None and app.themeManager.tokens is not None:
                apply_scalar_bar_theme(self._legend, app.themeManager.tokens)
        # DP-714. The scale the cells are painted on, and what it measures.
        low, high = self.band()
        self._legend.SetLookupTable(colour_standard.lookup_table(
            'quality', low, high, qualityIndex not in QUALITY_WORST_IS_LOW,
            limit=self._limits.get(qualityIndex)))
        self._legend.SetTitle(self.legendTitle())

        # Plan 39 VI9: a section coloured by this metric takes this band.
        quality_scale.set_band(qualityIndex.value, low, high,
                               self._limits.get(qualityIndex))
        self.scaleChanged.emit()
        self._setHighlighting(True)
        self._view.refresh()

    # -- worst cells as a list, and going to them -------------------------- #

    def _cellFocus(self) -> CellFocus:
        if self._focus is None:
            self._focus = CellFocus(self._view)
        return self._focus

    def flyToElement(self, element: dict) -> bool:
        """Take the camera to a ranked cell (or Gmsh element) and outline it.

        Plan 39 VI8 (F5b). The row carries the centroid; when it also names a
        cell of the loaded volume that cell's own faces are the outline.
        """
        centroid = element.get('centroid') or ()
        cell = element.get('cell')
        dataSet = None
        manager = getattr(app.window, 'meshManager', None)
        if cell is not None and manager is not None and hasattr(
                manager, 'actorInfo'):
            volume = manager.actorInfo('internalMesh')
            if isinstance(volume, MeshActor):
                dataSet = volume.dataSet()
        moved = self._cellFocus().flyToCell(
            centroid, element.get('size') or 0.0, dataSet=dataSet, cell=cell)
        if moved and dataSet is not None and cell is not None:
            # Plan 41 GN13. Going to a Gmsh element also reads it.
            self.inspectCell(dataSet, cell)
        return moved

    # -- Inspect element (Plan 41 GN13, A-13, I-02) ------------------------- #

    def inspected(self) -> dict:
        """The element last inspected, or {}."""
        return dict(self._inspected)

    def inspectCell(self, dataSet, cell) -> dict:
        """Read cell *cell* of *dataSet* and tell the inspector.

        The picked cell is a cell of what the mapper draws; its tag finds the
        element in the volume or a patch, which also carries the node tags.
        """
        from foammesh.core.mesh import cell_inspect

        found = cell_inspect.describe_cell(dataSet, cell)
        if not found:
            return {}
        sources = self._meshDataSets()
        source, row = cell_inspect.find_cell(sources, found['tag'])
        if source is not None:
            found = cell_inspect.describe_cell(source, row)
        self._inspected = found
        self.elementInspected.emit(cell_inspect.lines(found))
        return dict(found)

    def inspectElementTag(self, tag) -> dict:
        """Read element *tag* wherever the mesh on screen holds it."""
        from foammesh.core.mesh import cell_inspect

        source, row = cell_inspect.find_cell(self._meshDataSets(), tag)
        if source is None:
            return {}
        return self.inspectCell(source, row)

    def _meshDataSets(self) -> list:
        manager = getattr(app.window, 'meshManager', None)
        reader = getattr(manager, 'dataSets', None)
        return list(reader()) if reader is not None else []

    def setPickingElements(self, on: bool) -> None:
        """Switch clicking a cell to read it on or off."""
        if self._inspector is None:
            if not on:
                return
            from foammesh.rendering.cell_inspector import CellInspector

            self._inspector = CellInspector(
                self._view, lambda data, cell: bool(
                    self.inspectCell(data, cell)))
        self._inspector.setActive(bool(on))

    def isPickingElements(self) -> bool:
        return self._inspector is not None and self._inspector.isActive()

    def flyToFailedCells(self) -> bool:
        """Frame the failed-cell set on screen. False when none is shown."""
        manager = getattr(app.window, 'meshManager', None)
        overlay = (manager.actorInfo('failedCells')
                   if manager is not None and hasattr(manager, 'actorInfo')
                   else None)
        if overlay is None:
            return False
        try:
            bounds = tuple(overlay.dataSet().GetBounds())
        except Exception:                                          # noqa: BLE001
            return False
        return self._cellFocus().flyToBounds(bounds)

    def focusedElement(self):
        """The row the camera was last sent to, or None."""
        return None if self._focus is None else self._focus.focused()

    async def rankWorstCells(self, index: MeshQualityIndex | None = None,
                             count: int = DEFAULT_RANK_COUNT):
        """The ``count`` worst cells of a metric, worst first.

        Returns ``(outcome, rows)``. Locally when the loaded volume already
        carries the arrays (and there are no zones to name); otherwise in the
        mesh worker, which reads the polyMesh -- with progress and Cancel, as
        every measurement here has.
        """
        index = index or self.activeIndex()
        manager = getattr(app.window, 'meshManager', None)
        volume = (manager.actorInfo('internalMesh')
                  if manager is not None and hasattr(manager, 'actorInfo')
                  else None)
        if (isinstance(volume, MeshActor) and volume.hasScalar(index)
                and not manager.zoneIds()):
            return 'ready', volume.rankedCells(index, count)

        progress = ProgressDialog(app.window, self.tr('Worst cells'),
                                  cancelable=True, autoCloseOnCancel=False,
                                  onViewport=True)
        progress.setLabelText(self.tr(
            'Ranking the {0} worst cells by {1}…').format(
                count, self._index.itemText(self._index.findData(index))))
        progress.open()
        ranking = asyncio.ensure_future(self._workerRanking(count))
        cancelled = []

        def _cancelRanking():
            cancelled.append(True)
            progress.setLabelText(self.tr('Cancelling…'))
            ranking.cancel()
            self._cancelTask = asyncio.ensure_future(
                app.facadeClient.cancel_active_jobs())

        progress.cancelClicked.connect(_cancelRanking)
        try:
            ranked = await ranking
        except asyncio.CancelledError:
            if not cancelled:
                raise
            return 'cancelled', []
        except Exception:                                          # noqa: BLE001
            return 'failed', []
        finally:
            progress.close()
        rows = (ranked or {}).get(index.value)
        return ('ready', list(rows)) if rows is not None else ('failed', [])

    async def _workerRanking(self, count: int):
        from foammesh.view.facade_client import query_async

        try:
            result = await query_async(
                app.facadeClient, 'quality.cell_fields', {'rank_n': int(count)})
        except Exception:                                          # noqa: BLE001
            # Plan 48 GM12. A bare Gmsh mesh has no polyMesh for the case
            # operation to read; it is ranked from the arrays Gmsh measures
            # on the file itself.
            manager = getattr(app.window, 'meshManager', None)
            native = getattr(manager, 'nativePath', lambda: None)()
            if native is None:
                raise
            return await rankingFromFile(native, count)
        return (result.payload or {}).get('ranked')

    def requestRanking(self, tab, metric=None, count: int = DEFAULT_RANK_COUNT):
        """Rank and put the answer in ``tab``. Called from a button's slot."""
        self._rankTab = tab
        return asyncio.ensure_future(self._rankInto(tab, metric, count))

    async def _rankInto(self, tab, metric, count):
        index = MeshQualityIndex(metric) if metric else self.activeIndex()
        self.setWorstChoice(index)
        outcome, rows = await self.rankWorstCells(index, count)
        label = self._index.itemText(self._index.findData(index))
        if outcome == 'ready':
            tab.show_ranked(label, rows)
            if not rows:
                self.statusMessage.emit(self.tr(
                    'No cell could be ranked for {0}.').format(label))
        elif outcome == 'cancelled':
            self.statusMessage.emit(self.tr('Ranking cancelled.'))
        else:
            self.statusMessage.emit(self.tr(
                'This mesh could not be ranked for {0}.').format(label))
        return outcome, rows

    # -- the distribution and the limit (Plan 39 VI9) ----------------------- #

    def metricValues(self, index: MeshQualityIndex | None = None):
        """The per-cell array of a metric on the loaded volume, or None."""
        index = index or self.activeIndex()
        manager = getattr(app.window, 'meshManager', None)
        volume = (manager.actorInfo('internalMesh')
                  if manager is not None and hasattr(manager, 'actorInfo')
                  else None)
        if not isinstance(volume, MeshActor):
            return None
        from vtkmodules.util.numpy_support import vtk_to_numpy

        data = volume.dataSet()
        array = data.GetCellData().GetArray(index.value) if data else None
        return None if array is None else vtk_to_numpy(array)

    def distribution(self, index: MeshQualityIndex | None = None,
                     bins: int = 40) -> dict:
        """The histogram of a metric over every cell, or {} if not measured.

        Counted from the array the colouring thresholds, so the bars sum to
        the cell count and a bar is the cells the picture would show.
        """
        values = self.metricValues(index)
        return {} if values is None else histogram(values, bins=bins)

    def beyondCount(self, index: MeshQualityIndex | None = None):
        """``(cells beyond the limit, cells)`` or None without a limit or
        a measurement. Exact, never read off the bars."""
        index = index or self.activeIndex()
        limit = self.limitFor(index)
        values = self.metricValues(index)
        if limit is None or values is None:
            return None
        return int(limits.beyond_mask(values, limit, index.value).sum()),             int(values.size)

    def refreshDistribution(self, tab, *, mayCompute: bool = True) -> bool:
        """Draw the active metric's histogram, limit line and count on *tab*.

        Measures first when the cells have no quality arrays yet (once per
        press, like every control here). False when there is nothing to draw.
        """
        self._rankTab = tab
        index = self.activeIndex()
        # A histogram of another metric must not stay up under this one.
        tab.set_distribution('', {})
        if not self._hasMesh():
            return False
        if not self._metricAvailable(index):
            if mayCompute:
                return self._computeThen(
                    lambda: self.refreshDistribution(tab, mayCompute=False))
            return False
        hist = self.distribution(index)
        label = self._index.itemText(self._index.findData(index))
        limit = self.limitFor(index)
        tab.set_distribution(label, hist, limit=limit,
                             beyond_is_above=index not in QUALITY_WORST_IS_LOW)
        tab.set_beyond_count(self.beyondCount(index), limit)
        return bool(hist)

    def _redrawInspector(self) -> None:
        """The cells now carry their quality: draw what the inspector shows."""
        tab = getattr(self, '_rankTab', None)
        if tab is None:
            return
        try:
            self.refreshDistribution(tab, mayCompute=False)
            index = self.activeIndex()
            tab.set_beyond_count(self.beyondCount(index),
                                 self.limitFor(index))
        except Exception:                                          # noqa: BLE001
            pass

    def bindInspector(self, tab) -> None:
        """The Quality inspector this controller draws into."""
        self._rankTab = tab
        tab.set_statistics_source(self._gmshController, self._gmshClient())
        tab.statistics_panel().verdictReady.connect(self._judgedVerdict)
        tab.exportReportRequested.connect(lambda: self.exportReport(tab))

    # -- Plan 42 SG7: Gmsh's statistics in the tab ------------------------- #

    @staticmethod
    def _gmshClient():
        return getattr(app, '_facadeClient', None)

    def _gmshController(self):
        """The resident session controller; never starts a session."""
        from foammesh.view.gmsh_workflow.compute_page import (
            residentGenerateController)
        client = self._gmshClient()
        return (residentGenerateController(client)
                if client is not None else None)

    def syncStatistics(self) -> None:
        """Re-read the Gmsh items (a mesh edit or a new session mesh)."""
        self._syncStatistics()

    def _syncStatistics(self) -> None:
        """Show the Gmsh items while the volume on screen carries them."""
        tab = getattr(self, '_rankTab', None)
        if tab is None:
            return
        manager = getattr(getattr(app, 'window', None), 'meshManager', None)
        has = getattr(manager, 'hasGmshQuality', None)
        try:
            on = self._shown and callable(has) and bool(has())
            tab.show_mesh_statistics(on)
        except Exception:                                          # noqa: BLE001
            pass

    def _judgedVerdict(self, verdict: dict) -> None:
        show = getattr(getattr(app, 'window', None), 'showMeshVerdict', None)
        if callable(show):
            show(verdict)

    # -- the report (Plan 39 VI17, I7) ------------------------------------- #

    def reportIdentity(self) -> dict:
        """Engine, stage, revision and cell count of what is on screen."""
        manager = getattr(app.window, 'meshManager', None)

        def ask(name):
            getter = getattr(manager, name, None)
            try:
                return getter() if getter is not None else ''
            except Exception:                                      # noqa: BLE001
                return ''

        engine = ''
        try:
            from foammesh.view.engine_context import configured_engine
            engine = configured_engine(app)
        except Exception:                                          # noqa: BLE001
            pass
        root = self._caseRoot()
        return {'engine': engine, 'stage': ask('resultLabel'),
                'revision': ask('artifactId') or ask('runId'),
                'cells': ask('getNumberOfCells') or '',
                'case': str(root) if root else ''}

    @staticmethod
    def _caseRoot():
        """The open case's folder, or None when no case is open."""
        try:
            return getattr(getattr(app, 'facadeClient', None),
                           'case_root', None)
        except Exception:                                          # noqa: BLE001
            return None

    @staticmethod
    def _png(image) -> bytes:
        from PySide6.QtCore import QBuffer, QByteArray, QIODevice

        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'PNG')
        return bytes(data)

    def _histogramPng(self, tab) -> bytes | None:
        from PySide6.QtCore import QPoint
        from PySide6.QtGui import QImage, QPainter

        widget = tab.distribution_widget()
        width, height = ((widget.width(), widget.height())
                         if widget.width() > 100 else (640, 240))
        if widget.width() <= 100:
            widget.resize(width, height)
        image = QImage(width * 2, height * 2, QImage.Format.Format_ARGB32)
        image.fill(widget.palette().window().color())
        painter = QPainter(image)
        painter.scale(2, 2)
        widget.render(painter, QPoint())
        painter.end()
        return self._png(image)

    def exportReport(self, tab, case_root=None) -> dict:
        """Write the report beside the case's captures.

        It lists what the tab lists (the cells in its table, its metrics and
        findings), draws the active metric's histogram and a 2x capture of the
        view. Returns the paths, or {} when no case is open.
        """
        from foammesh.core.quality.quality_report import write_report

        root = case_root or self._caseRoot()
        if not root:
            self.statusMessage.emit(self.tr(
                'There is no case to write the report into.'))
            return {}
        index = self.activeIndex()
        label = self._index.itemText(self._index.findData(index))
        view_png = None
        try:
            from foammesh.view.main_window.view_capture import composeView
            image = composeView(self._view, scale=2)
            view_png = self._png(image) if image is not None else None
        except Exception:                                          # noqa: BLE001
            view_png = None
        paths = write_report(
            root, identity=self.reportIdentity(), verdict=tab.verdict(),
            metric_label=label, worst_title=tab.listed_title(),
            worst=tab.listed_elements(), limit=self.limitFor(index),
            histogram=self.distribution(index),
            histogram_png=self._histogramPng(tab), view_png=view_png)
        self.statusMessage.emit(self.tr('Quality report written: {0}').format(
            paths['html']))
        return paths

    def isShowingBeyondLimit(self) -> bool:
        """Whether the colouring on screen is the cells beyond a limit."""
        return self._highlighting and (self._worstKind or ('',))[0] == 'limit'

    def useLimit(self, index: MeshQualityIndex, source: str) -> float | None:
        """Take the mesher's or the check's limit for a metric as the limit."""
        value = (self.mesherLimitFor(index)
                 if source == limits.SOURCE_MESHER
                 else self.checkLimitFor(index))
        if value is not None:
            self.setLimit(index, value)
        return value

    def showBeyondLimit(self, on: bool) -> bool:
        """Colour exactly the cells beyond the limit, or take it off."""
        if not on:
            self.clearHighlight()
            return True
        if self.limitFor(self.activeIndex()) is None:
            self.statusMessage.emit(self.tr(
                'There is no limit for {0}; type one first.').format(
                    self._index.currentText()))
            return False
        return self.highlightWorst(mode='limit')

    def _clean(self):
        # Any measurement still running was started for a picture that is
        # being taken off right now, so its answer is no longer wanted.
        self._generation += 1
        self._worstBand = None
        self._worstKind = None
        if self._legend is not None:
            self._view.removeLegend('quality')
            self._legend = None

        if app.window.meshManager:
            app.window.meshManager.clearCellFilter()
        if quality_scale.band() is not None:
            quality_scale.clear_band()
            self.scaleChanged.emit()
        self._setHighlighting(False)
