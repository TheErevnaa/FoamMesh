"""Plan 45 start-up: the OpenGL driver wakes beside the GUI thread.

The first ``vtkRenderWindow()`` of the process wakes the graphics card's
OpenGL driver: MEASURED live 18 October 2026, ~0.18 s on the GUI thread
inside the window's construction (0.04 s once the driver is awake). VTK holds
the GIL while it does it, so a Python thread making the render window would
stall the imports it is meant to overlap. :func:`start` instead asks Windows
to describe the screen's first pixel format through ``ctypes`` -- which wakes
the same driver -- on a worker thread, with the GIL released for the whole
foreign call. Nothing is created that the viewport uses: the render window,
its native window and its GL context are made on the GUI thread, as before.

Called after the GPU preference is applied (the driver is chosen when it
wakes) and never in graphics safe mode.
"""
from __future__ import annotations

import ctypes
import sys
import threading

#: ``sizeof(PIXELFORMATDESCRIPTOR)``.
_DESCRIPTOR_BYTES = 40

_thread: threading.Thread | None = None


def _describe_first_pixel_format() -> None:
    user32 = ctypes.WinDLL('user32')
    gdi32 = ctypes.WinDLL('gdi32')
    user32.GetDC.restype = ctypes.c_void_p
    user32.GetDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.DescribePixelFormat.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
    screen = user32.GetDC(None)
    if not screen:
        return
    try:
        descriptor = ctypes.create_string_buffer(_DESCRIPTOR_BYTES)
        gdi32.DescribePixelFormat(screen, 1, _DESCRIPTOR_BYTES, descriptor)
    finally:
        user32.ReleaseDC(None, screen)


def start(safe_mode: bool = False) -> threading.Thread | None:
    """Wake the driver on a worker thread; once per process."""
    global _thread
    if _thread is not None or safe_mode or sys.platform != 'win32':
        return _thread
    _thread = threading.Thread(target=_describe_first_pixel_format,
                               name='foammesh-gl-driver', daemon=True)
    _thread.start()
    return _thread


def wait(timeout: float | None = 5.0) -> None:
    """Before the first render window is made: the driver is awake."""
    if _thread is not None:
        _thread.join(timeout)
