"""Run at every interpreter start of the Pixi env, via foammesh_envhooks.pth.

PyPI's Qt6Core.dll (PySide6 6.11+) links the unversioned ``icuuc.dll`` that
ships with Windows. The conda env also has an ``icuuc.dll`` (conda-forge ICU)
in Library/bin, which is found first, so ``import PySide6.QtWidgets`` fails
with "DLL load failed ... The specified procedure could not be found" unless
the System32 copy is already loaded. conda's own libraries link the versioned
``icuuc78.dll``, so loading the system copy first is safe for both.

foammesh/support/win_icu.py does the same from foammesh.main; this hook covers
every Python of the env, including children that import PySide6 first.
No-op off Windows. Never raises (a .pth failure would print at every start).
Imports nothing beyond os/sys (and _ctypes on Windows): ~6 ms per start.
"""
import os
import sys


def preload_windows_icu():
    if sys.platform != "win32":
        return
    try:
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icuuc.dll")
        if os.path.exists(path):  # absent before Windows 10 1703
            try:
                import _ctypes  # what ctypes.WinDLL calls; skips ~10 ms of ctypes setup
                _ctypes.LoadLibrary(path)
            except (ImportError, AttributeError):
                import ctypes
                ctypes.WinDLL(path)
    except Exception:
        pass  # Qt reports its own load error if this mattered.


preload_windows_icu()
