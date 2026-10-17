"""Prefer the dedicated (high-performance) GPU, and fall back when it fails.

Plan 39 VI2/VI14 support (GPUPREF, 2026-10-12). The user's rule: when the
option is on and the PC has a dedicated graphics card, that card draws the
viewport; if nothing works, the integrated graphics do; and the option can be
turned off. All vendors -- NVIDIA, AMD, Intel Arc, anything Windows lists --
not NVIDIA only.

**How.** OpenGL on a hybrid Windows PC draws on the GPU Windows hands the
process, and Windows' per-app graphics preference
(``HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences``, value name = the
program's path, data ``GpuPreference=2;``) is the one switch every vendor's
driver honours. This module writes that value for FoamMesh's own executable
(``sys.executable``: the frozen ``FoamMesh.exe``, or the interpreter in
development) when

* the setting is on (default on),
* Windows lists a dedicated adapter *and* another hardware adapter to choose
  between (:func:`plan`; the adapters come from ``gpu_profile``: DXGI
  ``EnumAdapterByGpuPreference``, else WMI ``Win32_VideoController`` here), and
* no fallback is in force.

Only a value FoamMesh wrote is ever removed or changed (the setting keeps a
marker of what was written); a value the user chose in Windows' own Graphics
settings is left alone. Turning the option off removes ours.

**Fallback.** After the first render window has a context
(:func:`confirm_render`): a software renderer (GDI Generic, llvmpipe,
Microsoft Basic Render) while Windows lists a hardware adapter means the
request failed, so the preference is cleared, the integrated adapter is
requested for the next start (``GpuPreference=1;``, again marked as ours), the
log gets a plain line and the viewport a one-time notice. A start that dies
before a healthy render leaves a marker file in the logs folder; the next
start finds it, drops the preference the same way, and never tries again until
the user switches the option off and on. Nothing here loops or can stop the
application from starting.

**No launcher stub.** The NVIDIA ``NvOptimusEnablement`` and AMD
``AmdPowerXpressRequestHighPerformance`` exports are read from the executable
itself. FoamMesh is shipped with PyInstaller's stock bootloader and
``packaging/`` holds no native launcher, so there is nothing to export them
from and none is added; the Windows preference above covers both vendors. The
NVIDIA shim variable ``gpu_profile.prefer_discrete_gpu`` sets stays as before.

**Tests never touch the registry.** :func:`write_registry` refuses the real
``winreg`` under pytest; tests hand it a stand-in. ``FOAMMESH_GPU_PREF_DRY_RUN=1``
computes and reports what would be written without writing it.

No Qt, no VTK.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from foammesh.rendering import gpu_profile
from foammesh.rendering.gpu_profile import DISCRETE, INTEGRATED, SOFTWARE

logger = logging.getLogger(__name__)

REG_PATH = r'Software\Microsoft\DirectX\UserGpuPreferences'
#: Windows' "High performance" and "Power saving" per-app choices.
HIGH_PERFORMANCE = 'GpuPreference=2;'
POWER_SAVING = 'GpuPreference=1;'
ENV_DRY_RUN = 'FOAMMESH_GPU_PREF_DRY_RUN'
MARKER_FILE = 'gpu_pref_start.json'

NOTICE_SOFTWARE = ('The dedicated GPU could not be used (the viewport was '
                   'drawn by {renderer}). FoamMesh will use the integrated '
                   'graphics from the next start. Settings > Preferences > '
                   'Appearance: turn the dedicated-GPU option off and on '
                   'again to try once more.')
NOTICE_CRASH = ('FoamMesh did not finish starting its viewport last time with '
                'the dedicated GPU, so it is using the integrated graphics. '
                'Settings > Preferences > Appearance: turn the dedicated-GPU '
                'option off and on again to try once more.')


class RegistryRefused(RuntimeError):
    """The real registry was asked for under a test run."""


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def wmi_adapters(runner=None) -> list:
    """Adapters from WMI ``Win32_VideoController`` (the DXGI fallback).

    Uses PowerShell, which every Windows install has. ``runner`` is the
    ``subprocess.run`` stand-in for tests.
    """
    if runner is None:
        if sys.platform != 'win32':
            return []
        import subprocess
        runner = subprocess.run
    command = ['powershell', '-NoProfile', '-NonInteractive', '-Command',
               'Get-CimInstance Win32_VideoController | '
               'Select-Object Name,AdapterRAM | ConvertTo-Json -Compress']
    try:
        done = runner(command, capture_output=True, text=True, timeout=20,
                      creationflags=0x08000000 if sys.platform == 'win32' else 0)
        rows = json.loads(done.stdout or '[]')
    except Exception:                                      # noqa: BLE001
        return []
    if isinstance(rows, dict):
        rows = [rows]
    found = []
    for row in rows if isinstance(rows, list) else []:
        name = str((row or {}).get('Name') or '').strip()
        if name:
            # AdapterRAM is a 32-bit field: 4 GiB reads as 0 or -1 on big cards.
            ram = (row or {}).get('AdapterRAM')
            ram = ram if isinstance(ram, int) and ram > 0 else 0
            found.append(gpu_profile.make_adapter(name, ram))
    return found


def detect(adapters=None, wmi=None) -> list:
    """Every adapter Windows lists, classified, best first."""
    if adapters is not None:
        return gpu_profile.ranked(adapters)
    found = list(gpu_profile.display_adapters())
    if not found:
        found = (wmi or wmi_adapters)()
    return gpu_profile.ranked(found)


@dataclass(frozen=True)
class Plan:
    adapters: tuple
    discrete: tuple
    integrated: tuple
    #: Windows has a dedicated adapter and another hardware one to choose from.
    hybrid: bool
    #: The preference to write when the option is on, or '' when nothing to choose.
    wanted: str
    reason: str


def plan(adapters) -> Plan:
    adapters = tuple(gpu_profile.ranked(adapters))
    discrete = tuple(a for a in adapters if a.tier == DISCRETE)
    integrated = tuple(a for a in adapters if a.tier not in (DISCRETE, SOFTWARE))
    hardware = [a for a in adapters if a.tier != SOFTWARE]
    if not discrete:
        reason = 'no dedicated GPU is listed; the integrated graphics draw'
        return Plan(adapters, discrete, integrated, False, '', reason)
    if len(hardware) < 2:
        reason = (f'{discrete[0].name} is the only GPU; nothing to choose '
                  'between')
        return Plan(adapters, discrete, integrated, False, '', reason)
    reason = f'prefer {discrete[0].name} over {hardware[-1].name}'
    if hardware[-1] is discrete[0]:
        reason = f'prefer {discrete[0].name}'
    return Plan(adapters, discrete, integrated, True, HIGH_PERFORMANCE, reason)


# --------------------------------------------------------------------------
# The registry (one place; refuses under pytest)
# --------------------------------------------------------------------------

def app_executable() -> str:
    """The program Windows keys the preference on: FoamMesh.exe when frozen,
    else the interpreter running the application."""
    return str(Path(sys.executable))


def dry_run(environ=None) -> bool:
    environ = os.environ if environ is None else environ
    return str(environ.get(ENV_DRY_RUN, '')).strip().lower() in (
        '1', 'true', 'yes', 'on')


def _registry(registry):
    real = sys.modules.get('winreg')
    if registry is None or (real is not None and registry is real):
        if 'PYTEST_CURRENT_TEST' in os.environ or 'pytest' in sys.modules:
            raise RegistryRefused(
                'the real registry is not written under pytest; pass a '
                'stand-in winreg')
        if registry is None:
            import winreg
            registry = winreg
    return registry


def read_registry(exe=None, registry=None):
    """The preference data for ``exe``, or None."""
    reg = _registry(registry)
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, REG_PATH, 0, reg.KEY_READ)
    except OSError:
        return None
    try:
        data, _kind = reg.QueryValueEx(key, exe or app_executable())
        return str(data)
    except OSError:
        return None
    finally:
        reg.CloseKey(key)


def write_registry(data, exe=None, registry=None) -> None:
    reg = _registry(registry)
    key = reg.CreateKeyEx(reg.HKEY_CURRENT_USER, REG_PATH, 0, reg.KEY_SET_VALUE)
    try:
        reg.SetValueEx(key, exe or app_executable(), 0, reg.REG_SZ, data)
    finally:
        reg.CloseKey(key)


def delete_registry(exe=None, registry=None) -> bool:
    reg = _registry(registry)
    try:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, REG_PATH, 0, reg.KEY_SET_VALUE)
    except OSError:
        return False
    try:
        reg.DeleteValue(key, exe or app_executable())
        return True
    except OSError:
        return False
    finally:
        reg.CloseKey(key)


# --------------------------------------------------------------------------
# What FoamMesh owns (kept in the application settings)
# --------------------------------------------------------------------------

def is_enabled(settings) -> bool:
    return bool(settings.getUseDedicatedGpu())


def load_state(settings) -> dict:
    state = settings.getGpuPreferenceState()
    return dict(state) if isinstance(state, dict) else {}


def _save_state(settings, state: dict) -> None:
    settings.setGpuPreferenceState(state)


@dataclass
class Outcome:
    """What a reconcile did, for the log, the tests and the diagnostics."""
    action: str = 'none'            # none | write | remove | skipped | dry-run
    value: str = ''
    detail: str = ''
    would_write: str = ''
    would_remove: bool = False


def reconcile(settings, adapters=None, registry=None, exe=None,
              environ=None) -> Outcome:
    """Bring the registry in line with the setting and the fallback state.

    * on, no fallback, a real choice of adapters: ``GpuPreference=2;``
    * fallback in force, a real choice: ``GpuPreference=1;``
    * off, or nothing to choose between: remove the value FoamMesh wrote.

    A value that is there but not ours is never replaced or removed.
    """
    exe = exe or app_executable()
    state = load_state(settings)
    chosen = plan(detect(adapters))
    if not is_enabled(settings):
        want = ''
    elif state.get('fallback'):
        want = POWER_SAVING if chosen.hybrid else ''
    else:
        want = chosen.wanted
    dry = dry_run(environ)
    ours = state.get('written') if state.get('exe') == exe else None
    try:
        current = read_registry(exe, registry)
    except RegistryRefused as refusal:
        return Outcome('skipped', detail=str(refusal))
    except Exception as error:                             # noqa: BLE001
        logger.warning('GPU preference: could not read the registry: %s', error)
        return Outcome('skipped', detail=str(error))

    if not want:
        if ours and current == ours:
            if dry:
                return Outcome('dry-run', detail='would remove our value',
                               would_remove=True)
            try:
                delete_registry(exe, registry)
            except Exception as error:                     # noqa: BLE001
                logger.warning('GPU preference: could not remove it: %s', error)
                return Outcome('skipped', detail=str(error))
            logger.info('GPU preference: removed the value FoamMesh wrote for '
                        '%s (%s)', exe, chosen.reason)
            state.pop('written', None)
            state.pop('exe', None)
            _save_state(settings, state)
            return Outcome('remove', detail=chosen.reason)
        if ours:                              # the user changed it since
            state.pop('written', None)
            state.pop('exe', None)
            _save_state(settings, state)
        return Outcome('none', detail=chosen.reason)

    if current is not None and current != ours:
        return Outcome('none', detail=f'Windows already has your own setting '
                       f'for FoamMesh ({current}); it is left alone')
    if current == want and ours == want:
        return Outcome('none', value=want, detail='already set')
    if dry:
        logger.info('GPU preference (dry run): would write %s for %s (%s)',
                    want, exe, chosen.reason)
        return Outcome('dry-run', value=want, would_write=want,
                       detail=chosen.reason)
    try:
        write_registry(want, exe, registry)
    except Exception as error:                             # noqa: BLE001
        logger.warning('GPU preference: could not write it: %s', error)
        return Outcome('skipped', detail=str(error))
    state['written'] = want
    state['exe'] = exe
    _save_state(settings, state)
    logger.info('GPU preference: set %s for %s (%s)', want, exe, chosen.reason)
    return Outcome('write', value=want, detail=chosen.reason)


def set_enabled(settings, enabled: bool, **kwargs) -> Outcome:
    """The Settings toggle. Switching it clears a fallback so it can retry;
    the new request is written at once and takes effect after a restart."""
    enabled = bool(enabled)
    if enabled == is_enabled(settings):
        return Outcome('none', detail='unchanged')
    settings.setUseDedicatedGpu(enabled)
    state = load_state(settings)
    state.pop('fallback', None)
    state.pop('fallback_reason', None)
    _save_state(settings, state)
    return reconcile(settings, **kwargs)


# --------------------------------------------------------------------------
# Start-up, the first render, the crash marker
# --------------------------------------------------------------------------

@dataclass
class Session:
    """What this process did at start and what it drew with."""
    enabled: bool = True
    effective: bool = False
    applied: str = ''
    outcome: Outcome = field(default_factory=Outcome)
    reason: str = ''
    info: dict = field(default_factory=dict)
    notice: str = ''


_session = Session()


def session() -> Session:
    return _session


def reset() -> None:
    """Tests only."""
    global _session
    _session = Session()


def _marker(log_directory):
    return None if log_directory is None else Path(log_directory) / MARKER_FILE


def _write_marker(log_directory) -> None:
    path = _marker(log_directory)
    if path is None:
        return
    try:
        path.write_text(json.dumps({'pending': True, 'pid': os.getpid()}),
                        encoding='utf-8')
    except OSError:
        pass


def _clear_marker(log_directory) -> None:
    path = _marker(log_directory)
    try:
        if path is not None:
            path.unlink()
    except OSError:
        pass


def _marker_pending(log_directory) -> bool:
    path = _marker(log_directory)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (AttributeError, OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get('pending'))


def _set_fallback(settings, reason: str) -> None:
    state = load_state(settings)
    state['fallback'] = True
    state['fallback_reason'] = reason
    _save_state(settings, state)


def startup(settings, log_directory=None, adapters=None, registry=None,
            environ=None, exe=None) -> Session:
    """Before Qt and OpenGL load: apply the setting; never raises."""
    global _session
    environ = os.environ if environ is None else environ
    session_ = Session(enabled=True)
    _session = session_
    try:
        session_.enabled = is_enabled(settings)
        if session_.enabled and _marker_pending(log_directory):
            reason = ('the previous start did not get as far as a drawn '
                      'viewport with the dedicated GPU')
            _set_fallback(settings, reason)
            session_.notice = NOTICE_CRASH
            logger.warning('GPU preference: %s; using the integrated '
                           'graphics', reason)
        _clear_marker(log_directory)
        state = load_state(settings)
        found = detect(adapters)
        session_.outcome = reconcile(settings, adapters=found,
                                     registry=registry, exe=exe,
                                     environ=environ)
        session_.reason = str(state.get('fallback_reason') or '')
        session_.applied = (session_.outcome.value
                            or str(load_state(settings).get('written') or ''))
        session_.effective = (session_.enabled and not state.get('fallback')
                              and plan(found).hybrid)
        if session_.effective:
            # The NVIDIA shim, set on top, for a driver that reads it; the
            # request above is what every other vendor reads.
            gpu_profile.prefer_discrete_gpu(environ, adapters=found)
            _write_marker(log_directory)
        elif not session_.enabled:
            environ.setdefault(gpu_profile.ENV_GPU, gpu_profile.INTEGRATED)
    except Exception:                                      # noqa: BLE001
        logger.warning('GPU preference: start-up step failed; the driver '
                       'chooses', exc_info=True)
    return session_


def confirm_render(info, settings=None, log_directory=None, adapters=None,
                   registry=None, environ=None, exe=None) -> str:
    """After the first render window has a context.

    Records the renderer for the diagnostics, clears the crash marker, and
    falls back (returns the notice) when a software renderer drew while a
    hardware adapter exists. ``''`` when all is well.
    """
    session_ = _session
    session_.info = dict(info or {})
    _clear_marker(log_directory)
    renderer = str(session_.info.get('renderer') or '')
    if not renderer or gpu_profile.classify(renderer) != SOFTWARE:
        return ''
    try:
        found = detect(adapters)
    except Exception:                                      # noqa: BLE001
        return ''
    if not any(a.tier != SOFTWARE for a in found):
        return ''                    # no hardware to fall back to
    notice = NOTICE_SOFTWARE.format(renderer=renderer)
    logger.warning('GPU preference: the viewport was drawn by %s although '
                   'Windows lists a hardware adapter (%s); falling back to '
                   'the integrated graphics', renderer,
                   ', '.join(a.name for a in found if a.tier != SOFTWARE))
    if settings is not None:
        try:
            _set_fallback(settings, f'software renderer {renderer}')
            reconcile(settings, adapters=found, registry=registry,
                      exe=exe, environ=environ)
        except Exception:                                  # noqa: BLE001
            logger.debug('GPU preference fallback bookkeeping failed',
                         exc_info=True)
    session_.effective = False
    session_.notice = notice
    return notice


def take_notice() -> str:
    """The one-time notice, once."""
    notice = _session.notice
    _session.notice = ''
    return notice


# --------------------------------------------------------------------------
# What About / Diagnostics shows
# --------------------------------------------------------------------------

def diagnostic_lines(settings=None, info=None, adapters=None) -> list:
    """Plain lines for the diagnostics text; cheap, never raises."""
    session_ = _session
    info = info if info else session_.info
    lines = []
    try:
        renderer = str((info or {}).get('renderer') or '')
        vendor = str((info or {}).get('vendor') or '')
        if renderer:
            tier = gpu_profile.classify(renderer)
            lines.append(f'Graphics in use: {renderer} ({vendor or "vendor unknown"}'
                         f'{", " + tier if tier != "unknown" else ""})')
        else:
            lines.append('Graphics in use: not drawn yet')
        found = detect(adapters)
        lines.append('Graphics adapters: ' + ('; '.join(
            f'{a.name} ({a.tier})' for a in found) or 'none listed'))
        if settings is not None:
            enabled = is_enabled(settings)
            state = load_state(settings)
            text = 'on' if enabled else 'off'
            if enabled and state.get('fallback'):
                text += (f' — not in use: {state.get("fallback_reason") or "fallback"}; '
                         'integrated graphics requested')
            elif enabled and not session_.effective and not plan(found).hybrid:
                text += ' — nothing to choose between on this PC'
            lines.append(f'Use the dedicated GPU when available: {text}')
            wants = (enabled and not state.get('fallback')
                     and plan(found).hybrid)
            if wants != session_.effective:
                lines.append('This change takes effect after restart.')
        if session_.outcome.would_write:
            lines.append(f'Dry run: would write {session_.outcome.would_write} '
                         f'for {app_executable()}')
        elif session_.outcome.would_remove:
            lines.append('Dry run: would remove the value FoamMesh wrote')
    except Exception as error:                             # noqa: BLE001
        lines.append(f'Graphics: could not be read ({error})')
    return lines


__all__ = ['Plan', 'detect', 'plan', 'startup', 'confirm_render', 'reconcile',
           'set_enabled', 'take_notice', 'diagnostic_lines', 'wmi_adapters',
           'read_registry', 'write_registry', 'delete_registry', 'session']
