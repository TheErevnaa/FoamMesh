"""Load Windows' own ICU before Qt, so PyPI Qt works inside a conda or Pixi env.

PyPI's Qt6Core.dll (PySide6 6.11 and later) links the unversioned
``icuuc.dll`` that ships with Windows. A conda or Pixi environment, and the
Anaconda base env, also has an ``icuuc.dll`` (conda-forge ICU, pulled in by
hdf5, libxml2 and others) in ``Library\\bin``, and that copy is found first, so
``import PySide6.QtCore`` fails with "DLL load failed ... The specified
procedure could not be found".

conda's own libraries link the versioned ``icuuc<NN>.dll``, so loading the
system copy first is safe for both: Qt then reuses the module that is already
loaded. This must run before the first PySide6 import. `foammesh/__init__.py`
calls it, so importing any ``foammesh`` module is enough; the Pixi env also
runs the same preload at every interpreter start (tools/deps/envhooks).

Same fix as FoamFlow's ``foamflow/_win_icu.py``.
"""
import os
import sys

_done = False


def preload_windows_icu():
    """Load ``%SystemRoot%\\System32\\icuuc.dll``; a no-op off Windows. Never raises."""
    global _done
    if _done or sys.platform != 'win32':
        return
    _done = True
    path = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'icuuc.dll')
    if not os.path.exists(path):
        return  # Windows before 10 1703 has no system ICU; nothing to do.
    try:
        import ctypes
        ctypes.WinDLL(path)
    except OSError:
        pass  # Qt reports its own load error if this mattered.
