"""Plan 41 GN11 (M-33): the source -> target arrow of a periodic pair.

The Periodic page draws, for the selected pair, where the source surface is,
where the target surface is, and where the transform carries the source. A
pair pointing the right way lands on the target; one pointing the wrong way
lands somewhere else and the view says by how much.

The centres come from the loaded geometry (the prepared scopes' actors). With
no geometry loaded the arrow is drawn from the origin by the transform alone,
and says so.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from foammesh.core import display_unit
from foammesh.core.gmsh.periodic import (
    PeriodicError, arrow_for, derive_periodic_pairs)


def _camel(name: str) -> str:
    head, *rest = name.split('_')
    return head + ''.join(part[:1].upper() + part[1:] for part in rest)


def stored_row(row: dict) -> dict:
    """A row keyed by the panel's relative ids, back in storage spelling."""
    out: dict = {}
    for key, value in dict(row or {}).items():
        if str(key).startswith('__'):
            continue
        first, *rest = str(key).split('.')
        if rest:
            out.setdefault(_camel(first), {})[rest[0]] = value
        else:
            out[_camel(first)] = value
    return out


def scope_centre(token: str):
    """Centre of the geometry a prepared scope covers, or ``None``."""
    try:
        from foammesh.app import app
        entity = app.selectionService.entity(token) if token else None
        window = app.window
        manager = getattr(window, 'geometryManager', None) if window else None
        if entity is None or manager is None:
            return None
        lows = [math.inf] * 3
        highs = [-math.inf] * 3
        for geometry_id in entity.geometry_ids:
            try:
                bounds = manager.polyData(geometry_id).GetBounds()
            except Exception:
                continue
            for axis in range(3):
                lows[axis] = min(lows[axis], bounds[2 * axis])
                highs[axis] = max(highs[axis], bounds[2 * axis + 1])
        if not all(math.isfinite(item) for item in lows + highs):
            return None
        return tuple((lows[i] + highs[i]) / 2.0 for i in range(3))
    except Exception:
        return None


def describe(row: dict, source_centre=None, target_centre=None) -> dict:
    """What the arrow view shows for one row: points, miss, a sentence.

    ``problem`` is set when the row cannot be derived (singular matrix, zero
    translation ...); nothing is drawn then.
    """
    stored = stored_row(row)
    stored.setdefault('masterScopeToken', 'source')
    stored.setdefault('slaveScopeToken', 'target')
    if stored['masterScopeToken'] == stored['slaveScopeToken']:
        stored['slaveScopeToken'] = str(stored['slaveScopeToken']) + '-target'
    stored['enabled'] = True
    try:
        plan = derive_periodic_pairs([stored])
    except (PeriodicError, ValueError, TypeError) as error:
        return {'problem': str(error)}
    if not plan.pairs:
        return {'problem': 'The pair is switched off.'}
    pair = plan.pairs[0]
    located = source_centre is not None
    arrow = arrow_for(pair.affine, source_centre or (0.0, 0.0, 0.0),
                      target_centre if located else None)
    if not located:
        text = ('No geometry is loaded for these surfaces: the arrow is '
                'the transform applied to the origin.')
    elif target_centre is None:
        text = 'The arrow ends where the transform carries the source centre.'
    elif arrow.miss <= max(pair.match_tolerance, 1e-9) * 1e3:
        text = 'The transform carries the source centre onto the target.'
    else:
        text = ('The transform lands %s from the target centre: check '
                'the direction and the values.'
                % display_unit.format_length(arrow.miss))
    return {'arrow': arrow, 'text': text, 'located': located,
            'transform': pair.transform}


class _ArrowCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._info: dict = {}
        self.setMinimumHeight(110)
        self.setObjectName('gmshPeriodicArrowCanvas')

    def setInfo(self, info: dict) -> None:
        self._info = dict(info or {})
        self.update()

    def projected(self):
        """The three points on the two axes they spread over most."""
        arrow = self._info.get('arrow')
        if arrow is None:
            return None
        points = [arrow.start, arrow.end, arrow.mapped]
        spread = [max(p[i] for p in points) - min(p[i] for p in points)
                  for i in range(3)]
        order = sorted(range(3), key=lambda i: -spread[i])
        axes = tuple(sorted(order[:2]))
        return axes, [(p[axes[0]], p[axes[1]]) for p in points]

    def paintEvent(self, _event):                       # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = self.palette()
        text = palette.color(palette.ColorRole.WindowText)
        projected = self.projected()
        if projected is None:
            painter.setPen(text)
            painter.drawText(self.rect().adjusted(8, 8, -8, -8),
                             Qt.AlignLeft | Qt.TextWordWrap,
                             str(self._info.get('problem') or ''))
            return
        _axes, (start, end, mapped) = projected
        xs = [start[0], end[0], mapped[0]]
        ys = [start[1], end[1], mapped[1]]
        lo_x, hi_x, lo_y, hi_y = min(xs), max(xs), min(ys), max(ys)
        width = max(hi_x - lo_x, 1e-12)
        height = max(hi_y - lo_y, 1e-12)
        pad = 18.0
        scale = min((self.width() - 2 * pad) / width,
                    (self.height() - 2 * pad) / height)
        if not math.isfinite(scale) or scale <= 0:
            scale = 1.0
        scale = min(scale, 1e9)

        def at(point):
            return QPointF(pad + (point[0] - lo_x) * scale,
                           self.height() - pad - (point[1] - lo_y) * scale)

        source_colour = QColor(40, 110, 200)
        target_colour = QColor(200, 100, 30)
        a, b, m = at(start), at(end), at(mapped)
        pen = QPen(text)
        pen.setWidthF(1.6)
        painter.setPen(pen)
        painter.drawLine(a, b)
        # Head of the arrow, at the target end.
        dx, dy = b.x() - a.x(), b.y() - a.y()
        length = math.hypot(dx, dy)
        if length > 1.0:
            ux, uy = dx / length, dy / length
            head = QPolygonF([
                b, QPointF(b.x() - 9 * ux + 4 * uy, b.y() - 9 * uy - 4 * ux),
                QPointF(b.x() - 9 * ux - 4 * uy, b.y() - 9 * uy + 4 * ux)])
            painter.setBrush(text)
            painter.drawPolygon(head)
        painter.setPen(Qt.NoPen)
        painter.setBrush(source_colour)
        painter.drawEllipse(a, 5, 5)
        painter.setBrush(target_colour)
        painter.drawEllipse(b, 5, 5)
        if math.hypot(m.x() - b.x(), m.y() - b.y()) > 3.0:
            painter.setBrush(Qt.NoBrush)
            miss_pen = QPen(QColor(190, 40, 40))
            miss_pen.setWidthF(1.6)
            miss_pen.setStyle(Qt.DashLine)
            painter.setPen(miss_pen)
            painter.drawEllipse(m, 6, 6)
            painter.drawLine(b, m)


class PeriodicArrowView(QWidget):
    """Canvas, key and sentence for the selected pair."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('gmshPeriodicArrowView')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._canvas = _ArrowCanvas(self)
        self._note = QLabel(self)
        self._note.setObjectName('gmshPeriodicArrowNote')
        self._note.setWordWrap(True)
        layout.addWidget(self._canvas)
        layout.addWidget(self._note)
        self._info: dict = {}
        self.showRow(None)

    def info(self) -> dict:
        return dict(self._info)

    def canvas(self):
        return self._canvas

    def showRow(self, row, source_centre=None, target_centre=None) -> None:
        if not row:
            self._info = {'problem': ''}
            self._note.setText(self.tr(
                'Select a pair to see its source to target arrow.'))
        else:
            self._info = describe(row, source_centre, target_centre)
            if 'problem' in self._info:
                self._note.setText(self._info['problem'])
            else:
                self._note.setText(
                    self.tr(self._info['text']) + ' ' + self.tr(
                        'Blue is the source, orange the target; a dashed '
                        'red circle is where the transform lands when that '
                        'is not the target.'))
        self._canvas.setInfo(self._info)
