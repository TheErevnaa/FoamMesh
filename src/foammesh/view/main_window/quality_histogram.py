"""The quality distribution, drawn as bars.

Plan 26 WP6.4. ``checkMesh`` reports a maximum. A maximum of 66 degrees does
not distinguish one bad cell from eight thousand -- the difference between
"ignore" and "remesh" -- so the maximum alone cannot support the decision a
user has to make with it.

Drawn with QPainter rather than a charting dependency: the whole figure is
bars, one axis and two labels, and adding a plotting stack to draw it would be
more code than this, not less. The `dataviz` conventions it does follow are the
ones that matter here -- one series, no gridlines competing with the bars, the
count and the range stated in text so the figure is readable without measuring
pixels, and colour used only to mark the region beyond the limit, never as the
sole carrier of that meaning.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


class QualityHistogram(QWidget):
    """One metric's distribution, with the limit marked."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('qualityHistogram')
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        self._metric = ''
        self._edges: list[float] = []
        self._counts: list[int] = []
        self._limit: float | None = None
        self._beyond_is_above = True
        self.clear()

    # -- data -------------------------------------------------------------- #

    def clear(self) -> None:
        self._metric, self._edges, self._counts = '', [], []
        self._limit = None
        self._describe()
        self.update()

    def set_distribution(self, metric: str, histogram: dict, *,
                         limit: float | None = None,
                         beyond_is_above: bool = True) -> None:
        """Show *histogram* for *metric*, marking *limit* if there is one.

        ``beyond_is_above`` says which side of the limit is the bad one:
        non-orthogonality and skewness fail high, element quality fails low.
        Getting it backwards would shade exactly the healthy cells.
        """
        self._metric = str(metric or '')
        self._edges = [float(edge) for edge in (histogram or {}).get('edges') or ()]
        self._counts = [int(count) for count in (histogram or {}).get('counts') or ()]
        self._limit = None if limit is None else float(limit)
        self._beyond_is_above = bool(beyond_is_above)
        self._describe()
        self.update()

    @property
    def total(self) -> int:
        return sum(self._counts)

    def beyond_limit(self) -> int:
        """Cells on the failing side of the limit, by bin.

        Approximate by construction -- a bin straddling the limit is counted
        whole -- so it is never presented as the authoritative count. The
        metric table carries that; this is for reading the shape.
        """
        if self._limit is None or len(self._edges) < 2:
            return 0
        total = 0
        for index, count in enumerate(self._counts):
            low, high = self._edges[index], self._edges[index + 1]
            middle = 0.5 * (low + high)
            if (middle > self._limit) if self._beyond_is_above else (
                    middle < self._limit):
                total += count
        return total

    def _describe(self) -> None:
        """The figure in words, for a reader that cannot see the bars."""
        if not self._counts:
            self.setAccessibleName('Quality distribution: no mesh measured')
            self.setAccessibleDescription('')
            return
        low, high = self._edges[0], self._edges[-1]
        text = (f'{self._metric} distribution over {self.total:,} cells, '
                f'from {low:.4g} to {high:.4g}')
        if self._limit is not None:
            side = 'above' if self._beyond_is_above else 'below'
            text += (f'; about {self.beyond_limit():,} cells are {side} the '
                     f'limit of {self._limit:.4g}')
        self.setAccessibleName(f'{self._metric} quality distribution')
        self.setAccessibleDescription(text)
        self.setToolTip(text)

    # -- painting ---------------------------------------------------------- #

    def paintEvent(self, event) -> None:      # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        text_colour = palette.windowText().color()
        painter.setPen(QPen(text_colour))

        if not self._counts:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self.tr('No mesh has been measured yet.'))
            return

        margin, label_height = 6.0, 18.0
        area = QRectF(margin, margin, self.width() - 2 * margin,
                      self.height() - 2 * margin - label_height)
        tallest = max(self._counts) or 1
        span = len(self._counts)
        width = area.width() / span

        normal = QColor(text_colour)
        normal.setAlpha(150)
        # The failing region is marked with a hatch as well as a colour, so the
        # distinction survives a colour-blind reader and a greyscale print.
        beyond = QColor(text_colour)
        beyond.setAlpha(90)

        for index, count in enumerate(self._counts):
            height = (count / tallest) * area.height()
            bar = QRectF(area.left() + index * width,
                         area.bottom() - height, max(width - 1.0, 1.0), height)
            middle = 0.5 * (self._edges[index] + self._edges[index + 1])
            failing = self._limit is not None and (
                middle > self._limit if self._beyond_is_above
                else middle < self._limit)
            painter.fillRect(bar, beyond if failing else normal)
            if failing:
                painter.setPen(QPen(text_colour, 1, Qt.PenStyle.DotLine))
                painter.drawRect(bar)
                painter.setPen(QPen(text_colour))

        # The limit itself, as a line the eye can find without reading a key.
        if self._limit is not None and self._edges[-1] > self._edges[0]:
            fraction = ((self._limit - self._edges[0])
                        / (self._edges[-1] - self._edges[0]))
            if 0.0 <= fraction <= 1.0:
                x = area.left() + fraction * area.width()
                painter.setPen(QPen(text_colour, 2, Qt.PenStyle.DashLine))
                painter.drawLine(int(x), int(area.top()),
                                 int(x), int(area.bottom()))
                painter.setPen(QPen(text_colour))

        painter.drawLine(int(area.left()), int(area.bottom()),
                         int(area.right()), int(area.bottom()))
        footer = QRectF(area.left(), area.bottom() + 2, area.width(),
                        label_height)
        painter.drawText(footer, Qt.AlignmentFlag.AlignLeft,
                         f'{self._edges[0]:.4g}')
        painter.drawText(footer, Qt.AlignmentFlag.AlignRight,
                         f'{self._edges[-1]:.4g}')
        painter.drawText(
            footer, Qt.AlignmentFlag.AlignCenter,
            self.tr('%s over %s cells') % (self._metric, f'{self.total:,}'))
