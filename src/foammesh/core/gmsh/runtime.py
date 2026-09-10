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

from .launch_profiles import GmshLaunchProfile, configured_profiles


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
        return subprocess.run(
            list(argv), capture_output=True, timeout=timeout, check=False)

    def probe(self, profile: GmshLaunchProfile, *,
              refresh: bool = False) -> GmshRuntimeReport:
        if not refresh and profile.profile_id in self._cache:
            return self._cache[profile.profile_id]
        report = self._probe_uncached(profile)
        self._cache[profile.profile_id] = report
        return report

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
        identity = {
            'profile': profile.fingerprint,
            'version': version,
            'python': str(payload.get('python') or ''),
            'capabilities': dict(capabilities),
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
                distribution=profile.distribution, raw_output=raw)

        return GmshRuntimeReport(
            profile_id=profile.profile_id, available=True,
            category=GmshFailureCategory.NONE, reason='',
            version=version, python_version=str(payload.get('python') or ''),
            threads=int(payload.get('threads') or 0),
            capabilities=capabilities, runtime_fingerprint=fingerprint,
            distribution=profile.distribution, raw_output=raw)


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
