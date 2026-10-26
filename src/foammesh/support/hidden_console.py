"""Keep child processes from opening console windows over the application.

FoamMesh is a windowed program. On Windows every console program it starts
(``wsl.exe``, Gmsh, ``checkMesh``, ``snappyHexMesh`` through WSL, ...) gets
its own black console window unless the launch asks for none, and that window
opens over the viewport until the program ends. Most launch sites did not ask.
Rather than depend on each of them (and on every future one), this module makes
"no window" the default for every ``subprocess.Popen`` in the process, which
``subprocess.run`` and asyncio's subprocess support also go through.

A launch that really wants a visible console says so with
``CREATE_NEW_CONSOLE`` (the "Open OpenFOAM terminal" action), and is left as
it asked.
"""
from __future__ import annotations

import subprocess
import sys

_INSTALLED = False


def install() -> bool:
    """Hide the console of every child process from now on; True when patched.

    Only Windows has the problem, so elsewhere this does nothing. Calling it
    again is harmless.
    """
    global _INSTALLED
    if _INSTALLED or sys.platform != 'win32':
        return False
    original = subprocess.Popen.__init__
    no_window = subprocess.CREATE_NO_WINDOW
    new_console = subprocess.CREATE_NEW_CONSOLE
    detached = getattr(subprocess, 'DETACHED_PROCESS', 0)

    def __init__(self, *args, **kwargs):
        flags = kwargs.get('creationflags', 0) or 0
        if not flags & (new_console | detached):
            kwargs['creationflags'] = flags | no_window
            info = kwargs.get('startupinfo')
            if info is None:
                info = subprocess.STARTUPINFO()
                kwargs['startupinfo'] = info
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = subprocess.SW_HIDE
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = __init__
    _INSTALLED = True
    return True
