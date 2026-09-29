"""PyInstaller runtime hook: diagnostics before anything else runs.

Plan 35 CR10 (with CR0 step 2). The packaged build is `console=False`, so
`sys.stderr` is None and anything native code writes to fd 2 or to the Win32
standard handles goes nowhere. Worse, a Qt or VTK DLL that looks the handle
up while loading keeps the NUL it saw. So the redirect has to happen before
the first of those DLLs loads, which in a frozen build means here: PyInstaller
runs custom runtime hooks before its own (the PySide6 one included) and before
`main.py`.

This hook therefore does two things and nothing else:

1. `foammesh.support.native_capture.install(~/.FoamMesh/logs)` -- the fd and
   handle redirect CR0 owns. If that module is not in the build yet, the
   hook carries on without it: a missing diagnostic must never stop a start.
2. The root log, `~/.FoamMesh/logs/foammesh.log` (5 x 5 MB), attached here so
   it holds a record even when `main.py` fails to import. The handler is named
   `ROOT_LOG_HANDLER` so the application can find it and not attach a second
   one to the same file.

`--crash-helper` and `--worker` processes are the same exe (Plan 35 D2). They
get the native capture (a per-pid file) but not the rotating root log, because
several processes rotating one file on Windows fail each other's renames.

Nothing in here may raise.
"""


def _foammesh_early_diagnostics():
    import sys
    from pathlib import Path

    ROOT_LOG_HANDLER = 'foammesh.rootlog'
    problems = []

    try:
        log_dir = Path.home() / '.FoamMesh' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return

    try:
        from foammesh.support import native_capture
    except ImportError:
        native_capture = None
    except Exception as error:                      # a broken module, not a missing one
        native_capture = None
        problems.append(f'native capture failed to import: {error!r}')
    if native_capture is not None:
        try:
            native_capture.install(log_dir)
        except Exception as error:
            problems.append(f'native capture failed to install: {error!r}')

    arguments = sys.argv[1:]
    if '--crash-helper' in arguments or '--worker' in arguments:
        return

    try:
        import logging
        import logging.handlers
        import os

        root = logging.getLogger()
        if any(handler.get_name() == ROOT_LOG_HANDLER for handler in root.handlers):
            return
        handler = logging.handlers.RotatingFileHandler(
            log_dir / 'foammesh.log', maxBytes=5 * 1024 * 1024, backupCount=5,
            encoding='utf-8')
        handler.set_name(ROOT_LOG_HANDLER)
        handler.setFormatter(logging.Formatter(
            '[%(asctime)s][%(process)d][%(threadName)s][%(name)s] %(levelname)s ==> %(message)s'))
        root.addHandler(handler)
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        startup = logging.getLogger('foammesh.startup')
        startup.info('process start pid=%d exe=%s argv=%r native_capture=%s',
                     os.getpid(), sys.executable, arguments,
                     'failed' if problems
                     else 'absent' if native_capture is None else 'installed')
        for problem in problems:
            startup.warning(problem)
    except Exception:
        return


_foammesh_early_diagnostics()
del _foammesh_early_diagnostics
