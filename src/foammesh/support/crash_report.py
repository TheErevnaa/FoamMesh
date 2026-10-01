#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR0 step 7: one zip that says what happened, and nothing private.

Help > "Create crash report..." shows the user the list :func:`collect`
returns, and only then writes the zip (:func:`write`) -- by default to the
Desktop. Nothing is sent anywhere (§9 D4: local only).

The bundle holds:

* the application logs (``~/.FoamMesh/logs``): ``foammesh.log`` and its
  rotations, ``faulthandler*.log``, ``native.*.log``, ``lifecycle.log``,
  ``watchdog.log``, ``vtk.log`` and the session records;
* the newest crash and hang dumps, from the crash helper's folder and from
  Windows Error Reporting's (``%LOCALAPPDATA%\\CrashDumps\\FoamMesh.exe.*``);
* the open case's ``foammesh/logs`` and a *listing* of every ``polyMesh``
  folder -- names and sizes, never the mesh itself;
* ``system.txt``: the FoamMesh version and build, Python/PySide6/VTK
  versions, Windows version, memory, the display adapters (from the registry;
  nothing is installed to find them), the OpenGL vendor and renderer the view
  reported, ``wsl --status``, ``wsl -l -v`` and ``~/.wslconfig``.

Mesh and geometry never go in: a file is refused when its name is one of the
polyMesh files or its suffix is a mesh or CAD format (:func:`is_mesh_data`),
whatever folder it was found in. Each text log contributes at most its last
:data:`TEXT_TAIL_BYTES`; the bundle stops adding files at
:data:`BUNDLE_LIMIT_BYTES` and lists what it left out and why.

This module imports no Qt and no VTK.
"""
from __future__ import annotations

import datetime
import io
import os
import platform
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

#: The most of any one text log that goes in (its end, where a crash is).
TEXT_TAIL_BYTES = 4 * 1024 * 1024
#: The most the bundle holds, uncompressed.
BUNDLE_LIMIT_BYTES = 200 * 1024 * 1024
#: Dumps taken, newest first.
KEEP_DUMPS = 3
#: Seconds each ``wsl`` query may take.
WSL_TIMEOUT_SECONDS = 10

POLYMESH_FILES = frozenset({
    'points', 'faces', 'owner', 'neighbour', 'boundary', 'cellZones',
    'faceZones', 'pointZones', 'cellLevel', 'pointLevel', 'level0Edge',
    'cellProcAddressing', 'faceProcAddressing', 'pointProcAddressing',
})
MESH_SUFFIXES = frozenset({
    '.stl', '.stlb', '.obj', '.step', '.stp', '.iges', '.igs', '.brep',
    '.msh', '.med', '.unv', '.vtk', '.vtu', '.vtp', '.vtm', '.cgns', '.su2',
    '.foam', '.ply', '.x_t', '.sat', '.fms', '.ftr', '.eMesh',
})
TEXT_SUFFIXES = frozenset({'.log', '.txt', '.json', ''})

DISPLAY_CLASS = (r'SYSTEM\CurrentControlSet\Control\Class'
                 r'\{4d36e968-e325-11ce-bfc1-08002be10318}')


def is_mesh_data(path) -> bool:
    """True for anything that is (part of) a mesh or a geometry."""
    path = Path(path)
    if path.name in POLYMESH_FILES:
        return True
    suffixes = [suffix.lower() for suffix in path.suffixes]
    if suffixes and suffixes[-1] == '.gz':
        # A compressed polyMesh file ("points.gz") or mesh ("part.stl.gz").
        if path.stem in POLYMESH_FILES:
            return True
        suffixes = suffixes[:-1]
    return bool(suffixes) and suffixes[-1] in {s.lower() for s in MESH_SUFFIXES}


@dataclass
class Entry:
    arcname: str
    source: Path | None = None
    data: bytes | None = None
    size: int = 0
    truncated: bool = False


@dataclass
class Manifest:
    entries: list[Entry] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def size(self) -> int:
        return sum(entry.size for entry in self.entries)

    def names(self) -> list[str]:
        return [entry.arcname for entry in self.entries]

    def describe(self) -> list[str]:
        """One line per file for the confirmation dialog."""
        lines = []
        for entry in self.entries:
            note = ' (last part only)' if entry.truncated else ''
            lines.append(f'{entry.arcname}  {_size_text(entry.size)}{note}')
        for name, reason in self.skipped:
            lines.append(f'left out: {name} ({reason})')
        return lines


def _size_text(size: int) -> str:
    for unit in ('bytes', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.0f} {unit}' if unit == 'bytes' else f'{size:.1f} {unit}'
        size /= 1024.0
    return f'{size} bytes'


# --------------------------------------------------------------------------
# What goes in
# --------------------------------------------------------------------------

def default_log_directory() -> Path:
    return Path.home() / '.FoamMesh' / 'logs'


def dump_directories() -> list[tuple[Path, str]]:
    """(folder, glob) pairs where FoamMesh dumps are written."""
    local = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    return [(Path(local) / 'FoamMesh' / 'CrashDumps', '*.dmp'),
            (Path(local) / 'CrashDumps', 'FoamMesh.exe.*.dmp')]


class _Builder:
    def __init__(self, limit: int):
        self.manifest = Manifest()
        self.limit = limit

    def _room(self, size: int) -> bool:
        return self.manifest.size + size <= self.limit

    def add_file(self, path: Path, arcname: str, *, tail: bool = True) -> None:
        if is_mesh_data(path):
            self.manifest.skipped.append((arcname, 'mesh or geometry data'))
            return
        try:
            size = path.stat().st_size
        except OSError as error:
            self.manifest.skipped.append((arcname, f'unreadable: {error}'))
            return
        truncated = tail and size > TEXT_TAIL_BYTES
        taken = TEXT_TAIL_BYTES if truncated else size
        if not self._room(taken):
            self.manifest.skipped.append(
                (arcname, f'{_size_text(size)} would pass the '
                          f'{_size_text(self.limit)} limit'))
            return
        self.manifest.entries.append(
            Entry(arcname, source=path, size=taken, truncated=truncated))

    def add_text(self, arcname: str, text: str) -> None:
        data = text.encode('utf-8', 'replace')
        if not self._room(len(data)):
            self.manifest.skipped.append((arcname, 'size limit'))
            return
        self.manifest.entries.append(Entry(arcname, data=data, size=len(data)))


def _files(directory: Path, pattern: str = '*'):
    try:
        return sorted((path for path in directory.glob(pattern) if path.is_file()),
                      key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return []


def polymesh_listing(case_root: Path) -> str:
    """Names and sizes of every file under every ``polyMesh`` in the case."""
    lines = [f'polyMesh listing of {case_root} (names and sizes only)']
    found = False
    try:
        folders = sorted(case_root.rglob('polyMesh'))
    except OSError as error:
        return f'{lines[0]}\ncould not walk the case: {error}\n'
    for folder in folders:
        if not folder.is_dir():
            continue
        found = True
        lines.append(f'{folder.relative_to(case_root)}/')
        for path in sorted(folder.rglob('*')):
            try:
                size = path.stat().st_size if path.is_file() else None
            except OSError:
                size = None
            name = path.relative_to(folder)
            lines.append(f'  {name}{"/" if size is None else f"  {size} bytes"}')
    if not found:
        lines.append('no polyMesh folder')
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------
# system.txt
# --------------------------------------------------------------------------

def _memory() -> str:
    if sys.platform != 'win32':
        return 'unknown'
    try:
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_uint32),
                        ('dwMemoryLoad', ctypes.c_uint32),
                        ('ullTotalPhys', ctypes.c_uint64),
                        ('ullAvailPhys', ctypes.c_uint64),
                        ('ullTotalPageFile', ctypes.c_uint64),
                        ('ullAvailPageFile', ctypes.c_uint64),
                        ('ullTotalVirtual', ctypes.c_uint64),
                        ('ullAvailVirtual', ctypes.c_uint64),
                        ('ullAvailExtendedVirtual', ctypes.c_uint64)]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 'unknown'
        gib = 1024 ** 3
        return (f'{status.ullTotalPhys / gib:.1f} GiB physical, '
                f'{status.ullAvailPhys / gib:.1f} GiB free '
                f'({status.dwMemoryLoad}% in use); commit '
                f'{status.ullAvailPageFile / gib:.1f} of '
                f'{status.ullTotalPageFile / gib:.1f} GiB free')
    except Exception as error:                             # noqa: BLE001
        return f'unknown ({error})'


def display_adapters() -> list[str]:
    """The display adapters Windows knows, with their driver versions."""
    if sys.platform != 'win32':
        return []
    try:
        import winreg
    except ImportError:
        return []
    adapters = []
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DISPLAY_CLASS)
    except OSError:
        return []
    with root:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            if not name.isdigit():
                continue
            try:
                with winreg.OpenKey(root, name) as key:
                    values = {}
                    for value in ('DriverDesc', 'DriverVersion', 'DriverDate',
                                  'ProviderName'):
                        try:
                            values[value] = winreg.QueryValueEx(key, value)[0]
                        except OSError:
                            values[value] = ''
            except OSError:
                continue
            if values['DriverDesc']:
                adapters.append('{DriverDesc} (driver {DriverVersion}, '
                                '{DriverDate}, {ProviderName})'.format(**values))
    return adapters


def _decode(output: bytes) -> str:
    """``wsl.exe`` writes UTF-16LE; anything else is UTF-8."""
    if not output:
        return ''
    if output.count(b'\x00') > len(output) // 4:
        return output.decode('utf-16-le', 'replace').replace('\x00', '')
    return output.decode('utf-8', 'replace')


def run_wsl(arguments, timeout: float = WSL_TIMEOUT_SECONDS) -> str:
    try:
        result = subprocess.run(
            ['wsl.exe', *arguments], capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError) as error:
        return f'(could not run wsl {" ".join(arguments)}: {error})'
    text = (_decode(result.stdout) + _decode(result.stderr)).strip()
    return text or f'(no output, exit code {result.returncode})'


def _module_version(name: str) -> str:
    module = sys.modules.get(name)
    if module is None:
        return 'not loaded'
    return str(getattr(module, '__version__', None)
               or getattr(module, 'VTK_VERSION', None) or 'unknown')


def system_text(*, version: str | None = None, gl_info: dict | None = None,
                probe_wsl: bool = True) -> str:
    try:
        from foammesh.core.branding import _build_identity
        build, commit = _build_identity(os.environ)
    except Exception:                                      # noqa: BLE001
        build, commit = 'unknown', 'unknown'
    vtk_version = 'not loaded'
    if 'vtkmodules' in sys.modules:
        try:
            from vtkmodules.vtkCommonCore import vtkVersion
            vtk_version = vtkVersion.GetVTKVersion()
        except Exception:                                  # noqa: BLE001
            vtk_version = 'unknown'
    lines = [
        f'FoamMesh crash report, {datetime.datetime.now().isoformat(timespec="seconds")}',
        f'FoamMesh version: {version or "unknown"}',
        f'build: {build}; commit: {commit}',
        f'frozen: {bool(getattr(sys, "frozen", False))}; executable: {sys.executable}',
        f'Python: {sys.version.split()[0]}',
        f'PySide6: {_module_version("PySide6")}',
        f'VTK: {vtk_version}',
        f'OS: {platform.platform()} ({platform.version()})',
        f'processor: {platform.processor()}; cores: {os.cpu_count()}',
        f'memory: {_memory()}',
    ]
    adapters = display_adapters()
    lines.append('display adapters:' if adapters else 'display adapters: unknown')
    lines.extend(f'  {adapter}' for adapter in adapters)
    gl_info = gl_info or {}
    lines.append(f'OpenGL vendor: {gl_info.get("vendor") or "unknown"}')
    lines.append(f'OpenGL renderer: {gl_info.get("renderer") or "unknown"}')
    lines.append(f'OpenGL version: {gl_info.get("version") or "unknown"}')
    # The class of the adapter drawing (gpu_profile) sets the display budgets.
    lines.append(f'drawing GPU class: {gl_info.get("tier") or "unknown"}')
    memory = gl_info.get('gpu_memory_bytes')
    try:
        memory = _size_text(int(memory)) if memory else 'unknown'
    except (TypeError, ValueError):
        memory = 'unknown'
    lines.append(f'drawing GPU memory: {memory}')
    if probe_wsl and sys.platform == 'win32':
        lines += ['', '--- wsl --status ---', run_wsl(['--status']),
                  '', '--- wsl -l -v ---', run_wsl(['-l', '-v'])]
    config = Path.home() / '.wslconfig'
    lines += ['', '--- .wslconfig ---']
    try:
        lines.append(config.read_text(encoding='utf-8', errors='replace'))
    except OSError:
        lines.append('(none)')
    return '\n'.join(lines) + '\n'


# --------------------------------------------------------------------------
# Collect and write
# --------------------------------------------------------------------------

def collect(case_root=None, *, log_dir=None, dump_dirs=None,
            version: str | None = None, gl_info: dict | None = None,
            probe_wsl: bool = True,
            limit: int = BUNDLE_LIMIT_BYTES) -> Manifest:
    """What the bundle would hold. Reads sizes only; copies nothing."""
    builder = _Builder(limit)
    log_dir = Path(log_dir) if log_dir else default_log_directory()
    if not gl_info:
        # Plan 35 CR8: the strings the viewport recorded, this session or
        # the one that died (``gl_info.json`` in the logs folder).
        from foammesh.rendering import gl_health
        gl_info = gl_health.recorded(log_dir)
    builder.add_text('system.txt', system_text(
        version=version, gl_info=gl_info, probe_wsl=probe_wsl))
    for path in _files(log_dir):
        builder.add_file(path, f'logs/{path.name}')
    dumps = []
    for folder, pattern in (dump_dirs if dump_dirs is not None
                            else dump_directories()):
        dumps += _files(Path(folder), pattern)
    dumps.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for path in dumps[:KEEP_DUMPS]:
        builder.add_file(path, f'dumps/{path.name}', tail=False)
    for path in dumps[KEEP_DUMPS:]:
        builder.manifest.skipped.append((f'dumps/{path.name}', 'older dump'))
    if case_root:
        case_root = Path(case_root)
        case_logs = case_root / 'foammesh' / 'logs'
        if case_logs.is_dir():
            for path in sorted(case_logs.rglob('*')):
                if path.is_file():
                    builder.add_file(
                        path, f'case/logs/{path.relative_to(case_logs).as_posix()}')
        builder.add_text('case/polyMesh-listing.txt', polymesh_listing(case_root))
    return builder.manifest


def default_destination() -> Path:
    desktop = Path.home() / 'Desktop'
    if sys.platform == 'win32':
        try:
            import ctypes
            from ctypes import wintypes

            buffer = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            # CSIDL_DESKTOPDIRECTORY; follows a Desktop redirected to OneDrive.
            if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0,
                                                      buffer) == 0:
                desktop = Path(buffer.value)
        except Exception:                                  # noqa: BLE001
            pass
    return desktop if desktop.is_dir() else Path.home()


def _read(entry: Entry) -> bytes:
    if entry.data is not None:
        return entry.data
    with open(entry.source, 'rb') as source:
        if entry.truncated:
            source.seek(-entry.size, os.SEEK_END)
        return source.read(entry.size)


def write(manifest: Manifest, destination=None) -> Path:
    """Write the zip into ``destination`` (a folder; the Desktop by default)."""
    folder = Path(destination) if destination else default_destination()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    path = folder / f'FoamMesh-crash-report-{stamp}.zip'
    contents = io.StringIO()
    contents.write('\n'.join(manifest.describe()) + '\n')
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for entry in manifest.entries:
            try:
                data = _read(entry)
            except OSError as error:
                contents.write(f'could not read {entry.arcname}: {error}\n')
                continue
            if entry.truncated:
                data = (f'[... the first part of this file was left out; the '
                        f'last {entry.size} bytes follow ...]\n').encode() + data
            bundle.writestr(entry.arcname, data)
        bundle.writestr('CONTENTS.txt', contents.getvalue())
    return path
