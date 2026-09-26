"""FoamMesh.

The package body does one thing, and it has to happen before anything else in
the process touches Qt: it names the Qt binding.

DP-358. `qasync` chooses a binding at import time, and with none already
chosen it tries `PyQt5`, `PyQt6`, `PySide2`, `PySide6` in that order and takes
the first that imports. On this machine -- an Anaconda base environment --
`PyQt5` imports, so a bare `import qasync` binds qasync's `QTimer`, `QThread`
and `QMutex` to Qt5 while the application's own widgets are PySide6's Qt6.
Nothing reports a mismatch. The two halves simply stop talking: a `QEventLoop`
built on the PySide6 `QApplication` never advances, because the thread that
must release its semaphore is a Qt5 thread belonging to a Qt5 application that
does not exist. The wait has no timeout, so the process hangs for ever.

`main.py` has pinned the binding since the Qt5/Qt6 crash it names, but a pin in
the launcher only protects the launcher. Every other way into this code --
the test suite, the strict-GUI scripts, an embedder importing
`foammesh.view.main_window` -- imported `qasync` through a module that had
never set the variable. Pinning here covers all of them, because importing any
`foammesh.*` module runs this file first.
"""
import os

os.environ.setdefault('QT_API', 'pyside6')
os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PySide6')

if os.environ['QT_API'] != 'pyside6':
    raise ImportError(
        'FoamMesh is a PySide6 (Qt6) application, but QT_API is set to '
        f'{os.environ["QT_API"]!r}. Qt-abstraction libraries — qasync and '
        'qtpy among them — would follow that setting and load a second Qt '
        'into this process. Unset QT_API, or set it to pyside6.')
