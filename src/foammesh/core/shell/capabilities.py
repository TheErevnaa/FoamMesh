"""Cached, explainable discovery of external utility capabilities."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from foammesh.core.openfoam_runtime import (
    LaunchCommand,
    OpenFoamLaunchProfile,
    configured_profiles,
)


OPENFOAM_UTILITIES = frozenset({
    'blockMesh', 'surfaceFeatures', 'snappyHexMesh', 'checkMesh',
    'decomposePar', 'reconstructPar', 'redistributePar',
    'surfaceRedistributePar', 'splitMeshRegions', 'createZones', 'createPatch',
    'createNonConformalCouples',
    'extrudeMesh', 'collapseEdges', 'transformPoints', 'foamMeshToFluent',
    'foamFormatConvert', 'renumberMesh', 'subsetMesh', 'combinePatchFaces',
    'fluentMeshToFoam', 'gmshToFoam', 'gambitToFoam', 'ideasUnvToFoam',
    'ansysToFoam', 'cfx4ToFoam', 'star3ToFoam', 'plot3dToFoam',
    'mpirun',
})


@dataclass(frozen=True)
class Capability:
    name: str
    available: bool
    executable: str | None
    reason: str = ''
    profile_id: str | None = None
    #: R194. True when the probe could not ask the runtime -- the launcher
    #: failed or the answer did not arrive in time. That is a statement
    #: about this attempt, not about the machine, so it is not cached.
    transient: bool = False


@dataclass(frozen=True)
class UtilityHelp:
    name: str
    available: bool
    output: str = ''
    reason: str = ''


@dataclass(frozen=True)
class OpenFoamRuntimeHealth:
    profile_id: str
    available: bool
    reason: str = ''
    project: str = ''
    version: str = ''
    wm_options: str = ''
    utility_paths: tuple[tuple[str, str], ...] = ()
    mpi_identity: str = ''
    fingerprint: str = ''
    distribution: str = ''
    user: str = ''
    bashrc: str = ''
    #: The numeric uid the runtime's own shell reports, as a string, or ''
    #: when the probe did not answer. Plan 31 CP-07: Open MPI aborts rather
    #: than start any rank as root unless ``--allow-run-as-root`` is given.
    uid: str = ''
    #: R194. See Capability.transient.
    transient: bool = False

    def to_dict(self) -> dict:
        return {
            'profile_id': self.profile_id,
            'available': self.available,
            'reason': self.reason,
            'project': self.project,
            'version': self.version,
            'wm_options': self.wm_options,
            'utilities': dict(self.utility_paths),
            'mpi_identity': self.mpi_identity,
            'fingerprint': self.fingerprint,
            'distribution': self.distribution,
            'user': self.user,
            'bashrc': self.bashrc,
            'uid': self.uid,
            'transient': self.transient,
        }


#: How long to wait for a runtime to identify itself.
#:
#: R194. MEASURED with the WSL distribution shut down: the identity probe
#: takes 41.6 s to answer cold and 2.3 s warm, because the first call has
#: to boot the distribution. The old budget was 60 s, which one cold probe
#: fits inside and two racing each other -- Gmsh and OpenFOAM are probed
#: together the moment the Meshing Method page opens -- do not. Nothing is
#: blocked by the wait: the facade answers `pending` after its own
#: deadline and the page says it is still probing.
RUNTIME_PROBE_TIMEOUT_SECONDS = 180.0


def parse_decomposition_probe(output: str) -> dict[str, str] | None:
    """The probe's library lines, as ``basename -> directory``.

    ``None`` when the runtime reported an error, so "we could not ask" is never
    recorded as "it has nothing".
    """
    if '__FOAMMESH_ERROR__' in (output or ''):
        return None
    found: dict[str, str] = {}
    saw_root = False
    for line in (output or '').splitlines():
        line = line.strip()
        if line.startswith('__FOAMMESH_LIBDIR__='):
            saw_root = bool(line.split('=', 1)[1].strip())
        elif line.startswith('__FOAMMESH_DECOMP__'):
            body = line[len('__FOAMMESH_DECOMP__'):]
            name, _, directory = body.partition('=')
            if name and directory:
                # A real library outranks a stub of the same name.
                if name not in found or found[name].endswith('/dummy'):
                    found[name] = directory
    return found if saw_root else None


def _run_cleanup(command) -> None:
    """Best effort: stop a timed-out launch and remove its pid file."""
    cleanup = getattr(command, 'cleanup_argv', ())
    if not cleanup:
        return
    try:
        subprocess.run(cleanup, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


class CapabilityRegistry:
    """Probe executables once per environment rather than failing after a click."""

    def __init__(self, *, environment=None,
                 profiles: tuple[OpenFoamLaunchProfile, ...] | None = None):
        self._cache: dict[str, Capability] = {}
        self._help_cache: dict[str, UtilityHelp] = {}
        self._state_callbacks = set()
        self._environment = environment
        self._profiles = profiles
        self._resolved_profiles: dict[str, OpenFoamLaunchProfile] = {}
        self._runtime_health: dict[str, OpenFoamRuntimeHealth] = {}
        self._decomposition_libraries: dict[str, str] | None = None

    def utility(self, name: str) -> Capability:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        if name in OPENFOAM_UTILITIES:
            capability = self._probe_openfoam_utility(name)
        else:
            executable = shutil.which(name)
            capability = Capability(
                name, executable is not None, executable,
                '' if executable is not None else
                f'{name} was not found in the configured environment')
        # R194. A probe that never reached the runtime is not an answer
        # about the runtime. Cached, one cold WSL start told the user for
        # the rest of the session that OpenFOAM was not installed, and only
        # someone who guessed that `Re-probe runtimes` meant them ever saw
        # otherwise. Left uncached, the next question asks again.
        if not capability.transient:
            self._cache[name] = capability
        return capability

    def utility_if_known(self, name: str) -> Capability | None:
        """Answer from the cache alone: instantly, or not at all.

        `utility()` always answers, and to do it may start a cold WSL runtime
        and wait tens of seconds. A caller on the GUI thread pays that wait with
        a frozen window -- the shell rebuilds its menus whenever a project
        opens, and that rebuild asked. This is the same question in a form that
        cannot block, so a view can draw a provisional state now and warm the
        cache off-thread. A `None` here means unknown, never unavailable.
        """
        return self._cache.get(name)

    def refresh(self):
        self._cache.clear()
        self._help_cache.clear()
        self._resolved_profiles.clear()
        self._runtime_health.clear()
        self._decomposition_libraries = None
        for callback in tuple(self._state_callbacks):
            callback()

    def configure_profiles(
            self, profiles: tuple[OpenFoamLaunchProfile, ...]) -> None:
        """Replace the process-wide runtime policy and invalidate all probes."""
        self._profiles = tuple(profiles)
        self.refresh()

    def subscribe_state(self, callback):
        """Notify shell adapters when the configured environment is reprobed."""
        self._state_callbacks.add(callback)

        def unsubscribe():
            self._state_callbacks.discard(callback)
        return unsubscribe

    def help(self, name: str, *, timeout: float = 30.0) -> UtilityHelp:
        """Probe the configured executable's own help without shell parsing.

        DP-693. A cold WSL distribution outlasted a 5 s probe, and the cached failure
        kept Mesh > Scale refusing until restart: the wait now covers a cold start and
        only an answer is remembered, so a failed probe is asked again next time.
        """
        if name in self._help_cache:
            return self._help_cache[name]
        capability = self.utility(name)
        if not capability.available or capability.executable is None:
            result = UtilityHelp(name, False, reason=capability.reason)
        else:
            try:
                # DP-800. Run from the temporary folder, not wherever the app was
                # started, and on a timeout run the cleanup that removes the launch's
                # pid file -- killing the Windows side alone left one behind each time.
                command = self.command(name, ('-help',), cwd=Path(tempfile.gettempdir()))
                try:
                    completed = subprocess.run(
                        command.argv, capture_output=True,
                        text=True, errors='replace', timeout=timeout, check=False)
                except subprocess.TimeoutExpired:
                    _run_cleanup(command)
                    raise
                output = '\n'.join(part for part in (completed.stdout, completed.stderr) if part).strip()
                result = UtilityHelp(
                    name, bool(output), output,
                    '' if output else f'{name} -help returned no output')
            except (OSError, subprocess.SubprocessError) as error:
                result = UtilityHelp(name, False, reason=f'could not inspect {name}: {error}')
        if result.available:
            self._help_cache[name] = result
        return result

    def help_if_known(self, name: str) -> UtilityHelp | None:
        """The cached ``-help`` answer, instantly, or ``None`` if not asked yet.

        Plan 35 CR7. :meth:`help` may boot a cold runtime; a caller on the
        owner loop asks it on a worker thread first and reads this after.
        """
        return self._help_cache.get(name)

    def command(self, name: str, arguments=(), *, cwd) -> LaunchCommand:
        capability = self.utility(name)
        if not capability.available or capability.executable is None:
            raise FileNotFoundError(capability.reason)
        profile = self._resolved_profiles.get(name)
        if profile is None:
            return LaunchCommand(
                (capability.executable, *(str(item) for item in arguments)),
                profile_id='host-path',
                runtime_cwd=str(Path(cwd).resolve()),
            )
        return profile.command(name, arguments, cwd=cwd)

    def runtime_fingerprint(self, name: str) -> str | None:
        """Return the probed exact-runtime fingerprint without exposing env."""
        capability = self.utility(name)
        health = self._runtime_health.get(capability.profile_id or '')
        return health.fingerprint if health is not None else None

    def mpi_options(self, name: str = 'mpirun') -> tuple[str, ...]:
        """Extra ``mpirun`` arguments this runtime needs to start any rank.

        Plan 31 CP-07 item 6. MEASURED on the OpenFOAM13Runtime WSL
        distribution, whose default user is root::

            $ mpirun -np 2 hostname
            mpirun has detected an attempt to run as root.
            ...
            (no ranks started)

        Open MPI refuses outright; it does not warn and continue. So on a
        runtime configured to run as root, ``mpirun -np N snappyHexMesh
        -parallel`` starts **zero** workers while decomposePar, the processor
        directories and the launch itself all look healthy -- exactly the
        disagreement between requested ranks and running ranks V16a exists to
        catch. The distribution's default ``foamuser`` account, which the
        shipped profile uses, needs nothing added and is given nothing.

        The flag is added only when the runtime's own shell said it is root,
        never speculatively: on a normal user account Open MPI treats
        ``--allow-run-as-root`` as an error, so guessing in either direction
        breaks one of the two runtimes.
        """
        capability = self.utility(name)
        health = self._runtime_health.get(capability.profile_id or '')
        if health is None or health.uid != '0':
            return ()
        return ('--allow-run-as-root',)

    def launch_profile(self, name: str) -> OpenFoamLaunchProfile | None:
        """Return the qualified profile selected for an OpenFOAM utility."""
        self.utility(name)
        return self._resolved_profiles.get(name)

    def decomposition_methods(self, *, timeout: float = 30.0) -> tuple:
        """Which decomposition methods the selected runtime can actually run.

        Plan 31 CP-07 item 4. Returns the registry's answer for every method
        the product can write, each carrying whether this runtime has it and,
        when it does not, why -- so the page can show the row disabled with the
        reason instead of quietly not having the feature.

        A probe that does not answer is not evidence of absence: the library
        map is then empty and every library-backed method is reported
        unavailable with "the runtime could not be probed" rather than with a
        claim about what it contains.
        """
        from foammesh.openfoam import decomposition

        return decomposition.selectable(self.decomposition_libraries(
            timeout=timeout))

    def decomposition_libraries(self, *, timeout: float = 30.0) -> dict | None:
        """``libfooDecomp.so`` -> the directory it was found in, or ``None``.

        ``None`` means the probe did not answer, which is a different thing
        from an empty map.
        """
        if self._decomposition_libraries is not None:
            return self._decomposition_libraries
        profile = self.launch_profile('decomposePar')
        probe = getattr(profile, 'decomposition_probe_argv', None)
        if probe is None:
            return None
        try:
            completed = subprocess.run(
                probe(), capture_output=True, text=True, errors='replace',
                timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        found = parse_decomposition_probe(completed.stdout)
        if found is None:
            return None
        self._decomposition_libraries = found
        return found

    def runtime_diagnostics(self) -> dict:
        """Return the effective exact-runtime identity and complete utility map."""
        profiles = self._profiles
        if profiles is None:
            profiles = configured_profiles(self._environment)
        health = [self._probe_profile(profile).to_dict() for profile in profiles]
        selected = next((item for item in health if item['available']), None)
        return {
            'required_project': 'OpenFOAM',
            'required_version': '13',
            'selected_profile': selected,
            'profiles': health,
            'native_fallback_allowed': any(
                profile.kind == 'native' for profile in profiles),
        }

    def _probe_openfoam_utility(self, name: str) -> Capability:
        profiles = self._profiles
        if profiles is None:
            profiles = configured_profiles(self._environment)
        failures = []
        transient = False
        for profile in profiles:
            health = self._probe_profile(profile)
            transient = transient or health.transient
            if health.available:
                path = dict(health.utility_paths).get(name)
                if path:
                    self._resolved_profiles[name] = profile
                    return Capability(
                        name, True, path, profile_id=profile.profile_id)
                failures.append(
                    f'{profile.profile_id}: {name} is not installed in the '
                    'qualified runtime')
                continue
            failures.append(f'{profile.profile_id}: {health.reason}')
        return Capability(
            name, False, None,
            '; '.join(failures) or
            f'{name} was not found in any configured OpenFOAM runtime',
            transient=transient)

    def _probe_profile(
            self, profile: OpenFoamLaunchProfile) -> OpenFoamRuntimeHealth:
        cached = self._runtime_health.get(profile.profile_id)
        if cached is not None:
            return cached
        # Production profiles expose an exact identity probe. The small
        # fallback preserves injectable test profiles without weakening the
        # production boundary.
        identity_probe = getattr(profile, 'identity_probe_argv', None)
        if identity_probe is None:
            health = self._probe_legacy_test_profile(profile)
            self._runtime_health[profile.profile_id] = health
            return health
        try:
            completed = subprocess.run(
                identity_probe(sorted(OPENFOAM_UTILITIES)),
                capture_output=True,
                text=True,
                errors='replace',
                timeout=RUNTIME_PROBE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # R194. Not an answer: the runtime was never asked to its face.
            # Returned without being cached, so the next question retries.
            return OpenFoamRuntimeHealth(
                profile.profile_id, False,
                'the runtime did not identify itself within '
                f'{RUNTIME_PROBE_TIMEOUT_SECONDS:g}s; it may still be '
                'starting', transient=True)
        except (OSError, subprocess.SubprocessError) as error:
            return OpenFoamRuntimeHealth(
                profile.profile_id, False,
                f'could not launch runtime probe: {error}', transient=True)

        values: dict[str, str] = {}
        utilities: dict[str, str] = {}
        for raw in (completed.stdout or '').splitlines():
            if raw.startswith('__FOAMMESH_UTILITY__') and '=' in raw:
                key, value = raw.split('=', 1)
                utilities[key[len('__FOAMMESH_UTILITY__'):]] = value.strip()
            elif raw.startswith('__FOAMMESH_') and '=' in raw:
                key, value = raw.split('=', 1)
                values[key.removeprefix('__FOAMMESH_').removesuffix('__')] = (
                    value.strip())
        reason = ''
        if completed.returncode != 0:
            reason = (values.get('ERROR') or completed.stderr.strip()
                      or f'runtime probe exited {completed.returncode}')
        elif values.get('PROJECT') != profile.expected_project:
            reason = (
                f'expected WM_PROJECT={profile.expected_project}, got '
                f'{values.get("PROJECT") or "unset"}')
        elif values.get('VERSION') != profile.expected_version:
            reason = (
                f'expected WM_PROJECT_VERSION={profile.expected_version}, got '
                f'{values.get("VERSION") or "unset"}')
        else:
            missing = sorted(
                name for name in OPENFOAM_UTILITIES if not utilities.get(name))
            if missing:
                reason = 'required utilities missing: ' + ', '.join(missing)
        identity = {
            'profile': profile.fingerprint,
            'project': values.get('PROJECT', ''),
            'version': values.get('VERSION', ''),
            'wm_options': values.get('OPTIONS', ''),
            'mpi': values.get('MPI', ''),
        }
        fingerprint = hashlib.sha256(json.dumps(
            identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        health = OpenFoamRuntimeHealth(
            profile.profile_id, not reason, reason,
            values.get('PROJECT', ''), values.get('VERSION', ''),
            values.get('OPTIONS', ''), tuple(sorted(utilities.items())),
            values.get('MPI', ''), fingerprint,
            str(profile.distribution or ''), str(profile.user or ''),
            str(profile.bashrc), values.get('UID', ''))
        self._runtime_health[profile.profile_id] = health
        return health

    def _probe_legacy_test_profile(self, profile) -> OpenFoamRuntimeHealth:
        utilities = {}
        failures = []
        for name in sorted(OPENFOAM_UTILITIES):
            try:
                completed = subprocess.run(
                    profile.probe_argv(name),
                    capture_output=True,
                    text=True,
                    errors='replace',
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                failures.append(str(error))
                continue
            if completed.returncode == 0 and completed.stdout.strip():
                utilities[name] = completed.stdout.strip().splitlines()[0]
            else:
                failures.append(
                    (completed.stderr or completed.stdout).strip()
                    or f'{name} not found')
        reason = '; '.join(failures)
        return OpenFoamRuntimeHealth(
            profile.profile_id, not reason, reason,
            'OpenFOAM', '13', '', tuple(sorted(utilities.items())), '',
            str(getattr(profile, 'fingerprint', '')))
