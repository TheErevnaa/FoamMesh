"""Stop controls from being drawn narrower than the words inside them.

Across the shell, buttons and table headers were clipped on *both* sides --
``uggest pla`` for "Suggest plan", ``meters (JS`` for "Parameters (JSON)",
``!evert and Ed`` for "Revert and edit". That is worse than an elide: an elide
keeps the beginning and marks what it dropped, whereas a clip removes the first
characters too, and the word cannot be recovered from what is left.

The register asks for this to be fixed once at the layout level rather than
label by label, which is what these helpers are for.
"""
from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtWidgets import QHeaderView, QLayout, QSizePolicy


#: Room for a button's own frame and padding on top of its text.
_BUTTON_PADDING = 28
#: Room for a header cell's padding, plus a sort indicator that may appear.
_HEADER_PADDING = 24


def fit_to_text(widget, padding: int = _BUTTON_PADDING):
    """Give ``widget`` a minimum width its own label fits inside."""
    text = widget.text() if hasattr(widget, 'text') else ''
    text = text.replace('&&', '\x00').replace('&', '').replace('\x00', '&')
    widget.setMinimumWidth(
        widget.fontMetrics().horizontalAdvance(text) + padding)
    widget.setSizePolicy(QSizePolicy.Policy.Minimum,
                         widget.sizePolicy().verticalPolicy())
    return widget


def fit_headers(view, stretch_column: int | None = None,
                padding: int = _HEADER_PADDING):
    """Size a table or tree's columns so no header text is clipped.

    Every column keeps at least its own title's width; ``stretch_column``
    absorbs whatever slack is left, so the free space lands on the column with
    the most to say instead of being shared out equally between one long
    description and three short ids.
    """
    header = view.horizontalHeader() if hasattr(view, 'horizontalHeader') else view.header()
    model = view.model()
    if model is None:
        return view
    metrics = header.fontMetrics()
    widest = 0
    for column in range(model.columnCount()):
        title = str(model.headerData(column, Qt.Orientation.Horizontal) or '')
        widest = max(widest, metrics.horizontalAdvance(title))
        header.setSectionResizeMode(
            column,
            QHeaderView.ResizeMode.Stretch if column == stretch_column
            else QHeaderView.ResizeMode.ResizeToContents)
    header.setMinimumSectionSize(widest + padding)
    return view


class FlowLayout(QLayout):
    """A row of controls that wraps instead of clipping its last item.

    A button row in a fixed-width panel had two ways to fail: squeeze every
    button until its label was cut off at both ends, or run off the edge and
    take a page-wide horizontal scrollbar with it. Wrapping to a second line
    costs one row of height and loses nothing.
    """

    def __init__(self, parent=None, spacing: int = 6):
        super().__init__(parent)
        self._items = []
        self.setSpacing(spacing)

    def addItem(self, item):                                 # noqa: N802
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):                                 # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):                                 # noqa: N802
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):                           # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):                             # noqa: N802
        return True

    def heightForWidth(self, width):                         # noqa: N802
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):                             # noqa: N802
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self):                                      # noqa: N802
        #: What one uncramped row would take. The *minimum* is one item wide,
        #: which is what lets the row wrap rather than clip.
        width = 0
        height = 0
        for index, item in enumerate(self._items):
            hint = item.sizeHint()
            width += hint.width() + (self.spacing() if index else 0)
            height = max(height, hint.height())
        margins = self.contentsMargins()
        return QSize(width + margins.left() + margins.right(),
                     height + margins.top() + margins.bottom())

    def minimumSize(self):                                   # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(),
                            margins.top() + margins.bottom())

    def _layout(self, rect, apply: bool) -> int:
        margins = self.contentsMargins()
        x = rect.x() + margins.left()
        y = rect.y() + margins.top()
        right = rect.right() - margins.right()
        line_height = 0
        spacing = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            if line_height and x + hint.width() > right:
                x = rect.x() + margins.left()
                y += line_height + spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x += hint.width() + spacing
            line_height = max(line_height, hint.height())
        return y + line_height + margins.bottom() - rect.y()
