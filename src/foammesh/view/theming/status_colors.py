"""Semantic status and colour-swatch helpers."""
from PySide6.QtGui import QColor


def set_status(widget, status: str | None) -> None:
    if status not in (None, 'success', 'warning', 'error', 'info'):
        raise ValueError(f'unknown semantic status: {status}')
    widget.setProperty('foammeshStatus', status or '')
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def apply_color_swatch(widget, color: QColor | None) -> None:
    """Paint a small square in ``color``, or clear it when ``color`` is None.

    The colour is written straight into the widget's own stylesheet rather than
    into its palette. The palette route went through a ``palette(window)``
    reference in the global stylesheet, and a widget that already carries an
    application stylesheet does not re-resolve that on a palette change -- so
    every swatch in Display Control stayed the neutral window colour no matter
    what colour its actor was actually drawn in.
    """
    widget.setProperty('foammeshColorSwatch', color is not None)
    widget.setAutoFillBackground(color is not None)
    if color is None:
        widget.setStyleSheet('')
    else:
        widget.setStyleSheet(
            f'background-color: {color.name()};'
            ' border: 1px solid rgba(0, 0, 0, 60); border-radius: 3px;')
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()



def swatch_color(widget) -> QColor | None:
    """The colour :func:`apply_color_swatch` last painted on *widget*.

    Plan 33 W-B. The swatch is a stylesheet, so the only way to ask a row
    what colour it is showing was to parse that stylesheet at the call site.
    Every reader that wants the answer -- the geometry list repainting a row,
    a gate proving the row matches its actor -- asks here instead, so one
    spelling of the declaration cannot drift from another.
    """
    if not widget.property('foammeshColorSwatch'):
        return None
    for declaration in str(widget.styleSheet()).split(';'):
        name, _, value = declaration.partition(':')
        if name.strip() == 'background-color':
            color = QColor(value.strip())
            return color if color.isValid() else None
    return None
