#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import logging
import os
import sys

# FoamMesh targets PySide6 (Qt6). Pin the Qt binding before importing qasync,
# qtpy or any Qt-abstraction library: with no binding chosen they default to
# PyQt5 when it is importable (e.g. an Anaconda base env), which pulls Qt5 into
# the same process as PySide6's Qt6 and crashes at runtime (BEX64 in Qt5Core).
os.environ.setdefault('QT_API', 'pyside6')
os.environ.setdefault('PYQTGRAPH_QT_LIB', 'PySide6')

import qasync
from PySide6.QtWidgets import QApplication

# To render SVG files.
# noinspection PyUnresolvedReferences
import PySide6.QtSvg
from vtkmodules.vtkCommonCore import vtkSMPTools

# To use ".qrc" QT Resource files
# noinspection PyUnresolvedReferences
import resource_rc

from app_properties import meshAppProperties
from analytics import Analytics
from analytics.events import EVENT_LOOP_ERROR

from foammesh.app import app
from foammesh.core.case import startup_case_path
from foammesh.support import lifecycle
from foammesh.view.main_window.main_window import MainWindow

logger = logging.getLogger()
formatter = logging.Formatter("[%(asctime)s][%(name)s] ==> %(message)s")
handler = logging.StreamHandler()
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)


def _package_self_test_path(arguments) -> str | None:
    """Return the report path for the frozen-package qualification mode."""
    arguments = list(arguments)
    if '--package-self-test' not in arguments:
        return None
    index = arguments.index('--package-self-test')
    if index + 1 >= len(arguments) or arguments[index + 1].startswith('--'):
        raise ValueError('--package-self-test requires a JSON report path')
    return arguments[index + 1]


def run_package_self_test(report_path):
    """Lazy import keeps package qualification out of normal GUI startup."""
    from foammesh.core.release.package_self_test import run
    return run(report_path)


def handle_exception(eType, eValue, eTraceback):
    if issubclass(eType, KeyboardInterrupt):
        sys.__excepthook__(eType, eValue, eTraceback)
        return

    logger.critical("Uncaught exception", exc_info=(eType, eValue, eTraceback))
    Analytics().captureException(eValue)


sys.excepthook = handle_exception


def loop_exception(loop, context):
    exception = context.get('exception')
    message = context.get('message', 'unhandled asyncio error')
    logger.error(
        'Unhandled event-loop error: %s', message,
        exc_info=(type(exception), exception, exception.__traceback__)
        if exception is not None else None)
    if exception is not None:
        Analytics().captureException(exception, {'source': 'event_loop'})
    else:
        Analytics().capture(EVENT_LOOP_ERROR, {'message': message})


#: Windows taskbar identity. Any stable, unique string works; changing it makes
#: Windows treat the app as a different program (new taskbar slot, lost pin).
APP_USER_MODEL_ID = 'FoamMesh.FoamMesh.Desktop.1'


def _claim_windows_taskbar_identity():
    """Make Windows show *our* icon on the taskbar, not the interpreter's.

    ``setWindowIcon`` only dresses the window itself. The taskbar button is
    grouped by Application User Model ID, which defaults to the host process --
    ``python.exe`` -- so the taskbar shows Python's icon while the title bar
    shows ours. Declaring an explicit ID before the first window exists gives
    the app its own taskbar identity, and lets a pinned shortcut match it.

    Best-effort: a failure here costs an icon, never a launch.
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            APP_USER_MODEL_ID)
    except Exception:                                   # pragma: no cover
        logging.getLogger(__name__).debug(
            'could not set the Windows taskbar identity', exc_info=True)


# DP-694. The help texts a menu dialog or the mesh check waits on before it can
# open. A cold WSL start held Mesh > Scale for seconds; fetched here, in the
# background, the dialog reads the cache instead.
STARTUP_HELP_UTILITIES = ('transformPoints', 'checkMesh')


async def warm_utility_help(capabilities, names=STARTUP_HELP_UTILITIES):
    """Fetch and cache each utility's help off the GUI thread; failures are retried on use."""
    for name in names:
        try:
            await asyncio.to_thread(capabilities.help, name)
        except Exception:                                   # noqa: BLE001
            # The dialog asks again when it opens (DP-693 caches only answers).
            continue


def main():
    self_test_path = _package_self_test_path(sys.argv[1:])
    if self_test_path is not None:
        report = run_package_self_test(self_test_path)
        return 0 if report['summary']['all_passed'] else 2

    initial_case = startup_case_path(sys.argv[1:])
    app.setupApplication(meshAppProperties)
    # DP-550/551. Before anything native can fail: a fatal fault, a VTK
    # warning and the way this process ends are all written under the
    # application's log directory, and a session that died last time is named.
    for dead in lifecycle.install(app.settings.settingsPath()):
        logger.warning(
            'A previous FoamMesh session (pid %s) ended without a clean exit; '
            'last operations: %s. See %s.', dead.get('pid'),
            ' | '.join((dead.get('recent_operations') or [])[-5:]) or 'none',
            lifecycle.log_directory())
    os.environ['LC_NUMERIC'] = 'C'
    _claim_windows_taskbar_identity()
    application = QApplication(sys.argv)
    application.setWindowIcon(meshAppProperties.icon())
    application.lastWindowClosed.connect(
        lambda: lifecycle.record('Qt lastWindowClosed'))
    application.aboutToQuit.connect(
        lambda: lifecycle.record('Qt aboutToQuit'))

    Analytics().configure(
        app_name=meshAppProperties.name,
        app_version=meshAppProperties.version,
        config_dir=app.settings.settingsPath())

    # Leave 1 core for users
    numCores = max(1, (os.cpu_count() or 2) - 1)

    smp = vtkSMPTools()
    smp.Initialize(numCores)
    smp.SetBackend('STDThread')

    app.qApplication = application

    loop = qasync.QEventLoop(application)
    asyncio.set_event_loop(loop)

    loop.set_exception_handler(loop_exception)

    app.applyLanguage()

    app.window = MainWindow()

    async def bootstrap():
        # First paint and event processing happen before environment probes,
        # consent, or optional case inspection can delay the shell.
        await app.window.start(initial_case)
        await asyncio.sleep(0)
        # A first start looks for OpenFOAM 13 and Gmsh in WSL rather than
        # assuming the distribution is called OpenFOAM13Runtime.
        try:
            found = await app.detectOpenFoamRuntime()
        except Exception:                                   # noqa: BLE001
            logger.warning('OpenFOAM runtime detection failed', exc_info=True)
            found = None
        if found is not None:
            app.window.statusBar().showMessage(QApplication.translate(
                'main', f'Using {found.describe()}.'), 10000)
        mpi = await asyncio.to_thread(app.capabilities.utility, 'mpirun')
        if not mpi.available:
            message = QApplication.translate(
                'main',
                'OpenFOAM 13 WSL MPI is unavailable; parallel actions are '
                f'disabled. {mpi.reason}')
            app.window.statusBar().showMessage(message, 10000)
        else:
            # The runtime just answered, so it is warm: fetch the help texts now,
            # alongside consent, instead of on the user's first transform click.
            warm = loop.create_task(warm_utility_help(app.capabilities))
            background_tasks.add(warm)
            warm.add_done_callback(background_tasks.discard)
        if await Analytics().ensureConsent(parent=app.window):
            Analytics().init()

    background_tasks = set()
    task = loop.create_task(bootstrap())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)

    with loop:
        loop.run_forever()

    lifecycle.record('event loop returned; exiting with code 0')
    loop.close()
    Analytics().shutdown(final=True)
    return 0


def _run_as_script(module_name=__name__):
    """Run the GUI when this module is used as a script."""
    if module_name == '__main__':
        result = main()
        if result:
            raise SystemExit(result)


_run_as_script()
