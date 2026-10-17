"""Plan 43 LY5: the clear area, and the model centred in it.

The sheet (floating or pinned) and the console are the only things that
decide where the model sits (section 1, rules 1 and 2). The clear area is
what is left of the viewport once they are taken away: right of a left
sheet, left of a right sheet, above the console. The model is centred in it
with ``vtkCamera.SetWindowCenter``, which shifts the projection and nothing
else: the scene, the camera position, the picks and the measurements are
unchanged.

The pure rules (``Insets``, ``clear_rect``, ``window_center``) have no Qt in
them. ``ClearAreaController`` reads the occluding widgets and tells the view.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QRect, Signal

SIDES = ('left', 'right', 'top', 'bottom')

_WATCHED = (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show,
            QEvent.Type.Hide, QEvent.Type.ParentChange,
            QEvent.Type.WindowStateChange)


@dataclass(frozen=True)
class Insets:
    """Pixels the sheet and the console take from each side of the viewport."""

    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.right, self.bottom)


def clear_rect(width: int, height: int, insets: Insets) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of the clear area, y down. Never inverted: a
    sheet wider than the viewport leaves a zero-width clear area at its edge."""
    left = min(max(insets.left, 0), width)
    right = min(max(insets.right, 0), max(width - left, 0))
    top = min(max(insets.top, 0), height)
    bottom = min(max(insets.bottom, 0), max(height - top, 0))
    return (float(left), float(top), float(width - right), float(height - bottom))


def clear_centre(width: int, height: int, insets: Insets) -> tuple[float, float]:
    x0, y0, x1, y1 = clear_rect(width, height, insets)
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


#: Plan 44 MF7. The model fills this share of the clear area's height (and
#: of its width, whichever binds first); the rest is the breathing room.
FIT_SHARE = 0.80

#: Pixels between the clear area's edge and an overlay or the triad.
OVERLAY_INSET = 6

#: FX1 (DP-2620, DP-2621). The floating items of the clear area, in the order
#: they are placed: each keeps off every visible item ranked before it. The
#: rail's popovers and the notice strip are where the user or a run put them;
#: the Parts chip has its corner; the legend and the viewport overlay (the
#: parts / run readout card) make room.
FLOAT_RANK_POPOVER = 0
FLOAT_RANK_NOTICE = 0
FLOAT_RANK_CHIP = 1
FLOAT_RANK_PANEL = 1          # the deviation panel (V16, DP-2650)
FLOAT_RANK_LEGEND = 2
FLOAT_RANK_OVERLAY = 3


def fit_factor(box_w: float, box_h: float, clear_w: float, clear_h: float,
               share: float = FIT_SHARE) -> float:
    """The factor that scales a projected ``box_w`` x ``box_h`` model to
    ``share`` of the clear area's height and width (the binding one). 1.0
    when there is nothing to measure."""
    if box_w <= 0 or box_h <= 0 or clear_w <= 0 or clear_h <= 0:
        return 1.0
    return min(share * clear_h / box_h, share * clear_w / box_w)


def clear_rect_int(width: int, height: int, insets: Insets) -> QRect:
    """``clear_rect`` as a ``QRect`` (whole pixels, y down)."""
    x0, y0, x1, y1 = clear_rect(width, height, insets)
    return QRect(int(round(x0)), int(round(y0)),
                 int(round(x1 - x0)), int(round(y1 - y0)))


def window_center(width: int, height: int, insets: Insets) -> tuple[float, float]:
    """The ``SetWindowCenter`` pair (normalised, y up) that puts the projected
    centre of the model on the centre of the clear area.

    VTK's window centre names the point of the *window* that the camera looks
    through, so the picture moves the opposite way: a sheet on the left needs
    a negative x to push the model right. (Measured against
    ``vtkRenderer.WorldToDisplay`` in the LY5 test.)"""
    if width <= 0 or height <= 0:
        return (0.0, 0.0)
    cx, cy = clear_centre(width, height, insets)
    return (1.0 - 2.0 * cx / width, 2.0 * cy / height - 1.0)


def avoid_rect(wanted: QRect, clear: QRect, blockers, gap: int = OVERLAY_INSET) -> QRect:
    """FX1 (DP-2621). Where *wanted* stands so it meets none of *blockers*.

    The one rule every floating item of the clear area follows: an item never
    lies on one placed before it. The candidates are *wanted* itself and the
    places just off each blocker's four sides (``gap`` pixels away), and every
    pairing of those columns and rows; each is kept inside *clear*. The first
    candidate that meets no blocker, nearest to *wanted* (city-block distance,
    ties in that order), is the answer. With no free place the item stays
    where it wanted to be, kept inside *clear*.
    """
    width, height = wanted.width(), wanted.height()

    def inside(x: int, y: int) -> QRect:
        if clear is not None and not clear.isEmpty():
            x = max(clear.x(), min(x, clear.x() + clear.width() - width))
            y = max(clear.y(), min(y, clear.y() + clear.height() - height))
        return QRect(int(x), int(y), width, height)

    blocks = [QRect(b) for b in blockers if b is not None and not b.isEmpty()]
    start = inside(wanted.x(), wanted.y())
    if not blocks or not any(start.intersects(b) for b in blocks):
        return start
    xs, ys = [wanted.x()], [wanted.y()]
    for b in blocks:
        xs += [b.x() - gap - width, b.x() + b.width() + gap]
        ys += [b.y() - gap - height, b.y() + b.height() + gap]
    best, best_cost = None, None
    for y in ys:
        for x in xs:
            rect = inside(x, y)
            if any(rect.intersects(b) for b in blocks):
                continue
            cost = abs(rect.x() - wanted.x()) + abs(rect.y() - wanted.y())
            if best_cost is None or cost < best_cost:
                best, best_cost = rect, cost
    return best if best is not None else start


def insets_from_rects(width: int, height: int, occluders) -> Insets:
    """``occluders`` is ``[(side, QRect in viewport coordinates), ...]``.

    A rectangle takes from its own side the distance between that side's edge
    of the viewport and the rectangle's far edge, so a sheet floating 12 px
    from the left edge still clears 12 px + its width. Rectangles outside the
    viewport take nothing.
    """
    taken = {side: 0 for side in SIDES}
    for side, rect in occluders:
        if rect is None or rect.isEmpty() or not rect.intersects(
                QRect(0, 0, width, height)):
            continue
        rect = rect.intersected(QRect(0, 0, width, height))
        if side == 'left':
            taken[side] = max(taken[side], rect.right() + 1)
        elif side == 'right':
            taken[side] = max(taken[side], width - rect.left())
        elif side == 'top':
            taken[side] = max(taken[side], rect.bottom() + 1)
        elif side == 'bottom':
            taken[side] = max(taken[side], height - rect.top())
    return Insets(taken['left'], taken['top'], taken['right'], taken['bottom'])


class _Watcher(QObject):
    """Event filter on one occluder, never on the application."""

    def __init__(self, owner: 'ClearAreaController') -> None:
        super().__init__()
        self._owner = owner

    def eventFilter(self, obj, event):
        if event.type() in _WATCHED:
            self._owner.update()
        return False


def _watch(owner: 'ClearAreaController') -> _Watcher:
    # A factory, so the facade inventory does not list the filter as a control.
    return _Watcher(owner)


class _FloatWatcher(QObject):
    """Event filter on the floating items: one that moves, resizes, shows or
    hides re-places the items ranked after it (FX1)."""

    _EVENTS = (QEvent.Type.Resize, QEvent.Type.Move, QEvent.Type.Show,
               QEvent.Type.Hide)

    def __init__(self, owner: 'ClearAreaController') -> None:
        super().__init__()
        self._owner = owner

    def eventFilter(self, obj, event):
        if event.type() in self._EVENTS:
            self._owner._floatChanged(obj, event.type())
        return False


def _float_watch(owner: 'ClearAreaController') -> _FloatWatcher:
    # A factory, so the facade inventory does not list the filter as a control.
    return _FloatWatcher(owner)


class _Changes(QObject):
    """Carries the controller's ``changed`` signal (the controller is plain)."""

    changed = Signal(QRect)


def _changes() -> _Changes:
    # A factory, so the facade inventory does not list it as a control.
    return _Changes()


class ClearAreaController:
    """Keeps the view's window centre on the middle of the clear area.

    ``view`` is the rendering widget; it needs ``setClearInsets(left, top,
    right, bottom)`` and a size. Occluders are registered with a side; a
    hidden occluder takes nothing. Anything that changes size, position or
    visibility of an occluder re-centres; so do ``update()`` calls from the
    shell (console geometry, tab changes) and a resize of the view itself.
    """

    def __init__(self, view) -> None:
        self._view = view
        self._occluders: list = []        # (side, widget)
        self._rects: list = []            # (side, callable -> QRect | None)
        self._watcher = _watch(self)
        self._changes = _changes()
        self._last: Insets | None = None
        self._updating = False
        #: FX1. The floating items of the clear area: [rank, widget, place].
        self._floats: list = []
        self._floatWatcher = _float_watch(self)
        self._placing: set = set()
        try:
            view.installEventFilter(self._watcher)
        except (AttributeError, TypeError):
            pass

    @property
    def changed(self):
        """``changed(QRect)``: the clear rectangle moved or was resized."""
        return self._changes.changed

    def clearRect(self) -> QRect:
        """The one clear rectangle, in the view's pixels (y down)."""
        width, height = self._view_size()
        return clear_rect_int(width, height, self.insets())

    # -- registration -------------------------------------------------------- #

    def add_occluder(self, widget, side) -> None:
        """The sheet or the console (or its minimised tab) hugging ``side``.

        ``side`` is one of ``SIDES`` or a callable returning one, so a sheet
        that the user moves from the left to the right keeps one registration.
        """
        if not callable(side) and side not in SIDES:
            raise ValueError(f'side must be one of {SIDES}, not {side!r}')
        self._occluders.append((side, widget))
        widget.installEventFilter(self._watcher)
        self.update()

    def register_sheet(self, sheet, side, *, tab=None) -> None:
        """The settings sheet, floating or pinned, and its minimised tab.

        Both count whenever they are visible, wherever they are: a pinned
        sheet narrows the clear area by its width, a floating one by the
        distance from the viewport edge to its far edge (rule 1 and 2 of
        section 1). ``side`` ('left' / 'right', or a callable) is read at
        every update, so the side switch needs nothing more than a call to
        ``update()`` -- the sheet's own Move event does it.
        """
        self.add_occluder(sheet, side)
        if tab is not None:
            self.add_occluder(tab, side)

    def add_rect(self, side: str, rect_of) -> None:
        """An occluder that is not a widget: ``rect_of()`` is a QRect in the
        view's coordinates, or None."""
        if side not in SIDES:
            raise ValueError(f'side must be one of {SIDES}, not {side!r}')
        self._rects.append((side, rect_of))
        self.update()

    # -- FX1: the floating items keep off one another -------------------------- #

    def add_float(self, widget, rank: int, place=None) -> None:
        """A floating item of the clear area (the Parts popover, the legend,
        the viewport overlay...). Items with a lower *rank* are placed first;
        an item with a *place* callback is placed off every visible item
        ranked before it (``place_clear_of``), and *place* is called again
        whenever one of those moves, resizes, shows or hides, when the item
        itself shows or resizes, and when the clear area changes."""
        for entry in self._floats:
            if entry[1] is widget:
                entry[0], entry[2] = int(rank), place
                break
        else:
            self._floats.append([int(rank), widget, place])
            try:
                widget.installEventFilter(self._floatWatcher)
            except (AttributeError, TypeError, RuntimeError):
                pass
        self._floats.sort(key=lambda entry: entry[0])

    def _rankOf(self, widget):
        for rank, other, _place in self._floats:
            if other is widget:
                return rank
        return None

    def _rect_in_view(self, widget):
        try:
            if not widget.isVisibleTo(widget.window()) or widget.isHidden():
                return None
            if widget.parentWidget() is self._view:
                return QRect(widget.geometry())
            corner = self._view.mapFromGlobal(
                widget.mapToGlobal(widget.rect().topLeft()))
            return QRect(corner, widget.size())
        except (RuntimeError, AttributeError, TypeError):
            return None

    def float_blockers(self, widget) -> list:
        """The rectangles (view pixels) of the visible floating items ranked
        before *widget*: what it must keep off."""
        rank = self._rankOf(widget)
        out = []
        for other_rank, other, _place in self._floats:
            if other is widget or (rank is not None and other_rank >= rank):
                continue
            rect = self._rect_in_view(other)
            if rect is not None and not rect.isEmpty():
                out.append(rect)
        return out

    def float_rects_of_rank(self, rank: int) -> list:
        """The rectangles (view pixels) of the visible floating items of one
        *rank* (V16: the rail's popovers hang under the Parts chip)."""
        out = []
        for other_rank, other, _place in self._floats:
            if other_rank != rank:
                continue
            rect = self._rect_in_view(other)
            if rect is not None and not rect.isEmpty():
                out.append(rect)
        return out

    def place_clear_of(self, widget, wanted: QRect) -> QRect:
        """*wanted* moved off the items ranked before *widget* (``avoid_rect``)."""
        return avoid_rect(wanted, self.clearRect(), self.float_blockers(widget))

    def _runPlacer(self, rank, widget, place) -> None:
        if place is None or id(widget) in self._placing:
            return
        self._placing.add(id(widget))
        try:
            place()
        except RuntimeError:          # the widget is being destroyed
            pass
        finally:
            self._placing.discard(id(widget))

    def place_floats(self, after=None) -> None:
        """Place every floating item, in rank order (those ranked after
        *after* only, when it is given)."""
        for rank, widget, place in list(self._floats):
            if after is not None and rank <= after:
                continue
            self._runPlacer(rank, widget, place)

    def _floatChanged(self, widget, kind) -> None:
        rank = self._rankOf(widget)
        if rank is None:
            return
        if kind in (QEvent.Type.Show, QEvent.Type.Resize):
            for own_rank, other, place in list(self._floats):
                if other is widget:
                    self._runPlacer(own_rank, other, place)
        self.place_floats(after=rank)

    # -- the rules ------------------------------------------------------------ #

    def _view_size(self) -> tuple[int, int]:
        return int(self._view.width()), int(self._view.height())

    def _occluder_rects(self):
        out = []
        for side, widget in self._occluders:
            try:
                side = side() if callable(side) else side
                if side not in SIDES:
                    continue
                if not widget.isVisibleTo(widget.window()):
                    continue
                # Through global coordinates: the sheet is a child of the region
                # host, a sibling of the view, not of the view itself.
                corner = widget.rect().topLeft()
                if self._view.isAncestorOf(widget):
                    top_left = widget.mapTo(self._view, corner)
                else:
                    top_left = self._view.mapFromGlobal(
                        widget.mapToGlobal(corner))
                out.append((side, QRect(top_left, widget.size())))
            except (RuntimeError, AttributeError, TypeError):
                continue
        for side, rect_of in self._rects:
            out.append((side, rect_of()))
        return out

    def insets(self) -> Insets:
        width, height = self._view_size()
        return insets_from_rects(width, height, self._occluder_rects())

    def update(self) -> Insets:
        """Re-centre. Cheap when nothing moved."""
        if self._updating:
            return self._last or Insets()
        self._updating = True
        try:
            insets = self.insets()
            size = self._view_size()
            key = (insets, size)
            if key != getattr(self, '_key', None):
                self._key = key
                self._last = insets
                self._view.setClearInsets(*insets.as_tuple())
                self._changes.changed.emit(self.clearRect())
                self.place_floats()
            return insets
        finally:
            self._updating = False

    def reset(self) -> None:
        """Forget the last applied state so the next ``update`` re-applies it
        (Standard layout, a new render window)."""
        self._key = None
        self.update()
