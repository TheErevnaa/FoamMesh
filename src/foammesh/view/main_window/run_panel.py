"""The live run panel (Plan 40 UXD1, section 4.5.7).

A tab beside the Console that draws what the running mesher is doing, from the
same lines the Console shows (``core.mesh.run_progress`` reads them). Nothing
is sampled or estimated here but the ETA the parser states.

Cost. The panel does no work while idle: the redraw timer runs only while a run
is going and the panel is on screen, fires at most 4 times a second, and
repaints only when the parsed numbers changed since the last tick. Lines are
parsed when they arrive, in batches of the job manager's own 100 ms, so a
chatty stage costs one parse of a few hundred short lines per batch.

Colour is never the only carrier: every stage row carries a glyph and a word,
every chart has a text summary as its accessible description, and the series
are told apart by marker shape as well as by hue.
"""
from __future__ import annotations

import functools
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy, QVBoxLayout,
    QWidget)

from foammesh.core.mesh.run_progress import RunProgress, is_mesher_job
from foammesh.view.theming.metrics import (
    BAR_INSET_H, BAR_INSET_V, GAP, GAP_SECTION)
from foammesh.view.engine_context import (
    GMSH, SNAPPY, UNSELECTED, EngineSwap, configured_engine)

#: Redraw at most this often (the plan's budget is 4 Hz).
REDRAW_MS = 250

_GLYPH = {'pending': '○', 'running': '▶', 'done': '✓', 'failed': '✗',
          'cancelled': '■'}


def format_seconds(value) -> str:
    if value is None:
        return '—'
    seconds = int(round(value))
    if seconds < 60:
        return f'{seconds} s'
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f'{minutes} min {seconds:02d} s'
    hours, minutes = divmod(minutes, 60)
    return f'{hours} h {minutes:02d} min'


_NO_APP = {'import_failed': False}


def _theme_tokens():
    """The running theme's tokens, or the shipped dark theme's."""
    if not _NO_APP['import_failed']:
        try:
            from foammesh.app import app
            manager = app.themeManager
            if manager is not None and manager.tokens is not None:
                return manager.tokens
        except ImportError:
            # Without the application object (a tool, a test) the import
            # fails the same way every time; do not search the disk again on
            # every colour of every paint.
            _NO_APP['import_failed'] = True
        except Exception:                                     # noqa: BLE001
            pass
    return _default_tokens()


def _token(name: str, fallback: str) -> QColor:
    try:
        return QColor(_theme_tokens().value(name))
    except Exception:                                         # noqa: BLE001
        return QColor(fallback)


@functools.lru_cache(maxsize=1)
def _default_tokens():
    """The shipped dark theme, read once: used when no theme is running."""
    from foammesh.view.theming.patch_palette import DEFAULT_THEME
    from foammesh.view.theming.tokens import load_theme_tokens
    return load_theme_tokens(DEFAULT_THEME)


class _Chart(QWidget):
    """One painted chart. Subclasses draw into ``plot`` (a rectangle)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        self.setMinimumHeight(130)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setAccessibleName(title)
        self.empty_text = ''
        #: How many times this chart painted; the redraw-rate test reads it.
        self.paints = 0

    def title(self) -> str:
        return self._title

    def has_data(self) -> bool:
        return False

    def paintEvent(self, _event):
        self.paints += 1
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = _token('foreground.primary', '#dddddd')
        muted = _token('foreground.muted', '#999999')
        painter.setPen(ink)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, self.width(), 18),
                         Qt.AlignmentFlag.AlignLeft
                         | Qt.AlignmentFlag.AlignVCenter, self._title)
        font.setBold(False)
        painter.setFont(font)
        plot = QRectF(64, 24, max(10, self.width() - 74),
                      max(10, self.height() - 46))
        if not self.has_data():
            painter.setPen(muted)
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter,
                             self.empty_text)
            painter.end()
            return
        painter.setPen(QPen(_token('border.default', '#666666'), 1))
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.topLeft(), plot.bottomLeft())
        self.draw(painter, plot, ink, muted)
        painter.end()

    def draw(self, painter, plot, ink, muted):
        raise NotImplementedError

    @staticmethod
    def _label(painter, text, x, y, colour, align=Qt.AlignmentFlag.AlignRight):
        painter.setPen(colour)
        metrics = QFontMetrics(painter.font())
        width = metrics.horizontalAdvance(text)
        left = x - width if align == Qt.AlignmentFlag.AlignRight else x
        painter.drawText(QPointF(left, y), text)


class RefinementChart(_Chart):
    """Cells per refinement level after every refinement step, stacked."""

    def __init__(self, progress: RunProgress, parent=None):
        super().__init__('Cells per refinement level', parent)
        self._progress = progress
        self.empty_text = 'Waiting for the first refinement step'

    def has_data(self):
        return any(step.levels for step in self._progress.refinement)

    def draw(self, painter, plot, ink, muted):
        steps = [step for step in self._progress.refinement if step.levels]
        top = max(sum(step.levels.values()) for step in steps) or 1
        levels = sorted({level for step in steps for level in step.levels})
        colours = [_token(f'viewport.patch.{1 + index % 6}', '#4477aa')
                   for index in range(len(levels))]
        count = len(steps)
        slot = plot.width() / count
        bar = max(2.0, min(28.0, slot * 0.7))
        for index, step in enumerate(steps):
            x = plot.left() + slot * index + (slot - bar) / 2
            base = plot.bottom()
            for position, level in enumerate(levels):
                cells = step.levels.get(level, 0)
                height = plot.height() * cells / top
                painter.fillRect(QRectF(x, base - height, bar, height),
                                 colours[position])
                base -= height
        self._label(painter, f'{top:,}', plot.left() - 4, plot.top() + 10, muted)
        self._label(painter, '0', plot.left() - 4, plot.bottom(), muted)
        # Legend: level swatches, with the level written beside each.
        x = plot.left()
        for position, level in enumerate(levels):
            painter.fillRect(QRectF(x, plot.bottom() + 8, 9, 9),
                             colours[position])
            painter.setPen(muted)
            painter.drawText(QPointF(x + 12, plot.bottom() + 17),
                             f'level {level}')
            x += 62


class SnapChart(_Chart):
    """Wanted snap displacement per morph iteration (average and largest)."""

    def __init__(self, progress: RunProgress, parent=None):
        super().__init__('Snap displacement per iteration', parent)
        self._progress = progress
        self.empty_text = 'Waiting for the first snap iteration'

    def has_data(self):
        return bool(self._progress.snap)

    def draw(self, painter, plot, ink, muted):
        steps = self._progress.snap
        top = max(step.maximum for step in steps) or 1.0
        total = max(len(steps) - 1, 1)
        series = (('largest', [step.maximum for step in steps],
                   _token('status.warning', '#d2a106'), 'square'),
                  ('average', [step.average for step in steps],
                   _token('status.info', '#4a90d9'), 'circle'))
        for name, values, colour, shape in series:
            pen = QPen(colour, 2)
            painter.setPen(pen)
            points = [QPointF(plot.left() + plot.width() * index / total,
                              plot.bottom() - plot.height() * value / top)
                      for index, value in enumerate(values)]
            for first, second in zip(points, points[1:]):
                painter.drawLine(first, second)
            painter.setBrush(colour)
            for point in points:
                if shape == 'square':
                    painter.drawRect(QRectF(point.x() - 3, point.y() - 3, 6, 6))
                else:
                    painter.drawEllipse(point, 3, 3)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        self._label(painter, f'{top:.2g} m', plot.left() - 4, plot.top() + 10, muted)
        self._label(painter, '0', plot.left() - 4, plot.bottom(), muted)
        x = plot.left()
        for name, _values, colour, _shape in series:
            painter.setPen(colour)
            painter.drawText(QPointF(x, plot.bottom() + 17), f'■ {name}'
                             if name == 'largest' else f'● {name}')
            x += 80


class LayerChart(_Chart):
    """Share of the wanted layer thickness reached, per patch."""

    def __init__(self, progress: RunProgress, parent=None):
        super().__init__('Layer coverage per patch', parent)
        self._progress = progress
        self.empty_text = 'Waiting for the layer summary'

    def has_data(self):
        return bool(self._progress.layer_patches)

    def draw(self, painter, plot, ink, muted):
        patches = self._progress.layer_patches
        slot = plot.height() / len(patches)
        bar = max(4.0, min(20.0, slot * 0.7))
        colour = _token('status.success', '#4caf50')
        for index, patch in enumerate(patches):
            y = plot.top() + slot * index + (slot - bar) / 2
            width = plot.width() * max(0.0, min(patch.percent, 100.0)) / 100
            painter.fillRect(QRectF(plot.left(), y, width, bar), colour)
            painter.setPen(ink)
            painter.drawText(
                QPointF(plot.left() + 4, y + bar - 3),
                f'{patch.patch}  {patch.percent:g} %')
        self._label(painter, '100 %', plot.right(), plot.bottom() + 14, muted)
        self._label(painter, '0', plot.left() + 6, plot.bottom() + 14, muted)


class StageRow(QFrame):
    """The stage timeline: glyph, name and seconds, in order."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(GAP_SECTION)
        self._labels: dict[str, QLabel] = {}
        self._layout.addStretch(1)
        self.setAccessibleName('Stage timeline')

    def show_stages(self, stages, now: float) -> None:
        keys = [stage.key for stage in stages]
        if keys != list(self._labels):
            for label in self._labels.values():
                self._layout.removeWidget(label)
                label.hide()
                label.setParent(None)
                label.deleteLater()
            self._labels.clear()
            for key in keys:
                label = QLabel(self)
                label.setObjectName('runStage')
                self._layout.insertWidget(self._layout.count() - 1, label)
                self._labels[key] = label
        for stage in stages:
            seconds = stage.seconds(now)
            text = f'{_GLYPH.get(stage.status, "○")} {stage.label}'
            if stage.total and stage.status == 'running':
                done = stage.iterations
                text += f' {done}/{stage.total}'
            if seconds is not None:
                text += f'  {format_seconds(seconds)}'
            label = self._labels[stage.key]
            # Only what changed: each of these tells the accessibility layer
            # and the style, which costs more than the paint does.
            if label.text() != text:
                label.setText(text)
            role = {'done': 'success', 'failed': 'error',
                    'cancelled': 'warning'}.get(stage.status, '')
            if label.property('foammeshStatus') != role:
                label.setProperty('foammeshStatus', role)
                label.style().unpolish(label)
                label.style().polish(label)
            name = f'{stage.label}, {stage.status}'
            if label.accessibleName() != name:
                label.setAccessibleName(name)


class RunPanel(QWidget):
    """The tab. ``feed_*`` take what the job events carry."""

    def __init__(self, parent=None, *, clock=time.monotonic):
        super().__init__(parent)
        self.setObjectName('runPanel')
        self.setAccessibleName('Live run')
        self._clock = clock
        self.progress = RunProgress()
        self._drawn = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(BAR_INSET_H, BAR_INSET_V, BAR_INSET_H, BAR_INSET_V)
        layout.setSpacing(GAP)
        head = QHBoxLayout()
        self._status = QLabel('No mesher is running.', self)
        self._status.setObjectName('runPanelStatus')
        self._status.setWordWrap(True)
        self._timing = QLabel('', self)
        self._timing.setObjectName('runPanelTiming')
        head.addWidget(self._status, 1)
        head.addWidget(self._timing)
        layout.addLayout(head)
        self._stages = StageRow(self)
        layout.addWidget(self._stages)
        self._bar = QProgressBar(self)
        self._bar.setObjectName('runPanelBar')
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        self._bar.setAccessibleName('Gmsh progress')
        layout.addWidget(self._bar)
        charts = QHBoxLayout()
        charts.setSpacing(GAP_SECTION)
        self.refinement_chart = RefinementChart(self.progress, self)
        self.snap_chart = SnapChart(self.progress, self)
        self.layer_chart = LayerChart(self.progress, self)
        for chart in (self.refinement_chart, self.snap_chart,
                      self.layer_chart):
            charts.addWidget(chart, 1)
        layout.addLayout(charts, 1)
        self._facts = QLabel('', self)
        self._facts.setObjectName('runPanelFacts')
        self._facts.setAlignment(Qt.AlignmentFlag.AlignCenter)
        font = self._facts.font()
        font.setPointSize(font.pointSize() + 6)
        self._facts.setFont(font)
        layout.addWidget(self._facts, 1)
        self._summary = QLabel('', self)
        self._summary.setObjectName('runPanelSummary')
        self._summary.setWordWrap(True)
        self._summary.setSizePolicy(QSizePolicy.Policy.Expanding,
                                    QSizePolicy.Policy.Maximum)
        layout.addWidget(self._summary)
        self._timer = QTimer(self)
        self._timer.setInterval(REDRAW_MS)
        self._timer.timeout.connect(self.redraw)
        #: How many ticks actually repainted; the rate test reads it.
        self.redraws = 0
        self._engine_shown = None
        #: The engine the case is set up for (Plan 42 SG3); empty until known.
        self._configured = UNSELECTED
        self._swap = EngineSwap(
            self.layout(),
            **{SNAPPY: [self.refinement_chart, self.snap_chart,
                        self.layer_chart],
               GMSH: [self._bar, self._facts]})
        self._show_for_engine()
        self._paint_status()

    # ----------------------------------------------------------- job events
    def job_started(self, argv=None, name: str = '') -> None:
        if argv is not None and not is_mesher_job(argv):
            return
        self.progress.job_started(self._clock())
        self._wake()

    def job_output(self, lines) -> None:
        if not lines:
            return
        if self.progress.feed(lines, self._clock()):
            self._wake()

    def job_ended(self, outcome: str = 'done') -> None:
        self.progress.job_ended(self._clock(), outcome)
        self.redraw(force=True)
        self._timer.stop()

    # ---------------------------------------------------------------- redraw
    def _wake(self) -> None:
        if self.progress.running and self.isVisible() \
                and not self._timer.isActive():
            self._timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        self.redraw(force=True)
        if self.progress.running:
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._timer.stop()

    def setEngine(self, engine: str) -> None:
        """Key the engine items to the case's engine (Plan 42 SG3)."""
        engine = engine or UNSELECTED
        if engine != self._configured:
            self._configured = engine
            self._show_for_engine()
            self._paint_status()

    def shown_engine(self) -> str:
        """The engine whose items show: the case's, else the run log's."""
        if self._configured != UNSELECTED:
            return self._configured
        return self.progress.engine or UNSELECTED

    def _show_for_engine(self) -> None:
        engine = self.shown_engine()
        self._engine_shown = (engine, self.progress.engine)
        # A name no group carries hides every engine item: no empty frames.
        self._swap.apply(engine)

    def redraw(self, force: bool = False) -> None:
        """Repaint if the parsed numbers changed since the last repaint."""
        changed = self.progress.revision != self._drawn
        running = self.progress.running
        if not (changed or force or running):
            self._timer.stop()
            return
        if (self.shown_engine(), self.progress.engine) != self._engine_shown:
            self._show_for_engine()
        self._paint_status()
        if changed or force:
            self._drawn = self.progress.revision
            self.redraws += 1
            for chart in (self.refinement_chart, self.snap_chart,
                          self.layer_chart):
                if chart.isVisibleTo(self):
                    chart.update()
        if not running:
            self._timer.stop()

    def _paint_status(self) -> None:
        progress = self.progress
        now = self._clock()
        shown = self.shown_engine()
        engine = {SNAPPY: 'snappyHexMesh', GMSH: 'Gmsh'}.get(shown, 'Mesher')
        if progress.state == 'idle':
            self._status.setText('No mesher is running.')
            self._timing.setText('')
        else:
            word = {'running': 'running', 'done': 'finished',
                    'failed': 'failed', 'cancelled': 'cancelled'}[progress.state]
            text = f'{engine} {word}'
            if shown == GMSH and progress.message:
                text += f': {progress.message}'
            cells = progress.current_cells()
            if cells is not None:
                text += f' · {cells:,} cells'
            self._status.setText(text)
            timing = f'elapsed {format_seconds(progress.elapsed(now))}'
            if progress.state == 'running':
                timing += f' · ETA {format_seconds(progress.eta(now))}'
            self._timing.setText(timing)
        self._bar.setValue(round(progress.fraction * 1000)
                           if shown == GMSH else 0)
        facts = ''
        if shown == GMSH and progress.element_count is not None:
            facts = f'{progress.element_count:,} elements'
            if progress.min_quality is not None:
                facts += f' · worst quality {progress.min_quality:.3g}'
        self._facts.setText(facts)
        self._stages.show_stages(progress.stages, now)
        summary = progress.summary(now)
        if summary != self._summary.text():
            self._summary.setText(summary)
            self._summary.setVisible(bool(summary))
        description = f'{self._status.text()}. {self._summary.text()}'
        if description != self.accessibleDescription():
            self.setAccessibleDescription(description)


    # ------------------------------------------------------------ read-back
    def readout(self) -> dict:
        """Everything the panel shows, as it shows it (Plan 48 PA5).

        The local API returns this: the status and timing labels, the bar, the
        facts and summary lines, the stage rows, and the series the three
        charts draw. The labels are brought up to date first, so a panel that
        is not on screen (its timer is stopped) reads as it will when shown.
        """
        self._paint_status()
        progress = self.progress

        def shown(widget) -> bool:
            return not widget.isHidden() and widget.isVisibleTo(self)

        stages = []
        for stage in progress.stages:
            label = self._stages._labels.get(stage.key)
            stages.append({
                'key': stage.key, 'label': stage.label,
                'status': stage.status,
                'text': label.text() if label is not None else '',
                'iterations': stage.iterations, 'total': stage.total})

        def chart(widget, series) -> dict:
            return {'title': widget.title(), 'has_data': widget.has_data(),
                    'empty_text': widget.empty_text, 'shown': shown(widget),
                    'series': series}

        refinement = [{'label': step.label, 'cells': step.cells,
                       'faces': step.faces, 'points': step.points,
                       'levels': {str(level): cells for level, cells
                                  in sorted(step.levels.items())}}
                      for step in progress.refinement]
        snap = [{'iteration': step.iteration, 'average': step.average,
                 'minimum': step.minimum, 'maximum': step.maximum}
                for step in progress.snap]
        layers = [{'patch': patch.patch, 'faces': patch.faces,
                   'layers': patch.layers, 'thickness': patch.thickness,
                   'percent': patch.percent}
                  for patch in progress.layer_patches]
        return {
            'state': progress.state, 'running': bool(progress.running),
            'engine': self.shown_engine(),
            'status': self._status.text(), 'timing': self._timing.text(),
            'bar': {'value': self._bar.value(),
                    'maximum': self._bar.maximum(),
                    'fraction': float(progress.fraction),
                    'shown': shown(self._bar)},
            'facts': {'text': self._facts.text(), 'shown': shown(self._facts)},
            'summary': {'text': self._summary.text(),
                        'shown': not self._summary.isHidden()},
            'stages': stages,
            'charts': {
                'refinement': chart(self.refinement_chart, refinement),
                'snap': chart(self.snap_chart, snap),
                'layers': chart(self.layer_chart, layers)},
        }


def install_run_panel(tabs) -> RunPanel:
    """Add the Live run tab to ``tabs`` and follow the job events.

    Only a job that runs snappyHexMesh or the Gmsh runner is followed, by its
    job id, so a checkMesh run that overlaps a mesh run cannot feed it.
    """
    from foammesh.app import app
    from foammesh.core.project import Event

    panel = RunPanel()
    tabs.register_permanent('run', panel, 'Live run')
    followed = {'job': None}

    def started(**payload):
        argv = payload.get('argv')
        if is_mesher_job(argv):
            followed['job'] = payload.get('job_id')
            panel.job_started(argv)

    def output(**payload):
        if payload.get('job_id') == followed['job']:
            panel.job_output(payload.get('lines') or payload.get('line'))

    def ended(outcome):
        def callback(**payload):
            if payload.get('job_id') == followed['job'] \
                    and followed['job'] is not None:
                followed['job'] = None
                panel.job_ended(outcome)
        return callback

    def engine_moved(**_payload):
        try:
            panel.setEngine(configured_engine(app))
        except RuntimeError:                                  # panel gone
            pass

    panel.setEngine(configured_engine(app))
    unsubscribes = [
        app.events.subscribe(getattr(Event, name), engine_moved)
        for name in ('TRANSACTION_APPLIED', 'UNDONE', 'REDONE',
                     'PROJECT_OPENED')]
    unsubscribes += [
        app.events.subscribe(Event.JOB_STARTED, started),
        app.events.subscribe(Event.JOB_OUTPUT, output),
        app.events.subscribe(Event.JOB_FINISHED, ended('done')),
        app.events.subscribe(Event.JOB_FAILED, ended('failed')),
        app.events.subscribe(Event.JOB_CANCELLED, ended('cancelled')),
    ]

    # Plan 42 SG16: a Gmsh session action is a run -- the Console and the
    # strip follow it the way this panel does.
    from .session_activity import install_session_activity
    unsubscribes.append(install_session_activity(
        app.events, lambda: getattr(app, 'window', None)))

    def release(*_args):
        for unsubscribe in unsubscribes:
            unsubscribe()

    panel.destroyed.connect(release)
    panel._unsubscribes = unsubscribes
    return panel
