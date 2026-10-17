"""Plan 43 LY11: feeds the one viewport legend from what is on screen.

The colouring on screen decides the legend: a painted deviation is the
Deviation mode, the poor-cell colouring is the Quality mode, and the Patches
mode is the plain patch colours (reached from the segment, which takes the
other two colourings off). The controller reads the window's mesh manager and
its quality control; it measures nothing.
"""
from __future__ import annotations

import logging

from foammesh.core.quality import legend_model as lm
from foammesh.support import colour_standard
from foammesh.view.display_control.viewport_legend import ViewportLegend
from foammesh.view.main_window.clear_area import FLOAT_RANK_LEGEND

logger = logging.getLogger(__name__)


class LegendController:
    """Owns the legend widget and keeps it in step with the colouring."""

    def __init__(self, window, parent):
        self._window = window
        self.legend = ViewportLegend(parent)
        #: The session's scale. LY12 replaces this with the colour standard.
        self.choice = lm.APP_STANDARD
        self.standard = lm.APP_STANDARD
        self._patchesShown = False
        self._rowsBound = False
        #: Callables told the mode on screen after every refresh (the View
        #: menu's "Colour the model by" follows the surface through this).
        self._modeListeners: list = []
        self._popover = None
        self._clearBound = False
        legend = self.legend
        legend.modeRequested.connect(self.modeRequested)
        legend.metricRequested.connect(self.metricRequested)
        legend.toleranceChanged.connect(self.toleranceEdited)
        legend.scaleToChanged.connect(self.scaleToEdited)
        legend.scaleButtonClicked.connect(self.scaleButtonClicked)

    # -- what is on screen -------------------------------------------------- #

    def _manager(self):
        return getattr(self._window, '_meshManager', None)

    def _info(self):
        display = getattr(self._window, '_displayControl', None)
        return display.meshQualityInfo() if display is not None else None

    def mode(self) -> str | None:
        """The mode the colouring on screen is in, or None for no colouring."""
        manager = self._manager()
        read = getattr(manager, 'deviationReadout', None)
        if read is not None and read() is not None:
            return lm.DEVIATION
        info = self._info()
        if info is not None and info.isHighlighting():
            return lm.QUALITY
        return lm.PATCHES if self._patchesShown else None

    def _rows(self):
        """The Mesh quality tab's surface rows, bound on first use."""
        tab = getattr(self._window, '_meshQualityTab', None)
        rows = tab.surface_rows() if hasattr(tab, 'surface_rows') else None
        if rows is not None and not self._rowsBound:
            self._rowsBound = True
            rows.toleranceChanged.connect(self.toleranceEdited)
        return rows

    def onModeShown(self, callback):
        """*callback(mode)* after each refresh; mode is None with no
        colouring.  Returns the function that stops it."""
        self._modeListeners.append(callback)
        return lambda: self._modeListeners.remove(callback)

    def _tellMode(self, mode):
        for callback in list(self._modeListeners):
            try:
                callback(mode)
            except Exception:                             # noqa: BLE001
                logger.debug('mode listener failed', exc_info=True)

    def _bindClearArea(self):
        """Place the legend by the clear area (MF7) once the window has one.

        The window builds its clear-area controller after this one, so the
        first refresh is the earliest moment it can be found.
        """
        if self._clearBound:
            return
        clear = getattr(self._window, '_clearArea', None)
        if clear is None:
            return
        self._clearBound = True
        self.legend.setClearProvider(clear.clearRect)
        # FX1 (DP-2621). The legend is one of the clear area's floating
        # items: it keeps off an open popover and the Parts chip.
        add_float = getattr(clear, 'add_float', None)
        if callable(add_float):
            legend = self.legend
            legend.setAvoider(
                lambda rect: clear.place_clear_of(legend, rect))
            add_float(legend, FLOAT_RANK_LEGEND, legend.place)
        try:
            clear.changed.connect(lambda _rect: self.legend.place())
        except (AttributeError, TypeError):
            logger.debug('clear area has no changed signal', exc_info=True)

    def refresh(self):
        """Redraw the legend from the colouring on screen."""
        self._bindClearArea()
        try:
            mode = self.mode()
            self._tellMode(mode)
            rows = self._rows()
            if rows is not None and mode != lm.DEVIATION:
                rows.clearDeviation()
            if mode == lm.DEVIATION:
                self._showDeviation()
            elif mode == lm.QUALITY:
                self._showQuality()
            elif mode == lm.PATCHES:
                self._showPatches()
            else:
                self.legend.clearSpec()
        except Exception:                                 # noqa: BLE001
            logger.debug('legend refresh failed', exc_info=True)
            self.legend.clearSpec()

    def _showDeviation(self):
        readout = self._manager().deviationReadout()
        fields = readout.signed_fields or readout.fields
        spec = lm.deviation_spec(
            fields.values(), readout.scale, readout.tolerance,
            signed=readout.signed, source=readout.source,
            choice=self.choice, standard=self.standard)
        state = getattr(self._manager(), '_deviationState', None) or {}
        self.legend.setSpec(
            spec, readout.lookup_table, tolerance=readout.tolerance,
            scale_to=readout.scale,
            auto_scale=state.get('scale_user') in (None, 0, 0.0))
        self._describeBand()
        rows = self._rows()
        if rows is not None:
            rows.showDeviation(spec, readout.tolerance)

    def _showQuality(self):
        info = self._info()
        index = info.activeIndex()
        low, high = info.band()
        worst_is_high = index not in _low_is_bad()
        volume = None
        manager = self._manager()
        getter = getattr(manager, 'actorInfo', None)
        if getter is not None:
            volume = getter('internalMesh')
        values = None
        if volume is not None and hasattr(volume, 'metricValues'):
            values = volume.metricValues(index)
        metrics = tuple(info.METRIC_LABELS[i] for i in info.metricOrder())
        spec = lm.quality_spec(
            info.METRIC_LABELS[index], low, high,
            unit=info.METRIC_UNITS.get(index, ''),
            worst_is_high=worst_is_high, limit=info.limitFor(index),
            values=values, metrics=metrics, choice=self.choice,
            standard=self.standard)
        self.legend.setSpec(spec, colour_standard.lookup_table(
            'quality', low, high, worst_is_high,
            limit=info.limitFor(index)))
        self._describeBand(info)

    def _describeBand(self, info=None):
        """Plan 44 FX3 (L2). The bar says which band it paints.

        The picture-only scalar bar's title ("worst 10% of range", "the
        worst 1,000 cells", "cells 0.1 to 0.4") drew as a second legend on
        screen. It is off the screen now, so the one legend's bar carries
        those words: its tooltip and accessible description.
        """
        getter = getattr(self.legend, 'bar', None)
        bar = getter() if getter is not None else None
        if bar is None:
            return
        reader = getattr(info, 'legendTitle', None) if info is not None else None
        words = ''
        if reader is not None:
            try:
                words = str(reader() or '').replace('\n', ': ')
            except Exception:                             # noqa: BLE001
                words = ''
        bar.setToolTip(words)
        bar.setAccessibleDescription(words)

    def _showPatches(self):
        manager = self._manager()
        rows = []
        for name in (manager.patchIds() if manager is not None else ()):
            actor = manager.actorInfo(name)
            colour = actor.color() if actor is not None else None
            if colour is not None and hasattr(colour, 'redF'):
                rows.append((name, (colour.redF(), colour.greenF(),
                                    colour.blueF())))
            else:
                rows.append((name, None))
        self.legend.setSpec(lm.patches_spec(
            rows, self.choice, self.standard))
        self._describeBand()

    # -- what the legend asks for ------------------------------------------- #

    def modeRequested(self, mode: str):
        window = self._window
        info = self._info()
        self._patchesShown = mode == lm.PATCHES
        try:
            if mode == lm.PATCHES:
                if getattr(window, '_fidelityPainted', False):
                    window.toggleGeometryFidelity()
                if info is not None and info.isHighlighting():
                    info.clearHighlight()
            elif mode == lm.QUALITY:
                if getattr(window, '_fidelityPainted', False):
                    window.toggleGeometryFidelity()
                if info is not None and not info.isHighlighting():
                    info.highlightWorst()
            elif mode == lm.DEVIATION:
                if info is not None and info.isHighlighting():
                    info.clearHighlight()
                if not getattr(window, '_fidelityPainted', False):
                    window.toggleGeometryFidelity()
        finally:
            self.refresh()

    def metricRequested(self, label: str):
        info = self._info()
        if info is None:
            return
        for index in info.metricOrder():
            if info.METRIC_LABELS[index] == label:
                info.highlightWorst(index)
                break
        self.refresh()

    def toleranceEdited(self, value: float):
        window = self._window
        panel = getattr(window, '_deviationPanel', None)
        if panel is not None:
            panel.adoptTolerance(float(value))
        edited = getattr(window, '_deviationToleranceEdited', None)
        if edited is not None:
            edited(float(value))
        self.syncTolerance(value)
        self.refresh()

    def scaleToEdited(self, value: float):
        window = self._window
        edited = getattr(window, '_deviationScaleEdited', None)
        if edited is not None:
            edited(float(value))
        self.refresh()

    def scaleButtonClicked(self):
        """The legend's scale button opens the colour scale popover (MF12).

        The popover holds the colour standard's controls, so a choice repaints
        the model and this legend (LY12); View > Colour scale (MF9's target
        before the popover existed) stays in the View menu. If the popover
        cannot be built the old popup is used, so the button never goes dead.
        """
        try:
            popover = self.scalePopover()
            popover.popupFor(self.legend)
            return
        except Exception:                                 # noqa: BLE001
            logger.debug('scale popover failed', exc_info=True)
        views = getattr(self._window, '_viewMenu', None)
        menu = views.groups.get('menuViewColour') if views else None
        if menu is None:
            return
        button = self.legend.scaleButton()
        menu.popup(button.mapToGlobal(button.rect().bottomLeft()))

    def scalePopover(self):
        """The one scale popover, made on first use."""
        if self._popover is None:
            from PySide6.QtWidgets import QWidget
            from foammesh.view.display_control.scale_popover import ScalePopover
            parent = self._window if isinstance(self._window, QWidget) else None
            self._popover = ScalePopover(parent, legend=self.legend)
        return self._popover

    def syncTolerance(self, value: float):
        """Make every tolerance control show ``value`` without a loop."""
        boxes = [self.legend.toleranceBox()]
        rows = self._rows()
        if rows is not None:
            boxes.append(rows.toleranceBox())
        for box in boxes:
            if abs(box.value() - float(value)) > 1e-12:
                box.blockSignals(True)
                box.setValue(abs(float(value)))
                box.blockSignals(False)


def _low_is_bad():
    from foammesh.rendering.actor_info import QUALITY_WORST_IS_LOW
    return QUALITY_WORST_IS_LOW
