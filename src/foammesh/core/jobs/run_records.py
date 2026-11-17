"""Durable run records, the orphan sweep and the recovery gate (Plan 35 CR5).

Every job that can write into a case gets ``<case>/foammesh/run/<run_id>.json``
before its process is spawned. The record outlives the GUI, so a crash in the
middle of a run is found on the next start, and no new run starts in the case
until the old writer is confirmed dead and the mesh it may have half written
has been put back.

States (the table in plan 35 CR5 step 4)::

    launching --> running --> exited ---------> closed
        |            |          |                 ^
        |            v          v                 |
        +------> recovery_pending --> restoring --+

``exited`` is only ever written on the wrapper's ``FOAMMESH_EXIT`` line (or a
native process's own exit): it is the one acknowledgement that no writer is
left. A transport that ended without it is ``recovery_pending``.

The Linux side keeps ``<run_id>.linux`` beside the record (the wrapper writes
it), so the state is recoverable from inside WSL even if this file was lost.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

RUN_DIRECTORY = ('foammesh', 'run')
KEEP_CLOSED = 20
PROBE_TIMEOUT_SECONDS = 10
#: The watcher's TERM grace plus margin: how long a lost transport may take
#: to kill its writer before the run is declared stuck.
CONFIRM_DEATH_SECONDS = 20

STATES = ('launching', 'running', 'exited', 'recovery_pending', 'restoring', 'closed')
TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({'launching'}),
    'launching': frozenset({'running', 'exited', 'recovery_pending', 'closed'}),
    'running': frozenset({'running', 'exited', 'recovery_pending'}),
    'exited': frozenset({'restoring', 'closed'}),
    'recovery_pending': frozenset({'recovery_pending', 'restoring', 'closed'}),
    'restoring': frozenset({'restoring', 'recovery_pending', 'closed'}),
    'closed': frozenset(),
}


class RunRecordError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def run_directory(case_path) -> Path:
    return Path(case_path).joinpath(*RUN_DIRECTORY)


def _replace(source: Path, destination: Path) -> None:
    for attempt in range(8):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(0.05 * (attempt + 1))


def _write_durably(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    _replace(temporary, path)


def wsl_target(argv: Iterable[str]) -> dict:
    """``wsl.exe --distribution D --user U --exec ...`` -> its executable,
    distribution and user; ``{}`` for a native command line."""
    items = [str(item) for item in argv]
    if not items or not Path(items[0]).name.lower().startswith('wsl'):
        return {}
    target = {'executable': items[0]}
    for flag, key in (('--distribution', 'distribution'), ('-d', 'distribution'),
                      ('--user', 'user'), ('-u', 'user')):
        if flag in items:
            index = items.index(flag)
            if index + 1 < len(items):
                target[key] = items[index + 1]
    return target


class RunRecordStore:
    """The run records of one case."""

    def __init__(self, case_path):
        self.case_path = Path(case_path)
        self.root = run_directory(case_path)

    def path(self, run_id: str) -> Path:
        return self.root / f'{run_id}.json'

    def linux_path(self, run_id: str) -> Path:
        return self.root / f'{run_id}.linux'

    def create(self, run_id: str, **fields) -> dict:
        if self.path(run_id).exists():
            raise RunRecordError(f'run record already exists: {run_id}')
        record = {'schema_version': 1, 'run_id': run_id, 'state': None,
                  'case': str(self.case_path), 'started_at': _now(), 'history': []}
        record.update(fields)
        return self._write(record, 'launching')

    def load(self, run_id: str) -> dict | None:
        try:
            value = json.loads(self.path(run_id).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def transition(self, run_id: str, state: str, **fields) -> dict:
        record = self.load(run_id)
        if record is None:
            raise RunRecordError(f'run record is missing: {run_id}')
        record.update(fields)
        return self._write(record, state)

    def update(self, run_id: str, **fields) -> dict:
        """Add facts without changing state."""
        record = self.load(run_id)
        if record is None:
            raise RunRecordError(f'run record is missing: {run_id}')
        record.update(fields)
        record['updated_at'] = _now()
        _write_durably(self.path(run_id), record)
        return record

    def _write(self, record: dict, state: str) -> dict:
        current = record.get('state')
        if state not in STATES:
            raise RunRecordError(f'unknown run state: {state}')
        if state not in TRANSITIONS.get(current, frozenset()):
            raise RunRecordError(f'run {record.get("run_id")}: {current} -> {state} is not allowed')
        record['state'] = state
        record['updated_at'] = _now()
        history = list(record.get('history') or ())
        history.append({'state': state, 'at': record['updated_at']})
        record['history'] = history[-32:]
        _write_durably(self.path(str(record['run_id'])), record)
        return record

    def linux_record(self, run_id: str) -> dict:
        try:
            value = json.loads(self.linux_path(run_id).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def records(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        result = []
        for item in sorted(self.root.glob('*.json')):
            try:
                value = json.loads(item.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                # A record we cannot read is a record we cannot close: treat
                # it as open, so the gate stays shut rather than guessing.
                value = {'run_id': item.stem, 'state': 'recovery_pending',
                         'unreadable': True}
            if isinstance(value, dict) and value.get('run_id'):
                result.append(value)
        return result

    def open_records(self) -> list[dict]:
        return [record for record in self.records() if record.get('state') != 'closed']

    def prune(self, keep: int = KEEP_CLOSED) -> int:
        closed = [record for record in self.records() if record.get('state') == 'closed']
        closed.sort(key=lambda record: str(record.get('updated_at') or ''), reverse=True)
        removed = 0
        for record in closed[keep:]:
            run_id = str(record['run_id'])
            for path in (self.path(run_id), self.linux_path(run_id)):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
            removed += 1
        return removed


# -- is the writer still alive? -------------------------------------------------

#: Every process of a run carries FOAMMESH_RUN_ID in its environment (the
#: watcher alone is started without it). /proc/<pid>/environ is readable for
#: the distro user's own processes, which is exactly the set we may touch.
_FIND = ('pids=$(grep -lzxF "FOAMMESH_RUN_ID=$1" /proc/[0-9]*/environ 2>/dev/null '
         '| cut -d/ -f3 | grep -vx "$$" | tr "\\n" " "); ')
_PROBE = _FIND + 'echo "FOAMMESH_PIDS:$pids"'
_STOP = (_FIND + 'echo "FOAMMESH_PIDS:$pids"; [ -z "$pids" ] && exit 0; '
         'kill -TERM $pids 2>/dev/null; i=0; '
         'while [ "$i" -lt 10 ]; do sleep 1; i=$((i+1)); '
         + _FIND.replace('pids=', 'left=') + '[ -z "$left" ] && exit 0; done; '
         + _FIND.replace('pids=', 'left=') + '[ -n "$left" ] && kill -KILL $left 2>/dev/null; '
         'sleep 1; ' + _FIND.replace('pids=', 'left=') + 'echo "FOAMMESH_LEFT:$left"')


@dataclass(frozen=True)
class Liveness:
    """``dead``, ``alive`` or ``unreachable``, with what was seen."""

    state: str
    pids: tuple[int, ...] = ()
    detail: str = ''


def _run_wsl(target: dict, script: str, run_id: str, timeout: float) -> subprocess.CompletedProcess:
    argv = [target.get('executable') or 'wsl.exe']
    if target.get('distribution'):
        argv += ['--distribution', target['distribution']]
    if target.get('user'):
        argv += ['--user', target['user']]
    argv += ['--exec', 'bash', '-c', script, 'fm-sweep', run_id]
    kwargs = {}
    if os.name == 'nt':
        kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=timeout, **kwargs)


def _pids_from(output: bytes, prefix: str) -> tuple[int, ...] | None:
    from foammesh.core.jobs.manager import decode_output
    for line in decode_output(output or b'').splitlines():
        if line.startswith(prefix):
            return tuple(int(item) for item in line[len(prefix):].split() if item.isdigit())
    return None


def probe_writer(record: dict, *, timeout: float = PROBE_TIMEOUT_SECONDS) -> Liveness:
    """Ask the runtime whether anything of this run is still alive."""
    run_id = str(record.get('run_id') or '')
    target = record.get('wsl') or {}
    if target:
        try:
            completed = _run_wsl(target, _PROBE, run_id, timeout)
        except subprocess.TimeoutExpired:
            return Liveness('unreachable', detail=f'WSL did not answer within {timeout:g} s')
        except OSError as error:
            return Liveness('unreachable', detail=f'WSL could not be started: {error}')
        pids = _pids_from(completed.stdout, 'FOAMMESH_PIDS:')
        if pids is None:
            return Liveness('unreachable', detail=(
                f'WSL answered without a process list (rc {completed.returncode})'))
        return Liveness('alive' if pids else 'dead', pids)
    host_pid = record.get('host_pid')
    if not host_pid:
        return Liveness('dead', detail='no process was recorded for this run')
    try:
        import psutil
        process = psutil.Process(int(host_pid))
        created = record.get('host_create_time')
        if created is not None and abs(process.create_time() - float(created)) > 1.0:
            return Liveness('dead', detail='the recorded pid now belongs to another process')
        if process.status() == psutil.STATUS_ZOMBIE:
            return Liveness('dead')
        return Liveness('alive', (int(host_pid),))
    except Exception:  # noqa: BLE001 - NoSuchProcess and friends
        return Liveness('dead')


def stop_writer(record: dict, *, timeout: float = 30) -> Liveness:
    """TERM, 10 s grace, KILL -- only processes carrying this run's id."""
    run_id = str(record.get('run_id') or '')
    target = record.get('wsl') or {}
    if target:
        try:
            completed = _run_wsl(target, _STOP, run_id, timeout)
        except subprocess.TimeoutExpired:
            return Liveness('unreachable', detail='WSL did not answer the stop request')
        except OSError as error:
            return Liveness('unreachable', detail=str(error))
        left = _pids_from(completed.stdout, 'FOAMMESH_LEFT:')
        return Liveness('alive' if left else 'dead', left or ())
    host_pid = record.get('host_pid')
    if host_pid:
        from .process_control import kill_process_tree
        try:
            kill_process_tree(int(host_pid))
        except Exception:  # noqa: BLE001
            pass
    return probe_writer(record)


# -- the gate -----------------------------------------------------------------

#: Plan 50 CX3 (DP-6205). The act that clears a run the gate could not
#: resolve, by code, in the words a refusal shows.
CLEARING_ACTIONS = {
    'stop_old_run': 'stop the old run (it is still running in WSL) or wait '
                    'for it to finish, then try again',
    'wait_for_wsl': 'make WSL reachable again (open a WSL terminal, or '
                    'restart WSL), then try again',
    'free_case_files': 'putting the previous mesh back failed on a file in '
                       'use: close whatever has the case files open (a file '
                       'browser, a viewer, another FoamMesh window), then '
                       'try again',
    'remove_record': 'its run record cannot be read: delete {record}, then '
                     'try again',
    'retry': 'try again; the run is resolved before the next run or unlock '
             'starts',
}

#: DP-6487. The same acts as a refused run's sentence says them: why the run
#: was not started, and what to do. ``remove_record`` ends with the record's
#: path, which :meth:`GateVerdict.refusal` puts last.
REFUSAL_WORDS = {
    'stop_old_run': ('an earlier run is still running in WSL',
                     'Stop it or wait for it to finish, then try again.'),
    'wait_for_wsl': ('an earlier run could not be checked because WSL is '
                     'not answering',
                     'Make WSL reachable again (open a WSL terminal, or '
                     'restart WSL), then try again.'),
    'free_case_files': ('the mesh an earlier run changed could not be put '
                        'back because a case file is in use',
                        'Close whatever has the case files open, then try '
                        'again.'),
    'remove_record': ('an earlier run left a record that cannot be read',
                      'To go on, delete this record and try again.'),
    'retry': ('an earlier run has not been tidied up yet', 'Try again.'),
}


def held_run(record: dict, case_path=None) -> dict:
    """What an unresolved run record holds, and the act that clears it."""
    run_id = str(record.get('run_id') or '?')
    path = (run_directory(case_path) / f'{run_id}.json'
            if case_path is not None else Path(f'{run_id}.json'))
    if record.get('unreadable'):
        action = 'remove_record'
    elif record.get('state') == 'restoring':
        action = 'free_case_files'
    elif record.get('writer') == 'alive':
        action = 'stop_old_run'
    elif record.get('writer') == 'unreachable':
        action = 'wait_for_wsl'
    else:
        action = 'retry'
    return {'run_id': run_id,
            'operation': record.get('operation') or record.get('name'),
            'state': record.get('state'),
            'writer': record.get('writer'),
            'writer_pids': list(record.get('writer_pids') or ()),
            'ranks': record.get('ranks'),
            'started_at': record.get('started_at'),
            'record': str(path),
            'action': action,
            'clears_with': CLEARING_ACTIONS[action].format(record=str(path))}


@dataclass
class GateVerdict:
    """What the sweep found: may a new run start in this case?"""

    blocked: bool = False
    read_only: bool = False
    records: list = field(default_factory=list)
    closed: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    case_path: Path | None = None

    @property
    def message(self) -> str:
        if not self.blocked:
            return ''
        names = ', '.join(
            f'{record.get("run_id", "?")[:8]} ({record.get("operation") or record.get("name") or "run"}, '
            f'{record.get("state")})' for record in self.records)
        text = f'Finish recovery first: {names}'
        if self.messages:
            text += ' — ' + '; '.join(self.messages)
        return text

    def held(self) -> list[dict]:
        """Plan 50 CX3. Each run still held, and the act that clears it."""
        return [held_run(record, self.case_path) for record in self.records]

    def actions(self) -> list[str]:
        """The clearing acts of :meth:`held`, each once, in order."""
        return list(dict.fromkeys(item['action'] for item in self.held()))

    def advice(self) -> str:
        """The held runs and their clearing acts, in one sentence each."""
        return ' '.join(
            f'Run {item["run_id"][:8]} ({item["operation"] or "run"}, '
            f'{item["state"]}): {item["clears_with"]}.'
            for item in self.held())

    def refusal(self) -> str:
        """DP-6487. The one sentence a refused run shows: that it was not
        started, why, and what to do, each said once, with no internal run id
        or state name, and a record's path last so a cut line loses only it.
        """
        if not self.blocked:
            return ''
        why, do, paths = [], [], []
        for item in self.held():
            reason, act = REFUSAL_WORDS[item['action']]
            why.append(reason)
            do.append(act)
            if item['action'] == 'remove_record':
                paths.append(item['record'])
        why = list(dict.fromkeys(why))
        do = list(dict.fromkeys(do))
        if not why:
            why = ['an earlier run of this case has not been resolved']
            do = ['Try again.']
        text = f'The run was not started: {"; ".join(why)}. {" ".join(do)}'
        if paths:
            text = text.rstrip('.') + ': ' + ', '.join(paths)
        return text

    def to_dict(self) -> dict:
        return {'blocked': self.blocked, 'read_only': self.read_only,
                'records': [record.get('run_id') for record in self.records],
                'closed': list(self.closed), 'messages': list(self.messages),
                'message': self.message, 'held': self.held(),
                'actions': self.actions()}


#: Beside the processor cases: the result waiting in them to be gathered, and
#: which case-root mesh they were split from (DP-1230).
PENDING_GATHER = ('foammesh', 'pending-gather.json')
DECOMPOSITION_RECORD = ('foammesh', 'decomposition.json')


def discard_decomposition(case_path) -> dict:
    """Remove the processor cases of *case_path*; never raises.

    Plan 50 CX1 (DP-6200). The pending-gather marker and the decomposition
    record go first, so a set that is only half removed is never described
    as a result to gather or a split to reuse; then the ``delete_me_*``
    folders an *earlier* clean-up left (never those renamed here, which
    their own background delete removes), then each ``processor*`` folder.
    Meant for a worker thread; on a thread running an asyncio loop (the GUI
    thread) it neither waits for a rename nor deletes anything itself.
    ``{'removed': [...], 'held': [...]}``: *held* names the folders that
    could not be removed yet. Nothing describes them any more, so the next
    clean-up of the case -- the next run, an unlock, the recovery gate --
    removes them.
    """
    from foammesh.support.utils import discard_tree, sweep_deferred_deletes
    case = Path(case_path)
    for parts in (PENDING_GATHER, DECOMPOSITION_RECORD):
        try:
            case.joinpath(*parts).unlink(missing_ok=True)
        except OSError:
            logger.warning('could not remove %s', case.joinpath(*parts),
                           exc_info=True)
    sweep_deferred_deletes(case)
    removed, held = [], []
    rename_seconds = None
    for processor in sorted(case.glob('processor[0-9]*')):
        if not processor.is_dir():
            continue
        if discard_tree(processor, rename_seconds=rename_seconds):
            removed.append(processor.name)
        else:
            held.append(processor.name)
            # One wait for Windows to let go is enough: the rest of a set a
            # killed run still holds is not waited on folder by folder.
            rename_seconds = 0
    return {'removed': removed, 'held': held}


def discard_processor_directories(case_path) -> list[str]:
    """Plan 35 D8: a failed parallel run's processor cases are deleted."""
    return discard_decomposition(case_path)['removed']


def mirror_confirms_dead(linux: dict) -> bool:
    """Does the wrapper's Linux mirror say no writer of the run is left?

    Two lines prove it: ``exited`` with an ``rc`` (the wrapper writes it only
    once its group and session are empty), and the lifetime-pipe watcher's
    ``killed-on-transport-loss`` with ``confirmed_dead: true`` (written only
    after KILL, when nothing of the group or session answered). Anything
    else -- ``running``, ``confirmed_dead: false``, no mirror -- proves
    nothing, and the writer may still be alive.
    """
    if not isinstance(linux, dict):
        return False
    if linux.get('state') == 'exited' and linux.get('rc') is not None:
        return True
    return linux.get('confirmed_dead') is True


class RecoveryGate:
    """Resolve every open run record of a case, or say why it cannot be."""

    def __init__(self, case_path, *, recovery=None,
                 probe: Callable[[dict], Liveness] = probe_writer,
                 stop: Callable[[dict], Liveness] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 confirm_seconds: float = CONFIRM_DEATH_SECONDS):
        from foammesh.core.mesh.recovery import MeshRecoveryService
        self.case_path = Path(case_path)
        self.store = RunRecordStore(case_path)
        self.recovery = recovery or MeshRecoveryService()
        self.probe = probe
        self.stop = stop
        self.sleep = sleep
        self.confirm_seconds = confirm_seconds

    def check(self, *, active_run_ids=()) -> GateVerdict:
        """Read-only: is anything open?"""
        records = [record for record in self.store.open_records()
                   if record.get('run_id') not in set(active_run_ids)]
        return GateVerdict(blocked=bool(records), records=records,
                           case_path=self.case_path)

    def resolve(self, *, active_run_ids=(), wait_for_death: bool = False,
                run_ids=None) -> GateVerdict:
        """Sweep: close what can be closed, restore what must be restored.

        *run_ids*, when given, limits the sweep to those records.
        """
        verdict = GateVerdict(case_path=self.case_path)
        active = set(active_run_ids)
        only = None if run_ids is None else {str(item) for item in run_ids}
        for record in self.store.open_records():
            run_id = str(record['run_id'])
            if run_id in active or (only is not None and run_id not in only):
                continue
            try:
                outcome = self._resolve_one(record, wait_for_death=wait_for_death)
            except Exception as error:  # noqa: BLE001 - the gate stays shut
                logger.exception('run record %s could not be resolved', run_id)
                outcome = f'could not be resolved: {error}'
            if outcome == 'closed':
                verdict.closed.append(run_id)
                continue
            verdict.blocked = True
            current = self.store.load(run_id) or record
            verdict.records.append(current)
            if outcome == 'unreachable':
                verdict.read_only = True
                verdict.messages.append(
                    'WSL cannot be reached to confirm the old run has stopped; '
                    'the case is read-only for meshing until it can')
            elif outcome == 'alive':
                verdict.messages.append(
                    f'run {run_id[:8]} is still running in WSL: stop it or wait for it')
            elif outcome:
                verdict.messages.append(f'run {run_id[:8]} {outcome}')
        try:
            self.store.prune()
        except OSError:
            pass
        return verdict

    def _resolve_one(self, record: dict, *, wait_for_death: bool) -> str:
        run_id = str(record['run_id'])
        state = record.get('state')
        if record.get('unreadable'):
            return 'has an unreadable record'
        if state == 'exited':
            if record.get('outcome') == 'ok':
                self.store.transition(run_id, 'closed', resolution='finished')
                return 'closed'
            return self._restore(record)
        if state == 'restoring':
            return self._restore(record)
        # launching, running or recovery_pending: is the writer gone?
        liveness = self._confirm_dead(record, wait=wait_for_death)
        if liveness.state == 'dead':
            if state != 'recovery_pending':
                self.store.transition(run_id, 'recovery_pending',
                                      writer='confirmed-dead')
            else:
                self.store.update(run_id, writer='confirmed-dead')
            return self._restore(self.store.load(run_id) or record)
        fields = {'writer': liveness.state, 'writer_pids': list(liveness.pids),
                  'writer_detail': liveness.detail, 'checked_at': _now()}
        if state == 'recovery_pending':
            self.store.update(run_id, **fields)
        else:
            self.store.transition(run_id, 'recovery_pending', **fields)
        return liveness.state

    def _confirm_dead(self, record: dict, *, wait: bool) -> Liveness:
        deadline = time.monotonic() + (self.confirm_seconds if wait else 0)
        while True:
            # The wrapper writes `exited` into the Linux mirror only once its
            # group and session are empty, and the watcher writes
            # `confirmed_dead: true` only when nothing answered after KILL:
            # each is a confirmation on its own.
            linux = self.store.linux_record(str(record['run_id']))
            if mirror_confirms_dead(linux):
                if linux.get('state') == 'exited':
                    return Liveness('dead', detail=f'the wrapper reported rc {linux.get("rc")}')
                return Liveness('dead', detail=(
                    f'the wrapper watcher confirmed it dead ({linux.get("state")})'))
            liveness = self.probe(record)
            if liveness.state != 'alive' or time.monotonic() >= deadline:
                return liveness
            self.sleep(2)

    def stop_and_resolve(self, run_id: str) -> GateVerdict:
        """[Stop it]: kill a surviving writer, then resolve."""
        record = self.store.load(run_id)
        if record is not None and self.stop is not None:
            self.stop(record)
        elif record is not None:
            stop_writer(record)
        return self.resolve()

    def _restore(self, record: dict) -> str:
        run_id = str(record['run_id'])
        if record.get('state') != 'restoring':
            record = self.store.transition(run_id, 'restoring')
        notes = []
        point = self.recovery.point_for(self.case_path, str(record.get('recovery_id') or ''))
        if point is not None:
            try:
                self.recovery.restore(self.case_path, point)
                notes.append('the previous mesh was restored')
            except (FileNotFoundError, ValueError) as error:
                # The snapshot itself is unusable: retrying cannot help, and
                # a gate that can never open is worse than saying so.
                notes.append(f'the previous mesh could not be restored ({error}); '
                             'the mesh on disk may be incomplete')
            # Any other OSError (a locked file) propagates: the record stays
            # at `restoring` and the next gate tries again.
        elif record.get('recovery_id'):
            notes.append('its recovery snapshot is gone; nothing was restored')
        else:
            notes.append('there was no earlier mesh to restore')
        if record.get('staging'):
            # Plan 37 UF17: the run wrote into a staging copy, not
            # into the case. The live processor cases are the source it was
            # protecting; its own transaction discards the stage.
            notes.append('it ran on a staging copy; the case was not touched')
        elif int(record.get('ranks') or 1) > 1:
            outcome = discard_decomposition(self.case_path)
            if outcome['removed']:
                notes.append(
                    f'{len(outcome["removed"])} processor directories were removed')
            if outcome['held']:
                # Plan 50 CX1. Named, not fatal: the run is resolved, and the
                # next run or unlock removes what Windows still held.
                notes.append(
                    'still in use, removed by the next run or unlock: '
                    + ', '.join(outcome['held']))
        self.store.transition(run_id, 'closed', resolution='restored', notes=notes)
        return 'closed'


def sweep_case(case_path, *, active_run_ids=()) -> GateVerdict:
    """The app-start sweep for one case (runs off the GUI thread)."""
    if not run_directory(case_path).is_dir():
        return GateVerdict()
    return RecoveryGate(case_path).resolve(active_run_ids=active_run_ids)
