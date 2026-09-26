#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio

from PySide6.QtCore import QObject, Signal
from superqt import QLabeledDoubleRangeSlider
from vtkmodules.vtkRenderingAnnotation import vtkScalarBarActor

from widgets.progress_dialog import ProgressDialog

from foammesh.app import app
from foammesh.rendering.actor_info import (
    QUALITY_WORST_IS_LOW, MeshQualityIndex)
from foammesh.view.main_window.main_window_ui import Ui_MainWindow
from foammesh.support.colormap import qualityBandLut
from widgets.rendering.rendering_widget import RenderingWidget
from foammesh.view.theming.vtk_theme import apply_scalar_bar_theme


class MeshQualityInfo(QObject):
    #: What the user should be told about a colouring that could not happen.
    #: A signal rather than a direct status-bar call so the panel stays
    #: drivable without a main window.
    statusMessage = Signal(str)

    def __init__(self, ui: Ui_MainWindow):
        super().__init__()

        self._widget = ui.meshQualityInfo
        self._header = ui.meshQualityHeader
        self._index = ui.meshQualityIndex
        self._slider = ui.meshQualityRangeSlider
        self._applyButton = ui.meshQualityApply
        self._view: RenderingWidget = ui.renderingView

        self._header.setContents(ui.meshQualityGroupBox)

        self._index.addItem('Aspect ratio', MeshQualityIndex.ASPECT_RATIO)
        self._index.addItem('Non-orthogonal angle', MeshQualityIndex.NON_ORTHO_ANGLE)
        self._index.addItem('Skewness', MeshQualityIndex.SKEWNESS)
        self._index.addItem('Volume', MeshQualityIndex.VOLUME)

        defaultIndex = self._index.findData(MeshQualityIndex.VOLUME)
        self._index.setCurrentIndex(defaultIndex)

        self._slider.setEdgeLabelMode(QLabeledDoubleRangeSlider.EdgeLabelMode.LabelIsRange)
        self._slider.setHandleLabelPosition(QLabeledDoubleRangeSlider.LabelPosition.LabelsAbove)
        self._slider.setRange(0, 100)
        self._slider.setValue((10, 20))

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

    def isVisible(self):
        return self._widget.isVisible()

    def hide(self):
        self._widget.hide()
        self._clean()

    def show(self):
        self._header.setChecked(False)
        self._widget.show()
        self._updateAvailability()

    def activeIndex(self) -> MeshQualityIndex:
        return self._index.currentData()

    def band(self):
        return self._slider.value()

    def highlightWorst(self, index: MeshQualityIndex | None = None, *,
                       mayCompute: bool = True) -> bool:
        """Colour the worst tenth of a metric range in one call.

        Finding the bad cells used to mean guessing a band and dragging until
        something appeared. Returns False -- without pretending otherwise --
        when there is no mesh to measure; when the mesh simply has not had its
        quality fields computed yet, it computes them and then colours, because
        "not computed yet" is not the same answer as "cannot".
        """
        if index is not None:
            position = self._index.findData(index)
            if position >= 0:
                self._index.setCurrentIndex(position)
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

        if index in self.WORST_IS_HIGH:
            band = (high - span * self.WORST_FRACTION, high)
        else:
            band = (low, low + span * self.WORST_FRACTION)

        self._slider.setRange(low, high)
        self._slider.setValue(band)
        self._worstBand = band
        self._apply()
        return True

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
            which = self.tr('worst {0:.0f}% of range').format(
                100 * self.WORST_FRACTION)
        elif len(band) == 2:
            which = self.tr('cells {0:.3g} to {1:.3g}').format(*band)
        else:
            which = ''
        return f'{metric}\n{which}' if which else metric

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
        self._view.refresh()

    def _setHighlighting(self, active: bool):
        active = bool(active)
        if active == self._highlighting:
            return
        self._highlighting = active
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
        progress = ProgressDialog(app.window, self.tr('Mesh quality'))
        progress.setLabelText(self.tr(
            'Measuring cell quality over {0:,} cells…').format(
                manager.getNumberOfDisplayedCells()))
        progress.open()
        try:
            outcome = await manager.ensureQualityFields()
        finally:
            progress.close()
            self._computing = False

        self._updateAvailability()
        if outcome == 'ready':
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
        available = self._metricAvailable(self.activeIndex())
        hasMesh = self._hasMesh()
        self._applyButton.setEnabled(hasMesh)
        self._applyButton.setToolTip(
            '' if available or not hasMesh else self.tr(
                'Measures cell quality for this mesh the first time you use '
                'it, then colours by {0}.').format(self._index.currentText()))
        return available

    def applyTheme(self, tokens):
        if self._legend is not None:
            apply_scalar_bar_theme(self._legend, tokens)
            self._view.refresh()

    def _connectSignalsSlots(self, ui):
        self._header.toggled.connect(self._toggled)
        self._index.currentIndexChanged.connect(self._meshQualityIndexChanged)
        self._applyButton.clicked.connect(self._apply)

    def _toggled(self, checked):
        if checked:
            self._index.setCurrentIndex(0)
        else:
            self._clean()

        self._view.refresh()

    def _meshQualityIndexChanged(self, index: int):
        qualityIndex: MeshQualityIndex = self._index.itemData(index)
        self._updateAvailability()
        self._syncSliderRange(qualityIndex)

    def _syncSliderRange(self, qualityIndex: MeshQualityIndex):
        """Put the slider on the metric's real range."""
        if not app.window.meshManager:
            return
        left, right = app.window.meshManager.getScalarRange(qualityIndex)

        # superqt Slider has an issue when left and right are same
        if left == right:
            if left == 0:  # (0, 0) defaults to (0, 1)
                right = 1
            else:
                left = left * 0.99  # 1% Reduction
                right = right * 1.01  # 1% Addition

        # set slider range and interval
        self._slider.setRange(left, right)
        self._slider.setValue((left, right))

    def _applyAfterCompute(self):
        """Apply once the numbers exist -- on their range, not the placeholder.

        Until the field is computed `getScalarRange` answers (0, 1), so the
        band the user is looking at describes nothing. Applying it unchanged
        would threshold a real metric against a placeholder interval and show a
        confident, meaningless picture.
        """
        self._syncSliderRange(self.activeIndex())
        self._apply(mayCompute=False)

    def _apply(self, *, mayCompute: bool = True):
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
            app.window.meshManager.setScalarBand(*self._slider.value())
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
            self._legend.SetWidth(0.2)
            self._legend.SetHeight(0.35)
            self._legend.SetPosition(0.78, 0.55)
            self._view.addActor(self._legend)
            if app.themeManager is not None and app.themeManager.tokens is not None:
                apply_scalar_bar_theme(self._legend, app.themeManager.tokens)
        # DP-714. The scale the cells are painted on, and what it measures.
        low, high = self.band()
        self._legend.SetLookupTable(qualityBandLut(
            low, high, worstIsHigh=qualityIndex not in QUALITY_WORST_IS_LOW))
        self._legend.SetTitle(self.legendTitle())

        self._setHighlighting(True)
        self._view.refresh()

    def _clean(self):
        # Any measurement still running was started for a picture that is
        # being taken off right now, so its answer is no longer wanted.
        self._generation += 1
        self._worstBand = None
        if self._legend is not None:
            self._view.removeActor(self._legend)
            self._legend = None

        if app.window.meshManager:
            app.window.meshManager.clearCellFilter()
        self._setHighlighting(False)
