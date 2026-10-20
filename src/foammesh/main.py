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
    # GPUPREF: the dedicated-GPU request is applied in main(), once the
    # settings are readable (it honours the Settings toggle), still before Qt
    # or VTK loads OpenGL.

import qasync
from PySide6.QtWidgets import QApplication

# To render SVG files.
# noinspection PyUnresolvedReferences
import PySide6.QtSvg

# To use ".qrc" QT Resource files
# noinspection PyUnresolvedReferences
import resource_rc

from app_properties import meshAppProperties
from analytics import Analytics
from analytics.events import EVENT_LOOP_ERROR

from foammesh.support.deferred_log import DeferredHandler
from foammesh.support import (
    crash_helper, lifecycle, loop_executor, qt_messages, safe_mode, watchdog)
from foammesh.view import startup_splash

#: Plan 45 splash. The application's heavy modules -- ~1.2 s of imports,
#: measured live -- are imported by main() once the QApplication and the
#: splash exist, so something of FoamMesh is on screen within about a second.
#: The module-level names they provide: {name: (module, attribute)}, an
#: attribute of None naming the module itself.
DEFERRED_NAMES = {
    'vtkSMPTools': ('vtkmodules.vtkCommonCore', 'vtkSMPTools'),
    'app': ('foammesh.app', 'app'),
    'startup_case_path': ('foammesh.core.case', 'startup_case_path'),
    'MainWindow': ('foammesh.view.main_window.main_window', 'MainWindow'),
    'rendering_widget': ('widgets.rendering.rendering_widget', None),
}


def import_application(serve=None):
    """Import the heavy modules and bind the names main() uses from them.

    ``serve`` is called between two imports (at start: the event queue, so
    the splash stays drawn and no one import holds the GUI thread long). The
    imports are written out, not looked up by name, so the package build
    still finds them. A name already bound -- a test's stand-in -- is left.
    """
    serve = serve or (lambda: None)
    # Plan 45 BOOT2. Seven groups of ~0.1-0.25 s, not three of up to ~0.45 s
    # (MEASURED live, 17 October 2026): the largest dependency of each of the
    # old groups is imported first, on its own.
    import vtkmodules.vtkCommonCore  # noqa: F401
    import foammesh.openfoam.file_system  # noqa: F401
    serve()
    import foammesh.app  # noqa: F401
    serve()
    import foammesh.core.case  # noqa: F401
    import widgets.rendering.rendering_widget  # noqa: F401
    serve()
    import superqt  # noqa: F401
    import foammesh.view.display_control.mesh_quality_info  # noqa: F401
    serve()
    import foammesh.view.display_control.display_control  # noqa: F401
    import foammesh.view.main_window.rendering_tool  # noqa: F401
    serve()
    import foammesh.view.main_window.step_manager  # noqa: F401
    serve()
    import foammesh.view.main_window.main_window  # noqa: F401
    serve()
    namespace = globals()
    for name, (module_name, attribute) in DEFERRED_NAMES.items():
        if name not in namespace:
            module = sys.modules[module_name]
            namespace[name] = module if attribute is None else getattr(module, attribute)


def __getattr__(name):
    """Any other reader of ``foammesh.main.app`` (and the rest) gets them too."""
    if name in DEFERRED_NAMES:
        import_application()
        return globals()[name]
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


logger = logging.getLogger()
formatter = logging.Formatter("[%(asctime)s][%(name)s] ==> %(message)s")
# Plan 45 start-up. The console is written by a thread of its own: MEASURED
# live, one start-up warning (the viewport GPU note) held the GUI thread
# ~0.4 s in the console write while the window was being built.
handler = DeferredHandler(logging.StreamHandler())
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


def _describe_platform_aside():
    """Ask what Windows this is on a worker thread, before anything else does.

    The first ``platform.system()`` of the process runs ``cmd /c ver`` (Python
    3.11) and waits for it; the answer is cached. PyFoam and the MPI support
    ask while the window's modules are imported -- MEASURED live 18 October
    2026, a ~0.02 s wait on the GUI thread. The child process is waited for
    with the GIL released.
    """
    import platform
    import threading
    threading.Thread(target=platform.uname, name='foammesh-platform',
                     daemon=True).start()


def _early_settings(properties):
    """The application settings, read before the heavy modules are imported."""
    from foammesh.settings.app_settings import appSettings
    appSettings.load(properties.name)
    return appSettings


def _serve_events(application):
    """Serve the event queue between two start-up imports (the splash)."""
    process = getattr(application, 'processEvents', None)
    if process is not None:
        process()


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


def disable_combo_animation():
    """Plan 45 K. Qt rolls a combo's list open with an animation, and a
    tooltip appearing over it cancels the roll, so the list never opens (seen
    live in unpinned runs). The effect is off for the whole application."""
    from PySide6.QtCore import Qt
    QApplication.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, False)


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
    # Plan 45 start-up: the cyclic collector's start-up threshold, before the
    # heavy imports rather than after the window (MEASURED live 18 October
    # 2026: 407 automatic collections, ~0.06 s, while the window was built).
    from foammesh.support import gc_policy
    gc_policy.raise_threshold()
    _describe_platform_aside()

    # Plan 45 splash: the settings are read here without the application's
    # heavy modules (``app.setupApplication`` below reads the same file into
    # the same object once they are imported).
    settings = _early_settings(meshAppProperties)
    # DP-550/551. Before anything native can fail: a fatal fault, a VTK
    # warning and the way this process ends are all written under the
    # application's log directory, and a session that died last time is named.
    dead_sessions = lifecycle.install(settings.settingsPath(),
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
    # GPUPREF: Settings > Use the dedicated GPU when available. Before Qt
    # reads OpenGL; a failed or software start falls back by itself.
    try:
        from foammesh.rendering import gpu_preference
        gpu_preference.startup(settings, lifecycle.log_directory())
    except Exception:                                       # noqa: BLE001
        logger.warning('GPU preference could not be applied', exc_info=True)
    # Plan 45 start-up: the OpenGL driver wakes beside the imports (~0.14 s
    # of the first render window's construction, paid on the GUI thread).
    from foammesh.rendering import gl_driver_warm
    gl_driver_warm.start(safe_mode=graphics.active)
    if crash_helper.start(lifecycle.log_directory()) is None:
        logger.info('No crash helper this session; a native crash leaves '
                    'faulthandler.log only.')
    qt_messages.install()
    os.environ['LC_NUMERIC'] = 'C'
    _claim_windows_taskbar_identity()
    # Plan 40 UXE3: UI scale and the HiDPI rounding policy can only be set
    # before the QApplication exists.
    from foammesh.view.theming import appearance
    appearance.prepare_application(appearance.Appearance.from_settings(settings))
    application = QApplication(sys.argv)
    disable_combo_animation()
    application.setWindowIcon(meshAppProperties.icon())
    application.lastWindowClosed.connect(
        lambda: lifecycle.record('Qt lastWindowClosed'))
    application.aboutToQuit.connect(
        lambda: lifecycle.record('Qt aboutToQuit'))
    # Plan 45 splash: on screen and painted before the heavy imports. It is
    # closed once the window is shown, before the crash note, and on any
    # failure from here on.
    splash = [startup_splash.show(
        application, meshAppProperties,
        getattr(settings, 'getTheme', lambda: None)())]
    # Plan 45 start-up: Qt's full scan of the installed fonts (~0.27 s, paid
    # inside the window's construction) runs beside the imports instead.
    from foammesh.view.theming import font_population
    font_population.start()

    def close_splash():
        startup_splash.close(splash[0])
        splash[0] = None

    # Plan 46: the --api-* options are taken out before the case path is read.
    from foammesh.support import local_api
    try:
        api_request, arguments = local_api.parse_arguments(sys.argv[1:])
    except ValueError as error:
        logger.warning('Local API not started: %s', error)
        api_request = local_api.Request()
        arguments = [item for item in sys.argv[1:] if not item.startswith('--api-')]

    try:
        import_application(lambda: _serve_events(application))
        initial_case = startup_case_path(arguments)
        app.setupApplication(meshAppProperties)

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
        # Plan 45 shut-down: qasync's own executor made close() wait for every
        # queued and running background job (WSL probes held the exit 4-65 s).
        loop_executor.install(loop)
        asyncio.set_event_loop(loop)

        loop.set_exception_handler(loop_exception)

        app.applyLanguage()

        # Plan 45 start-up: the window is built in stages with the event queue
        # served between them (one piece held the GUI thread ~2.5 s).
        # Plan 45 start-up: the native-sibling rule waits until the window is
        # built (natives moved by the docking were made twice).
        from foammesh.view.main_window import native_siblings
        gl_driver_warm.wait()
        with native_siblings.held_off() as built:
            app.window = MainWindow(staged=True)
            built(app.window)
        # CR1: the GUI thread's heartbeat, and a status-bar note after a stall.
        watchdog.start(lifecycle.watchdog_log_path(), app.window)
        if dead_sessions:
            # CR0 step 6: a non-modal banner naming the session that died.
            # Plan 45 splash: the splash is gone before the note is shown.
            close_splash()
            app.window.showPreviousCrash(dead_sessions)
        # Plan 35 CR3: freeze the start-up heap and steer the cyclic collector
        # onto the GUI thread before any project object exists. Plan 45: with
        # no case to open, the freeze waits for the window's first paint.
        gc_policy.startup_complete(
            after_paint=app.window.windowHandle() if initial_case is None
            else None)
    except BaseException:
        close_splash()
        raise

    async def bootstrap():
        # First paint and event processing happen before environment probes,
        # consent, or optional case inspection can delay the shell.
        try:
            await app.window.start(initial_case)
        finally:
            # Plan 45 splash: the window is shown; the splash goes before
            # anything the start may ask of the user.
            close_splash()
        await asyncio.sleep(0)
        # DP-2817: the modules pages import on first use are imported now, on
        # a worker, so a cold .pyc read never holds the GUI thread (measured
        # 11.15 s and 3.7 s holds in importlib's get_data).
        try:
            from foammesh.support import import_warmup
            import_warmup.start()
        except Exception:                                   # noqa: BLE001
            logger.debug('the import warm-up did not start', exc_info=True)
        # Plan 46: off unless --api-attach or Preferences > Connections.
        # A failure here is logged; it never stops the window.
        try:
            from foammesh.view.main_window import local_api_indicator
            api = local_api.install(app.facade, app.settings, api_request)
            local_api_indicator.install(app.window, api)
            if local_api.should_start(app.settings, api_request):
                if not await api.start():
                    app.window.statusBar().showMessage(QApplication.translate(
                        'main', f'The local API could not start: {api.problem}'), 10000)
        except Exception:                                   # noqa: BLE001
            logger.warning('The local API could not be set up', exc_info=True)
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
    if local_api.current() is not None and local_api.current().active:
        local_api.current().remove_discovery_file()
    # Plan 45 shut-down: the viewport GL contexts go with the process; the
    # driver's context delete held the exit 1-2 s on about one exit in four.
    rendering_widget.leaveContextsToProcessExit()
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
