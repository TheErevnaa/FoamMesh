"""Plan 45 start-up: Qt's one full scan of the installed fonts, off the GUI thread.

The bundled Pretendard is a Latin subset. The first time the window lays out a
glyph it lacks (eight do: ⇄ ⓘ ▭ ▸ ▾ ◈ ✕ ⟂), or uses a font naming more than
one family, Qt asks the platform for the fallback families, and the Windows
(DirectWrite) font database answers by populating every installed family:
~0.27 s on the GUI thread, measured live 18 October 2026 (263 families, inside
the window's construction). The glyphs and families are drawn exactly as
before; only where the scan is paid changes.

PySide holds the GIL through every QFontDatabase and QFontMetrics call, so a
Python thread asking Qt for it would stall the imports it is meant to overlap.
:func:`start` instead calls ``QFontDatabase::families(Latin)`` -- which
populates every family -- through ``ctypes``, which releases the GIL for the
whole foreign call. Qt guards its font database with its own mutex; a font
asked for on the GUI thread meanwhile waits for the scan rather than repeating
it. Where the export is not found (another Qt build) nothing starts and the
scan stays where it was.
"""
from __future__ import annotations

import ctypes
import sys
import threading

#: ``static QStringList QFontDatabase::families(QFontDatabase::WritingSystem)``
#: as the MSVC build of Qt 6 exports it. The mangled name carries the whole
#: signature, so a different signature cannot be found under this name.
_FAMILIES = '?families@QFontDatabase@@SA?AV?$QList@VQString@@@@W4WritingSystem@1@@Z'
_LATIN = 1                   # QFontDatabase::Latin
_RESULT_BYTES = 64           # a QList is three pointers (24 bytes) in Qt 6

_thread: threading.Thread | None = None


def _families_export():
    if sys.platform != 'win32':
        return None
    try:
        # Qt6Gui.dll is already loaded by PySide6.QtGui; by base name the
        # loader hands back that module rather than loading a second copy.
        function = getattr(ctypes.WinDLL('Qt6Gui.dll'), _FAMILIES)
    except (OSError, AttributeError):
        return None
    function.restype = ctypes.c_void_p
    function.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return function


def start() -> threading.Thread | None:
    """Begin the scan on a worker thread; once per process.

    Call after the QGuiApplication exists. The returned list is left alive
    (a few kilobytes, once): its destructor is not exported.
    """
    global _thread
    from PySide6.QtGui import QGuiApplication

    application = QGuiApplication.instance()
    if _thread is not None or application is None:
        return _thread
    function = _families_export()
    if function is None:
        return None

    def scan():
        result = ctypes.create_string_buffer(_RESULT_BYTES)
        function(result, _LATIN)
        _keep.append(result)

    _thread = threading.Thread(target=scan, name='foammesh-font-scan', daemon=True)
    _thread.start()
    # Qt's font database must outlive the scan: a quit within the first
    # ~0.3 s waits for it rather than tearing the database down under it.
    application.aboutToQuit.connect(wait)
    return _thread


def wait(timeout: float | None = 5.0) -> None:
    if _thread is not None:
        _thread.join(timeout)


_keep: list = []
