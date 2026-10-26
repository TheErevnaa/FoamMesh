"""Typed Gmsh runtime probe.

Two defects from the pipeline this replaces are designed out here:

* **Garbled probe output.** ``wsl.exe`` writes its own errors in UTF-16LE, so
  decoding with the locale encoding turned "There is no distribution with the
  supplied name" into mojibake and the user saw nothing usable. Output is
  decoded from bytes with the encoding detected, never from ``text=True``.
* **Untyped failures.** "unavailable" with no category left callers unable to
  say whether the distribution, the package or the version was the problem.
  Every failure carries a :class:`GmshFailureCategory` and a readable reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import subprocess

from .launch_profiles import (
    BUILD_BUNDLE_RELATIVE, BUILD_ID, BUILD_REQUIRED_OPTION,
    BUILD_SETUP_ACTION, GmshLaunchProfile, configured_profiles)


class GmshFailureCategory(str, Enum):
    """Why a Gmsh runtime is unusable, in terms a user can act on."""

    NONE = 'none'
    #: The named WSL distribution does not exist or will not start.
    DISTRO_MISSING = 'distro_missing'
    #: The distribution runs but ``import gmsh`` fails.
    NOT_INSTALLED = 'not_installed'
    #: Gmsh imports but is older than the qualified minimum.
    UNSUPPORTED_VERSION = 'unsupported_version'
    #: Gmsh imports but the API this pipeline needs is absent.
    MISSING_CAPABILITY = 'missing_capability'
    #: The probe itself could not be launched or returned nothing usable.
    PROBE_FAILED = 'probe_failed'


#: API surfaces the pipeline actually calls. Each is probed, so an environment
#: that cannot deliver one is rejected with that name rather than failing
#: mid-mesh.
REQUIRED_CAPABILITIES = ('gmsh.occ', 'gmsh.boundary_layer', 'gmsh.periodic')

_MARKER = '__FOAMMESH_GMSH__'

#: Which Gmsh answered: FoamMesh's own build (Revoropt on, CVTRemesh) or the
#: official wheel that stands in when the build is not installed.
BUILD_FOAMMESH = 'foammesh'
BUILD_WHEEL = 'wheel'

#: Said whenever the wheel answers, so the fallback is never silent.
WHEEL_NOTICE = (
    "FoamMesh's own Gmsh build is not installed in this runtime, so the "
    'official Gmsh wheel is in use and CVT surface remeshing is not '
    'available. Use "' + BUILD_SETUP_ACTION + '" to install it (about one '
    'file copied into your home folder in the runtime; nothing is '
    'downloaded and nothing needs administrator rights).')


def build_options_from(text: str) -> str:
    """The ``Build options:`` words in ``General.BuildInfo`` (or bare options)."""
    text = str(text or '').strip()
    marker = 'Build options:'
    if marker in text:
        text = text.split(marker, 1)[1]
        text = text.split(';', 1)[0]
    return ' '.join(text.split())


def option_words(options: str) -> frozenset:
    return frozenset(str(options).split())


def build_kind(options: str, gmsh_file: str = '') -> str:
    """``foammesh`` when the Revoropt build answered, else ``wheel``."""
    if BUILD_REQUIRED_OPTION in option_words(options):
        return BUILD_FOAMMESH
    if BUILD_ID in str(gmsh_file or ''):
        return BUILD_FOAMMESH
    return BUILD_WHEEL


def decode_process_output(raw: bytes) -> str:
    """Decode probe output, tolerating the UTF-16 that ``wsl.exe`` emits.

    ``wsl.exe`` writes its *own* diagnostics (a missing distribution, for
    example) as UTF-16LE while the guest writes UTF-8. Guessing wrong is how a
    perfectly clear error message reaches the user as ``T\\x00h\\x00e\\x00``.
    """
    if not raw:
        return ''
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return raw.decode('utf-16', errors='replace')
    # No BOM: a run of NUL bytes in an ASCII-dominated stream means UTF-16.
    sample = raw[:512]
    if sample.count(b'\x00') > len(sample) // 4:
        codec = 'utf-16-le' if raw[1:2] == b'\x00' else 'utf-16-be'
        return raw.decode(codec, errors='replace')
    return raw.decode('utf-8', errors='replace')


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for chunk in str(value).split('.'):
        digits = ''.join(item for item in chunk if item.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


@dataclass(frozen=True)
class GmshRuntimeReport:
    """What one probe of one profile found."""

    profile_id: str
    available: bool
    category: GmshFailureCategory = GmshFailureCategory.NONE
    reason: str = ''
    version: str = ''
    python_version: str = ''
    threads: int = 0
    capabilities: tuple[tuple[str, bool], ...] = ()
    runtime_fingerprint: str = ''
    distribution: str = ''
    #: Plan 48 GM16a. ``foammesh`` or ``wheel``; the options it reported; and
    #: the sentence to show when the wheel stands in for the build.
    build: str = ''
    build_options: str = ''
    notice: str = ''
    raw_output: str = field(default='', repr=False)

    def to_dict(self) -> dict:
        return {
            'profile_id': self.profile_id,
            'available': self.available,
            'category': self.category.value,
            'reason': self.reason,
            'version': self.version,
            'python_version': self.python_version,
            'threads': self.threads,
            'capabilities': dict(self.capabilities),
            'runtime_fingerprint': self.runtime_fingerprint,
            'distribution': self.distribution,
            'build': self.build,
            'build_options': self.build_options,
            'notice': self.notice,
        }


class GmshRuntimeProbe:
    """Probe a Gmsh profile once and cache the verdict for this process."""

    def __init__(self, *, timeout_seconds: float = 60.0, runner=None):
        self.timeout_seconds = float(timeout_seconds)
        # Injectable so tests can drive the parsing without a distribution,
        # while production always goes through subprocess.
        self._runner = runner or self._run
        self._cache: dict[str, GmshRuntimeReport] = {}

    @staticmethod
    def _run(argv, timeout):
        # Plan 45 BOOT2. No console window for the wsl.exe child (it took the
        # focus from a console-less app), and no inherited stdin.
        return subprocess.run(
            list(argv), capture_output=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def probe(self, profile: GmshLaunchProfile, *,
              refresh: bool = False) -> GmshRuntimeReport:
        # Keyed on the fingerprint, not the id: the id stays the same when
        # the distribution or user changes, and would answer for the old one.
        key = profile.fingerprint
        if not refresh and key in self._cache:
            return self._cache[key]
        # Plan 41 GN1. The resident session's first answer is the probe: when
        # one is up there is no second process to start (the cold probe held
        # the command scheduler for ~32 s after every commit, Plan 40 F6e).
        from foammesh.core.gmsh.session import session_hello
        hello = session_hello(profile)
        if hello is not None:
            report = self._report_from_payload(profile, hello)
            self._cache[key] = report
            return report
        report = None
        if self._runner == self._run:
            report = self._probe_by_session(profile)
        if report is None:
            report = self._probe_uncached(profile)
        self._cache[key] = report
        return report

    def _probe_by_session(self, profile):
        """Probe by starting the resident session (one launch, GN1)."""
        from foammesh.core.gmsh.session import (
            GmshSessionCrashed, start_idle_session)
        try:
            session = start_idle_session(
                profile, start_timeout=self.timeout_seconds)
        except (GmshSessionCrashed, OSError):
            # Not importable, no distribution, a bad interpreter: the identity
            # probe still names which of those it is.
            return None
        return self._report_from_payload(profile, session.hello)

    def _failure(self, profile, category, reason, raw=''):
        return GmshRuntimeReport(
            profile_id=profile.profile_id, available=False, category=category,
            reason=reason, distribution=profile.distribution, raw_output=raw)

    def _probe_uncached(self, profile: GmshLaunchProfile) -> GmshRuntimeReport:
        try:
            completed = self._runner(
                profile.identity_probe_argv(), self.timeout_seconds)
        except subprocess.TimeoutExpired:
            return self._failure(
                profile, GmshFailureCategory.PROBE_FAILED,
                f'the Gmsh probe did not answer within '
                f'{self.timeout_seconds:g}s; the {profile.distribution} '
                'distribution may be starting for the first time')
        except (OSError, subprocess.SubprocessError) as error:
            return self._failure(
                profile, GmshFailureCategory.DISTRO_MISSING,
                f'could not launch the {profile.distribution} distribution: '
                f'{error}')

        stdout = decode_process_output(getattr(completed, 'stdout', b'') or b'')
        stderr = decode_process_output(getattr(completed, 'stderr', b'') or b'')
        raw = (stdout + stderr).strip()

        payload = None
        for line in stdout.splitlines():
            if line.startswith(_MARKER):
                try:
                    payload = json.loads(line[len(_MARKER):])
                except ValueError:
                    payload = None
                break

        if payload is None:
            detail = stderr.strip() or stdout.strip()
            if not detail:
                detail = f'the probe exited {getattr(completed, "returncode", "?")}'
            lowered = detail.lower()
            category = (
                GmshFailureCategory.DISTRO_MISSING
                if ('no distribution' in lowered or 'not installed' in lowered
                    or 'wsl' in lowered)
                else GmshFailureCategory.PROBE_FAILED)
            return self._failure(
                profile, category,
                f'{profile.distribution}: {detail.splitlines()[0][:200]}', raw)

        return self._report_from_payload(profile, payload, raw)

    def _report_from_payload(self, profile, payload, raw=''):
        """The report for one identity payload (probe process or session)."""
        if payload.get('error'):
            category = GmshFailureCategory(
                payload.get('category') or GmshFailureCategory.PROBE_FAILED.value)
            reason = str(payload['error'])
            if category is GmshFailureCategory.NOT_INSTALLED:
                reason = (
                    f'Gmsh is not importable by {profile.python} in '
                    f'{profile.distribution} ({reason}). Install it with: '
                    f'pip install gmsh')
            return self._failure(profile, category, reason, raw)

        version = str(payload.get('version') or '').strip()
        if not version:
            return self._failure(
                profile, GmshFailureCategory.PROBE_FAILED,
                'the Gmsh probe reported no version', raw)
        if _version_tuple(version) < _version_tuple(profile.minimum_version):
            return self._failure(
                profile, GmshFailureCategory.UNSUPPORTED_VERSION,
                f'Gmsh {version} is older than the qualified minimum '
                f'{profile.minimum_version}', raw)

        capabilities = (
            ('gmsh.occ', bool(payload.get('has_occ'))),
            ('gmsh.boundary_layer', bool(payload.get('has_boundary_layer'))),
            ('gmsh.periodic', bool(payload.get('has_periodic'))),
        )
        missing = [name for name, present in capabilities if not present]
        options = build_options_from(payload.get('build') or '')
        kind = build_kind(options, str(payload.get('gmsh_file') or ''))
        identity = {
            'profile': profile.fingerprint,
            'version': version,
            'python': str(payload.get('python') or ''),
            'capabilities': dict(capabilities),
            # A rebuilt Gmsh is another runtime: evidence measured on the
            # other one is stale (Plan 48 section 4.15).
            'build': kind,
            'build_options': options,
        }
        fingerprint = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

        if missing:
            return GmshRuntimeReport(
                profile_id=profile.profile_id, available=False,
                category=GmshFailureCategory.MISSING_CAPABILITY,
                reason=('this Gmsh build lacks: ' + ', '.join(missing)),
                version=version, python_version=str(payload.get('python') or ''),
                threads=int(payload.get('threads') or 0),
                capabilities=capabilities, runtime_fingerprint=fingerprint,
                distribution=profile.distribution, build=kind,
                build_options=options,
                notice=WHEEL_NOTICE if kind == BUILD_WHEEL else '',
                raw_output=raw)

        return GmshRuntimeReport(
            profile_id=profile.profile_id, available=True,
            category=GmshFailureCategory.NONE, reason='',
            version=version, python_version=str(payload.get('python') or ''),
            threads=int(payload.get('threads') or 0),
            capabilities=capabilities, runtime_fingerprint=fingerprint,
            distribution=profile.distribution, build=kind,
            build_options=options,
            notice=WHEEL_NOTICE if kind == BUILD_WHEEL else '',
            raw_output=raw)


def probe_configured(*, timeout_seconds: float = 60.0,
                     refresh: bool = False,
                     probe: GmshRuntimeProbe | None = None
                     ) -> tuple[GmshRuntimeReport, ...]:
    """Probe every configured profile; empty when the host has no WSL."""
    profiles = configured_profiles()
    if not profiles:
        return ()
    service = probe or GmshRuntimeProbe(timeout_seconds=timeout_seconds)
    return tuple(service.probe(item, refresh=refresh) for item in profiles)


def select_runtime(reports=None, **kwargs) -> GmshRuntimeReport | None:
    """The first usable runtime, or ``None`` with the failures left to report."""
    reports = probe_configured(**kwargs) if reports is None else tuple(reports)
    return next((item for item in reports if item.available), None)


def unavailable_reason(reports) -> str:
    """One readable sentence explaining why no runtime is usable."""
    reports = tuple(reports)
    if not reports:
        return ('no Gmsh runtime is configured; Gmsh runs in a WSL '
                'distribution and this host has none')
    return '; '.join(
        f'{item.profile_id}: {item.reason}' for item in reports if item.reason)


# --------------------------------------------------------------------------- #
# FoamMesh's own Gmsh build in the user's distribution (Plan 48 GM16a)
# --------------------------------------------------------------------------- #
#
# The same shape as ``foammesh-sizefield`` (``core/sizing/tool_setup.py``): the
# build is one file FoamMesh ships, copied into the distribution the user
# already has on their say-so, checked by sha256 and by importing it. There is
# no distribution image and nothing is downloaded. See ``launch_profiles`` for
# how every Python in the distribution then finds it.

class GmshBuildSetupError(RuntimeError):
    pass


def bundle_path():
    """The build as this FoamMesh ships it (a tarball; may be absent)."""
    from resources import resource
    return resource.file(BUILD_BUNDLE_RELATIVE)


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _build_runner(argv, timeout):
    from foammesh.core.openfoam_runtime.detect import _run
    return _run(argv, timeout)


def _build_text(output: bytes) -> str:
    from foammesh.core.openfoam_runtime.detect import _text
    return _text(output)


def _key_values(output: bytes) -> dict:
    found = {}
    for line in _build_text(output).splitlines():
        key, separator, value = line.strip().partition('=')
        if separator:
            found.setdefault(key, value.strip())
    return found


def build_status(profile: GmshLaunchProfile, *, runner=None,
                 timeout: float = 60.0,
                 expected_bundle_sha: str | None = None) -> dict:
    """Where FoamMesh's Gmsh build is in *profile*'s distribution.

    ``state``: ``ok`` (installed, in use, and the shipped one when the
    shipped sha is known), ``stale`` (installed but not the shipped file),
    ``not_in_use`` (the files are there but nothing points Python at them),
    ``missing`` or ``unreachable``. ``in_use`` is what the wheel fallback
    notice is decided from; ``message`` is always readable.
    """
    runner = runner or _build_runner
    code, output = runner(profile.build_status_argv(), timeout)
    values = _key_values(output)
    result = {
        'distribution': str(getattr(profile, 'distribution', '') or ''),
        'user': values.get('user.name', ''), 'build_id': BUILD_ID,
        'path': values.get('build.dir', ''), 'sha256': values.get('build.sha', ''),
        'in_use': False, 'state': 'missing', 'message': WHEEL_NOTICE,
        'expected_bundle_sha256': expected_bundle_sha or '',
    }
    if code != 0 or not values.get('user.name'):
        result.update(state='unreachable', message=(
            "The OpenFOAM 13 runtime did not answer, so FoamMesh's Gmsh "
            'build could not be looked for.'))
        return result
    if 'build.dir' not in values:
        return result
    record = {}
    if values.get('build.record'):
        try:
            record = json.loads(values['build.record'])
        except ValueError:
            record = {}
    in_use = (values.get('pth.target', '').rstrip('/')
              == values['build.dir'].rstrip('/') + '/lib')
    result.update(in_use=in_use, record=record)
    if expected_bundle_sha and record.get('bundle_sha256') != expected_bundle_sha:
        result.update(state='stale', message=(
            "The Gmsh build in the runtime is not the one this FoamMesh "
            'ships. Use "' + BUILD_SETUP_ACTION + '" to copy it again.'))
    elif not in_use:
        result.update(state='not_in_use', message=WHEEL_NOTICE)
    else:
        result.update(state='ok', message='')
    return result


def install_build(profile: GmshLaunchProfile, *, runner=None,
                  timeout: float = 600.0, bundle=None) -> dict:
    """Copy the shipped build into the runtime and switch to it; never silent.

    Raises :class:`GmshBuildSetupError` with the measured reason when the
    bundle is missing, the copy fails, or the installed Gmsh does not import.
    """
    from pathlib import Path
    runner = runner or _build_runner
    source = Path(bundle) if bundle is not None else Path(bundle_path())
    if not source.is_file():
        raise GmshBuildSetupError(
            f"This FoamMesh does not contain its Gmsh build ({source}).")
    expected = _file_sha256(source)
    before = build_status(profile, runner=runner, timeout=timeout,
                          expected_bundle_sha=expected)
    if before['state'] == 'unreachable':
        raise GmshBuildSetupError(before['message'])
    if before['state'] == 'ok':
        return before
    code, output = runner(profile.build_install_argv(
        profile.translate_host_path(source), expected), timeout)
    if code != 0 or 'done=' not in _build_text(output):
        raise GmshBuildSetupError(
            "Copying FoamMesh's Gmsh into the runtime failed: "
            + (_build_text(output).strip()[-400:] or f'exit code {code}'))
    after = build_status(profile, runner=runner, timeout=timeout,
                         expected_bundle_sha=expected)
    if after['state'] != 'ok':
        raise GmshBuildSetupError(
            "The copied Gmsh build did not pass its check: "
            + (after['message'] or after['state']))
    after['copied'] = True
    return after


def disable_build(profile: GmshLaunchProfile, *, runner=None,
                  timeout: float = 60.0) -> dict:
    """Stop using the build (the wheel answers again, with the notice)."""
    runner = runner or _build_runner
    code, output = runner(profile.build_disable_argv(), timeout)
    if code != 0:
        raise GmshBuildSetupError(
            'Switching back to the Gmsh wheel failed: '
            + (_build_text(output).strip()[-400:] or f'exit code {code}'))
    return build_status(profile, runner=runner, timeout=timeout)
