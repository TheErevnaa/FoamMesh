"""Shared non-colour visual metrics for custom widgets."""
from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import (
    QDoubleSpinBox, QHBoxLayout, QLabel, QProxyStyle, QSizePolicy, QSpinBox,
    QStyle, QTableView, QWidget,
)

FOCUS_WIDTH = 2
CONTROL_RADIUS = 4
CONTROL_PADDING = 4
SELECTION_BORDER_WIDTH = 2
ICON_SIZE = 20

# --------------------------------------------------------------------------- #
# DP-189. One height for every row of controls in the app.
#
# MEASURED offscreen under the windows11 style with the real theme and the
# application font at 10pt, on the surfaces a user meets in one meshing run:
# a page push button came out 27px, a ribbon tool button 28, an output-band
# tab 28, a metrics table row 30, an outline row 31 and a metrics table
# header 34. Six heights spanning 7px, in a column the eye reads top to
# bottom, and not one of them was chosen: the text is 15px tall on all six
# and the whole spread is chrome, contributed by five QSS rules that had
# never been written next to each other -- `padding: 5px 14px` on buttons,
# `4px 6px` on tool buttons, `6px 14px` on tabs, `5px 8px` on header
# sections and `6px 6px` on outline rows -- plus the vertical header's own
# default section size, which no stylesheet reaches at all.
#
# The QSS now carries the arithmetic per surface (each rule says what it
# adds up to), because padding and border differ per widget and the style
# adds 3px of its own to a tool button. What cannot be said in QSS is said
# here: a table's row height comes from its vertical header, so it is set
# once for every table in the app rather than at each of a dozen
# construction sites, which is what let the tables drift in the first place.
# --------------------------------------------------------------------------- #

#: The height every control in the app is laid out to.
CONTROL_HEIGHT = 28

#: Marks the application the polish filter below is already installed on, so
#: re-applying the theme does not stack a second one.
_CONTROL_METRICS_INSTALLED = '_foammesh_control_metrics_installed'


def apply_table_metrics(table) -> None:
    """Give one table's rows the shared control height.

    Rows that are sized to their contents still grow past it; this sets the
    floor the table starts from, which is the number the eye compares against
    the button and the tab beside it.
    """
    header = table.verticalHeader()
    if header is not None:
        header.setDefaultSectionSize(CONTROL_HEIGHT)


class _ControlMetrics(QObject):
    """Applies the shared row height to every table as it is polished."""

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Polish and isinstance(watched, QTableView):
            apply_table_metrics(watched)
        return False


def install_control_metrics(application) -> bool:
    """Put every table in the application on the shared control height.

    An application-wide filter rather than a call at each table: there are a
    dozen places that build one, and a thirteenth added later would silently
    be 30px tall again. Tables that exist already are done directly, because
    a widget is polished once and will not be polished again when the theme
    is re-applied.

    Returns whether this call installed the filter.
    """
    if application.property(_CONTROL_METRICS_INSTALLED):
        return False
    application.installEventFilter(_ControlMetrics(application))
    application.setProperty(_CONTROL_METRICS_INSTALLED, True)
    for widget in application.allWidgets():
        if isinstance(widget, QTableView):
            apply_table_metrics(widget)
    return True


# --------------------------------------------------------------------------- #
# DP-105. One spacing scale for the settings forms of both pipelines.
#
# Until this existed there was none: the view layer carried eight distinct
# `.ui` spacing values, twelve distinct `.ui` margin values, ten distinct
# `setContentsMargins` tuples and five distinct `setSpacing` values, and every
# form took whatever the platform style handed it. MEASURED in the twenty-leg
# sweep: on one task page the Guided group's field column started at a
# different x from the Advanced group's and its rows were about eight pixels
# taller, so two halves of one page read as two pages. These are the numbers
# every registry-built form uses, so a settings form looks the same wherever
# it is built.
# --------------------------------------------------------------------------- #

#: Vertical gap between two rows of a settings form.
#:
#: Plan 33 FORM-04. This was 6, the gap between two siblings anywhere in the
#: shell. MEASURED at a 520 px panel with the shipped font: a Gmsh Global
#: Sizing row was 34 px tall at that pitch and the page showed nineteen of
#: them, so the pitch alone spent 114 px of the middle panel on nothing. Two
#: rows of one form are closer to each other than two siblings are -- they
#: are one list -- so the pitch is the tight step of the gap scale, 4.
FORM_ROW_SPACING = 4
#: Horizontal gap between a field's label and its editor.
FORM_LABEL_SPACING = 8
#: Space between a group box's frame and the form inside it.
FORM_MARGIN = 8


# --------------------------------------------------------------------------- #
# Gaps
#
# DP-194. The scale above says how far a thing sits from an edge. Nothing
# said how far two things sit from each other, so every gap in the product
# was a number typed at its own call site: 38 sites, 37 of them bare
# numbers, eleven different values -- 0, 2, 3, 4, 5, 6, 8, 9, 10, 16 and 20.
# Two of them are on screen together: the parts overlay spaces its rows 4px
# apart while the verdict line under it spaces its controls 8px apart. One
# layout carried two answers at once, `horizontalLayout_2`, written 6 in the
# form and overwritten with 2 from Python a moment later.
#
# Three steps, because a gap only ever says one of three things: these two
# belong to each other, these two are siblings, these two are different
# groups.
#
# `GAP` is 6 because 6 is what the product already used. 240 of its 263
# layouts name no spacing at all and take the style's, and every style Qt
# offers here -- windows11, windowsvista, Fusion, Windows -- hands back 6.
# The fix therefore writes down the gap the product had rather than
# imposing a new one, which is why it moves 20 sites and not 263, and why
# only 14 of those 20 change the number a reader actually sees. It is
# `FORM_ROW_SPACING` by construction: the distance between two rows of a
# form is the distance between two siblings anywhere else.
# --------------------------------------------------------------------------- #

#: Gap between two sibling controls. The default; use it unless the two
#: things are closer than siblings or further apart than siblings.
#:
#: Plan 33 FORM-04 untied this from `FORM_ROW_SPACING`. The two were one
#: number because a form row and a pair of siblings were assumed to want the
#: same distance; the middle panel says they do not, and 6 is the one of the
#: two that Qt itself hands back, so it is the one that keeps the name.
GAP = 6
#: Gap between two things that belong to each other -- a glyph and its word,
#: the rows of one list, the parts of one chip. Shares its value with
#: `CONTROL_PADDING` and `BAR_INSET_V`, which is the same distance doing the
#: same job in the other direction.
GAP_TIGHT = CONTROL_PADDING
#: Gap between two groups that are not the same thing.
GAP_SECTION = GAP * 2


# --------------------------------------------------------------------------- #
# Bars
#
# DP-191. Four horizontal bars sit in the shell -- the run status line above
# the wizard actions, the verdict line above the output tabs, the parts
# overlay floating on the viewport, and the wizard action row itself -- and
# each had invented its own padding. Measured on screen their content started
# at x = 8, 9, 7 and 2, so the left edge of the window read as four edges, and
# two of them spent more space below their content than above it, so what they
# painted sat off centre inside its own bar.
#
# The inset is measured from the bar's OUTER edge, because that is the edge a
# reader sees. A bar that draws a frame therefore spends part of the inset on
# the frame rather than adding to it, which is what put the verdict line's
# glyph a pixel to the right of the run status text.
# --------------------------------------------------------------------------- #

#: Distance from a bar's outer edge to the first thing it paints.
BAR_INSET_H = FORM_MARGIN
#: Space above and below a bar's content. A bar carrying one control is
#: therefore CONTROL_HEIGHT + 2 * BAR_INSET_V tall, and every such bar in the
#: shell is the same height as a consequence rather than by coincidence.
BAR_INSET_V = 4

def apply_bar_metrics(widget, layout=None) -> None:
    """Give one shell bar the house inset.

    `widget` is the bar; `layout` its layout, read off the widget when it
    is not passed. The frame the bar draws, if any, counts towards the
    inset. The gap between the things inside a bar is left alone: that is
    the bar's own business and is not what DP-191 measured.
    """
    bar_layout = widget.layout() if layout is None else layout
    frame = widget.frameWidth() if hasattr(widget, 'frameWidth') else 0
    horizontal = max(0, BAR_INSET_H - frame)
    vertical = max(0, BAR_INSET_V - frame)
    bar_layout.setContentsMargins(
        horizontal, vertical, horizontal, vertical)


def apply_form_metrics(layout, wrap: bool = True) -> None:
    """Give one `QFormLayout` the shared column, pitch and margins.

    Called by every place that builds a registry-backed form. Keeping it a
    function rather than four copies of six setters is the point: the next
    form added to the app is uniform because it cannot easily be anything
    else.
    """
    from PySide6.QtWidgets import QFormLayout

    # Plan 33 FORM-04. The form inside a group box is nested twice over --
    # the box's own frame, then this -- and 8 px on each side of a panel
    # whose minimum is 340 spends 16 px of it on the inside of a border the
    # reader can already see. The tight step of the margin scale, 4.
    layout.setContentsMargins(MARGIN_TIGHT, MARGIN_TIGHT,
                              MARGIN_TIGHT, MARGIN_TIGHT)
    layout.setVerticalSpacing(FORM_ROW_SPACING)
    layout.setHorizontalSpacing(FORM_LABEL_SPACING)
    # The label column is what makes two groups read as one page: left
    # alignment puts every field's editor at the same x whatever the label
    # says, and `AllNonFixedFieldsGrow` stops a short editor from sitting in
    # the middle of a wide row while a long one fills it.
    layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter)
    layout.setFormAlignment(Qt.AlignmentFlag.AlignLeft
                            | Qt.AlignmentFlag.AlignTop)
    layout.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    # A form in a panel narrow enough to squeeze the editors wraps the label
    # above its field rather than shaving the field to nothing.
    #
    # Plan 33 FORM-04 asks for label, input and unit on one row wherever the
    # width permits, and `WrapLongRows` is Qt deciding that per row from its
    # own size hints: MEASURED at the shell's own panel minimum, the long
    # labels of Gmsh Global Sizing wrapped above their editors and doubled
    # the height of five of nineteen rows. The watcher below answers the one
    # question the policy cannot: whether the panel is at least as wide as
    # the shell guarantees it will be.
    if not wrap:
        # Plan 33 W-O2. `wrap=False` is for a form that is already as narrow
        # as it will ever be: one cell of a `ReflowGrid`. The watcher below
        # compares the host's width with a settings column's minimum, and a
        # grid cell never reaches it, so every row of every cell would wrap
        # its label above its field for ever. That is not only ugly, it
        # feeds back: a wrapped row is narrower, a narrower cell lets the
        # grid count another column, and the narrower cells wrap harder.
        # MEASURED on `snappy.base_grid` at a 635 px settings column, the
        # six background faces went from two columns to four. The grid
        # decides how many columns a width holds (DP-160); a cell keeps its
        # label beside its field.
        layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        return
    layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    _watch_form_width(layout)


#: The width at or above which a form keeps label, editor and unit on one
#: row. It is the shell's own guaranteed width for the settings region
#: (`three_region_shell.REGION_B_MINIMUM`), written here as a number because
#: the theming layer cannot import the shell without a cycle; a gate holds
#: the two equal.
FORM_SINGLE_ROW_WIDTH = 340


class _FormWrapWatcher(QObject):
    """Keep one `QFormLayout` on one row per field while the width allows.

    Plan 33 FORM-04. Installed on the widget the form is built into, so the
    policy follows the panel the reader is actually looking at rather than a
    decision made once at construction.

    DP-573 (0924 rerun2, S6 Boundary layers). "The width allows" was read
    off the host's own width, and a host is never narrower than its own
    minimum -- which, on one row, is the aligned label column plus the
    widest field. On snappy Layers that is 465 px: at a 443 px settings
    column the form stayed on one row because it was 465 px wide, and the
    page's scroller, which has no sideways bar, cut its right-hand 70 px --
    the group border, the unit column and the ends of the number boxes.
    The width is now the one the host is *offered* -- its own, less
    whatever the page's scroller cuts off -- and one row is kept only where
    it fits in that.
    """

    def __init__(self, layout, host) -> None:
        super().__init__(host)
        self._layout = layout
        self._host = host
        self._viewport = None
        self._apply()

    def _scroller(self):
        from PySide6.QtWidgets import QScrollArea

        widget = self._host.parentWidget()
        while widget is not None:
            if isinstance(widget, QScrollArea) and widget.widget() is not None:
                return widget
            widget = widget.parentWidget()
        return None

    def _offered(self) -> int:
        """The host's width less what the nearest scroller cuts off."""
        width = self._host.width()
        scroller = self._scroller()
        if scroller is None:
            return width
        viewport = scroller.viewport()
        if viewport is not self._viewport:
            if self._viewport is not None:
                self._viewport.removeEventFilter(self)
            self._viewport = viewport
            viewport.installEventFilter(self)
        overflow = scroller.widget().width() - viewport.width()
        return width - max(0, overflow)

    def _one_row_need(self) -> int:
        """The contents width every row asks with label and field side by side.

        Read from the widgets' own minimums, which the wrap policy does not
        change, so wrapping cannot talk itself back out of wrapping.
        """
        from PySide6.QtWidgets import QFormLayout

        layout = self._layout
        spacing = max(0, layout.horizontalSpacing())
        need = 0
        for row in range(layout.rowCount()):
            widths = []
            for role in (QFormLayout.ItemRole.LabelRole,
                         QFormLayout.ItemRole.FieldRole,
                         QFormLayout.ItemRole.SpanningRole):
                item = layout.itemAt(row, role)
                if item is not None and not item.isEmpty():
                    widths.append(item.minimumSize().width())
            if widths:
                need = max(need, sum(widths) + spacing * (len(widths) - 1))
        margins = layout.contentsMargins()
        chrome = self._host.width() - self._host.contentsRect().width()
        return need + margins.left() + margins.right() + chrome

    def _apply(self, *_args) -> None:
        from PySide6.QtWidgets import QFormLayout

        offered = self._offered()
        policy = (QFormLayout.RowWrapPolicy.DontWrapRows
                  if offered >= FORM_SINGLE_ROW_WIDTH
                  and offered >= self._one_row_need()
                  else QFormLayout.RowWrapPolicy.WrapLongRows)
        if self._layout.rowWrapPolicy() != policy:
            self._layout.setRowWrapPolicy(policy)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._apply()
        return False


def _watch_form_width(layout) -> None:
    """Follow the host's width, so a form only wraps when it has to."""
    host = layout.parentWidget()
    if host is None:
        return
    watcher = _FormWrapWatcher(layout, host)
    host.installEventFilter(watcher)


def align_form_columns(layouts) -> int:
    """Put the label column of several `QFormLayout`s at one width.

    DP-151. The comment above says one call gives both groups one scale, and
    the docstring above says left alignment "puts every field's editor at the
    same x whatever the label says". Neither is true of the width: a
    `QFormLayout` sizes its label column from its own longest label, so two
    groups stacked on one page put their editors wherever their own longest
    label happens to put them. MEASURED on the current build -- Compute Mesh
    at guided x=242 against advanced x=217, Global Sizing 69 px apart, and
    the same jog visible in the `9fe32a2a` sweep frames at 437 against 411.

    Each label is reset before it is measured, so a page whose labels get
    shorter -- CP-09 strips " (inactive)" as often as it adds it -- narrows
    again instead of holding the widest text it has ever shown.

    Returns the width applied, which is 0 when there are no labels to align.
    """
    from PySide6.QtWidgets import QFormLayout

    labels = []
    for layout in layouts:
        if layout is None:
            continue
        for row in range(layout.rowCount()):
            item = layout.itemAt(row, QFormLayout.ItemRole.LabelRole)
            widget = item.widget() if item is not None else None
            if widget is not None:
                labels.append(widget)
    if not labels:
        return 0
    for label in labels:
        label.setMinimumWidth(0)
    width = max(label.sizeHint().width() for label in labels)
    for label in labels:
        label.setMinimumWidth(width)
    return width


UNIT_LABEL_NAME = 'fieldUnit'


class UnitLabel(QLabel):
    """The unit suffix that ends a field cell.

    DP-156. It reports the width of its column as its preferred size and its
    own text as its minimum, so the units line up whenever the page has room
    and the row gives the space back rather than wrapping when it does not.
    A plain `setMinimumWidth` was tried first and wrapped `gmsh.boundary_layers`
    at 640 px: the widest unit there is `ratio`, and forcing that width on the
    empty unit of the Mode combo took the cell's minimum past what the field
    column could give, so `QFormLayout` dropped the combo under its label.
    """

    def __init__(self, text: str = '', parent=None) -> None:
        super().__init__(text, parent)
        self.setObjectName(UNIT_LABEL_NAME)
        self._columnWidth = 0

    def columnWidth(self) -> int:
        return self._columnWidth

    def setColumnWidth(self, width: int) -> None:
        width = max(0, int(width))
        if width == self._columnWidth:
            return
        self._columnWidth = width
        self.updateGeometry()

    def sizeHint(self):
        hint = super().sizeHint()
        if self._columnWidth > hint.width():
            hint.setWidth(self._columnWidth)
        return hint


def align_unit_column(layouts) -> int:
    """Put the unit suffixes of several `QFormLayout`s in one column.

    DP-156. The unit is laid out inside the field cell, to the right of the
    editor, so the editor gives up the unit's width -- and units are not one
    width. MEASURED across both pipelines: nine task pages drew their number
    boxes with **two to four different right edges**, because `deg` is 24 px
    and `m` is 11 px and a field with no unit gives up nothing at all. On the
    snappy Castellation and Layers pages that is four edges down one column
    (`fraction`, `deg`, `cells`, none), which reads as four indents nothing
    chose.

    Every field cell now ends with a unit label whether or not the field has
    a unit, and this gives all of them one width, so every editor in the
    column ends at the same x and every unit starts at the same x. When no
    field in the group has a unit the labels are hidden instead -- a hidden
    widget takes neither width nor the spacing before it -- so a page without
    units is not made narrower to match one that has them.

    Returns the width applied, 0 when the group has no units.
    """
    from PySide6.QtWidgets import QFormLayout

    units = []
    for layout in layouts:
        if layout is None:
            continue
        for row in range(layout.rowCount()):
            item = layout.itemAt(row, QFormLayout.ItemRole.FieldRole)
            cell = item.widget() if item is not None else None
            if cell is None:
                continue
            units.extend(
                child for child in cell.findChildren(QLabel)
                if child.objectName() == UNIT_LABEL_NAME)
    if not units:
        return 0
    for label in units:
        label.setColumnWidth(0)
    width = max((label.sizeHint().width() for label in units if label.text()),
                default=0)
    for label in units:
        label.setVisible(bool(width))
        label.setColumnWidth(width)
    return width


class ReflowGrid(QWidget):
    """Equal cells that drop to fewer columns rather than off the page.

    DP-160. The six background faces of `snappy.base_grid` were laid three
    across in a plain `QGridLayout`. Each face group needs 262 px, so the row
    needs 822 px, and the task panel gives its pages a 615 px viewport whose
    horizontal scrollbar is deliberately off (C5: only the innermost
    scrollable thing scrolls). MEASURED: the page's scroll content was 840 px
    wide in that 615 px viewport, so the whole third column -- `Low Y (yMin)`,
    `High Y (yMax)`, `High Z (zMax)` -- was clipped off the right edge with no
    scrollbar, no ellipsis and no way to reach it. Two of the six faces of the
    background mesh could not be named or typed at all, and the writer refuses
    a mesh whose face is classified but unnamed.

    A grid cannot be asked to wrap, so this counts. The widget's minimum width
    is one cell, not one row, and on every resize it re-lays the cells into as
    many columns as the width it was actually given will hold. At 615 px that
    is two columns of three rows; on a wider panel it goes back to three.
    """

    def __init__(self, parent=None, spacing: int = FORM_ROW_SPACING) -> None:
        from PySide6.QtWidgets import QGridLayout

        super().__init__(parent)
        self._cells: list = []
        self._columns = 0
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(spacing)
        self._grid.setVerticalSpacing(spacing)

    def addCell(self, widget) -> None:
        """Add one cell. Cells keep the order they are added in."""
        widget.setParent(self)
        self._cells.append(widget)
        self._columns = 0
        self._relayout(self._fit(self.width()))

    def cells(self) -> tuple:
        return tuple(self._cells)

    def columns(self) -> int:
        """How many columns the last layout used."""
        return self._columns

    def cellWidth(self) -> int:
        """What the widest cell needs. No column is ever narrower."""
        return max([cell.minimumSizeHint().width() for cell in self._cells]
                   or [0])

    def _fit(self, available: int) -> int:
        """How many of these cells sit side by side in `available` pixels."""
        if not self._cells:
            return 0
        cell = self.cellWidth()
        spacing = self._grid.horizontalSpacing()
        columns = 1
        while columns < len(self._cells):
            if (columns + 1) * cell + columns * spacing > available:
                break
            columns += 1
        return columns

    def _relayout(self, columns: int) -> None:
        columns = max(1, columns)
        # One cell, not one row. This is the lever that lets the grid be given
        # less than a full row so that `resizeEvent` can count again. It is
        # set on every pass because a cell that is filled after it is added --
        # every one of these is, the forms are populated from the registry
        # later -- is wider than it was when the column count last changed.
        self.setMinimumWidth(self.cellWidth())
        if columns == self._columns:
            return
        self._columns = columns
        while self._grid.count():
            self._grid.takeAt(0)
        for index, cell in enumerate(self._cells):
            self._grid.addWidget(cell, index // columns, index % columns)
        # A column that holds nothing is told to hold nothing: a stale stretch
        # left behind by a wider layout would keep its share of the width.
        for column in range(self._grid.columnCount()):
            self._grid.setColumnStretch(column, 1 if column < columns else 0)
            self._grid.setColumnMinimumWidth(column, 0)
        self._grid.invalidate()
        self.updateGeometry()

    def event(self, event):
        # The cells grow after they are added, so the count is taken again
        # whenever anything inside asks for a new layout, not only on resize.
        if event.type() == QEvent.Type.LayoutRequest:
            self._relayout(self._fit(self.width()))
        return super().event(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout(self._fit(event.size().width()))


# --------------------------------------------------------------------------- #
# DP-164. One number box, and one place for the unit beside it.
#
# These two classes were written for the registry-built forms and lived in
# `workflow_controls/field_widgets.py`, which a dialog or a hand-built page
# cannot import without dragging the whole task-page module in behind it. So
# sixteen number boxes elsewhere in the app were plain `QDoubleSpinBox` and
# `QSpinBox`, and printed their values by a different rule from every box on
# a task page. They live here, beside `UnitLabel`, because this module is the
# one both sides already import.
# --------------------------------------------------------------------------- #

#: Headroom every number box reserves regardless of the value it holds, so a
#: column of boxes is one width and a box does not resize as it is typed into.
_BOX_HINT_SAMPLE = '000000'

#: Floor under the computed width, in case a font makes the arithmetic below
#: collapse. Nothing observed hits it; it exists so nothing can.
_BOX_MIN_WIDTH = 72


def _value_sized_hint(box, hint):
    """Size a spin box from the value it shows, not the range it could hold.

    DP-130. Qt sizes a spin box from `textFromValue(minimum())` and
    `textFromValue(maximum())`. Both halves of that were wrong here, in
    opposite directions, on the same page.

    `CompactDoubleSpinBox.textFromValue` trims trailing zeros (DP-101), and
    Qt asks the subclass. So a 0..1 fraction box reports its endpoints as
    `0` and `1` and asks for the width of one character -- 50 px measured --
    while holding `0.005`, which needs 60. The box was narrower than its own
    contents and clipped them.

    An integer field the schema leaves unbounded gets the full int32 range,
    whose endpoint text is `-2147483647`. That box demands 168 px, 3.4x the
    fraction box beside it, which is enough for `QFormLayout`'s
    `WrapLongRows` to wrap that one row -- label above, field across the
    whole panel -- while its neighbours stay on one line.

    One rule fixes both: ask for the width of the text actually on show,
    floored at a fixed sample so the column is uniform and a box does not
    twitch as digits are typed. The base hint is kept and only its text
    allowance is swapped, so frame, buttons and prefix stay Qt's business.

    DP-164. A box with a special value text -- Repair's optional distances
    show `Automatic` at zero -- shows that word, not a number, and Qt has
    already sized the hint to hold it. Counting it on both sides of the swap
    leaves the frame allowance intact and keeps the word from being clipped
    by a narrower number.
    """
    metrics = box.fontMetrics()
    endpoints = max(metrics.horizontalAdvance(box.textFromValue(box.minimum())),
                    metrics.horizontalAdvance(box.textFromValue(box.maximum())))
    wanted = max(metrics.horizontalAdvance(box.textFromValue(box.value())),
                 metrics.horizontalAdvance(_BOX_HINT_SAMPLE))
    special = box.specialValueText()
    if special:
        allowance = metrics.horizontalAdvance(special)
        endpoints = max(endpoints, allowance)
        wanted = max(wanted, allowance)
    hint.setWidth(max(hint.width() + wanted - endpoints, _BOX_MIN_WIDTH))
    return hint


class CompactDoubleSpinBox(QDoubleSpinBox):
    """A number box that keeps nine decimals and does not display nine.

    DP-101. Every number field on every task page of both pipelines is built
    here, and every one of them carried `setDecimals(9)`, so a refinement
    level of 0.1 was shown as `0.100000000`, a default of zero as
    `0.000000000`, and a page of five such fields read as a column of noise
    in which the one number the user had changed was indistinguishable from
    the four they had not. The precision is worth keeping; printing it is
    not. `textFromValue` trims the zeros the value does not need, and
    `valueFromText` is inherited unchanged, so the box still accepts and
    stores all nine.

    DP-164. "Every number field on every task page" was true and was not the
    whole app: thirteen boxes in the geometry pages, the refinement dialogs,
    the base-grid page and the mesh-transform dialog were plain
    `QDoubleSpinBox`, and printed `1.00000000`, `150.000`, `30.0` and
    `90.000000` on pages whose other numbers had been trimmed since DP-101.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # DP-130. The hint below depends on the value, so the layout has to
        # be told when the value moves.
        self.valueChanged.connect(self._reHint)

    def textFromValue(self, value: float) -> str:
        text = super().textFromValue(value)
        point = self.locale().decimalPoint()
        if point not in text:
            return text
        text = text.rstrip('0')
        return text[:-1] if text.endswith(point) else text

    def _reHint(self, _value) -> None:
        self.updateGeometry()

    def sizeHint(self):
        return _value_sized_hint(self, super().sizeHint())

    def minimumSizeHint(self):
        return _value_sized_hint(self, super().minimumSizeHint())


class CompactSpinBox(QSpinBox):
    """A whole-number box that is not as wide as the int32 it could hold.

    DP-130. See `_value_sized_hint`: an unbounded integer field is the widest
    thing on its page for no reason anyone chose, and on a narrow task panel
    that width is what wraps its row.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.valueChanged.connect(self._reHint)

    def _reHint(self, _value) -> None:
        self.updateGeometry()

    def sizeHint(self):
        return _value_sized_hint(self, super().sizeHint())

    def minimumSizeHint(self):
        return _value_sized_hint(self, super().minimumSizeHint())


def unit_cell(editor, unit: str = '', parent=None):
    """An editor and the unit it is measured in, as one form field.

    DP-164. The registry-built rows have ended this way since DP-156: the
    editor, then a `UnitLabel` in its own column. Everywhere else the unit
    was written into one of two other places -- inside the box as a Qt
    suffix (`150.000 deg`, `30.0` with a degree sign) or inside the row's
    label in brackets (`Angular deflection (deg)`, `Target cell size (m)`) --
    and the same unit was spelled seven different ways across the app.
    A suffix is the worse of the two: it is part of the number, so it moves
    as the number is typed, it defeats the unit column entirely, and on
    `snappy.surface_features` it put `150.000 deg` beside four registry
    boxes reading `0` with `deg` in a column of its own.

    Returns the widget to hand `QFormLayout.addRow` in place of the editor.
    """
    row = QHBoxLayout()
    # DP-198. Zero, because this cell sits INSIDE a form field that has
    # already been given the form's margin. MEASURED on a three-row form
    # under the real theme: with the layout default of 8 the editor of the
    # unit row started at x=159 while its two neighbours started at 151 and
    # ended at 374 against their 412, and the row stood 43px tall against
    # their 27. Every one of those numbers is the margin, not the unit.
    row.setContentsMargins(
        MARGIN_NONE, MARGIN_NONE, MARGIN_NONE, MARGIN_NONE)
    row.addWidget(editor, 1)
    row.addWidget(UnitLabel(unit or ''))
    container = QWidget(parent)
    container.setLayout(row)
    # The row's label is given this container as its buddy, so the mnemonic
    # has to land on the editor rather than on the box holding it.
    container.setFocusProxy(editor)
    return container


#: Where a stacked page's own vertical policy is kept while it is not the
#: page on show, so restoring it does not have to guess.
_PARKED_VERTICAL = '_foammesh_parked_vertical_policy'


def size_stack_to_current_page(stack) -> None:
    """Let a stacked widget be only as tall as the page it is showing.

    DP-168. `QStackedLayout.sizeHint` is the maximum over every page it
    holds, whether or not that page is the one on screen, so a dialog that
    switches between a short variant and a tall one is always as tall as
    the tall one. Hiding the other pages does not help -- a stacked layout
    manages its pages' visibility itself and measures them all regardless
    -- which is why the `hide()` loop and the `adjustSize()` that used to
    stand here moved nothing.

    The fix is to take the pages that are not current out of the
    measurement, by ignoring their size hint. Only the vertical policy is
    touched: the width stays the widest page's, so switching variants
    changes the dialog's height and never jogs its width.
    """
    current = stack.currentWidget()
    for index in range(stack.count()):
        page = stack.widget(index)
        policy = page.sizePolicy()
        parked = page.property(_PARKED_VERTICAL)
        if parked is None:
            parked = policy.verticalPolicy()
            page.setProperty(_PARKED_VERTICAL, parked)
        policy.setVerticalPolicy(
            parked if page is current else QSizePolicy.Policy.Ignored)
        page.setSizePolicy(policy)
    # The stack's own layout notices the change, but the layout ABOVE the stack
    # caches a size hint per child widget and is never told. Without this the
    # parent goes on reserving room for the tallest page and the dialog does not
    # move at all.
    stack.updateGeometry()


# --------------------------------------------------------------------------- #
# Margins
#
# DP-195. DP-194 wrote down the gap between two things. This writes down the
# distance from a thing to the edge it sits in, which the forms had been
# answering three different ways at once.
#
# MEASURED over the 263 layouts in the `.ui` files. 146 of them write no
# margin at all; 58 write some sides and leave the rest; 59 write all four.
# A side nobody writes is not zero -- it is whatever the style says, and
# every style available here (windows11, windowsvista, Fusion, Windows)
# says 9 on all four sides. So the product's most common margin was a number
# nobody chose, and the house constant `FORM_MARGIN` above, which says 8,
# reached only the forms the field registry builds.
#
# The proof that this was confusing rather than merely untidy: **9 is typed
# by hand 22 times**, in seven forms, to match the default the same form
# inherits three lines further down. Somebody measured the screen, read 9,
# and wrote 9, because there was no way to say "the same as the others".
#
# `HouseStyle` answers the six layout metrics from the scale instead: the
# four margins give `FORM_MARGIN` and the two spacings give `GAP`, so a
# layout that names nothing gets the house numbers rather than Qt's. It
# keeps the base style's `objectName`, so `application.style().objectName()`
# still reads `windows11` and anything that saves and restores the style by
# name is unaffected.
# --------------------------------------------------------------------------- #

#: A margin of nothing: this layout is flush inside its parent.
MARGIN_NONE = 0
#: A tight margin, for a row dense enough that a full one would crowd it.
#: The same distance as `BAR_INSET_V`, doing the same job.
MARGIN_TIGHT = CONTROL_PADDING
#: A wide margin, for something deliberately indented from what is around it.
MARGIN_WIDE = FORM_MARGIN * 2

_HOUSE_METRICS = {
    QStyle.PixelMetric.PM_LayoutLeftMargin: FORM_MARGIN,
    QStyle.PixelMetric.PM_LayoutTopMargin: FORM_MARGIN,
    QStyle.PixelMetric.PM_LayoutRightMargin: FORM_MARGIN,
    QStyle.PixelMetric.PM_LayoutBottomMargin: FORM_MARGIN,
    QStyle.PixelMetric.PM_LayoutHorizontalSpacing: GAP,
    QStyle.PixelMetric.PM_LayoutVerticalSpacing: GAP,
}


class HouseStyle(QProxyStyle):
    """The platform style, answering the six layout metrics from the scale.

    Only those six. Everything else is the platform's, because this exists to
    settle the numbers the house has an opinion about and not to re-implement
    a style.
    """

    def pixelMetric(self, metric, option=None, widget=None):
        value = _HOUSE_METRICS.get(metric)
        if value is not None:
            return value
        return super().pixelMetric(metric, option, widget)


def house_style_in_effect(application) -> bool:
    """Whether the layout metrics a widget will be built against are ours.

    Not `isinstance`: setting an application stylesheet makes Qt wrap the
    style in an internal proxy of its own, so from that moment the object the
    application hands back is no longer the one that was installed. That
    wrapper forwards the metrics to whatever it wraps, so the honest question
    is not which class this is but what it answers.
    """
    style = application.style()
    if isinstance(style, HouseStyle):
        return True
    return all(style.pixelMetric(metric) == value
               for metric, value in _HOUSE_METRICS.items())


def install_house_style(application) -> bool:
    """Put the application on `HouseStyle`, keeping the style's name.

    Idempotent: re-applying the theme finds the scale already answered and
    does not wrap a second proxy around the first. Returns whether this call
    installed it.
    """
    current = application.style()
    if house_style_in_effect(application):
        return False
    name = current.objectName()
    proxy = HouseStyle(name) if name else HouseStyle()
    proxy.setObjectName(name)
    application.setStyle(proxy)
    return True


# --------------------------------------------------------------------------- #
# Units on a form the Designer built
#
# DP-198. DP-164 took the unit out of the row label everywhere the page builds
# its own rows. Sixteen fields come out of a `.ui` instead, and every one of
# them still said it in brackets `Rotation angle (deg)`,
# `Point inside the region (m)`, `Max face non-orthogonality (deg)`. The
# label is the wrong place for the same reason it was everywhere else: the
# unit belongs to the number, not to the name of the field, so it has to sit
# where the number ends, in a column of its own that the boxes can be lined
# up against.
# --------------------------------------------------------------------------- #

def _layout_holding(editor):
    """The layout `editor` is an item of, however deep it is nested."""
    parent = editor.parentWidget()
    while parent is not None:
        layout = parent.layout()
        if layout is not None:
            found = _search_layout(layout, editor)
            if found is not None:
                return found
        parent = parent.parentWidget()
    return None


def _search_layout(layout, editor):
    if layout.indexOf(editor) >= 0:
        return layout
    for index in range(layout.count()):
        child = layout.itemAt(index).layout()
        if child is not None:
            found = _search_layout(child, editor)
            if found is not None:
                return found
    return None


def place_unit(editor, unit: str):
    """Give a form-built editor the unit column `unit_cell` gives a fresh one.

    DP-198. `unit_cell` wraps an editor before its row is added, which is no
    use to a field that a `.ui` has already placed. This puts the unit in the
    same place without rebuilding the form Designer produced, for the three
    layout kinds those forms use: a `QFormLayout` field becomes the editor and
    the unit as one cell, a `QGridLayout` cell gets the unit in the column
    after the editor, and a box layout gets it immediately after.

    Returns the `UnitLabel`, so a caller can hand it to `align_unit_column`.
    """
    from PySide6.QtWidgets import QBoxLayout, QFormLayout, QGridLayout

    layout = _layout_holding(editor)
    if layout is None:
        raise ValueError(
            'no layout holds ' + (editor.objectName() or repr(editor)))
    label = UnitLabel(unit or '')
    if isinstance(layout, QFormLayout):
        row, role = layout.getWidgetPosition(editor)
        layout.removeWidget(editor)
        cell = QWidget(layout.parentWidget())
        box = QHBoxLayout(cell)
        box.setContentsMargins(
            MARGIN_NONE, MARGIN_NONE, MARGIN_NONE, MARGIN_NONE)
        box.addWidget(editor, 1)
        box.addWidget(label)
        # The row label is buddied to the cell, so the mnemonic has to land
        # on the editor rather than on the box that holds it.
        cell.setFocusProxy(editor)
        layout.setWidget(row, role, cell)
    elif isinstance(layout, QGridLayout):
        row, column, rowSpan, columnSpan = layout.getItemPosition(
            layout.indexOf(editor))
        layout.addWidget(label, row, column + columnSpan, rowSpan, 1)
    elif isinstance(layout, QBoxLayout):
        layout.insertWidget(layout.indexOf(editor) + 1, label)
    else:
        raise TypeError(
            'no rule for putting a unit beside an editor in a '
            + type(layout).__name__)
    return label


# --------------------------------------------------------------------------- #
# DP-220. One measure for every paragraph the product paints.
#
# A paragraph that wraps is running text, and running text needs a measure:
# a line long enough not to chop the sentence up, short enough that the eye
# drops onto the start of the next line instead of hunting for it.  Without
# one, a page whose only content is a paragraph lets that paragraph grow to
# whatever width the pane happens to be, and on a wide screen the reader is
# handed a single line a hundred and ten characters long.
#
# The measure is stated in characters rather than pixels, because characters
# are what a reader reads.  Eighty is the wide end of the usual range, which
# suits a desktop pane better than the book-typography sixty-six.  The
# pixel width is measured from the font in hand, so it follows the theme.
PROSE_MEASURE_CHARACTERS = 80
PROSE_MEASURE_SAMPLE = (
    'the quick brown fox jumps over a lazy dog and then it walks '
    'slowly back home now')


def prose_measure(font):
    """The pixel width of one comfortable line of running text."""
    return int(round(QFontMetricsF(font).horizontalAdvance(
        PROSE_MEASURE_SAMPLE)))


def apply_prose_measure(label):
    """Hold a wrapping label to one comfortable line of running text."""
    label.setMaximumWidth(prose_measure(label.font()))
    return label
