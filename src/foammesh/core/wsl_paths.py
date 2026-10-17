"""Host paths that OpenFOAM can open from inside WSL (DP-1440).

OpenFOAM refuses a file name that holds a space or a quote
(``fileName::stripInvalid() called for invalid fileName``), and a Windows
user name such as ``KIRAN JOY`` puts one in nearly every project path.

Copying the case to a space-free folder and syncing it back was rejected: a
cancelled or crashed run would leave the project half written, and every
caller that reads results would have to know about the copy. A Windows
junction or a symlink does not help either: ``getcwd`` reports the real
path, which still has the space (measured 2026-10-12, the same abort).

What does work is a bind mount. The folder that holds the unsafe name is
mounted a second time at ``/mnt/foammesh-cases/<hash>``; the kernel reports
that name as the working directory, and both names are the same folder, so
results, logs and polyMesh land in the project, cancel and the Plan 35 run
records work unchanged, and the user only ever sees their own path.

The mount is made by ``wsl.exe --user root`` (always allowed for the
user's own distribution), is remade whenever it is missing, and goes away
with the WSL virtual machine. If it cannot be made the run is refused with a
sentence that says so.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import subprocess
import threading
import time

BACKSLASH = chr(92)
# What OpenFOAM's fileName::valid() rejects, plus control whitespace.
UNSAFE_CHARACTERS = frozenset([' ', chr(9), chr(13), chr(10), chr(39), chr(34)])
MOUNT_ROOT = '/mnt/foammesh-cases'
# A mount just verified is trusted for this long: WSL does not stop its
# virtual machine within seconds of a call that held it.
VERIFIED_SECONDS = 3.0

_MOUNT_SCRIPT = (
    'src="$1"; mp="$2"; '
    'mountpoint -q "$mp" 2>/dev/null && exit 0; '
    'mkdir -p "$mp" && mount --bind "$src" "$mp"')

_lock = threading.Lock()
_verified: dict[tuple, float] = {}
_origin: dict[str, str] = {}


class WslPathError(ValueError):
    """A host path cannot be given a name WSL can use."""


def is_unsafe(component: str) -> bool:
    return any(char in UNSAFE_CHARACTERS for char in component)


def _refusal(folder, reason) -> WslPathError:
    return WslPathError(
        f'The folder "{folder}" has a space or a quote in its name, which '
        'OpenFOAM cannot open, and FoamMesh could not give it a space-free '
        f'name ({reason}). Move the project to a folder whose whole path has '
        'no spaces or quotes, for example D:' + BACKSLASH + 'FoamMeshCases.')


def _mount(source: PurePosixPath, host: Path, distribution, wsl_executable):
    mount_point = f'{MOUNT_ROOT}/' + hashlib.sha1(
        (str(distribution) + '|' + str(source).lower()).encode('utf-8')
    ).hexdigest()[:12]
    key = (str(distribution), mount_point)
    with _lock:
        _origin[mount_point] = str(host)
        if time.monotonic() - _verified.get(key, -1e9) < VERIFIED_SECONDS:
            return PurePosixPath(mount_point)
    argv = [wsl_executable]
    if distribution:
        argv += ['--distribution', str(distribution)]
    argv += ['--user', 'root', '--exec', 'bash', '-c', _MOUNT_SCRIPT, '_',
             str(source), mount_point]
    try:
        done = subprocess.run(
            argv, capture_output=True, timeout=60,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _refusal(host, error) from error
    if done.returncode != 0:
        said = (done.stdout + done.stderr).decode('utf-8', 'replace')
        said = said.replace(chr(0), '').strip()
        raise _refusal(host, said or f'mount exit {done.returncode}')
    with _lock:
        _verified[key] = time.monotonic()
    return PurePosixPath(mount_point)


def runtime_path(path, *, distribution=None, wsl_executable='wsl.exe') -> str:
    """The ``/mnt/...`` name of a Windows drive path, free of spaces/quotes."""
    windows = PureWindowsPath(str(path))
    parts = list(windows.parts)
    posix = PurePosixPath('/mnt', windows.drive.rstrip(':').lower())
    host = Path(parts[0])
    for index, part in enumerate(parts[1:], start=1):
        host = host / part
        if not is_unsafe(part):
            posix = posix / part
            continue
        if index == len(parts) - 1 and not host.is_dir():
            raise WslPathError(
                f'The file name "{part}" has a space or a quote in it, which '
                'OpenFOAM cannot open. Choose a name without them.')
        host.mkdir(parents=True, exist_ok=True)
        posix = _mount(posix / part, host, distribution, wsl_executable)
    return str(posix)


def host_from_runtime(path: str) -> str:
    """The user's own path for a ``/mnt/...`` name, with mounts looked through."""
    if path.startswith(MOUNT_ROOT + '/'):
        name, _, rest = path[len(MOUNT_ROOT) + 1:].partition('/')
        origin = _origin.get(f'{MOUNT_ROOT}/{name}')
        if origin:
            return (origin + '/' + rest).replace(BACKSLASH, '/').rstrip('/')
        return path
    if path.startswith('/mnt/') and len(path) >= 7 and path[6] == '/':
        return (path[5].upper() + ':' + path[6:]).replace(BACKSLASH, '/')
    return path
