"""Prop picks that draw only the renderer they pick in (DP-2713).

A prop pick (``vtkPropPicker.PickProp``) draws the whole render window in
selection mode. The other layers -- the corner triad's renderer over the
scene -- were drawn in that pass too, and drawing them reached VTK's hardware
selector before it had numbered a prop: it read its prop number from memory
it never set, and whenever that held a large value it logged "Too many props.
Currently only 16777214 props are supported." (MEASURED live on the pipe
case: 22 to 53 of 81 hover picks; none with the corner renderer not drawn).
The pick only ever reads the renderer it was asked about, so the others sit
the pass out and are drawn again at once; the selection pass is not shown.
"""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def drawing_only(renderer):
    """Leave every other renderer of *renderer*'s window undrawn meanwhile."""
    getWindow = getattr(renderer, 'GetRenderWindow', None)
    window = getWindow() if getWindow is not None else None
    others = []
    if window is not None:
        renderers = window.GetRenderers()
        renderers.InitTraversal()
        for _ in range(renderers.GetNumberOfItems()):
            other = renderers.GetNextItem()
            if other is not None and other is not renderer and other.GetDraw():
                other.SetDraw(0)
                others.append(other)
    try:
        yield
    finally:
        for other in others:
            other.SetDraw(1)


def pick_prop(picker, x, y, renderer, props=None):
    """``picker.PickProp`` with only *renderer* drawn in the selection pass."""
    with drawing_only(renderer):
        if props is None:
            return picker.PickProp(x, y, renderer)
        return picker.PickProp(x, y, renderer, props)
