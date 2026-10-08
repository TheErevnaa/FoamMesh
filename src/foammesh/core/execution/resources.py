"""Portable resource intent and deterministic effective allocations."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from typing import Mapping


class ResourceError(ValueError):
    pass


class ResourceMode(str, Enum):
    AUTO = 'auto'
    SERIAL = 'serial'
    PARALLEL = 'parallel'


@dataclass(frozen=True)
class ResourcePolicy:
    mode: ResourceMode = ResourceMode.AUTO
    max_cpu_cores: int | None = None
    max_memory_bytes: int | None = None
    allow_distributed: bool = False
    preferred_backend: str = 'local'

    def __post_init__(self) -> None:
        object.__setattr__(self, 'mode', ResourceMode(self.mode))
        if self.max_cpu_cores is not None and self.max_cpu_cores < 1:
            raise ResourceError('max_cpu_cores must be positive')
        if self.max_memory_bytes is not None and self.max_memory_bytes < 1:
            raise ResourceError('max_memory_bytes must be positive')
        if not self.preferred_backend.strip():
            raise ResourceError('preferred_backend must not be empty')
        if not self.allow_distributed and self.preferred_backend not in {
                'local', 'openfoam-mpi', 'gmsh-threads'}:
            raise ResourceError('distributed backend requires allow_distributed')

    def to_dict(self) -> dict:
        return {
            'mode': self.mode.value,
            'max_cpu_cores': self.max_cpu_cores,
            'max_memory_bytes': self.max_memory_bytes,
            'allow_distributed': self.allow_distributed,
            'preferred_backend': self.preferred_backend,
        }


@dataclass(frozen=True)
class ResourceRequest:
    cpu_ranks: int
    threads_per_rank: int = 1
    memory_bytes: int | None = None
    wall_time_seconds: float | None = None
    backend_id: str = 'local'
    profile_id: str | None = None
    placement: str = 'local'
    explicit: bool = False

    def __post_init__(self) -> None:
        if self.cpu_ranks < 1 or self.threads_per_rank < 1:
            raise ResourceError('ranks and threads_per_rank must be positive')
        if self.memory_bytes is not None and self.memory_bytes < 1:
            raise ResourceError('memory_bytes must be positive')
        if self.wall_time_seconds is not None and self.wall_time_seconds <= 0:
            raise ResourceError('wall_time_seconds must be positive')

    @property
    def cpu_slots(self) -> int:
        return self.cpu_ranks * self.threads_per_rank

    def to_dict(self) -> dict:
        return {
            'cpu_ranks': self.cpu_ranks,
            'threads_per_rank': self.threads_per_rank,
            'memory_bytes': self.memory_bytes,
            'wall_time_seconds': self.wall_time_seconds,
            'backend_id': self.backend_id,
            'profile_id': self.profile_id,
            'placement': self.placement,
            'explicit': self.explicit,
        }


@dataclass(frozen=True)
class ResourceFacts:
    cpu_cores: int
    memory_bytes: int | None = None
    affinity_cores: int | None = None
    backend_capabilities: Mapping[str, bool] = field(default_factory=dict)

    @property
    def effective_cpu_limit(self) -> int:
        return max(1, min(
            self.cpu_cores,
            self.affinity_cores if self.affinity_cores is not None
            else self.cpu_cores))

    @classmethod
    def local(cls) -> 'ResourceFacts':
        cores = os.cpu_count() or 1
        affinity = None
        if hasattr(os, 'sched_getaffinity'):
            try:
                affinity = len(os.sched_getaffinity(0))
            except OSError:
                pass
        memory = None
        try:
            import psutil
            memory = int(psutil.virtual_memory().available)
        except (ImportError, OSError):
            pass
        return cls(cores, memory, affinity, {'local': True})


@dataclass(frozen=True)
class ResourceAllocation:
    effective_ranks: int
    effective_threads_per_rank: int
    backend_id: str
    profile_id: str | None = None
    memory_bytes: int | None = None
    hosts_digest: str | None = None
    runtime_fingerprint: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.effective_ranks < 1 or self.effective_threads_per_rank < 1:
            raise ResourceError('effective allocation must have positive CPU values')

    @property
    def cpu_slots(self) -> int:
        return self.effective_ranks * self.effective_threads_per_rank

    def to_dict(self) -> dict:
        return {
            'effective_ranks': self.effective_ranks,
            'effective_threads_per_rank': self.effective_threads_per_rank,
            'backend_id': self.backend_id,
            'profile_id': self.profile_id,
            'memory_bytes': self.memory_bytes,
            'hosts_digest': self.hosts_digest,
            'runtime_fingerprint': self.runtime_fingerprint,
            'warnings': list(self.warnings),
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()


def allocate_resources(policy: ResourcePolicy, request: ResourceRequest,
                       facts: ResourceFacts) -> ResourceAllocation:
    """Resolve resources without silently weakening explicit requests."""
    cpu_limit = facts.effective_cpu_limit
    if policy.max_cpu_cores is not None:
        cpu_limit = min(cpu_limit, policy.max_cpu_cores)
    requested_slots = request.cpu_slots
    warnings = []
    if policy.mode is ResourceMode.SERIAL:
        ranks, threads = 1, 1
    elif requested_slots <= cpu_limit:
        ranks, threads = request.cpu_ranks, request.threads_per_rank
    elif request.explicit or policy.mode is ResourceMode.PARALLEL:
        raise ResourceError(
            f'explicit request needs {requested_slots} CPU slots; {cpu_limit} available')
    else:
        if request.cpu_ranks > 1 and request.threads_per_rank == 1:
            ranks, threads = cpu_limit, 1
        elif request.cpu_ranks == 1:
            ranks, threads = 1, cpu_limit
        else:
            ranks, threads = max(1, cpu_limit // request.threads_per_rank), \
                request.threads_per_rank
        warnings.append(
            f'auto allocation reduced {requested_slots} CPU slots to {ranks * threads}')
    memory_limit = policy.max_memory_bytes
    if facts.memory_bytes is not None:
        memory_limit = min(
            facts.memory_bytes,
            memory_limit if memory_limit is not None else facts.memory_bytes)
    if request.memory_bytes is not None and memory_limit is not None:
        if request.memory_bytes > memory_limit and request.explicit:
            raise ResourceError(
                f'explicit request needs {request.memory_bytes} bytes; '
                f'{memory_limit} available')
        memory = min(request.memory_bytes, memory_limit)
    else:
        memory = request.memory_bytes or memory_limit
    if request.backend_id.startswith('gmsh') and ranks != 1:
        raise ResourceError('Gmsh threaded execution requires exactly one rank')
    if request.backend_id.startswith('openfoam') and threads != 1:
        raise ResourceError('OpenFOAM MPI execution requires one thread per rank')
    return ResourceAllocation(
        ranks, threads, request.backend_id, request.profile_id, memory,
        warnings=tuple(warnings))


def machine_cpu_limit(facts: 'ResourceFacts | None' = None) -> int:
    """How many CPU slots this machine offers meshing.

    Kept here so that the answer to "what does automatic mean" is a policy
    function with a name, rather than a call to ``os.cpu_count`` copied into
    whichever surface needed it next.
    """
    return int((facts or ResourceFacts.local()).effective_cpu_limit)


def requested_cpu_count(policy, *, requested: int = 0,
                        configured: int = 0) -> int:
    """What this case asks meshing to run on, before the machine is asked.

    Plan 33 SETUP-03. The precedence lives here, in one function, because it
    used to live in three: the launcher read the parallel environment alone,
    the plan preview read the environment and then the ceiling, and the page
    showed the ceiling and called it the count. MEASURED: a case whose page
    said three cores and whose parallel environment had never been opened
    meshed on one, because an unopened environment answers 1 and 1 outranked
    the ceiling.

    An environment of 1 is the value a case that nobody configured carries,
    so it is not read as a request. ``0`` is returned when nothing asked at
    all, which is the caller's cue to apply its own idea of automatic.
    """
    mode, ceiling = _policy_reading(policy)
    if mode == 'serial':
        return 1
    if requested and int(requested) > 0:
        return int(requested)
    if configured and int(configured) > 1:
        return int(configured)
    if ceiling:
        return int(ceiling)
    return 0


def effective_cpu_count(policy, *, requested: int = 0, configured: int = 0,
                        unasked: int = 0, facts=None) -> int:
    """The count a run will really use: what was asked for, clamped.

    ``unasked`` is what to do when nothing asked for anything: threads on one
    machine cost nothing, so Gmsh leaves it at 0 and takes the machine; ranks
    change a snappy mesh, so the launcher passes 1 and stays serial until
    someone says otherwise.
    """
    mode, ceiling = _policy_reading(policy)
    if mode == 'serial':
        return 1
    limit = machine_cpu_limit(facts)
    if ceiling:
        limit = min(limit, int(ceiling))
    count = requested_cpu_count(policy, requested=requested,
                                configured=configured)
    if count <= 0:
        count = int(unasked) if unasked else limit
    return max(1, min(int(count), limit))


def _policy_reading(policy) -> tuple[str, int | None]:
    """The two things the precedence needs, from a policy or a plain mapping.

    The facade carries the policy as a mapping and the engines carry it as a
    ``ResourcePolicy``; asking both the same question here keeps the callers
    from each doing their own conversion.
    """
    if isinstance(policy, ResourcePolicy):
        return policy.mode.value, policy.max_cpu_cores
    values = policy or {}
    mode = str(values.get('mode') or 'auto').split('.')[-1].lower()
    ceiling = values.get('max_cpu_cores')
    try:
        ceiling = int(ceiling or 0)
    except (TypeError, ValueError):
        ceiling = 0
    return mode, (ceiling or None)


def execution_record(policy, *, effective: int, unit: str,
                     facts=None, auto=None) -> dict:
    """The CPU cap a run was given and the count it really used, for a file.

    DP-678. ``max_cpu_cores`` is a ceiling, and the rank or thread count a run
    uses is derived from it, the mode and the machine. None of the three
    reached any file, so a written case could not say what it was capped at:
    a ``numberOfSubdomains 12`` read the same whether the cap was 12 or none.
    ``maxCpuCores`` is ``None`` when no cap was set; ``unit`` is ``ranks``
    for OpenFOAM MPI and ``threads`` for Gmsh.
    """
    mode, ceiling = _policy_reading(policy)
    return {
        'mode': mode,
        'maxCpuCores': int(ceiling) if ceiling else None,
        'machineCpus': machine_cpu_limit(facts),
        'unit': str(unit),
        'effective': max(1, int(effective or 1)),
        **({'auto': dict(auto)} if auto else {}),
    }


#: Where the snappy stage route records each stage's :func:`execution_record`,
#: beside ``stage-runs.json``. The whole-pipeline route carries the same record
#: in its run's ``job.json``.
STAGE_EXECUTION_FILE = ('foammesh', 'dictionaries', 'stage-execution.json')


def record_stage_execution(case_path, stage: str, record: dict):
    """Keep ``record`` as the execution of ``stage``; returns the file path."""
    from pathlib import Path

    target = Path(case_path).joinpath(*STAGE_EXECUTION_FILE)
    document = {'schema_version': 1, 'stages': {}}
    try:
        loaded = json.loads(target.read_text(encoding='utf-8'))
        if isinstance(loaded, dict) and isinstance(loaded.get('stages'), dict):
            document = loaded
    except (OSError, ValueError):
        pass
    document['stages'][str(stage)] = dict(record)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n',
                         encoding='utf-8')
    os.replace(temporary, target)
    return target


# --------------------------------------------------------------------------- #
# DP-1231. What "Auto" means: the WSL machine the mesher runs on.
# --------------------------------------------------------------------------- #
#
# A case nobody gave a core count meshed on one rank: `requested_cpu_count`
# answers 0 for "nobody asked", and every snappy call site read 0 as 1. Gmsh
# took the Windows logical count instead, capped at 64. Neither is the machine
# the mesher runs on: both engines run inside the WSL distro, whose processors
# are whatever `.wslconfig processors=` gives it, and whose free memory is the
# VM's, not Windows'. So Auto is asked of WSL itself, once, off the loop.
#
# Physical cores, not logical. MEASURED in OpenFOAM13Runtime (Ryzen 9 7950X):
# `nproc` 32, `lscpu -p=CORE,SOCKET` 16 distinct cores, `mpirun (Open MPI)
# 4.1.2`. Open MPI's default slot count is the physical core count, so a
# 32-rank `mpirun -np` without `--oversubscribe` is refused outright, and
# snappyHexMesh is memory-bandwidth bound, so a hyper-thread sibling buys no
# meshing anyway. One core is left for the window, the WSL relay and the OS.
# There is no cell cap and no thread cap: the only other limit is RAM.

import threading  # noqa: E402
import time  # noqa: E402
from dataclasses import asdict  # noqa: E402

#: Cores Auto leaves free for the window, the WSL relay and the OS.
AUTO_HEADROOM_CORES = 1
#: How long a probed reading stands. The core count cannot change while WSL
#: runs (`processors=` needs `wsl --shutdown`); free memory can, so a reading
#: older than this is refreshed -- on a worker thread, never by the caller.
HOST_FACTS_TTL_SECONDS = 300.0

_HOST_PROBE_SCRIPT = (
    'echo "logical=$(nproc 2>/dev/null)"; '
    'echo "physical=$(lscpu -p=CORE,SOCKET 2>/dev/null | grep -v "^#" '
    '| sort -u | wc -l)"; '
    "awk '/^MemAvailable:/{print \"available_kb=\" $2} "
    "/^MemTotal:/{print \"total_kb=\" $2}' /proc/meminfo 2>/dev/null")


@dataclass(frozen=True)
class MeshingHost:
    """The CPUs and memory of the machine a mesher runs on."""

    logical_cores: int
    physical_cores: int | None = None
    memory_available_bytes: int | None = None
    memory_total_bytes: int | None = None
    source: str = 'local'
    probed_at: float = 0.0

    @property
    def cores(self) -> int:
        """The cores Auto counts: physical where known, else logical."""
        return max(1, int(self.physical_cores or self.logical_cores or 1))

    @property
    def core_kind(self) -> str:
        return 'physical' if self.physical_cores else 'logical'

    def resource_facts(self) -> ResourceFacts:
        """The allocator's view: an explicit request is clamped by every CPU
        the host has, Auto by :func:`auto_cpu_count`."""
        return ResourceFacts(max(1, int(self.logical_cores or 1)),
                             self.memory_available_bytes, None,
                             {'wsl': self.source.startswith('wsl')})

    def to_dict(self) -> dict:
        return asdict(self)


def local_meshing_host() -> MeshingHost:
    """This process's own machine: the fallback before WSL has answered."""
    logical = os.cpu_count() or 1
    physical = available = total = None
    try:
        import psutil
        physical = psutil.cpu_count(logical=False) or None
        memory = psutil.virtual_memory()
        available, total = int(memory.available), int(memory.total)
    except (ImportError, OSError, AttributeError):
        pass
    return MeshingHost(int(logical), physical, available, total, 'local',
                       time.monotonic())


def parse_host_probe(text: str, source: str) -> MeshingHost | None:
    """A :class:`MeshingHost` from the probe's ``key=value`` lines."""
    values: dict[str, int] = {}
    for line in str(text or '').splitlines():
        key, separator, value = line.strip().partition('=')
        if not separator:
            continue
        try:
            values.setdefault(key.strip(), int(value.strip()))
        except ValueError:
            continue
    logical = values.get('logical') or 0
    if logical < 1:
        return None
    physical = values.get('physical') or None
    if physical is not None and not 1 <= physical <= logical:
        physical = None
    kib = 1024
    return MeshingHost(
        logical, physical,
        values['available_kb'] * kib if values.get('available_kb') else None,
        values['total_kb'] * kib if values.get('total_kb') else None,
        source, time.monotonic())


def probe_wsl_host(distribution: str, user: str | None = None, *,
                   runner=None, timeout: float = 60.0) -> MeshingHost | None:
    """Ask *distribution* for its cores and free memory. Blocks: a cold WSL
    boot has measured ~20-32 s, so callers run this on a worker thread."""
    from foammesh.core.openfoam_runtime.detect import _run, _text

    argv = ['wsl.exe', '--distribution', distribution]
    if user:
        argv += ['--user', user]
    argv += ['--exec', 'bash', '-c', _HOST_PROBE_SCRIPT]
    code, output = (runner or _run)(tuple(argv), timeout)
    if code != 0:
        return None
    return parse_host_probe(_text(output), f'wsl:{distribution}')


_HOST_CACHE: dict[tuple, MeshingHost] = {}
_HOST_LOCK = threading.Lock()
_HOST_WARMING: set = set()


def _runtime_target() -> tuple[str, str] | None:
    """``(distro, user)`` the mesher runs in, or ``None`` off Windows (then
    the mesher runs on this machine and its own facts are the host's)."""
    if os.name != 'nt':
        return None
    try:
        from foammesh.settings.app_settings import AppSettings
        runtime = AppSettings().getOpenFoamRuntime()
        return (str(runtime.get('wsl_distro') or 'OpenFOAM13Runtime'),
                str(runtime.get('wsl_user') or ''))
    except Exception:                                       # noqa: BLE001
        return ('OpenFOAM13Runtime', 'foamuser')


def _probe_and_keep(target: tuple) -> MeshingHost | None:
    try:
        host = probe_wsl_host(*target)
    except Exception:                                       # noqa: BLE001
        host = None
    if host is not None:
        with _HOST_LOCK:
            _HOST_CACHE[target] = host
    return host


def _warm_in_background(target: tuple) -> None:
    with _HOST_LOCK:
        if target in _HOST_WARMING:
            return
        _HOST_WARMING.add(target)

    def work():
        try:
            _probe_and_keep(target)
        finally:
            with _HOST_LOCK:
                _HOST_WARMING.discard(target)

    threading.Thread(target=work, name='foammesh-host-probe',
                     daemon=True).start()


def meshing_host(*, wait: bool = False, refresh: bool = False) -> MeshingHost:
    """The machine the mesher runs on, as last probed.

    Never blocks unless *wait* (then the caller is a worker thread -- see
    :func:`warm_meshing_host`). With *refresh*, a cold or stale reading
    starts a probe on a daemon thread and this call answers with what it
    has: the last reading, else this machine's own facts.
    """
    target = _runtime_target()
    if target is None:
        return local_meshing_host()
    with _HOST_LOCK:
        cached = _HOST_CACHE.get(target)
    fresh = (cached is not None and time.monotonic() - cached.probed_at
             < HOST_FACTS_TTL_SECONDS)
    if fresh:
        return cached
    if wait:
        return _probe_and_keep(target) or cached or local_meshing_host()
    if refresh:
        _warm_in_background(target)
    return cached or local_meshing_host()


async def warm_meshing_host() -> MeshingHost:
    """:func:`meshing_host`, probed on a worker thread if it is not fresh."""
    import asyncio
    return await asyncio.to_thread(meshing_host, wait=True)


def remember_meshing_host(host: MeshingHost | None) -> None:
    """Put *host* in the cache for the current runtime (``None`` empties it).
    For a probe made elsewhere, and for tests."""
    target = _runtime_target() or ('local', '')
    with _HOST_LOCK:
        if host is None:
            _HOST_CACHE.clear()
        else:
            _HOST_CACHE[target] = host


@dataclass(frozen=True)
class CpuCount:
    """How many ranks (snappy) or threads (Gmsh) a run gets, and why.

    ``source`` is ``serial``, ``requested`` (a count or ceiling was set),
    ``recorded`` (Auto, kept from the mesh's first stage) or ``auto``.
    """

    count: int
    unit: str
    source: str
    auto: bool
    cores: int = 0
    core_kind: str = ''
    headroom: int = 0
    memory_limit: int | None = None
    memory_available_bytes: int | None = None
    cells: int | None = None
    limited_by: str = ''
    host: str = ''
    reason: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


def auto_cpu_count(engine: str, *, cells: int | None = None,
                   host: MeshingHost | None = None) -> CpuCount:
    """Auto: the host's cores less one, and no more ranks than RAM holds.

    Ranks are separate processes, so each pays
    ``support.resource_budget.MESHER_FIXED_BYTES`` on top of its share of
    the cells: ``ranks * fixed + per_cell * cells <= MemAvailable``. Gmsh's
    threads share one process, so memory does not limit them. *cells* is the
    largest mesh the caller knows the run will hold; unknown means no limit
    beyond the per-rank cost.
    """
    from foammesh.support.resource_budget import (
        MESHER_BYTES_PER_CELL, MESHER_FIXED_BYTES,
    )

    host = host or meshing_host()
    engine = 'gmsh' if str(engine).startswith('gmsh') else 'snappy'
    unit = 'threads' if engine == 'gmsh' else 'ranks'
    by_cores = max(1, host.cores - AUTO_HEADROOM_CORES)
    count, limited_by, memory_limit = by_cores, 'cores', None
    available = host.memory_available_bytes
    if engine == 'snappy' and available:
        spare = float(available) - MESHER_BYTES_PER_CELL['snappy'] * float(
            max(0, int(cells or 0)))
        memory_limit = (max(1, int(spare // MESHER_FIXED_BYTES))
                        if spare > 0 else 1)
        if memory_limit < count:
            count, limited_by = memory_limit, 'memory'
    reason = (f'Auto: {host.cores} {host.core_kind} cores less '
              f'{AUTO_HEADROOM_CORES} = {by_cores}')
    if limited_by == 'memory':
        reason += (f'; free memory holds {memory_limit} {unit}'
                   + (f' for {int(cells):,} cells' if cells else ''))
    return CpuCount(count, unit, 'auto', True, host.cores, host.core_kind,
                    AUTO_HEADROOM_CORES, memory_limit, available,
                    int(cells) if cells else None, limited_by, host.source,
                    reason)


def meshing_cpu_count(policy, *, engine: str, requested: int = 0,
                      cells: int | None = None, recorded: int = 0,
                      host: MeshingHost | None = None) -> CpuCount:
    """The count a run of *engine* will use, before the allocator clamps it.

    One answer for the launcher, the plan preview and the page (DP-1231):
    serial is 1; a count asked for (the run's ``cores``, or the Meshing
    resources ceiling) is that count; otherwise Auto -- the *recorded*
    count when an earlier stage of this mesh already ran on Auto (DP-1232),
    so every stage of one mesh uses the same decomposition, else
    :func:`auto_cpu_count`. Never blocks; see :func:`warm_meshing_host`.
    """
    unit = 'threads' if str(engine).startswith('gmsh') else 'ranks'
    mode, _ceiling = _policy_reading(policy)
    if mode == 'serial':
        return CpuCount(1, unit, 'serial', False, reason='serial')
    asked = requested_cpu_count(policy, requested=requested)
    if asked > 0:
        return CpuCount(int(asked), unit, 'requested', False,
                        reason=f'{asked} {unit} requested')
    if recorded and int(recorded) > 0:
        return CpuCount(int(recorded), unit, 'recorded', True,
                        reason=f'Auto: {recorded} {unit}, as the first stage '
                               'of this mesh ran')
    return auto_cpu_count(engine, cells=cells, host=host)


def recorded_auto_count(case_path, stage: str) -> int:
    """The Auto rank count this mesh's first stage ran on, or 0 (DP-1232).

    Snap and Layers continue the mesh Castellation decomposed: an Auto that
    answered differently between them (free memory moved, the cache aged)
    would re-decompose midway and make the mesh depend on the moment it was
    run. Only an Auto record counts; a set count is read fresh.
    """
    from pathlib import Path

    if str(stage) not in {'snap', 'layers'}:
        return 0
    try:
        stages = json.loads(Path(case_path).joinpath(
            *STAGE_EXECUTION_FILE).read_text(encoding='utf-8'))['stages']
    except (OSError, ValueError, KeyError, TypeError):
        return 0
    if not isinstance(stages, dict):
        return 0
    for first in ('castellation', 'snappyHexMesh'):
        record = stages.get(first)
        if isinstance(record, dict) and record.get('auto'):
            try:
                return max(0, int(record.get('effective') or 0))
            except (TypeError, ValueError):
                return 0
    return 0
