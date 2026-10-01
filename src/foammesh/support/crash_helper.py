#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR0/CR1: the process that writes the GUI's dump from outside it.

``FoamMesh.exe --crash-helper <gui-pid>`` (source builds:
``python -m foammesh.support.crash_helper <gui-pid>``) is a small process with
no Qt and no VTK. The GUI starts it at launch (:func:`start`). It:

* opens the GUI process for querying and reading, and waits on the process
  handle and on the *request* event the GUI's native unhandled-exception
  filter signals (``support.crash_filter``);
* on a request, calls ``MiniDumpWriteDump`` on the GUI with the exception the
  filter handed over, records it in ``lifecycle.log``, and sets *done*;
* every 500 ms reads the heartbeat block (``support.heartbeat``). When the GUI
  thread has not ticked for :data:`HANG_DUMP_SECONDS` it writes a *hang* dump
  and records the stall with the last operation; it kills nothing. At
  :data:`HANG_PROMPT_SECONDS` it shows its own message box, "FoamMesh has not
  responded for 60 s", offering to keep waiting or to save a report and
  close -- the GUI is terminated only on the user's click;
* when the GUI ends without having marked a clean exit, records the exit code
  (named when it is a known NTSTATUS) in ``lifecycle.log`` and in the dead
  session's ``session-<pid>.json``, so the next start's banner can say how
  the session ended.

Dumps go to ``%LOCALAPPDATA%\\FoamMesh\\CrashDumps`` (the folder the
installer's WER LocalDumps key names too), newest :data:`KEEP_DUMPS` kept.

:func:`set_last_op` is ``support.heartbeat.set_last_op``: what the GUI is
doing now (a facade operation name, ``render:<scene>``), read by this helper
without asking the frozen process.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from foammesh.support import heartbeat
from foammesh.support.heartbeat import set_last_op  # noqa: F401  (contract)

HANG_DUMP_SECONDS = 10.0
HANG_PROMPT_SECONDS = 60.0
POLL_SECONDS = 0.5
KEEP_DUMPS = 10

LIFECYCLE_LOG = 'lifecycle.log'
SESSION_PATTERN = 'session-{pid}.json'

PROCESS_TERMINATE = 0x0001
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102

#: MiniDumpWithIndirectlyReferencedMemory | WithUnloadedModules |
#: WithThreadInfo: small, and enough for native stacks with locals' targets.
DUMP_TYPE = 0x40 | 0x20 | 0x1000

#: Exit codes worth a name in the report.
EXIT_CODES = {
    0xC0000005: 'access violation',
    0xC0000409: 'stack buffer overrun / fail-fast',
    0xC00000FD: 'stack overflow',
    0xC0000017: 'out of memory',
    0xC0000374: 'heap corruption',
    0xC000001D: 'illegal instruction',
    0xC0000094: 'integer divide by zero',
    0x80000003: 'breakpoint',
    0xE06D7363: 'unhandled C++ exception',
    0x40010004: 'terminated by the debugger',
    0xC000013A: 'closed by Ctrl+C / console close',
    3: 'abort() (exit code 3)',
    1: 'exit code 1 (often a kill from outside, e.g. Task Manager)',
}


def request_event_name(pid: int) -> str:
    return f'Local\\FoamMesh.crash.{int(pid)}.request'


def done_event_name(pid: int) -> str:
    return f'Local\\FoamMesh.crash.{int(pid)}.done'


def default_dump_directory() -> Path:
    base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    return Path(base) / 'FoamMesh' / 'CrashDumps'


def describe_exit_code(code: int) -> str:
    code &= 0xFFFFFFFF
    name = EXIT_CODES.get(code)
    # A hex prefix, not a multiplication: kept apart from the digits (DP-179).
    text = '0x' + format(code, '08X') if code > 0xFFFF else str(code)
    return f'{text} ({name})' if name else text


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec='milliseconds')


# --------------------------------------------------------------------------
# The helper process
# --------------------------------------------------------------------------

class _Win32:
    """The handful of kernel32/dbghelp/user32 calls the helper makes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenEventW.restype = wintypes.HANDLE
        k.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k.SetEvent.argtypes = (wintypes.HANDLE,)
        k.WaitForMultipleObjects.argtypes = (
            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
            wintypes.DWORD)
        k.WaitForMultipleObjects.restype = wintypes.DWORD
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = (wintypes.HANDLE,
                                         ctypes.POINTER(wintypes.DWORD))
        k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel32 = k
        self.wintypes = wintypes
        self._dbghelp = None

    def dbghelp(self):
        if self._dbghelp is None:
            ctypes = self.ctypes
            wintypes = self.wintypes
            d = ctypes.WinDLL('dbghelp', use_last_error=True)
            d.MiniDumpWriteDump.argtypes = (
                wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE,
                wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_void_p)
            d.MiniDumpWriteDump.restype = wintypes.BOOL
            self._dbghelp = d
        return self._dbghelp


def _exception_information(ctypes, thread_id: int, pointers: int):
    class MinidumpExceptionInformation(ctypes.Structure):
        _pack_ = 4
        _fields_ = [('ThreadId', ctypes.c_uint32),
                    ('ExceptionPointers', ctypes.c_uint64),
                    ('ClientPointers', ctypes.c_int32)]

    return MinidumpExceptionInformation(thread_id, pointers, 1)


class Helper:
    def __init__(self, pid: int, *, log_dir: Path, dump_dir: Path,
                 hang_dump_after: float = HANG_DUMP_SECONDS,
                 hang_prompt_after: float | None = HANG_PROMPT_SECONDS,
                 clock=time.monotonic):
        self.pid = int(pid)
        self.log_dir = Path(log_dir)
        self.dump_dir = Path(dump_dir)
        self.hang_dump_after = hang_dump_after
        self.hang_prompt_after = hang_prompt_after
        self.clock = clock
        self.win = _Win32()
        self.process = None
        self.request = None
        self.done = None
        self.block = None
        self.dumps: list[str] = []
        self.hangs: list[dict] = []
        self._last_seq = None
        self._last_change = clock()
        self._hang_dumped = False
        self._prompt = None
        self._closed_by_user = False

    # -- records -------------------------------------------------------
    def record(self, text: str) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_dir / LIFECYCLE_LOG, 'a', encoding='utf-8') as log:
                log.write(f'[{_now()}] pid={self.pid} crash-helper '
                          f'(pid={os.getpid()}): {text}\n')
        except OSError:
            pass

    def _update_session(self, fields: dict) -> None:
        path = self.log_dir / SESSION_PATTERN.format(pid=self.pid)
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        record.update(fields)
        try:
            temporary = path.with_suffix('.helper.tmp')
            temporary.write_text(json.dumps(record, indent=1), encoding='utf-8')
            os.replace(temporary, path)
        except OSError:
            pass

    def _snapshot(self) -> dict:
        if self.block is None:
            return {}
        try:
            return heartbeat.read(self.block)
        except (ValueError, TypeError):
            return {}

    # -- set-up ----------------------------------------------------------
    def open(self) -> bool:
        k = self.win.kernel32
        self.process = k.OpenProcess(
            PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | SYNCHRONIZE
            | PROCESS_TERMINATE, False, self.pid)
        if not self.process:
            self.record('could not open the GUI process; no dumps this session')
            return False
        access = SYNCHRONIZE | EVENT_MODIFY_STATE
        self.request = k.OpenEventW(access, False, request_event_name(self.pid))
        self.done = k.OpenEventW(access, False, done_event_name(self.pid))
        self.block = heartbeat.attach(self.pid)
        return True

    # -- dumps -------------------------------------------------------------
    def _prune_dumps(self) -> None:
        try:
            dumps = sorted(self.dump_dir.glob('*.dmp'),
                           key=lambda path: path.stat().st_mtime, reverse=True)
        except OSError:
            return
        for stale in dumps[KEEP_DUMPS:]:
            try:
                stale.unlink()
            except OSError:
                pass

    def write_dump(self, kind: str, thread_id: int = 0,
                   pointers: int = 0) -> Path | None:
        ctypes = self.win.ctypes
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        path = self.dump_dir / f'FoamMesh.{self.pid}.{kind}.{stamp}.dmp'
        # DP-987. Two requests in one second (a driver thread faulting next
        # to the GUI thread) named the same file: the second truncated the
        # first, failed, and unlinked it, leaving no dump at all.
        serial = 1
        while path.exists():
            serial += 1
            path = self.dump_dir / f'FoamMesh.{self.pid}.{kind}.{stamp}-{serial}.dmp'
        try:
            import msvcrt
            with open(path, 'wb') as file:
                handle = msvcrt.get_osfhandle(file.fileno())
                information = None
                if thread_id and pointers:
                    information = ctypes.byref(
                        _exception_information(ctypes, thread_id, pointers))
                ok = self.win.dbghelp().MiniDumpWriteDump(
                    self.process, self.pid, handle, DUMP_TYPE, information,
                    None, None)
        except OSError as error:
            self.record(f'could not write a {kind} dump: {error}')
            return None
        if not ok:
            error = ctypes.get_last_error()
            self.record(f'MiniDumpWriteDump failed for the {kind} dump '
                        f'(error {error})')
            try:
                path.unlink()
            except OSError:
                pass
            return None
        self.dumps.append(str(path))
        self._prune_dumps()
        return path

    def on_request(self) -> None:
        snapshot = self._snapshot()
        path = self.write_dump('crash', snapshot.get('thread_id', 0),
                               snapshot.get('exception_pointers', 0))
        self.record(
            'the GUI raised a fatal native exception on thread {0} during '
            '"{1}"; dump {2}'.format(
                snapshot.get('thread_id'), snapshot.get('last_op') or 'none',
                path or 'not written'))
        if self.done:
            self.win.kernel32.SetEvent(self.done)

    # -- hangs ---------------------------------------------------------------
    def check_heartbeat(self) -> None:
        snapshot = self._snapshot()
        if not snapshot:
            self.block = heartbeat.attach(self.pid)
            return
        now = self.clock()
        state = snapshot['state']
        if snapshot['seq'] != self._last_seq or state != heartbeat.STATE_TICKING:
            if self._hang_dumped:
                self.record('the GUI is responding again after {0:.1f} s'.format(
                    now - self._last_change))
                self._dismiss_prompt()
            self._last_seq = snapshot['seq']
            self._last_change = now
            self._hang_dumped = False
            return
        stale = now - self._last_change
        if stale >= self.hang_dump_after and not self._hang_dumped:
            self._hang_dumped = True
            path = self.write_dump('hang')
            hang = {'at': _now(), 'seconds': round(stale, 1),
                    'last_op': snapshot['last_op'], 'dump': str(path or '')}
            self.hangs.append(hang)
            self.record(
                'the GUI thread has not responded for {0:.1f} s during "{1}"; '
                'hang dump {2}'.format(stale, snapshot['last_op'] or 'none',
                                       path or 'not written'))
        if (self.hang_prompt_after is not None
                and stale >= self.hang_prompt_after and self._prompt is None):
            self._prompt = threading.Thread(
                target=self._ask, args=(stale, snapshot['last_op']),
                name='crash-helper-prompt', daemon=True)
            self._prompt.start()

    PROMPT_TITLE = 'FoamMesh is not responding'

    def _ask(self, stale: float, last_op: str) -> None:
        import ctypes
        text = (
            'FoamMesh has not responded for {0:.0f} s{1}.\n\n'
            'A report has been saved in the logs folder.\n\n'
            'Retry: keep waiting.\n'
            'Cancel: save the report and close FoamMesh.'.format(
                stale, f' (last operation: {last_op})' if last_op else ''))
        # MB_RETRYCANCEL | MB_ICONWARNING | MB_SETFOREGROUND | MB_TOPMOST
        answer = ctypes.windll.user32.MessageBoxW(
            None, text, self.PROMPT_TITLE, 0x5 | 0x30 | 0x10000 | 0x40000)
        if self._prompt is None:        # dismissed because the GUI recovered
            return
        if answer == 2:                 # IDCANCEL
            self._closed_by_user = True
            self.record('the user chose "Save report and close" after {0:.0f} s '
                        'without a response'.format(self.clock() - self._last_change))
            self.win.kernel32.TerminateProcess(self.process, 0xDEAD)
        else:
            self._prompt = None
            self._last_change = self.clock()   # ask again after another 60 s

    def _dismiss_prompt(self) -> None:
        prompt, self._prompt = self._prompt, None
        if prompt is None:
            return
        try:
            import ctypes
            user32 = ctypes.windll.user32
            window = user32.FindWindowW(None, self.PROMPT_TITLE)
            if window:
                user32.PostMessageW(window, 0x0010, 0, 0)       # WM_CLOSE
        except Exception:                                  # noqa: BLE001
            pass

    # -- the GUI ended ---------------------------------------------------------
    def on_exit(self) -> None:
        code = self.win.wintypes.DWORD()
        self.win.kernel32.GetExitCodeProcess(self.process, self.win.ctypes.byref(code))
        snapshot = self._snapshot()
        if snapshot.get('state') == heartbeat.STATE_CLEAN_EXIT and not self._closed_by_user:
            return
        described = describe_exit_code(code.value)
        if self._closed_by_user:
            ended = ('closed from the "not responding" prompt after a hang '
                     f'during "{snapshot.get("last_op") or "none"}"')
        elif snapshot.get('state') == heartbeat.STATE_CRASHED:
            ended = f'crashed with {described}'
        else:
            ended = f'ended with exit code {described}'
        self.record(f'the GUI {ended}; last operation: '
                    f'{snapshot.get("last_op") or "none"}; dumps: '
                    f'{", ".join(self.dumps) or "none"}')
        self._update_session({
            'ended_at': _now(),
            'exit_code': code.value,
            'exit_description': described,
            'ended': ended,
            'last_op': snapshot.get('last_op') or '',
            'hang': bool(self.hangs) or self._closed_by_user,
            'hangs': self.hangs,
            'dumps': self.dumps,
        })

    # -- main loop ---------------------------------------------------------------
    def run(self) -> int:
        if not self.open():
            return 1
        k = self.win.kernel32
        HANDLE = self.win.wintypes.HANDLE
        handles = [self.process] + ([self.request] if self.request else [])
        array = (HANDLE * len(handles))(*handles)
        timeout = int(POLL_SECONDS * 1000)
        while True:
            result = k.WaitForMultipleObjects(len(handles), array, False, timeout)
            if result == WAIT_OBJECT_0:
                self.on_exit()
                return 0
            if result == WAIT_OBJECT_0 + 1:
                self.on_request()
                continue
            if result == WAIT_TIMEOUT:
                self.check_heartbeat()
                continue
            self.record(f'waiting on the GUI failed ({result}); helper exits')
            return 1


def main(argv=None) -> int:
    """``--crash-helper <pid> [--log-dir D] [--dump-dir D] [--hang-dump S]
    [--hang-prompt S | --no-prompt]``; ``argv`` excludes the program name."""
    import argparse

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == '--crash-helper':
        arguments = arguments[1:]
    parser = argparse.ArgumentParser(prog='FoamMesh --crash-helper')
    parser.add_argument('pid', type=int)
    parser.add_argument('--log-dir', type=Path, default=None)
    parser.add_argument('--dump-dir', type=Path, default=None)
    parser.add_argument('--hang-dump', type=float, default=HANG_DUMP_SECONDS)
    parser.add_argument('--hang-prompt', type=float, default=HANG_PROMPT_SECONDS)
    parser.add_argument('--no-prompt', action='store_true')
    options = parser.parse_args(arguments)
    if sys.platform != 'win32':
        return 2
    from foammesh.support.native_capture import default_log_directory
    helper = Helper(
        options.pid, log_dir=options.log_dir or default_log_directory(),
        dump_dir=options.dump_dir or default_dump_directory(),
        hang_dump_after=options.hang_dump,
        hang_prompt_after=None if options.no_prompt else options.hang_prompt)
    return helper.run()


# --------------------------------------------------------------------------
# The GUI side
# --------------------------------------------------------------------------

_gui: dict = {'process': None, 'events': None, 'reported_dead': False}


def helper_command(pid: int) -> list[str]:
    """How to start the helper for ``pid`` in this build."""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--crash-helper', str(pid)]
    return [sys.executable, '-m', 'foammesh.support.crash_helper', str(pid)]


def _create_events(pid: int):
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateEventW.restype = wintypes.HANDLE
    k.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                               wintypes.LPCWSTR)
    request = k.CreateEventW(None, False, False, request_event_name(pid))
    done = k.CreateEventW(None, True, False, done_event_name(pid))
    return (request, done) if request and done else None


def start(log_dir: Path | None = None, *, dump_dir: Path | None = None,
          extra_arguments=(), install_filter: bool = True):
    """Launch the helper for this process; the ``Popen`` or ``None``.

    Creates the heartbeat block and the two events first, so the helper
    finds them, and then installs the native unhandled-exception filter.
    Never raises: without a helper the GUI still runs, with thinner evidence.
    """
    if _gui['process'] is not None:
        return _gui['process']
    if sys.platform != 'win32':
        return None
    pid = os.getpid()
    if heartbeat.create(pid) is None:
        return None
    try:
        events = _create_events(pid)
    except Exception:                                      # noqa: BLE001
        events = None
    if events is None:
        return None
    command = helper_command(pid)
    if log_dir is not None:
        command += ['--log-dir', str(log_dir)]
    if dump_dir is not None:
        command += ['--dump-dir', str(dump_dir)]
    command += list(extra_arguments)
    environment = dict(os.environ)
    if not getattr(sys, 'frozen', False):
        source = str(Path(__file__).resolve().parents[2])
        environment['PYTHONPATH'] = os.pathsep.join(
            filter(None, (source, environment.get('PYTHONPATH'))))
    try:
        process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=environment, close_fds=True,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        return None
    _gui['process'] = process
    _gui['events'] = events
    if install_filter:
        from foammesh.support import crash_filter
        crash_filter.install(events[0], events[1], int(process._handle))
    return process


def helper_alive() -> bool | None:
    """``None`` when no helper was started; otherwise whether it still runs."""
    process = _gui['process']
    if process is None:
        return None
    return process.poll() is None


def check_helper(logger) -> None:
    """Log once if the helper died; the GUI carries on either way."""
    if helper_alive() is False and not _gui['reported_dead']:
        _gui['reported_dead'] = True
        logger.warning('The crash helper (pid %s) exited with %s; native '
                       'crashes and hangs will leave thinner evidence.',
                       _gui['process'].pid, _gui['process'].returncode)


def stop() -> None:
    """Clean exit: tell the helper, which then ends with this process."""
    heartbeat.mark_clean_exit()


if __name__ == '__main__':
    raise SystemExit(main())
