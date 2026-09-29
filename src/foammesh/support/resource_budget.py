"""How much memory a worker may have, and who goes next (Plan 35 CR2/CR3).

A polyMesh parse used to run inside the GUI process. On a mesh a few million
cells large it took gigabytes, the machine paged, WSL stalled and the window
went with it. The parse now runs in a worker process (``foammesh.workers``),
and this module decides, *before* that worker is started, whether the machine
can afford it:

* **The budget** is the physical memory Windows says is available
  (``GlobalMemoryStatusEx``), minus the working set of the WSL VM (``vmmem`` /
  ``vmmemWSL``, best effort -- the VM's claim is invisible to "available" until
  it has already taken the memory), minus a reserve kept for the GUI itself.
* **The estimate** comes from the list counts in the polyMesh headers
  (:func:`foammesh.core.mesh.poly_mesh_boundary.read_counts`), times a factor
  per operation recorded next to :data:`PEAK_FACTORS`.
* **One heavy worker at a time**, with a queue. Checks after a run are queued
  rather than raced.
* **A hard cap per worker**: a Windows Job Object with
  ``JOB_OBJECT_LIMIT_PROCESS_MEMORY`` set to the grant, plus
  ``KILL_ON_JOB_CLOSE`` so a worker cannot outlive the GUI that started it. A
  wrong estimate therefore ends the *worker* -- reported ``over_budget``, not as
  a crash -- instead of paging the whole machine.

Numbers from Plan 35 §9 D12, to be re-set from the CR11 K5 measurements: a
1 GB GUI reserve; the preview capped at 2 M triangles / 256 MB; the full
volume capped at 5 M cells / 1 GB.

Nothing here imports Qt or VTK; the worker imports it too.
"""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
import itertools
import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

MIB = 1024 * 1024
GIB = 1024 * MIB

#: D12. Kept back for the GUI process whatever the workers are doing.
GUI_RESERVE_BYTES = 1 * GIB
#: D12. The boundary preview (CR3) is capped at this many triangles and bytes.
PREVIEW_MAX_TRIANGLES = 2_000_000
PREVIEW_MAX_BYTES = 256 * MIB
#: D12. "Load full volume" is capped at this many cells and bytes.
FULL_VOLUME_MAX_CELLS = 5_000_000
FULL_VOLUME_MAX_BYTES = 1 * GIB

#: What a worker costs before it reads anything: the interpreter, numpy and
#: the facade modules it imports. MEASURED at ~60 MB RSS for the imports on
#: this machine; the floor is generous because a cap below the import cost
#: fails every check for a reason that has nothing to do with the mesh.
WORKER_FLOOR_BYTES = 384 * MIB
#: The grant is the estimate times this, so an estimate that is a little low
#: does not end a worker that would have finished.
CAP_HEADROOM = 1.5

#: Test and support overrides. The budget one replaces the measured budget
#: (K5b "budget forced low"); the cap one replaces every worker's Job Object
#: limit (K5b "cap below the real need").
ENV_BUDGET = 'FOAMMESH_ADMISSION_BUDGET_BYTES'
ENV_WORKER_CAP = 'FOAMMESH_WORKER_MEMORY_CAP_BYTES'

#: Bytes of peak per unit of mesh, per operation. The parse is the peak: the
#: current reader (CR2b replaces it) tokenises each list into Python ``bytes``
#: objects before converting, which costs roughly 60 bytes per token on top of
#: the raw file and its comment-stripped copy. ``file`` multiplies the size on
#: disk of the lists the operation reads; the rest are per count.
PEAK_FACTORS: dict[str, dict[str, float]] = {
    # Boundary-only read: the faces file is scanned, only boundary faces are
    # tokenised, and points are converted in bounded chunks.
    'quality.fidelity': {'file': 1.2, 'boundary_faces': 400.0,
                         'points': 40.0, 'fixed': 64 * MIB},
    # The full volume, then a VTK polyhedron grid and a cell locator.
    'quality.resolution': {'file': 6.0, 'faces': 120.0, 'points': 48.0,
                           'cells': 900.0, 'fixed': 64 * MIB},
    # The full volume and a handful of per-cell float arrays. DP-1022, CR11
    # K5: on the 9.9 M-cell layered fixture (1.86 GB of ASCII polyMesh) the
    # worker peaked at 27.7 GB against 16.3 GB estimated (200 a cell), so a
    # granted run died at its 1.5x cap after 70 s instead of being refused up
    # front. 1750 a cell puts that mesh at 30.6 GB, 10% over the measurement.
    'quality.cell_fields': {'file': 6.0, 'faces': 120.0, 'points': 48.0,
                            'cells': 1750.0, 'fixed': 64 * MIB},
    # Composes reports already on disk; reads no mesh.
    'quality.summary': {'fixed': 16 * MIB},
    # CR3. The preview: the bounded boundary read, then VTK polydata, the
    # feature edges and the packed copy -- a few hundred bytes a boundary
    # face. Face zones are read in full (``read_face_zone_surfaces``), which
    # ``estimate_peak_bytes`` adds when the case has any. Unmeasured.
    'mesh.preview': {'file': 1.2, 'boundary_faces': 600.0, 'points': 40.0,
                     'fixed': 128 * MIB},
    # The preview of a case the bounded reader cannot read: the VTK reader
    # holds the whole mesh while it extracts the patches.
    'mesh.preview.vtk': {'file': 3.0, 'faces': 60.0, 'points': 48.0,
                         'fixed': 128 * MIB},
    # "Load full volume": the reader's volume, its exterior and the .vtu.
    'mesh.volume': {'file': 6.0, 'faces': 120.0, 'points': 48.0,
                    'cells': 900.0, 'fixed': 128 * MIB},
}
#: Plan 35 CR7. OCCT and the export writers in a worker. A B-Rep read costs
#: far more than its file: MEASURED on `helical_pipe.step` (1.2 MB), a
#: default-deflection import peaked at 0.22 GiB and a 0.01 one at 1.25 GiB
#: (DP-508) -- the tessellation cap bounds the rest.
PEAK_FACTORS.update({
    'cad.import': {'file': 200.0, 'fixed': 768 * MIB},
    'cad.check': {'file': 100.0, 'fixed': 512 * MIB},
    'cad.repair_preview': {'file': 200.0, 'fixed': 768 * MIB},
    'cad.solids': {'file': 100.0, 'fixed': 256 * MIB},
    'cad.tessellate_file': {'file': 200.0, 'fixed': 768 * MIB},
    'export.dataset': {'fixed': 768 * MIB},
})
#: What a face-zone read costs, per face and point of the whole mesh.
FACE_ZONE_FACTORS = {'faces': 120.0, 'points': 48.0}
#: Operations that hold the single heavy slot.
HEAVY_OPERATIONS = frozenset({
    'quality.fidelity', 'quality.resolution', 'quality.cell_fields',
    'mesh.preview', 'mesh.volume',
})
#: Queue priorities: lower goes first. The preview goes before the checks,
#: because the user is looking at the viewport (CR3 step 2).
PRIORITY_PREVIEW = 0
PRIORITY_INTERACTIVE = 1
PRIORITY_BACKGROUND = 2


# --------------------------------------------------------------------------- #
# The machine
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class MemorySnapshot:
    total: int
    available: int
    wsl_vm: int
    reserve: int
    budget: int
    forced: bool = False

    def to_dict(self) -> dict:
        return {'total_bytes': self.total, 'available_bytes': self.available,
                'wsl_vm_bytes': self.wsl_vm, 'reserve_bytes': self.reserve,
                'budget_bytes': self.budget, 'forced': self.forced}


def _global_memory_status() -> tuple[int, int] | None:
    """``(total, available)`` physical bytes from ``GlobalMemoryStatusEx``."""
    if sys.platform != 'win32':
        return None
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [('dwLength', wintypes.DWORD),
                    ('dwMemoryLoad', wintypes.DWORD),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    try:
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    except (AttributeError, OSError):
        return None
    if not ok:
        return None
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def physical_memory() -> tuple[int, int]:
    """``(total, available)`` physical memory in bytes."""
    measured = _global_memory_status()
    if measured is not None:
        return measured
    try:
        import psutil

        memory = psutil.virtual_memory()
        return int(memory.total), int(memory.available)
    except Exception:                                       # noqa: BLE001
        return 0, 0


_VM_NAMES = ('vmmem', 'vmmem.exe', 'vmmemwsl', 'vmmemwsl.exe')
_vm_cache: tuple[float, int] | None = None


def wsl_vm_working_set(*, max_age: float = 2.0) -> int:
    """Working set of the WSL VM, or 0 when it is not running or unreadable.

    Best effort by design: ``vmmem`` is a protected process on some builds and
    its memory is then unreadable, which counts as zero rather than refusing
    every check on the machine.
    """
    global _vm_cache
    now = time.monotonic()
    if _vm_cache is not None and now - _vm_cache[0] < max_age:
        return _vm_cache[1]
    total = 0
    try:
        import psutil

        for process in psutil.process_iter(['name']):
            name = str(process.info.get('name') or '').lower()
            if name not in _VM_NAMES:
                continue
            try:
                total += int(process.memory_info().rss)
            except Exception:                               # noqa: BLE001
                continue
    except Exception:                                       # noqa: BLE001
        total = 0
    _vm_cache = (now, total)
    return total


def _env_bytes(name: str) -> int | None:
    value = os.environ.get(name, '').strip()
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except ValueError:
        logger.warning('%s=%r is not a byte count; ignored', name, value)
        return None


def snapshot() -> MemorySnapshot:
    """What the machine can give a worker now. Blocking; call off the loop."""
    total, available = physical_memory()
    forced = _env_bytes(ENV_BUDGET)
    if forced is not None:
        return MemorySnapshot(total, available, 0, GUI_RESERVE_BYTES,
                              forced, forced=True)
    vm = wsl_vm_working_set()
    budget = max(0, available - vm - GUI_RESERVE_BYTES)
    return MemorySnapshot(total, available, vm, GUI_RESERVE_BYTES, budget)


# --------------------------------------------------------------------------- #
# Estimates
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Estimate:
    operation: str
    peak_bytes: int
    counts: dict = field(default_factory=dict)
    basis: str = ''

    def to_dict(self) -> dict:
        return {'operation': self.operation, 'peak_bytes': self.peak_bytes,
                'counts': dict(self.counts), 'basis': self.basis}


def estimate_peak_bytes(operation: str, counts: dict | None) -> Estimate:
    """Peak memory ``operation`` needs for a mesh with these header counts.

    ``counts`` is :meth:`MeshCounts.to_dict` or ``None`` when no header could
    be read -- then the estimate is the fixed cost only, and the worker's own
    reader produces the same refusal (missing mesh, binary format...) the
    in-process reader always did.
    """
    counts = dict(counts or {})
    factors = PEAK_FACTORS.get(operation, {'fixed': 64 * MIB})
    if operation == 'mesh.preview' and counts.get('bounded') is False:
        factors = PEAK_FACTORS['mesh.preview.vtk']
    peak = WORKER_FLOOR_BYTES / 2 + factors.get('fixed', 0)
    parts = []
    files = counts.get('file_bytes') or {}
    if 'file' in factors and files:
        if (operation in ('quality.fidelity', 'mesh.preview')
                and factors is not PEAK_FACTORS['mesh.preview.vtk']):
            read = (int(files.get('faces', 0))
                    + int(files.get('points', 0)) + int(files.get('boundary', 0)))
        else:
            read = sum(int(value) for value in files.values())
        peak += factors['file'] * read
        parts.append(f'{factors["file"]:g} x {read} file bytes')
    for key in ('faces', 'boundary_faces', 'points', 'cells'):
        if key in factors and counts.get(key):
            peak += factors[key] * int(counts[key])
            parts.append(f'{factors[key]:g} x {int(counts[key])} {key}')
    if operation == 'mesh.preview' and counts.get('face_zone_bytes'):
        for key, factor in FACE_ZONE_FACTORS.items():
            if counts.get(key):
                peak += factor * int(counts[key])
        parts.append('a full read for the face zones')
    return Estimate(operation, int(peak), counts,
                    ' + '.join(parts) or 'fixed cost only')


# --------------------------------------------------------------------------- #
# Admission
# --------------------------------------------------------------------------- #

class OverBudget(Exception):
    """The worker was not started: the estimate exceeds the budget."""

    reason = 'over_budget'

    def __init__(self, estimate: Estimate, snapshot_: MemorySnapshot):
        self.estimate = estimate
        self.snapshot = snapshot_
        super().__init__(
            '{0} needs about {1} and {2} is free for it ({3} available, '
            '{4} held by WSL, {5} kept for the window)'.format(
                estimate.operation, format_bytes(estimate.peak_bytes),
                format_bytes(snapshot_.budget),
                format_bytes(snapshot_.available),
                format_bytes(snapshot_.wsl_vm),
                format_bytes(snapshot_.reserve)))

    def to_dict(self) -> dict:
        return {'reason': self.reason, 'message': str(self),
                'estimate': self.estimate.to_dict(),
                'budget': self.snapshot.to_dict()}


def format_bytes(value: int) -> str:
    value = float(value)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if abs(value) < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit in ('B', 'KB') \
                else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} GB'


@dataclass
class Grant:
    """Permission to run one worker, with the memory cap it runs under."""

    operation: str
    estimate: Estimate
    cap_bytes: int
    snapshot: MemorySnapshot
    heavy: bool
    _controller: 'AdmissionController | None' = None
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._controller is not None and self.heavy:
            self._controller._release_heavy()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.release()
        return False

    def to_dict(self) -> dict:
        return {'operation': self.operation, 'cap_bytes': self.cap_bytes,
                'heavy': self.heavy, 'estimate': self.estimate.to_dict(),
                'budget': self.snapshot.to_dict()}


def cap_for(estimate: Estimate, snapshot_: MemorySnapshot) -> int:
    """The Job Object limit a granted worker runs under."""
    forced = _env_bytes(ENV_WORKER_CAP)
    if forced is not None:
        return forced
    wanted = max(int(estimate.peak_bytes * CAP_HEADROOM), WORKER_FLOOR_BYTES)
    if snapshot_.budget > 0:
        wanted = min(wanted, max(snapshot_.budget, estimate.peak_bytes))
    return int(wanted)


class AdmissionController:
    """One heavy worker at a time, in priority order, within the budget."""

    def __init__(self, *, snapshot_fn=None):
        self._snapshot_fn = snapshot_fn or snapshot
        self._heavy_busy = False
        self._waiters: list[tuple[int, int, asyncio.Future]] = []
        self._sequence = itertools.count()

    @property
    def busy(self) -> bool:
        return self._heavy_busy

    @property
    def queued(self) -> int:
        return sum(1 for *_rest, future in self._waiters if not future.done())

    async def admit(self, operation: str, estimate: Estimate, *,
                    priority: int = PRIORITY_INTERACTIVE,
                    budget_override: int | None = None) -> Grant:
        """Wait for the heavy slot, then grant or refuse with the numbers.

        The budget is measured when the slot is reached, not when the request
        was queued: the job ahead may have been a WSL run that left the VM
        holding gigabytes.
        """
        heavy = operation in HEAVY_OPERATIONS
        if heavy:
            await self._acquire_heavy(priority)
        try:
            measured = await asyncio.to_thread(self._snapshot_fn)
            if budget_override is not None:
                # CR3 "Build anyway": the user raised the budget knowingly.
                measured = dataclasses.replace(
                    measured, budget=int(budget_override))
            if estimate.peak_bytes > measured.budget:
                raise OverBudget(estimate, measured)
            return Grant(operation, estimate, cap_for(estimate, measured),
                         measured, heavy, self)
        except BaseException:
            if heavy:
                self._release_heavy()
            raise

    async def _acquire_heavy(self, priority: int) -> None:
        if not self._heavy_busy and not self.queued:
            self._heavy_busy = True
            return
        future = asyncio.get_running_loop().create_future()
        entry = (int(priority), next(self._sequence), future)
        self._waiters.append(entry)
        self._waiters.sort(key=lambda item: (item[0], item[1]))
        try:
            await future
        except BaseException:
            if entry in self._waiters:
                self._waiters.remove(entry)
            if future.done() and not future.cancelled():
                # Handed the slot as we were cancelled: pass it on.
                self._release_heavy()
            raise

    def _release_heavy(self) -> None:
        while self._waiters:
            _priority, _sequence, future = self._waiters.pop(0)
            if future.done():
                continue
            future.set_result(True)
            return                  # the slot passes straight to the waiter
        self._heavy_busy = False


_controller: AdmissionController | None = None


def controller() -> AdmissionController:
    """The process-wide admission controller."""
    global _controller
    if _controller is None:
        _controller = AdmissionController()
    return _controller


def reset_controller() -> None:
    """Tests only: forget queued and granted work."""
    global _controller
    _controller = None


# --------------------------------------------------------------------------- #
# Job Objects
# --------------------------------------------------------------------------- #

JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


class JobObjectError(OSError):
    pass


def _job_structures():
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                    ('PerJobUserTimeLimit', ctypes.c_int64),
                    ('LimitFlags', wintypes.DWORD),
                    ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t),
                    ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t),
                    ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class EXTENDED(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', BASIC),
                    ('IoInfo', IO),
                    ('ProcessMemoryLimit', ctypes.c_size_t),
                    ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t),
                    ('PeakJobMemoryUsed', ctypes.c_size_t)]

    return EXTENDED


def job_objects_supported() -> bool:
    return sys.platform == 'win32'


class JobObject:
    """A Windows Job Object that caps each member's committed memory.

    ``KILL_ON_JOB_CLOSE`` means the worker dies with this handle -- so with
    the GUI process, even when that process is killed and never runs its
    cleanup.
    """

    def __init__(self, memory_limit_bytes: int):
        if not job_objects_supported():
            raise JobObjectError('Job Objects exist only on Windows')
        import ctypes

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        self._kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        self._kernel32.OpenProcess.restype = ctypes.c_void_p
        for name in ('SetInformationJobObject', 'QueryInformationJobObject'):
            getattr(self._kernel32, name).argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                ctypes.c_uint32] + (
                    [ctypes.c_void_p] if name.startswith('Query') else [])
        self._kernel32.AssignProcessToJobObject.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p]
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.limit = int(memory_limit_bytes)
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise JobObjectError(ctypes.get_last_error(),
                                 'CreateJobObjectW failed')
        info = _job_structures()()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        info.ProcessMemoryLimit = self.limit
        if not self._kernel32.SetInformationJobObject(
                self._handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise JobObjectError(error, 'SetInformationJobObject failed')

    def assign(self, pid: int) -> None:
        ctypes = self._ctypes
        process = self._kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not process:
            raise JobObjectError(ctypes.get_last_error(),
                                 f'OpenProcess({pid}) failed')
        try:
            if not self._kernel32.AssignProcessToJobObject(
                    self._handle, process):
                raise JobObjectError(ctypes.get_last_error(),
                                     'AssignProcessToJobObject failed')
        finally:
            self._kernel32.CloseHandle(process)

    def peak_process_memory(self) -> int:
        """Largest commit any member reached, in bytes (0 if unreadable)."""
        if not self._handle:
            return 0
        ctypes = self._ctypes
        info = _job_structures()()
        returned = ctypes.c_uint32(0)
        if not self._kernel32.QueryInformationJobObject(
                self._handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info),
                ctypes.byref(returned)):
            return 0
        return int(info.PeakProcessMemoryUsed)

    def cpu_seconds(self) -> float:
        """User plus kernel time every member has used (-1 if unreadable).

        A worker that reached its cap and stopped using the processor is
        wedged, not measuring: an allocation failed somewhere Python could
        not turn into a ``MemoryError`` (a thread start, an import).
        """
        if not self._handle:
            return -1.0
        ctypes = self._ctypes

        class ACCOUNTING(ctypes.Structure):
            _fields_ = [('TotalUserTime', ctypes.c_int64),
                        ('TotalKernelTime', ctypes.c_int64),
                        ('ThisPeriodTotalUserTime', ctypes.c_int64),
                        ('ThisPeriodTotalKernelTime', ctypes.c_int64),
                        ('TotalPageFaultCount', ctypes.c_uint32),
                        ('TotalProcesses', ctypes.c_uint32),
                        ('ActiveProcesses', ctypes.c_uint32),
                        ('TotalTerminatedProcesses', ctypes.c_uint32)]

        info = ACCOUNTING()
        returned = ctypes.c_uint32(0)
        if not self._kernel32.QueryInformationJobObject(
                self._handle, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info),
                ctypes.byref(returned)):
            return -1.0
        return (info.TotalUserTime + info.TotalKernelTime) / 1e7

    def close(self) -> None:
        handle, self._handle = getattr(self, '_handle', None), None
        if handle:
            self._kernel32.CloseHandle(handle)

    def __del__(self):
        try:
            self.close()
        except Exception:                                   # noqa: BLE001
            pass
