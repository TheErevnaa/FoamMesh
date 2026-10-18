"""The quality views of the window (Plan 43 LY10).

Owns, for one main window:

* the Mesh quality tab's layout (A, B or C), remembered per user (in the View menu's record);
* the worst values written on the model and the statistics caption, both on
  by default, switched from View > Labels and snapshots;
* the link to the View > Mesh quality entries (LY8 builds them).

The numbers are worked out by ``core.quality.worst_values`` (no Qt); the
actors are ``rendering.worst_labels``. This class only reads what the window
already holds (the quality colouring, the deviation colouring, the cell the
reader selected) and keeps the two in step.
"""
from __future__ import annotations

import logging
from datetime import date

import numpy as np
from PySide6.QtCore import QObject

from foammesh.core.quality import worst_values as wv
from foammesh.core.quality.verdict import verdict_source
from foammesh.rendering.worst_labels import WorstLabelLayer
from foammesh.view.main_window.mesh_quality_tab import (
    LAYOUT_MODES, LAYOUT_NAMES)

logger = logging.getLogger(__name__)



def current_scale_name() -> str:
    """The scale the colouring uses now: the colour standard's (LY12)."""
    from foammesh.support import colour_standard
    return colour_standard.current().scale_label


def _version() -> str:
    try:
        from app_properties import APP_VERSION
        return str(APP_VERSION)
    except Exception:                                         # noqa: BLE001
        return ''


class MeshQualityViews(QObject):
    """See the module docstring."""

    def __init__(self, window, tab, settings=None) -> None:
        super().__init__(window)
        self._window = window
        self._tab = tab
        self._settings = settings
        record = self._read()
        self._worst_on = bool(record['worst_values'])
        self._caption_on = bool(record['caption'])
        self._layer = None
        self._beyond = None
        self._scale_name = None           # None: follow the colour standard
        from foammesh.support import colour_standard
        colour_standard.subscribe(self._standard_changed)
        self._state = None
        self._show_report = None
        self._observers: list = []
        tab.set_layout_mode(record['layout'])
        tab.layoutModeChanged.connect(self._layout_changed)
        self._tab_signals = False
        self.attach_tab_signals()

    def attach_tab_signals(self) -> None:
        """Follow the tab's verdict and cell list (DP-1948): the labels on a
        mesh shown plainly come from what the checkMesh report filed, so they
        change when the tab shows a verdict, a ranked list, or is cleared.
        Safe to call twice."""
        if not self._tab_signals:
            self._tab_signals = True
            self._tab.verdictChanged.connect(self.refresh)

    # -- settings ---------------------------------------------------------- #

    def _read(self) -> dict:
        """The record kept before LY8 (read once, by :meth:`bind`)."""
        reader = getattr(self._settings, 'getMeshQualityViews', None)
        try:
            return reader() if reader is not None else {
                'layout': 'B', 'worst_values': True, 'caption': True}
        except Exception:                                     # noqa: BLE001
            return {'layout': 'B', 'worst_values': True, 'caption': True}

    # -- state ------------------------------------------------------------- #

    def _beyond_on(self) -> bool:
        state = getattr(self, '_state', None)
        if state is not None:
            return bool(state.get('quality.beyond_limit'))
        return bool(self._tab._beyond_button.isChecked())

    def _set_beyond(self, data, ids) -> None:
        """Remember the cells beyond the limit; outline them while the
        Show-cells-beyond-limit choice is on."""
        self._beyond = None if data is None or ids is None else (data, ids)
        self._sync_outline()

    def _sync_outline(self) -> None:
        beyond = getattr(self, '_beyond', None)
        if beyond is None or not self._beyond_on():
            if self._layer is not None:
                self._layer.set_beyond_cells(None, None)
            return
        self.layer().set_beyond_cells(*beyond)

    def worst_values_on(self) -> bool:
        return self._worst_on

    def caption_on(self) -> bool:
        return self._caption_on

    def layer(self) -> WorstLabelLayer:
        if self._layer is None:
            self._layer = WorstLabelLayer(self._window._ui.renderingView)
            self._layer.set_enabled(self._worst_on, self._caption_on)
            # Plan 44 MF16: the labels keep inside the clear rectangle.
            feed = getattr(self._window, 'feedClearRect', None)
            if callable(feed):
                feed(self._layer)
            try:
                from foammesh.app import app
                if app.themeManager is not None:
                    app.themeManager.themeChanged.connect(
                        lambda _name: self._layer.refresh_theme())
            except Exception:                                 # noqa: BLE001
                logger.debug('theme change not followed', exc_info=True)
        return self._layer

    def subscribe(self, callback):
        """*callback(key, value)* after the worst values or the caption are
        switched (key 'worst' or 'caption'); the View menu state follows."""
        self._observers.append(callback)
        return lambda: self._observers.remove(callback)

    def _notify(self, key, value) -> None:
        for callback in list(self._observers):
            try:
                callback(key, value)
            except Exception:                                 # noqa: BLE001
                logger.debug('quality observer failed', exc_info=True)

    def set_worst_values(self, on: bool) -> None:
        self._worst_on = bool(on)
        self._push('labels.worst', self._worst_on)
        self.layer().set_enabled(self._worst_on, self._caption_on)
        self._notify('worst', self._worst_on)

    def set_caption(self, on: bool) -> None:
        self._caption_on = bool(on)
        self._push('labels.caption', self._caption_on)
        self.layer().set_enabled(self._worst_on, self._caption_on)
        self._notify('caption', self._caption_on)

    def set_size_factor(self, factor: float) -> None:
        self.layer().set_size_factor(factor)

    def set_scale_name(self, name: str) -> None:
        self._scale_name = str(name)
        self.refresh()

    def _standard_changed(self, _standard=None) -> None:
        """The caption names the scale in use, so it follows the standard."""
        if self._scale_name is None:
            try:
                self.refresh()
            except Exception:                                 # noqa: BLE001
                logger.debug('caption refresh after a scale change failed',
                             exc_info=True)

    def scale_name(self) -> str:
        return (self._scale_name if self._scale_name is not None
                else current_scale_name())

    def _layout_changed(self, mode: str) -> None:
        self._push('quality.layout', mode)

    # -- what is on the model --------------------------------------------- #

    def select(self, element) -> None:
        """The cell selected in the Mesh quality tab gets its own tag."""
        layer = self.layer()
        try:
            centroid = tuple(float(c) for c in element.get('centroid'))
            value = element.get('value')
            text = ('#{0}'.format(element.get('tag'))
                    + ('' if value is None
                       else '  ' + wv.format_value(float(value))))
        except (TypeError, ValueError):
            layer.set_selected(None)
            return
        layer.set_selected((centroid, text))

    def clear(self) -> None:
        self._beyond = None
        if self._layer is not None:
            self._layer.clear()

    def refresh(self) -> None:
        """Read the colouring on screen and write its worst values."""
        try:
            self._refresh()
        except Exception:                                     # noqa: BLE001
            logger.debug('worst values not drawn', exc_info=True)
            self.clear()

    def _case_name(self) -> str:
        try:
            root = self._window._captureManager.caseRoot()
            return root.name if root is not None else ''
        except Exception:                                     # noqa: BLE001
            return ''

    def _refresh(self) -> None:
        manager = getattr(self._window, '_meshManager', None)
        readout = getattr(manager, 'deviationReadout', None)
        readout = readout() if readout is not None else None
        stamp = '{0} {1} {2}'.format(
            date.today().day, date.today().strftime('%b'), date.today().year)
        if readout is not None and readout.fields:
            self._show_deviation(manager, readout, stamp)
            return
        info = self._window._displayControl.meshQualityInfo()
        if info.isHighlighting():
            self._show_quality(manager, info, stamp)
            return
        self._show_verdict(stamp)

    def _show_verdict(self, stamp) -> None:
        """A mesh shown plainly (no quality colouring): the three worst cells
        the tab lists, and the caption of the metric the checkMesh report
        judged hardest, so the labels show on every tab (section 3)."""
        tab = self._tab
        verdict = tab.verdict()
        metric = wv.headline_metric(verdict.get('metrics'))
        targets = []
        for element in tab.listed_elements():
            try:
                centre = tuple(float(c) for c in element['centroid'])
                value = float(element['value'])
            except (KeyError, TypeError, ValueError):
                continue
            if len(centre) != 3 or value != value:
                continue
            tag = element.get('tag')
            targets.append((centre, wv.format_value(value),
                            _suffix('cell', tag, not targets)))
            if len(targets) >= wv.LABEL_COUNT:
                break
        self._set_beyond(None, None)
        if not targets and metric is None:
            self.clear()
            return
        layer = self.layer()
        layer.set_targets(targets)
        if metric is None:
            layer.set_caption(())
            return
        layer.set_caption(wv.verdict_caption(
            case=self._case_name(), metric=metric,
            source=verdict_source(verdict), scale_name=self._scale_name,
            version=_version(), stamp=stamp))

    def _show_quality(self, manager, info, stamp) -> None:
        from foammesh.rendering.actor_info import QUALITY_WORST_IS_LOW

        index = info.activeIndex()
        values = info.metricValues(index)
        volume = manager.actorInfo('internalMesh')
        if values is None or volume is None or volume.dataSet() is None:
            self.clear()
            return
        high = index not in QUALITY_WORST_IS_LOW
        data = volume.dataSet()
        picks = wv.pick_worst(values, worst_is_high=high)
        targets = []
        for pick in picks:
            bounds = data.GetCell(pick.index).GetBounds()
            centre = ((bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2,
                      (bounds[4] + bounds[5]) / 2)
            targets.append((centre, wv.label_for(pick, deviation=False),
                            _suffix('cell', pick.index, not targets)))
        limit = info.limitFor(index)
        self._set_beyond(data, None if limit is None else np.nonzero(
            (np.asarray(values) > float(limit)) if high
            else (np.asarray(values) < float(limit)))[0])
        label = info.METRIC_LABELS.get(index, index.value)
        layer = self.layer()
        layer.set_targets(targets)
        layer.set_caption(wv.quality_caption(
            case=self._case_name(), metric_label=str(label), values=values,
            worst_is_high=high, limit=limit, source='checkMesh report',
            scale_name=self.scale_name(), version=_version(), stamp=stamp))

    def _show_deviation(self, manager, readout, stamp) -> None:
        names, arrays = [], []
        for name, field in readout.fields.items():
            names.append(name)
            arrays.append(np.asarray(field, dtype=np.float64).ravel())
        joined = np.concatenate(arrays) if arrays else np.zeros(0)
        offsets = np.cumsum([0] + [a.size for a in arrays])
        picks = wv.pick_worst(joined, by_magnitude=True)
        targets = []
        for pick in picks:
            which = int(np.searchsorted(offsets, pick.index, side='right')) - 1
            info = manager.actorInfo(names[which])
            data = info.dataSet() if info is not None else None
            if data is None:
                continue
            bounds = data.GetCell(pick.index - int(offsets[which])).GetBounds()
            centre = ((bounds[0] + bounds[1]) / 2,
                      (bounds[2] + bounds[3]) / 2,
                      (bounds[4] + bounds[5]) / 2)
            targets.append((centre, wv.label_for(pick, deviation=True),
                            _suffix('face', pick.index - int(offsets[which]),
                                    not targets)))
        self._set_beyond(None, None)
        layer = self.layer()
        layer.set_targets(targets)
        # Plan 45 P2: no centred statistics caption in Deviation mode. The
        # legend's figures already say max, mean, RMS and the share within
        # tolerance; a second copy in the middle of the view was the third.
        layer.set_caption(())

    # -- the View menu's state --------------------------------------------- #

    def bind(self, state, show_report=None) -> None:
        """Follow the View menu's state (``window._viewState``), which is the
        one record of the layout, the worst values and the caption.

        The menu entries are LY8's (View > Mesh quality, View > Labels and
        snapshots); a change of one of the keys drives the tab and the layer,
        and a change made on the tab or by :meth:`set_worst_values` and
        :meth:`set_caption` is written back, so the two never disagree. A
        record this class kept before LY8 is carried over once for the keys
        the View menu has not saved.
        """
        self._state = state
        self._show_report = show_report
        legacy = self._read()
        for key, value in (('quality.layout', legacy['layout']),
                           ('labels.worst', bool(legacy['worst_values'])),
                           ('labels.caption', bool(legacy['caption']))):
            if state.get(key) == state.default(key) and value != state.get(key):
                state.set(key, value)
        if legacy != {'layout': 'B', 'worst_values': True, 'caption': True}:
            # Carried over: the old record is put back to its defaults so it
            # cannot override a later choice made in the View menu.
            writer = getattr(self._settings, 'updateMeshQualityViews', None)
            if writer is not None:
                try:
                    writer(layout='B', worst_values=True, caption=True)
                except Exception:                             # noqa: BLE001
                    logger.debug('old quality record kept', exc_info=True)
        self._worst_on = bool(state.get('labels.worst'))
        self._caption_on = bool(state.get('labels.caption'))
        self._apply('quality.layout')
        self._apply('quality.beyond_limit')
        self._apply('quality.inspect')
        tab = self._tab
        tab._beyond_button.toggled.connect(
            lambda on: self._push('quality.beyond_limit', bool(on)))
        tab.beyondFollowed.connect(
            lambda on: self._push('quality.beyond_limit', bool(on)))
        tab._pick_element.toggled.connect(
            lambda on: self._push('quality.inspect', bool(on)))
        state.subscribe(self._state_changed)
        if self._layer is not None:
            self._layer.set_enabled(self._worst_on, self._caption_on)

    def _push(self, key: str, value) -> None:
        state = getattr(self, '_state', None)
        if state is None or getattr(self, '_applying', False):
            return           # the tab answering a change the menu just made
        state.set(key, value)

    def _state_changed(self, key: str) -> None:
        if key in ('quality.layout', 'quality.taller', 'quality.beyond_limit',
                   'quality.inspect', 'labels.worst', 'labels.caption'):
            self._apply(key)

    def _apply(self, key: str) -> None:
        # While the menu's choice is being put on the tab, what the tab says
        # back is not a new choice: with nothing to show it puts its button
        # up again, and pushing that made the two flip each other until the
        # stack ran out (MF11). The choice stays in the state.
        if getattr(self, '_applying', False):
            return
        self._applying = True
        try:
            self._apply_now(key)
        finally:
            self._applying = False

    def _apply_now(self, key: str) -> None:
        state, tab = self._state, self._tab
        value = state.get(key)
        if key == 'quality.layout':
            if tab.layout_mode() != value:
                tab.set_layout_mode(value)
        elif key == 'quality.taller':
            if value and self._show_report is not None:
                self._show_report()
        elif key == 'quality.beyond_limit':
            if tab._beyond_button.isChecked() != value:
                tab._beyond_button.setChecked(value)
            self._sync_outline()
        elif key == 'quality.inspect':
            if tab._pick_element.isChecked() != value:
                tab._pick_element.setChecked(value)
        elif key == 'labels.worst':
            if self._worst_on != value:
                self.set_worst_values(value)
        elif key == 'labels.caption':
            if self._caption_on != value:
                self.set_caption(value)


def _suffix(kind: str, ident, first: bool) -> str:
    """The mockup's suffix: ``cell 123`` and, on the worst, ``cell 123 · worst``."""
    text = f'{kind} {ident}' if ident is not None else ''
    if first:
        text = f'{text} · worst' if text else 'worst'
    return text


def install_quality_views(window, tab, settings=None) -> MeshQualityViews:
    """The window's quality views (see :class:`MeshQualityViews`)."""
    return MeshQualityViews(window, tab, settings)
