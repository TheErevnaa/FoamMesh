"""The worst values written on the model, and the statistics caption.

Plan 43 LY10 (section 3). Each label is a 2-D text actor with a leader to its
anchor on the model. Both are drawn over the scene in the overlay pass (as
DP-925 did for the region labels): a worst cell is usually inside the surface,
so a depth-tested label would be hidden by the very wall that wraps it. The
layout is redone at the start of every render, so the boxes follow the camera
and never overlap.

Plan 44 MF16 (``final-layout.html`` M:323-331, 357, 808-844): a label is two
text actors (a bold value and a dim suffix, placed with ``GetBoundingBox``
because one ``vtkTextActor`` has one style), a 7 px anchor dot with a dark
ring, a 14 px leader and a side flip. The selected tag is white with a white
border and a dashed ring of generated segments (line stipple is unreliable on
core-profile GL). The layout is bounded by the clear rectangle and the
caption is a framed two-line block above the console.

The actors are VTK actors, not Qt overlays, so a snapshot grabbed with
magnification draws their text at the picture's size (Plan 43 section 5).
There is no cap on the number of labels.
"""
from __future__ import annotations

import math

from foammesh.core.quality import worst_values as wv

FONT_SIZE = 13
#: Small, Normal, Large, Extra large (Plan 43 section 5).
SIZE_FACTORS = (0.88, 1.0, 1.25, 1.5)

_FALLBACKS = {
    'value': '#ffffff', 'suffix': '#aab0b8', 'fill': '#0d0f13',
    'ring': '#0d0f13', 'frame': '#ffffff', 'caption': '#d9dde3',
    'caption.dim': '#9aa2ae', 'caption.frame': '#363b44',
}
_LABEL_OPACITY = 0.88           # #0d0f13e0
_FRAME_OPACITY = 0.69           # #ffffffb0
_CAPTION_OPACITY = 0.82         # #0d0f13d0
_DOT_RADIUS = 3.5
_DOT_RING = 1.5
_GAP = 16.0                     # dot centre to the box edge (14 px leader)
_RING_RADIUS = 11.0
_CAPTION_LEFT = 84.0
_CAPTION_ABOVE = 22.0
_CAPTION_RADIUS = 4.0
_PAD_X, _PAD_Y = 6.0, 1.0
_SHAPE_KEYS = ('fill', 'frame', 'selframe', 'leader', 'ring', 'dotring',
               'dot')


def _hex6(value, fallback: str) -> str:
    text = str(value or '')
    if len(text) >= 7 and text[0] == '#':
        try:
            int(text[1:7], 16)
            return text[:7]
        except ValueError:
            pass
    return fallback


def _colours() -> dict:
    """Theme colours, each with a safe fallback (MF0's tokens may be absent)."""
    from foammesh.rendering.region_labels import _token
    from foammesh.view.theming.vtk_theme import rgb

    def pick(name, key):
        return rgb(_hex6(_token(name, _FALLBACKS[key]), _FALLBACKS[key]))

    return {
        'value': rgb(_FALLBACKS['value']),
        'suffix': rgb(_FALLBACKS['suffix']),
        'fill': rgb(_FALLBACKS['fill']),
        'ring': rgb(_FALLBACKS['ring']),
        'frame': rgb(_FALLBACKS['frame']),
        'caption': pick('foreground.primary', 'caption'),
        'caption.dim': rgb(_FALLBACKS['caption.dim']),
        'caption.fill': pick('overlay.scrim', 'fill'),
        'caption.frame': pick('border.legend', 'caption.frame'),
    }


def rounded_rect(x0, y0, x1, y1, radius, steps=4) -> list:
    """Points of a rounded rectangle, clockwise from the top-right corner."""
    r = max(min(radius, (x1 - x0) / 2.0, (y1 - y0) / 2.0), 0.0)
    if r <= 0.0:
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    pts = []
    for cx, cy, a0 in ((x1 - r, y0 + r, -90.0), (x1 - r, y1 - r, 0.0),
                       (x0 + r, y1 - r, 90.0), (x0 + r, y0 + r, 180.0)):
        for k in range(steps + 1):
            a = math.radians(a0 + 90.0 * k / steps)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def disc(cx, cy, radius, steps=12) -> list:
    return [(cx + radius * math.cos(2 * math.pi * k / steps),
             cy + radius * math.sin(2 * math.pi * k / steps))
            for k in range(steps)]


def dash_segments(cx, cy, radius, dash=3.0, gap=2.0, arc_steps=3) -> list:
    """Polylines of a dashed circle: *dash* px drawn, *gap* px skipped,
    measured along the circumference."""
    period = dash + gap
    count = max(int(round(2.0 * math.pi * radius / period)), 3)
    out = []
    for k in range(count):
        a0 = 2.0 * math.pi * k / count
        span = 2.0 * math.pi * (dash / period) / count
        out.append([(cx + radius * math.cos(a0 + span * j / arc_steps),
                     cy + radius * math.sin(a0 + span * j / arc_steps))
                    for j in range(arc_steps + 1)])
    return out


def _is_light() -> bool:
    try:
        from foammesh.app import app
        return str(app.themeManager.resolved_name) == 'light'
    except Exception:                                         # noqa: BLE001
        return False


def outline_colour() -> tuple:
    """The beyond-limit outline: ``foreground.strong`` (white in dark), a dark
    slate in light when the token is absent."""
    from foammesh.rendering.region_labels import _token
    from foammesh.view.theming.vtk_theme import rgb
    fallback = '#1c2430' if _is_light() else '#ffffff'
    return rgb(_hex6(_token('foreground.strong', fallback), fallback))


OUTLINE_WIDTH = 1.6


class _Shapes:
    """One 2-D polydata actor accumulating polygons or polylines."""

    def __init__(self, name, colour, opacity, *, lines, width=1.0) -> None:
        from vtkmodules.vtkCommonCore import vtkPoints
        from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData
        from vtkmodules.vtkRenderingCore import vtkActor2D, vtkPolyDataMapper2D
        self.data = vtkPolyData()
        self.points = vtkPoints()
        self.cells = vtkCellArray()
        self.data.SetPoints(self.points)
        if lines:
            self.data.SetLines(self.cells)
        else:
            self.data.SetPolys(self.cells)
        mapper = vtkPolyDataMapper2D()
        mapper.SetInputData(self.data)
        self.actor = vtkActor2D()
        self.actor.SetMapper(mapper)
        self.actor.SetObjectName(name)
        prop = self.actor.GetProperty()
        prop.SetColor(*colour)
        prop.SetOpacity(opacity)
        prop.SetLineWidth(width)
        self.actor.PickableOff()

    def reset(self) -> None:
        self.points.Reset()
        self.cells.Reset()

    def add(self, pts, closed=False) -> None:
        ids = [self.points.InsertNextPoint(x, y, 0.0) for x, y in pts]
        if closed:
            ids.append(ids[0])
        self.cells.InsertNextCell(len(ids))
        for i in ids:
            self.cells.InsertCellPoint(i)

    def done(self) -> None:
        self.data.Modified()


def _measure(actor, renderer) -> tuple:
    """``(width, height)`` of a text actor in display pixels, or (0, 0).

    ``vtkTextRenderer.GetBoundingBox`` measures the same string with the same
    text property without a GL context (``vtkTextActor.GetBoundingBox`` needs
    a live render window and crashes without one)."""
    try:
        import vtkmodules.vtkRenderingFreeType  # noqa: F401  (registers it)
        from vtkmodules.vtkRenderingCore import vtkTextRenderer
        dpi = 72
        try:
            dpi = int(renderer.GetRenderWindow().GetDPI()) or 72
        except Exception:                                     # noqa: BLE001
            pass
        box = [0, 0, 0, 0]
        text = actor.GetInput() or ''
        if not text:
            return (0.0, 0.0)
        if not vtkTextRenderer.GetInstance().GetBoundingBox(
                actor.GetTextProperty(), text, box, dpi):
            return (0.0, 0.0)
        return (float(max(box[1] - box[0] + 1, 0)),
                float(max(box[3] - box[2] + 1, 0)))
    except Exception:                                         # noqa: BLE001
        return (0.0, 0.0)


def _text_actor(text, colour, size, *, bold=False, name='', mono=False):
    from vtkmodules.vtkRenderingCore import vtkTextActor
    actor = vtkTextActor()
    actor.SetInput(text)
    if name:
        actor.SetObjectName(name)
    prop = actor.GetTextProperty()
    prop.SetFontSize(size)
    prop.SetColor(*colour)
    prop.SetBold(bool(bold))
    if mono:
        prop.SetFontFamilyToCourier()
    prop.SetJustificationToLeft()
    prop.SetVerticalJustificationToBottom()
    actor.PickableOff()
    return actor


class WorstLabelLayer:
    """Owns the label, leader and caption actors of one renderer."""

    def __init__(self, view) -> None:
        self._view = view
        self._targets: list = []
        self._selected = None
        self._caption: tuple = ()
        self._scale = 1.0
        self._labels: list = []        # [(value actor, suffix actor | None)]
        self._metrics: list = []       # [(value width, suffix width, height)]
        self._shapes: dict = {}
        self._texts: list = []
        self._caption_actors: list = []
        self._observer = None
        self._enabled = True
        self._caption_enabled = True
        self._boxes: list = []
        self._caption_box = None
        self._clear = None             # (x0, y0, x1, y1), y down, or None
        #: Plan 45 V3/V10. ``keep_out()`` -> [(x0, y0, x1, y1)], y down: the
        #: widgets standing over the picture (the overlay card, the bottom
        #: bar, the legend...), which the tags and the caption keep off.
        self._keep_out = None
        self._outline = None           # the beyond-limit edge actor
        #: DP-2770. Groups ('worst', 'caption') held off for one capture.
        self._suppressed: set = set()

    # -- state ---------------------------------------------------------- #

    def set_targets(self, targets) -> None:
        """``[(world xyz, text[, suffix]), ...]``, worst first."""
        out = []
        for item in targets or ():
            suffix = str(item[2]) if len(item) > 2 and item[2] else ''
            out.append((tuple(float(c) for c in item[0]), str(item[1]),
                        suffix))
        self._targets = out
        self._rebuild()

    def set_selected(self, target) -> None:
        """The cell selected in the Mesh quality tab: ``(xyz, text)`` or None."""
        self._selected = None if target is None else (
            tuple(float(c) for c in target[0]), str(target[1]), '')
        self._rebuild()

    def set_caption(self, lines) -> None:
        self._caption = tuple(lines or ())
        self._rebuild()

    def set_enabled(self, labels: bool, caption: bool) -> None:
        self._enabled = bool(labels)
        self._caption_enabled = bool(caption)
        self._rebuild()

    # -- the snapshot's Include boxes (DP-2770) -------------------------- #

    def group_props(self, group: str) -> list:
        """The props of one Include group: 'worst' (the value tags, their
        boxes, leaders and dots) or 'caption' (the statistics caption)."""
        if group == 'caption':
            return list(self._caption_actors) + [
                self._shapes[key].actor for key in ('capfill', 'capframe')
                if key in self._shapes]
        if group == 'worst':
            return [actor for pair in self._labels for actor in pair
                    if actor is not None] + [
                self._shapes[key].actor for key in _SHAPE_KEYS
                if key in self._shapes]
        return []

    def group_shown(self, group: str) -> bool:
        """Whether *group* is drawn: the view's Include switch for it
        (``isLabelGroupShown``), unless a capture holds it off."""
        if group in self._suppressed:
            return False
        shown = getattr(self._view, 'isLabelGroupShown', None)
        if not callable(shown):
            return True
        try:
            return bool(shown(group))
        except Exception:                                     # noqa: BLE001
            return True

    def _apply_groups(self) -> None:
        for group in ('worst', 'caption'):
            on = self.group_shown(group)
            for prop in self.group_props(group):
                if bool(prop.GetVisibility()) != on:
                    prop.SetVisibility(on)

    def suppressed(self, groups):
        """Hold *groups* off for the length of a ``with`` block (a snapshot
        whose Include boxes are unticked), then put them back."""
        import contextlib

        groups = {g for g in groups if g in ('worst', 'caption')}
        if not groups:
            return contextlib.nullcontext()

        @contextlib.contextmanager
        def held():
            before = set(self._suppressed)
            self._suppressed |= groups
            self._apply_groups()
            try:
                yield self
            finally:
                self._suppressed = before
                self._apply_groups()

        return held()

    def set_size_factor(self, factor: float) -> None:
        self._scale = float(factor)
        self._rebuild()

    def set_clear_rect(self, rect) -> None:
        """The clear rectangle ``(x0, y0, x1, y1)`` in render pixels, y down
        (``clear_area.clear_rect``). ``None`` falls back to the view's
        ``clearRect()`` accessor if it has one, else the whole renderer."""
        self._clear = None if rect is None else tuple(float(v) for v in rect)
        if self._shapes:
            self.relayout()
            self._refresh()

    def set_keep_out(self, provider) -> None:
        """Plan 45 V3/V10. ``provider()`` returns the rectangles, in render
        pixels with y down, that the widgets over the view cover; the tags
        and the caption are placed off them. None for no rule. Read at every
        layout, so a widget that moves is followed at the next frame."""
        self._keep_out = provider

    def keep_out_boxes(self, height) -> list:
        """The keep-out rectangles in display coordinates (y up)."""
        provider = self._keep_out
        if provider is None:
            return []
        try:
            rects = list(provider() or ())
        except Exception:                                     # noqa: BLE001
            return []
        out = []
        for rect in rects:
            try:
                x0, y0, x1, y1 = (float(v) for v in rect)
            except (TypeError, ValueError):
                continue
            if x1 > x0 and y1 > y0:
                out.append((x0, float(height) - y1, x1, float(height) - y0))
        return out

    def set_beyond_cells(self, dataset, cell_ids) -> None:
        """Outline the cells *cell_ids* of *dataset* (their edges, 1.6 px) in
        the theme's strong foreground. ``None`` or no ids removes it. No cap
        on the number of cells."""
        self._drop_outline()
        if dataset is None or cell_ids is None or len(cell_ids) == 0:
            self._refresh()
            return
        from vtkmodules.vtkCommonCore import vtkIdList
        from vtkmodules.vtkFiltersCore import vtkExtractCells, vtkExtractEdges
        from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper
        ids = vtkIdList()
        ids.SetNumberOfIds(len(cell_ids))
        for n, value in enumerate(cell_ids):
            ids.SetId(n, int(value))
        extract = vtkExtractCells()
        extract.SetInputData(dataset)
        extract.SetCellList(ids)
        edges = vtkExtractEdges()
        edges.SetInputConnection(extract.GetOutputPort())
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(edges.GetOutputPort())
        mapper.ScalarVisibilityOff()
        mapper.SetResolveCoincidentTopologyToPolygonOffset()
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.SetObjectName('worstBeyondLimitOutline')
        prop = actor.GetProperty()
        prop.SetLineWidth(OUTLINE_WIDTH)
        prop.SetColor(*outline_colour())
        prop.LightingOff()
        actor.PickableOff()
        self._view.addActor(actor)
        self._outline = actor
        self._refresh()

    def refresh_theme(self) -> None:
        """Re-read the theme colours (a theme change)."""
        if self._outline is not None:
            self._outline.GetProperty().SetColor(*outline_colour())
        if self._shapes or self._texts:
            self._rebuild()
        else:
            self._refresh()

    def outline_actor(self):
        return self._outline

    def _drop_outline(self) -> None:
        if self._outline is not None:
            try:
                self._view.removeActor(self._outline)
            except Exception:                                 # noqa: BLE001
                pass
            self._outline = None

    def clear(self) -> None:
        self._drop_outline()
        self._targets, self._selected, self._caption = [], None, ()
        self._rebuild()

    def label_texts(self) -> list:
        """What is written on the model now (worst first, then the tag)."""
        return [text for _p, text, _s in self._all() if self._enabled]

    def caption_lines(self) -> tuple:
        return self._caption if self._caption_enabled else ()

    def boxes(self) -> list:
        """The boxes of the last layout (display coordinates, y up)."""
        return list(self._boxes)

    def caption_box(self):
        """The caption's frame ``(x0, y0, x1, y1)`` (display, y up) or None."""
        return self._caption_box

    def clear_bounds(self):
        """The bounds the layout uses, display coordinates (y up)."""
        renderer = self._renderer()
        if renderer is None:
            return None
        width, height = renderer.GetSize()
        return self._bounds(width, height)

    def detach(self) -> None:
        self._drop_outline()
        self._targets, self._selected, self._caption = [], None, ()
        self._rebuild()
        self._unobserve()

    # -- drawing -------------------------------------------------------- #

    def _all(self) -> list:
        extra = [self._selected] if self._selected is not None else []
        return list(self._targets) + extra

    def _renderer(self):
        try:
            return self._view.renderer()
        except Exception:                                     # noqa: BLE001
            return None

    def _bounds(self, width, height):
        rect = self._clear
        if rect is None:
            try:
                rect = self._view.clearRect()
            except Exception:                                 # noqa: BLE001
                rect = None
        if rect is None:
            return (0.0, 0.0, float(width), float(height))
        x0, y0, x1, y1 = (float(v) for v in rect)
        x0, x1 = max(x0, 0.0), min(x1, float(width))
        lo, hi = max(height - y1, 0.0), min(height - y0, float(height))
        return (x0, lo, max(x1, x0), max(hi, lo))

    def _remove_actors(self) -> None:
        actors = list(self._texts) + [s.actor for s in self._shapes.values()]
        for actor in actors:
            try:
                self._view.removeActor(actor)
            except Exception:                                 # noqa: BLE001
                pass
        self._labels, self._shapes, self._texts = [], {}, []
        self._caption_actors = []

    def _unobserve(self) -> None:
        renderer = self._renderer()
        if self._observer is not None and renderer is not None:
            renderer.RemoveObserver(self._observer)
        self._observer = None

    def _shape(self, key, name, colour, opacity, *, lines, width=1.0) -> None:
        shape = _Shapes(name, colour, opacity, lines=lines, width=width)
        self._shapes[key] = shape
        self._view.addActor(shape.actor)

    def _rebuild(self) -> None:
        self._remove_actors()
        renderer = self._renderer()
        shown = self._all() if self._enabled else []
        want_caption = bool(self._caption and self._caption_enabled)
        if renderer is None or not (shown or want_caption):
            self._unobserve()
            self._boxes, self._caption_box = [], None
            self._refresh()
            return
        colours = _colours()
        size = max(int(round(FONT_SIZE * self._scale)), 6)
        small = max(int(round(FONT_SIZE * 0.9 * self._scale)), 6)
        selected_index = len(self._targets) if self._selected else -1
        if shown:
            # Draw order: boxes, frames, leaders, ring, dots, then the text.
            self._shape('fill', 'worstValueBoxes', colours['fill'],
                        _LABEL_OPACITY, lines=False)
            self._shape('frame', 'worstValueFrames', colours['frame'],
                        _FRAME_OPACITY, lines=True)
            self._shape('selframe', 'worstSelectedFrame', colours['frame'],
                        1.0, lines=True)
            self._shape('leader', 'worstValueLeaders', colours['frame'],
                        _FRAME_OPACITY, lines=True)
            self._shape('ring', 'worstSelectedRing', colours['frame'], 1.0,
                        lines=True, width=1.2)
            self._shape('dotring', 'worstValueDotRings', colours['ring'],
                        1.0, lines=False)
            self._shape('dot', 'worstValueDots', colours['value'], 1.0,
                        lines=False)
        if want_caption:
            self._shape('capfill', 'statisticsCaptionBox',
                        colours['caption.fill'], _CAPTION_OPACITY,
                        lines=False)
            self._shape('capframe', 'statisticsCaptionFrame',
                        colours['caption.frame'], 1.0, lines=True)
        for index, (_point, label, suffix) in enumerate(shown):
            if index == selected_index:
                value = _text_actor(label, colours['value'], size,
                                    name='worstValueLabel', mono=True)
                tail = None
            else:
                value = _text_actor(label, colours['value'], size, bold=True,
                                    name='worstValueLabel')
                text = suffix or ('worst' if index == 0 else '')
                tail = (_text_actor(text, colours['suffix'], small,
                                    name='worstValueSuffix')
                        if text else None)
            for actor in (value, tail):
                if actor is not None:
                    self._view.addActor(actor)
                    self._texts.append(actor)
            self._labels.append((value, tail))
        if self._observer is None:
            self._observer = renderer.AddObserver(
                'StartEvent', lambda _o, _e: self.relayout())
        self.relayout()
        self._refresh()

    def _refresh(self) -> None:
        try:
            self._view.refresh()
        except Exception:                                     # noqa: BLE001
            pass

    # -- layout --------------------------------------------------------- #

    @staticmethod
    def _place(anchors, sizes, bounds, selected_index, blocked=()) -> list:
        """One box per label inside *bounds*. The preferred spot is ``_GAP``
        right of the anchor, or left of it in the left half of the bounds (the
        mockup's side flip); then the nearest free spot up and down on either
        side; then a stack from the corner. Rows are bucketed so any number of
        labels stays cheap.

        Plan 45 V3. *blocked* (display boxes, y up) are taken before the
        first label: a tag never runs under the overlay card, the bottom bar
        or the legend."""
        x0, y0, x1, y1 = bounds
        pad = 4.0
        bucket = 32.0
        rows: dict = {}
        placed: list = []

        def free(box):
            for r in range(int(box[1] // bucket), int(box[3] // bucket) + 1):
                for other in rows.get(r, ()):
                    if not (box[2] + pad <= other[0] or other[2] + pad <= box[0]
                            or box[3] + pad <= other[1]
                            or other[3] + pad <= box[1]):
                        return False
            return True

        def commit(box):
            placed.append(box)
            for r in range(int(box[1] // bucket), int(box[3] // bucket) + 1):
                rows.setdefault(r, []).append(box)

        for box in blocked:
            for r in range(int(box[1] // bucket), int(box[3] // bucket) + 1):
                rows.setdefault(r, []).append(tuple(box))

        mid = (x0 + x1) / 2.0
        stacked = y0
        for n, ((ax, ay), (bw, bh)) in enumerate(zip(anchors, sizes)):
            gap = _RING_RADIUS + 3.0 if n == selected_index else _GAP
            left_first = ax < mid and n != selected_index
            xs = ((ax - gap - bw, ax + gap) if left_first
                  else (ax + gap, ax - gap - bw))
            step = bh + pad
            chosen = None
            k = 0
            while chosen is None and k * step <= (y1 - y0) + step:
                for dy in ((0.0,) if k == 0 else (k * step, -k * step)):
                    for bx in xs:
                        bx = min(max(bx, x0), max(x1 - bw, x0))
                        by = min(max(ay - bh / 2.0 + dy, y0), max(y1 - bh, y0))
                        box = (bx, by, bx + bw, by + bh)
                        if free(box):
                            chosen = box
                            break
                    if chosen:
                        break
                k += 1
            if chosen is None:
                # Nothing free beside the anchor: scan the bounds for any free
                # slot; if the bounds are full, stay inside them regardless.
                chosen = (min(x0, max(x1 - bw, x0)), min(stacked, max(y1 - bh, y0)))
                chosen = (chosen[0], chosen[1], chosen[0] + bw, chosen[1] + bh)
                yy = y0
                while yy + bh <= y1 + 0.01:
                    xx = x0
                    while xx + bw <= x1 + 0.01:
                        box = (xx, yy, xx + bw, yy + bh)
                        if free(box):
                            chosen = box
                            yy = y1 + 1.0
                            break
                        xx += max(bw / 2.0, 4.0)
                    yy += max(step / 2.0, 4.0)
            commit(chosen)
        return placed

    def relayout(self) -> None:
        """Place the boxes for the camera as it is now."""
        from foammesh.rendering import label_pass
        if label_pass.capturing():
            # DP-2740: the tiles of a magnified picture move the camera; the
            # boxes keep the places they had on the screen.
            return
        renderer = self._renderer()
        if renderer is None:
            return
        width, height = renderer.GetSize()
        if width <= 0 or height <= 0:
            return
        bounds = self._bounds(width, height)
        shown = self._all() if self._enabled else []
        selected_index = len(self._targets) if self._selected else -1
        font = FONT_SIZE * self._scale
        anchors, sizes, self._metrics = [], [], []
        for (point, _label, _s), (value, tail) in zip(shown, self._labels):
            renderer.SetWorldPoint(point[0], point[1], point[2], 1.0)
            renderer.WorldToDisplay()
            x, y, _z = renderer.GetDisplayPoint()
            anchors.append((x, y))
            vw, vh = _measure(value, renderer)
            tw, th = _measure(tail, renderer) if tail is not None else (0, 0)
            if vw <= 0:                       # no font metrics yet
                vw = 0.62 * font * max(len(value.GetInput()), 1)
            hh = max(vh, th, font)
            inner = vw + ((6.0 + tw) if tail is not None else 0.0)
            sizes.append((inner + 2 * _PAD_X, hh + 2 * _PAD_Y + 2.0))
            self._metrics.append((vw, tw, hh))
        blocked = self.keep_out_boxes(height)
        self._boxes = self._place(anchors, sizes, bounds, selected_index,
                                  blocked)
        if shown:
            for key in _SHAPE_KEYS:
                self._shapes[key].reset()
            k = self._scale
            for n, ((ax, ay), box, (value, tail), (vw, _tw, _h)) in enumerate(
                    zip(anchors, self._boxes, self._labels, self._metrics)):
                outline = rounded_rect(*box, 3.0 * k, steps=3)
                self._shapes['fill'].add(outline)
                if n == selected_index:
                    self._shapes['selframe'].add(outline, closed=True)
                    for seg in dash_segments(ax, ay, _RING_RADIUS * k):
                        self._shapes['ring'].add(seg)
                else:
                    self._shapes['frame'].add(outline, closed=True)
                    self._shapes['leader'].add(
                        [(ax, ay), wv.leader_end((ax, ay), box)])
                    self._shapes['dotring'].add(
                        disc(ax, ay, _DOT_RADIUS + _DOT_RING))
                    self._shapes['dot'].add(disc(ax, ay, _DOT_RADIUS))
                value.SetPosition(box[0] + _PAD_X, box[1] + _PAD_Y + 2.0)
                if tail is not None:
                    tail.SetPosition(box[0] + _PAD_X + vw + 6.0,
                                     box[1] + _PAD_Y + 2.0)
            for key in _SHAPE_KEYS:
                self._shapes[key].done()
        self._layout_caption(renderer, bounds, blocked)
        self._apply_groups()

    def _wrapped(self, renderer, room) -> list:
        """The caption lines, each source line broken at ' · ' so none is
        wider than *room* pixels (a single segment is never cut)."""
        k = self._scale
        csize = max(int(round(11.5 * k)), 6)
        probe = _text_actor('', (1, 1, 1), csize)

        def width(text):
            probe.SetInput(text)
            w, _h = _measure(probe, renderer)
            return w if w > 0 else 0.6 * 11.5 * k * max(len(text), 1)

        out = []
        for n, line in enumerate(self._caption):
            current = ''
            for part in str(line).split(' · '):
                trial = part if not current else current + ' · ' + part
                if current and width(trial) > room:
                    out.append((current, n))
                    current = part
                else:
                    current = trial
            out.append((current, n))
        return out

    def _sync_caption(self, renderer, bounds) -> None:
        if not (self._caption and self._caption_enabled):
            return
        k = self._scale
        room = max(bounds[2] - bounds[0] - _CAPTION_LEFT - 18.0 * k - 4.0, 40.0)
        wanted = self._wrapped(renderer, room)
        have = [(a.GetInput(), a._fm_line) for a in self._caption_actors]
        if have == wanted:
            return
        for actor in self._caption_actors:
            try:
                self._view.removeActor(actor)
            except Exception:                                 # noqa: BLE001
                pass
            if actor in self._texts:
                self._texts.remove(actor)
        self._caption_actors = []
        colours = _colours()
        csize = max(int(round(11.5 * k)), 6)
        for index, (text, source) in enumerate(wanted):
            actor = _text_actor(
                text, colours['caption'] if source == 0
                else colours['caption.dim'], csize,
                name='statisticsCaption' if index == 0
                else 'statisticsCaptionLine')
            actor._fm_line = source
            self._view.addActor(actor)
            self._texts.append(actor)
            self._caption_actors.append(actor)

    def _layout_caption(self, renderer, bounds, blocked=()) -> None:
        self._caption_box = None
        self._sync_caption(renderer, bounds)
        if not self._caption_actors:
            return
        x0, y0, x1, y1 = bounds
        k = self._scale
        padx, pady = 9.0 * k, 5.0 * k
        measured = []
        for actor in self._caption_actors:
            w, h = _measure(actor, renderer)
            if w <= 0:
                w, h = 0.6 * 11.5 * k * max(len(actor.GetInput()), 1), 11.5 * k
            measured.append((w, h))
        width = max(w for w, _h in measured)
        line_h = max(h for _w, h in measured) * 1.45
        box_w = width + 2 * padx
        box_h = line_h * len(measured) + 2 * pady
        left = x0 + _CAPTION_LEFT
        if left + box_w > x1 - 2.0:
            left = max(x1 - 2.0 - box_w, x0 + 2.0)
        bottom = y0 + _CAPTION_ABOVE
        # Plan 45 V10. The caption stands above whatever covers its place
        # (the bottom bar at 1280x800 hid its second line).
        for _turn in range(len(blocked) + 1):
            hits = [b for b in blocked
                    if b[0] < left + box_w and left < b[2]
                    and b[1] < bottom + box_h and bottom < b[3]]
            if not hits:
                break
            bottom = max(b[3] for b in hits) + 4.0
        if bottom + box_h > y1:
            bottom = max(y1 - box_h, y0)
        box = (left, bottom, left + box_w, bottom + box_h)
        self._caption_box = box
        pts = rounded_rect(*box, _CAPTION_RADIUS, steps=3)
        for key in ('capfill', 'capframe'):
            self._shapes[key].reset()
        self._shapes['capfill'].add(pts)
        self._shapes['capframe'].add(pts, closed=True)
        for key in ('capfill', 'capframe'):
            self._shapes[key].done()
        top = box[3] - pady
        for n, (actor, (_w, h)) in enumerate(zip(self._caption_actors,
                                                 measured)):
            actor.SetPosition(left + padx,
                              top - line_h * (n + 1) + (line_h - h) / 2.0)
