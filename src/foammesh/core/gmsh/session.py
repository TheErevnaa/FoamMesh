"""Client of the resident Gmsh session (Plan 41 GN1).

``GmshSession`` starts ``session_v1.py`` in the WSL distribution once per open
project and speaks JSON lines to it (see that file for the protocol).  Every
operation that changes the model is appended to the op log by the session; a
crash, a cancel or a reopen is recovered by :meth:`GmshSession.rebuild`, which
starts a fresh process and replays the log.  The window never dies with the
session: a dead session raises :class:`GmshSessionCrashed` from the call that
was waiting and leaves the object ready to rebuild.

The client is thread safe in the way the GUI needs it: one thread calls
operations (one at a time, serialised by a lock), any other thread may call
:meth:`cancel` or :meth:`kill`, and Gmsh's console is delivered to
``on_event`` as it is written.

The session's first answer carries the runtime identity (version, threads,
capabilities).  That answer is what the runtime probe uses, so the machine is
probed once, when the session starts, and not by a second process
(:func:`probe_from_session`).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from foammesh.core.gmsh.launch_profiles import GmshLaunchProfile

SESSION_LOG_NAME = 'session.log.jsonl'
EXIT_ABORTED = 75
EXIT_TIMEOUT = 76
EXIT_MEMORY = 77


LEVELS = ('debug', 'info', 'warning', 'error')


def console_level(line: str) -> str:
    """The level Gmsh's own prefix gives a console line ("Info    : ...")."""
    head = line.split(':', 1)[0].strip().lower()
    if head in ('warning',):
        return 'warning'
    if head in ('error', 'fatal'):
        return 'error'
    if head == 'debug':
        return 'debug'
    return 'info'


class GmshConsole:
    """Gmsh's message console: kept as it streams, filtered, saved.

    Use as ``GmshSession(..., on_event=console.feed)``; a listener may be
    added with :meth:`subscribe` to see each new line as it arrives.
    """

    def __init__(self, limit: int = 200_000):
        self._lines: list[tuple[str, str]] = []
        self._limit = limit
        self._lock = threading.Lock()
        self._listeners: list[Callable[[str, str], None]] = []

    def subscribe(self, listener: Callable[[str, str], None]) -> None:
        self._listeners.append(listener)

    def feed(self, event: dict) -> None:
        if event.get('event') != 'log':
            return
        for line in event.get('lines', ()):
            level = console_level(line)
            with self._lock:
                self._lines.append((level, line))
                if len(self._lines) > self._limit:
                    del self._lines[:len(self._lines) - self._limit]
            for listener in self._listeners:
                listener(level, line)

    def lines(self, minimum: str = 'debug') -> list[str]:
        floor = LEVELS.index(minimum)
        with self._lock:
            return [line for level, line in self._lines
                    if LEVELS.index(level) >= floor]

    def save(self, path: str | Path, minimum: str = 'debug') -> int:
        text = self.lines(minimum)
        Path(path).write_text(chr(10).join(text) + (chr(10) if text else ''),
                              encoding='utf-8')
        return len(text)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


class GmshSessionError(RuntimeError):
    """An operation was refused by Gmsh; the session itself is alive."""

    def __init__(self, op: str, message: str, log=()):
        super().__init__(f'{op}: {message}')
        self.op = op
        self.log = tuple(log)


class GmshSessionCrashed(RuntimeError):
    """The session process ended while (or before) an operation ran."""

    def __init__(self, reason: str, *, returncode=None, op: str = ''):
        super().__init__(reason)
        self.reason = reason
        self.returncode = returncode
        self.op = op


def session_script_path() -> Path:
    from resources import resource

    return Path(resource.file('gmsh/session_v1.py')).resolve()


def session_log_path(case_path: str | Path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / SESSION_LOG_NAME


def read_op_log(case_path: str | Path) -> list[dict]:
    path = session_log_path(case_path)
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and 'op' in value:
            entries.append(value)
    return entries


class GmshSession:
    """One resident Gmsh process and its op log."""

    def __init__(self, profile: GmshLaunchProfile, case_path: str | Path, *,
                 on_event: Callable[[dict], None] | None = None,
                 job_sink=None, script: str | Path | None = None,
                 argv: tuple[str, ...] | None = None,
                 memory_mb: float | None = None,
                 start_timeout: float = 120.0):
        self.profile = profile
        self.case_path = Path(case_path)
        self.on_event = on_event
        self.job_sink = job_sink
        self.memory_mb = memory_mb
        self.start_timeout = float(start_timeout)
        self._script = Path(script) if script else None
        self._argv = argv
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._call_lock = threading.RLock()
        self._state = threading.Condition()
        self._responses: dict[int, dict] = {}
        self._ready: dict | None = None
        self._ended: tuple[str, int | None] | None = None
        self._next_id = 0
        self._current_op = ''
        self._cancelled = False
        self.hello: dict = {}
        self.generation = 0
        self._fatal = None

    # -- launch -------------------------------------------------------- #

    @property
    def log_path(self) -> Path:
        return session_log_path(self.case_path)

    def runtime_path(self, host_path: str | Path) -> str:
        return self.profile.translate_host_path(host_path)

    def launch_argv(self) -> tuple[str, ...]:
        if self._argv is not None:
            return self._argv
        script = self.runtime_path(self._script or session_script_path())
        command = [self.profile.python, '-u', script,
                   '--log', self.runtime_path(self.log_path)]
        if self.memory_mb:
            command += ['--memory-mb', str(self.memory_mb)]
        return (*self.profile._wsl_prefix(), *command)

    @property
    def alive(self) -> bool:
        process = self._process
        return (process is not None and process.poll() is None
                and self._ended is None)

    @property
    def busy(self) -> bool:
        return bool(self._current_op)

    @property
    def pid(self) -> int | None:
        return self.hello.get('pid')

    def start(self) -> dict:
        """Start the process and wait for its identity answer."""
        with self._call_lock:
            if self.alive:
                return self.hello
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._responses.clear()
            self._ready = None
            self._ended = None
            self._cancelled = False
            self.generation += 1
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            self._process = subprocess.Popen(
                list(self.launch_argv()), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=flags)
            generation = self.generation
            self._reader = threading.Thread(
                target=self._pump, args=(self._process, generation),
                name='gmsh-session', daemon=True)
            self._reader.start()
            threading.Thread(
                target=self._drain_stderr, args=(self._process,),
                daemon=True).start()
            deadline = time.monotonic() + self.start_timeout
            with self._state:
                while self._ready is None and self._ended is None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._state.wait(min(left, 0.5))
            if self._ready is None:
                reason = (self._ended[0] if self._ended else
                          f'the Gmsh session did not answer within '
                          f'{self.start_timeout:g}s')
                code = self._ended[1] if self._ended else None
                self._hard_kill()
                raise GmshSessionCrashed(reason, returncode=code)
            self.hello = dict(self._ready)
            _register_hello(self.profile, self.hello)
            return self.hello

    def _pump(self, process, generation: int) -> None:
        stream = process.stdout
        try:
            for raw in iter(stream.readline, b''):
                text = raw.decode('utf-8', 'replace').strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    self._emit({'event': 'log', 'lines': [text]})
                    continue
                if message.get('event') == 'ready':
                    with self._state:
                        self._ready = message
                        self._state.notify_all()
                elif 'event' in message:
                    if message['event'] == 'fatal':
                        with self._state:
                            self._fatal = message
                    self._emit(message)
                elif message.get('id') is not None:
                    with self._state:
                        self._responses[int(message['id'])] = message
                        self._state.notify_all()
        finally:
            code = process.wait()
            reason = _exit_reason(code, getattr(self, '_fatal', None),
                                  self._cancelled)
            with self._state:
                if generation == self.generation:
                    self._ended = (reason, code)
                    _forget_hello(self.profile)
                self._state.notify_all()

    def _drain_stderr(self, process) -> None:
        for raw in iter(process.stderr.readline, b''):
            text = raw.decode('utf-8', 'replace').rstrip()
            if text:
                self._emit({'event': 'log', 'lines': [text], 'stream': 'err'})

    def _emit(self, event: dict) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # a listener must never break the pump
                pass

    # -- calls --------------------------------------------------------- #

    def call(self, op: str, *args, timeout: float | None = None, **kwargs):
        """Run one operation; return its result."""
        response = self._send(op, list(args), kwargs, timeout)
        if not response.get('ok'):
            raise GmshSessionError(op, response.get('error', 'failed'),
                                   response.get('log') or ())
        return response.get('result')

    def _send(self, op, args, kwargs, timeout, *, replay=False) -> dict:
        with self._call_lock:
            if not self.alive:
                raise GmshSessionCrashed(
                    self._ended[0] if self._ended else 'the Gmsh session is '
                    'not running', returncode=(
                        self._ended[1] if self._ended else None), op=op)
            self._next_id += 1
            identifier = self._next_id
            request = {'id': identifier, 'op': op, 'args': args,
                       'kwargs': kwargs}
            if timeout:
                request['timeout'] = float(timeout)
            if replay:
                request['replay'] = True
            self._current_op = op
            started = time.monotonic()
            self._sink('started', op, args=args)
            try:
                self._process.stdin.write(
                    (json.dumps(request, separators=(',', ':')) + '\n'
                     ).encode('utf-8'))
                self._process.stdin.flush()
            except (OSError, ValueError):
                self._current_op = ''
                raise GmshSessionCrashed('the Gmsh session closed its input',
                                         op=op)
            with self._state:
                while identifier not in self._responses:
                    if self._ended is not None:
                        break
                    self._state.wait(0.25)
                response = self._responses.pop(identifier, None)
            self._current_op = ''
            if response is None:
                reason, code = self._ended or ('the Gmsh session ended', None)
                self._sink('crashed', op, reason=reason,
                           seconds=time.monotonic() - started)
                raise GmshSessionCrashed(reason, returncode=code, op=op)
            self._sink('finished' if response.get('ok') else 'failed', op,
                       seconds=time.monotonic() - started,
                       error=response.get('error'))
            return response

    def _sink(self, state: str, op: str, **fields) -> None:
        if self.job_sink is not None:
            try:
                self.job_sink(state, op, generation=self.generation,
                              **fields)
            except Exception:
                pass

    # -- named operations the pages use --------------------------------- #

    def import_geometry(self, host_path: str | Path, **kwargs):
        return self.call('session.import', self.runtime_path(host_path),
                         **kwargs)

    def generate(self, dim: int = 3, *, timeout: float | None = None):
        return self.call('mesh.generate', int(dim), timeout=timeout)

    def state(self) -> dict:
        return self.call('session.state')

    def analyse(self, dim: int = 3, hiding: float | None = None) -> dict:
        """Run Gmsh's AnalyseMeshQuality plugin (Plan 41 GN14)."""
        kwargs = {'dim': dim}
        if hiding is not None:
            kwargs['hiding'] = hiding
        return self.call('session.analyse', **kwargs)

    def plugin_run(self, name: str, directory: str | Path,
                   numbers: dict | None = None, *, edits: bool = False,
                   view: int | None = None, other_view: int | None = None,
                   timeout: float | None = None) -> dict:
        """Run Gmsh plugin *name*; its views are written into *directory*.

        Plan 41 GN20. ``edits`` names a run that changes the mesh (it is then
        logged and replays after a crash).  Every view and entity the plugin
        made comes back with the host path of its ``.vtu``.
        """
        kwargs = {'numbers': dict(numbers or {}),
                  'directory': self.runtime_path(directory)}
        if view is not None:
            kwargs['view'] = int(view)
        if other_view is not None:
            kwargs['other_view'] = int(other_view)
        answer = self.call('session.plugin_edit' if edits
                           else 'session.plugin_run', name,
                           timeout=timeout, **kwargs)
        root = Path(directory)
        for item in answer.get('views') or ():
            if item.get('path'):
                item['host_path'] = str(root / Path(item['path']).name)
        return answer

    def plugin_views(self) -> list:
        return self.call('session.plugin_views')

    def plugin_remove(self, tag: int | None = None) -> dict:
        return self.call('session.plugin_remove', tag=tag)

    def handover(self, directory: str | Path, **kwargs) -> dict:
        """Write ``mesh.vtu``/``geometry.vtp`` into *directory* (host path)."""
        written = self.call('session.handover', self.runtime_path(directory),
                            **kwargs)
        root = Path(directory)
        for item in written.values():
            if item.get('path'):
                item['host_path'] = str(root / Path(item['path']).name)
        return written

    def reference_surface(self, directory: str | Path, **kwargs) -> dict:
        """Plan 42 SG2. The model's CAD surface, finely tessellated, written
        as ``reference.vtp`` into *directory* (host path); the mesh in the
        session is not touched."""
        written = self.call('session.reference_surface',
                            self.runtime_path(directory), **kwargs)
        if written.get('path'):
            written['host_path'] = str(Path(directory)
                                       / Path(written['path']).name)
        return written

    def export_facts(self) -> dict:
        """Order, element families, partitions and counts of the mesh in hand."""
        return self.call('session.export_facts')

    def export_file(self, host_path: str | Path, options: dict | None = None,
                    *, kind: str = 'mesh', outputs=(),
                    timeout: float | None = None) -> dict:
        """Write the model to *host_path* with the given Gmsh options (GN15).

        The options are set for this one write and put back.  ``files`` of the
        answer lists every file the write created, partition files included,
        each with its host path.
        """
        result = self.call(
            'session.export', self.runtime_path(host_path),
            options=dict(options or {}), kind=kind,
            outputs=[self.runtime_path(item) for item in outputs],
            timeout=timeout)
        root = Path(host_path).parent
        for item in result.get('files', ()):
            item['host_path'] = str(root / Path(item['path']).name)
        return result

    def write_mesh(self, host_path: str | Path) -> dict:
        return self.call('session.write', self.runtime_path(host_path))

    # -- cancel, crash, recovery ----------------------------------------- #

    def cancel(self) -> None:
        """Stop what is running.  The log is intact; call :meth:`rebuild`."""
        self._cancelled = True
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.write(b'{"op":"session.abort"}\n')
            process.stdin.flush()
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + 3.0
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            self.kill()

    def kill(self) -> None:
        """End the session without asking, in the runtime and on the host."""
        pid = self.hello.get('pid')
        if pid:
            try:
                subprocess.run(
                    [*self.profile._wsl_prefix(), 'kill', '-KILL', str(pid)],
                    capture_output=True, timeout=15, check=False)
            except (OSError, subprocess.SubprocessError):
                pass
        self._hard_kill()

    def _hard_kill(self) -> None:
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def rebuild(self) -> dict:
        """Start a fresh process and replay the op log; return the state."""
        with self._call_lock:
            self.kill()
            thread = self._reader
            if thread is not None:
                thread.join(timeout=5)
            self.start()
            replayed = self.call('session.replay',
                                 self.runtime_path(self.log_path))
            return {'replayed': replayed.get('replayed', 0),
                    'state': self.state(), 'hello': self.hello}

    def close(self) -> None:
        with self._call_lock:
            if self.alive:
                try:
                    self._send('session.quit', [], {}, 10)
                except GmshSessionCrashed:
                    pass
            process = self._process
            if process is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.kill()
            _forget_hello(self.profile)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()


def _exit_reason(code, fatal, cancelled) -> str:
    if cancelled or code == EXIT_ABORTED:
        return 'the Gmsh session was cancelled'
    if code == EXIT_TIMEOUT:
        return 'an operation passed its time limit and the session was ended'
    if code == EXIT_MEMORY:
        return ('an operation passed the memory limit and the session was '
                'ended')
    if code in (-9, 137):
        return 'the Gmsh session was killed'
    if code in (-11, 139):
        return 'the Gmsh session crashed (segmentation fault)'
    return f'the Gmsh session ended with exit code {code}'


# -- one probe: the session's first answer ------------------------------------ #

_hellos: dict[str, dict] = {}
_hello_lock = threading.Lock()


def _register_hello(profile: GmshLaunchProfile, hello: dict) -> None:
    with _hello_lock:
        _hellos[profile.fingerprint] = dict(hello)


def _forget_hello(profile: GmshLaunchProfile) -> None:
    with _hello_lock:
        _hellos.pop(profile.fingerprint, None)


def session_hello(profile: GmshLaunchProfile) -> dict | None:
    """The identity answer of the live session for *profile*, if one is up."""
    with _hello_lock:
        value = _hellos.get(profile.fingerprint)
    return dict(value) if value else None


def session_job_sink(manager, *, case_path):
    """A ``job_sink`` that gives every session operation a job result.

    Each operation is recorded with the JobManager as a finished job (name
    ``gmsh.session.<op>``), so the Runs list, the durable job log and the
    event bus see it exactly as they see a batch run.
    """
    def sink(state, op, *, generation=0, seconds=0.0, error=None, **_):
        if state in ('started',):
            manager.session_event('gmsh.session.started', op=op,
                                  generation=generation)
            return
        manager.record_session_operation(
            name=f'gmsh.session.{op}', state=state, seconds=seconds,
            error=error, cwd=case_path, generation=generation)

    return sink


# -- the idle session: probing and starting are one launch ----------------------- #

_idle: dict[str, 'GmshSession'] = {}


def start_idle_session(profile: GmshLaunchProfile, *,
                       start_timeout: float = 120.0) -> 'GmshSession':
    """Start the session for *profile* that a project will later claim.

    Plan 41 GN1 retires the separate cold probe: the first launch of the
    runtime is the session itself, its first answer is the probe, and the
    process stays resident until :func:`claim_idle_session` hands it to a
    project (or the application exits).
    """
    import atexit
    import tempfile

    with _hello_lock:
        existing = _idle.get(profile.fingerprint)
    if existing is not None and existing.alive:
        return existing
    scratch = Path(tempfile.gettempdir()) / f'foammesh-gmsh-idle-{os.getpid()}'
    session = GmshSession(profile, scratch, start_timeout=start_timeout)
    session.start()
    with _hello_lock:
        _idle[profile.fingerprint] = session
    atexit.register(session.close)
    return session


def claim_idle_session(profile: GmshLaunchProfile, case_path: str | Path,
                       **options) -> 'GmshSession | None':
    """Give the probe's resident session to the project at *case_path*."""
    with _hello_lock:
        session = _idle.pop(profile.fingerprint, None)
    if session is None or not session.alive:
        return None
    session.case_path = Path(case_path)
    for key, value in options.items():
        setattr(session, key, value)
    session.log_path.parent.mkdir(parents=True, exist_ok=True)
    session.call('session.set_log', session.runtime_path(session.log_path))
    return session
