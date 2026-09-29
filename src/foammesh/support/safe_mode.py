"""Plan 35 CR8: graphics safe mode, and when a start should use it.

A GPU driver that kills the process cannot be survived from inside it (plan
§9 D11). What this module does instead is decide, before the Qt application
exists, whether this start should draw with the safest settings:

* ``--safe-mode`` on the command line (the installer's "FoamMesh (safe
  mode)" Start-menu entry passes exactly that);
* "Start in safe mode next time", chosen on the previous start's crash
  notice, or asked for by a weak OpenGL found at run time (one start only);
* two render-attributed deaths in a row: a dead session whose last operation
  was ``render:*``, or whose crash dump faulted inside a GPU driver DLL.

No Qt and no VTK here: it runs before either is imported, and the minidump
reader is plain ``struct``. The state lives in ``safe_mode.json`` next to the
lifecycle logs, so it survives the process it is about.
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path

FLAG = '--safe-mode'
STATE_FILE = 'safe_mode.json'
RENDER_OP_PREFIX = 'render:'
#: Render-attributed deaths in a row that switch the next start to safe mode.
AUTOMATIC_AFTER = 2
#: The render preset safe mode draws with (``render_style.SAFE_PRESET``).
SAFE_PRESET = 'fast'

#: User-mode OpenGL/D3D driver DLLs. A crash whose faulting module is one of
#: these is the driver's, whatever the last operation said.
GPU_DRIVER_PREFIXES = (
    'ig',          # Intel: ig9icd64, igxelpicd64, igdumdim64, igd10iumd64 ...
    'atio', 'atig', 'atiu', 'atidx', 'amdx', 'amdvlk', 'aticfx',   # AMD
    'nvogl', 'nvwgf', 'nvd3dum', 'nvldumd', 'nvoglv',              # NVIDIA
)
GPU_DRIVER_NAMES = frozenset({'opengl32sw.dll', 'vulkan-1.dll'})

REASON_FLAG = 'FoamMesh was started with --safe-mode.'
REASON_AUTOMATIC = ('FoamMesh closed unexpectedly twice in a row while drawing '
                    'the viewport.')
REASON_REQUESTED = 'Safe mode was chosen for this start.'


@dataclass(frozen=True)
class Decision:
    """What this start does about graphics safe mode."""
    active: bool = False
    #: Why, in a sentence the viewport placeholder can show.
    reason: str = ''
    #: Offer "Start in safe mode next time" on the crash notice.
    offer: bool = False
    #: Render-attributed deaths in a row, this one included.
    render_deaths: int = 0


_current = Decision()


def current() -> Decision:
    """The decision this process made at start (inactive until `decide`)."""
    return _current


def requested_by_argv(argv) -> bool:
    return FLAG in list(argv or [])[1:]


# --------------------------------------------------------------------------
# Which module a crash dump faulted in
# --------------------------------------------------------------------------

_MODULE_LIST_STREAM = 4
_EXCEPTION_STREAM = 6
_MODULE_SIZE = 108


def _string(data: bytes, rva: int) -> str:
    (length,) = struct.unpack_from('<I', data, rva)
    return data[rva + 4:rva + 4 + length].decode('utf-16-le', 'replace')


def faulting_module(dump_path) -> str | None:
    """The file name of the module the dump's exception address lies in.

    Reads the minidump's exception and module-list streams only. None when
    the dump has no exception (a hang dump) or cannot be read.
    """
    try:
        data = Path(dump_path).read_bytes()
    except OSError:
        return None
    try:
        if data[:4] != b'MDMP':
            return None
        count, directory = struct.unpack_from('<II', data, 8)
        streams = {}
        for index in range(count):
            kind, _size, rva = struct.unpack_from('<III', data, directory + 12 * index)
            streams.setdefault(kind, rva)
        if _EXCEPTION_STREAM not in streams or _MODULE_LIST_STREAM not in streams:
            return None
        # MINIDUMP_EXCEPTION_STREAM: ThreadId, alignment, then the record:
        # ExceptionCode, ExceptionFlags, ExceptionRecord (u64), ExceptionAddress.
        (address,) = struct.unpack_from('<Q', data, streams[_EXCEPTION_STREAM] + 8 + 16)
        base = streams[_MODULE_LIST_STREAM]
        (modules,) = struct.unpack_from('<I', data, base)
        for index in range(modules):
            offset = base + 4 + _MODULE_SIZE * index
            start, size = struct.unpack_from('<QI', data, offset)
            if start <= address < start + size:
                (name_rva,) = struct.unpack_from('<I', data, offset + 20)
                return _string(data, name_rva).replace('/', '\\').rsplit('\\', 1)[-1]
    except (struct.error, IndexError, ValueError):
        return None
    return None


def is_gpu_driver(module) -> bool:
    name = str(module or '').lower()
    if not name.endswith('.dll'):
        return False
    return name in GPU_DRIVER_NAMES or name.startswith(GPU_DRIVER_PREFIXES)


def _crash_dumps(record: dict) -> list[str]:
    return [path for path in record.get('dumps') or []
            if '.crash.' in Path(path).name]


def is_render_attributed(record: dict) -> bool:
    """Did this dead session die drawing? (its last op, or its dump's module)"""
    if str(record.get('last_op') or '').startswith(RENDER_OP_PREFIX):
        return True
    return any(is_gpu_driver(faulting_module(path))
               for path in _crash_dumps(record))


# --------------------------------------------------------------------------
# The persisted state
# --------------------------------------------------------------------------

def _state_path(directory) -> Path | None:
    return None if directory is None else Path(directory) / STATE_FILE


def load_state(directory) -> dict:
    path = _state_path(directory)
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
    except (AttributeError, OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def save_state(directory, state: dict) -> None:
    path = _state_path(directory)
    if path is None:
        return
    try:
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(state, indent=1), encoding='utf-8')
        os.replace(temporary, path)
    except OSError:
        pass


def remember_next_start(directory, reason: str = REASON_REQUESTED) -> None:
    """Start in safe mode next time (the crash notice's button, a weak GL)."""
    state = load_state(directory)
    state['next_start'] = True
    state['next_reason'] = str(reason or REASON_REQUESTED)
    save_state(directory, state)


def next_start_requested(directory) -> bool:
    return bool(load_state(directory).get('next_start'))


def _ended_at(record: dict) -> str:
    return str(record.get('ended_at') or '')


def decide(argv, dead_sessions, directory) -> Decision:
    """This start's safe-mode decision; updates the persisted count.

    A start that finds no dead session follows a clean exit, which breaks a
    run of render deaths. A one-shot "next time" request is used up here.
    """
    global _current
    state = load_state(directory)
    count = int(state.get('consecutive_render_deaths') or 0)
    dead = sorted(dead_sessions or [], key=_ended_at)
    attributed = False
    if not dead:
        count = 0
    for record in dead:
        if is_render_attributed(record):
            count += 1
            attributed = True
        else:
            count = 0
    state['consecutive_render_deaths'] = count

    reason = ''
    if requested_by_argv(argv):
        reason = REASON_FLAG
    elif state.get('next_start'):
        reason = str(state.get('next_reason') or REASON_REQUESTED)
    elif count >= AUTOMATIC_AFTER:
        reason = REASON_AUTOMATIC
    state.pop('next_start', None)
    state.pop('next_reason', None)
    if reason:
        state['last_reason'] = reason
    save_state(directory, state)
    _current = Decision(active=bool(reason), reason=reason,
                        offer=attributed and not reason, render_deaths=count)
    return _current


def apply_environment(decision: Decision) -> None:
    """Before the QApplication: Qt's own widgets draw with software OpenGL."""
    if decision.active:
        os.environ['QT_OPENGL'] = 'software'


def reset() -> None:
    """Tests only: forget this process's decision."""
    global _current
    _current = Decision()
