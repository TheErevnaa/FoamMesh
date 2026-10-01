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


def _subprocess_role(arguments):
    """Plan 35 (D2): the same program is also the crash helper and the worker.

    ``--crash-helper <pid>`` and ``--worker <op> ...`` are answered here,
    before Qt or VTK is imported: neither role needs them, and the crash helper
    must not be able to fail on a GL driver or a Qt plugin. Returns the exit
    code, or ``None`` for a GUI start.
    """
    arguments = list(arguments)
    if not arguments:
        return None
    if arguments[0] == '--crash-helper':
        from foammesh.support import crash_helper
        return _run_role(lambda: crash_helper.main(arguments), 1)
    if arguments[0] == '--worker':
        try:
            from foammesh.workers import mesh_worker
        except ImportError:
            if sys.stderr is not None:
                print('FoamMesh --worker: this build has no mesh worker '
                      '(foammesh.workers.mesh_worker)', file=sys.stderr)
            return 2
        return _run_role(lambda: mesh_worker.main(arguments[1:]), 2)
    return None


def _run_role(body, failed_code):
    """Run a subprocess role; an error it does not catch becomes an exit code.

    The packaged build has no console: an exception escaping to the
    bootloader opens a modal "Unhandled exception in script" dialog, and the
    helper or worker then waits for a click instead of exiting -- a hang its
    parent can only end by timing out.
    """
    try:
        return body()
    except SystemExit:
        raise
    except BaseException:                                   # noqa: BLE001
        if sys.stderr is not None:
            import traceback

            traceback.print_exc()
        return failed_code


if __name__ == '__main__':
    _role_exit_code = _subprocess_role(sys.argv[1:])
    if _role_exit_code is not None:
        raise SystemExit(_role_exit_code)
    if sys.stderr is None:
        # pythonw / a console-less start: native output would go to NUL. The
        # packaged build's runtime hook has already done this; it is a no-op
        # then.
        from foammesh.support import native_capture
        native_capture.install()
    # 2026-10-01. Ask for the high-performance GPU before Qt or VTK loads
    # OpenGL; the driver falls back to the integrated GPU by itself.
    try:
        from foammesh.rendering import gpu_profile as _gpu_profile
        _gpu_profile.prefer_discrete_gpu()
    except Exception:                                       # noqa: BLE001
        pass

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
from foammesh.support import (
    crash_helper, lifecycle, qt_messages, safe_mode, watchdog)
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
    dead_sessions = lifecycle.install(app.settings.settingsPath(),
                                      version=meshAppProperties.version)
    # Plan 35 CR0: the root log, thread and unraisable exceptions, native
    # dumps from outside the process, and Qt's own messages -- all before the
    # QApplication exists.
    lifecycle.attach_root_log()
    lifecycle.install_hooks()
    for dead in dead_sessions:
        logger.warning(
            'A previous FoamMesh session (pid %s) ended without a clean exit%s; '
            'last operations: %s. See %s.', dead.get('pid'),
            f' ({dead["ended"]})' if dead.get('ended') else '',
            ' | '.join((dead.get('recent_operations') or [])[-5:]) or 'none',
            lifecycle.log_directory())
    # Plan 35 CR8: graphics safe mode for this start -- asked for with
    # --safe-mode, chosen on the last start's crash notice, or after two
    # render-attributed deaths in a row. Decided before Qt reads QT_OPENGL.
    graphics = safe_mode.decide(sys.argv, dead_sessions,
                                lifecycle.log_directory())
    safe_mode.apply_environment(graphics)
    if graphics.active:
        logger.warning('Graphics safe mode: %s', graphics.reason)
        lifecycle.record(f'graphics safe mode: {graphics.reason}')
    if crash_helper.start(lifecycle.log_directory()) is None:
        logger.info('No crash helper this session; a native crash leaves '
                    'faulthandler.log only.')
    qt_messages.install()
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
    # CR1: the GUI thread's heartbeat, and a status-bar note after a stall.
    watchdog.start(lifecycle.watchdog_log_path(), app.window)
    if dead_sessions:
        # CR0 step 6: a non-modal banner naming the session that died.
        app.window.showPreviousCrash(dead_sessions)
    # Plan 35 CR3: freeze the start-up heap and steer the cyclic collector
    # onto the GUI thread before any project object exists.
    from foammesh.support import gc_policy
    gc_policy.startup_complete()

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
        # Plan 35 CR6: probe at start and every minute while idle, off-thread.
        app.startWslHealth()
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
    watchdog.stop()
    crash_helper.stop()
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
