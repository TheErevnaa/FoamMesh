#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One layout for everything drawn over the picture (Plan 39 VI15, I4).

The scalar bars (deviation, quality, a section's colour, later the size
field's), the section label, the progress strip, the hover readout and the
resource panel each used to place themselves, so two legends could land on the
same corner and a label could sit on the mesh. This module owns the places:

* ``TOP``          a strip across the whole width (the progress strip);
* ``TOP_LEFT``     notices, stacked downwards;
* ``TOP_RIGHT``    the resource / overlay panel, stacked downwards, left of
                   the tool rail;
* ``BOTTOM_LEFT``  legends, stacked upwards, above the orientation corner;
* ``BOTTOM_RIGHT`` section labels, stacked upwards.

It is geometry only: an item registers a slot and a wanted size, and gets a
rectangle in pixels back (origin top-left). What the rectangle is applied to --
a Qt widget, a VTK scalar bar -- is the item's ``apply`` callback. Because the
rectangles come from one function, "no two overlap" is a property of that
function and is tested on it, with any number of legends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from foammesh.view.theming.metrics import FORM_MARGIN

#: Pixels between two stacked items, and between an item and the edge.
#: One distance, the house edge distance: a stack reads as evenly spaced
#: from its corner. DP-194 writes the gap scale in metrics.py alone, so
#: this is named for what it spaces rather than redefining `GAP`.
EDGE = FORM_MARGIN
STACK_GAP = EDGE


class Slot(Enum):
    TOP = 'top'
    TOP_LEFT = 'top-left'
    TOP_RIGHT = 'top-right'
    BOTTOM_LEFT = 'bottom-left'
    BOTTOM_RIGHT = 'bottom-right'


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    def intersects(self, other: 'Rect') -> bool:
        return (self.x < other.right and other.x < self.right
                and self.y < other.bottom and other.y < self.bottom)


@dataclass
class _Item:
    key: str
    slot: Slot
    size: object
    apply: Optional[Callable[[Rect], None]] = None
    order: int = 0

    def wanted(self) -> tuple:
        """The size now: a stored pair, or whatever the item answers."""
        size = self.size() if callable(self.size) else self.size
        return int(size[0]), int(size[1])


@dataclass
class OverlayLayout:
    """Slots, the items registered in them, and the rectangles they get."""

    #: Pixels kept free along the left edge's bottom (the orientation corner)
    #: and along the right edge (the tool rail).
    corner: int = 0
    right_inset: int = 0
    top_inset: int = 0
    #: Plan 44 MF7: the clear rectangle and the inset from its edges.
    bounds: Optional[tuple] = None
    inset: int = EDGE
    _items: dict = field(default_factory=dict)
    _counter: int = 0

    # -- registering -------------------------------------------------------- #

    def register(self, key: str, slot: Slot, size=(200, 160),
                 apply: Optional[Callable[[Rect], None]] = None) -> None:
        """Put *key* in *slot*. Registering again replaces it in place."""
        previous = self._items.get(key)
        order = previous.order if previous is not None else self._counter
        if previous is None:
            self._counter += 1
        self._items[key] = _Item(key, Slot(slot), size, apply, order)

    def unregister(self, key: str) -> bool:
        return self._items.pop(key, None) is not None

    def keys(self, slot: Optional[Slot] = None) -> list[str]:
        items = sorted(self._items.values(), key=lambda item: item.order)
        return [item.key for item in items
                if slot is None or item.slot == Slot(slot)]

    def setCorner(self, pixels: int) -> None:
        self.corner = max(0, int(pixels))

    def setRightInset(self, pixels: int) -> None:
        self.right_inset = max(0, int(pixels))

    def setTopInset(self, pixels: int) -> None:
        self.top_inset = max(0, int(pixels))

    def setBounds(self, bounds, inset: Optional[int] = None) -> None:
        """Plan 44 MF7. Lay every slot out inside *bounds* (``(x0, y0, x1,
        y1)``, y down: the clear rectangle) at *inset* pixels from its edges.
        ``None`` goes back to the whole view at the house edge distance."""
        self.bounds = None if bounds is None else tuple(
            int(round(v)) for v in bounds)
        self.inset = EDGE if inset is None else max(0, int(inset))

    # -- geometry ----------------------------------------------------------- #

    def _box(self, width: int, height: int) -> tuple:
        """The rectangle the slots live in: the clear rectangle clipped to
        the view."""
        x0, y0, x1, y1 = self.bounds or (0, 0, width, height)
        x0, y0 = min(max(x0, 0), width), min(max(y0, 0), height)
        x1 = min(max(x1, x0), width)
        y1 = min(max(y1, y0), height)
        return x0, y0, max(x1, x0), y1

    def rects(self, width: int, height: int) -> dict:
        """Every registered item's rectangle for a ``width`` x ``height`` view."""
        width, height = max(0, int(width)), max(0, int(height))
        x0, y0, x1, y1 = self._box(width, height)
        inset = self.inset
        out: dict = {}
        out.update(self._top(x0, y0, x1))
        # The tool rail is on the view's right edge: a slot clears it, unless
        # a sheet on the right already stands further in.
        x1 = min(x1, max(width - self.right_inset, x0))
        top = self.top_inset + max((r.bottom for r in out.values()),
                                   default=y0)
        out.update(self._stack_down(Slot.TOP_LEFT, x0 + inset, top, x0, x1,
                                    y1, left=True))
        out.update(self._stack_down(Slot.TOP_RIGHT, x1 - inset, top, x0, x1,
                                    y1, left=False))
        out.update(self._stack_up(Slot.BOTTOM_LEFT, x0 + inset,
                                  y1 - inset - self.corner, x0, x1, y0,
                                  left=True))
        out.update(self._stack_up(Slot.BOTTOM_RIGHT, x1 - inset, y1 - inset,
                                  x0, x1, y0, left=False))
        return out

    def arrange(self, width: int, height: int) -> dict:
        """Compute the rectangles and hand each to its item."""
        rects = self.rects(width, height)
        for key, rect in rects.items():
            apply = self._items[key].apply
            if apply is not None:
                apply(rect)
        return rects

    def _in(self, slot: Slot) -> list:
        return sorted((item for item in self._items.values()
                       if item.slot == slot), key=lambda item: item.order)

    def _top(self, x0: int, y0: int, x1: int) -> dict:
        out, y = {}, y0
        for item in self._in(Slot.TOP):
            out[item.key] = Rect(x0, y, x1 - x0, item.wanted()[1])
            y += item.wanted()[1]
        return out

    def _stack_down(self, slot, edge_x, top, x0, x1, y1, *, left):
        out, y = {}, top + self.inset
        for item in self._in(slot):
            w = min(item.wanted()[0], max(0, x1 - x0 - 2 * self.inset))
            h = min(item.wanted()[1], max(0, y1 - y - self.inset))
            x = edge_x if left else edge_x - w
            out[item.key] = Rect(x, y, w, h)
            y += h + STACK_GAP
        return out

    def _stack_up(self, slot, edge_x, bottom, x0, x1, y0, *, left):
        items = self._in(slot)
        out = {}
        if not items:
            return out
        room = max(0, bottom - self.inset - self.top_inset - y0)
        wanted = [item.wanted()[1] for item in items]
        gaps = STACK_GAP * (len(items) - 1)
        if sum(wanted) + gaps > room:
            # Share what is there: never overlap. A window too short for
            # that many legends makes each of them shorter, not stacked.
            each = max(0, (room - gaps) // len(items))
            wanted = [min(h, each) for h in wanted]
        y = bottom
        for item, h in zip(items, wanted):
            w = min(item.wanted()[0], max(0, x1 - x0 - 2 * self.inset))
            x = edge_x if left else edge_x - w
            y -= h
            out[item.key] = Rect(x, max(y, y0), w, h)
            y -= STACK_GAP
        return out


def apply_to_scalar_bar(bar, rect: Rect, width: int, height: int) -> None:
    """Place a ``vtkScalarBarActor`` on *rect* (pixels, origin top-left).

    A scalar bar is placed in normalised viewport coordinates with the origin
    at the bottom left, so the rectangle is flipped and divided by the view.
    """
    if width <= 0 or height <= 0:
        return
    bar.SetPosition(rect.x / width, (height - rect.bottom) / height)
    bar.SetWidth(rect.w / width)
    bar.SetHeight(rect.h / height)
