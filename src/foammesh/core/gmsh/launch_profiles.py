"""WSL-only launch profile for the Gmsh meshing runtime.

Gmsh runs inside the same qualified distribution as OpenFOAM 13, so a published
mesh is checked by ``checkMesh`` from the same runtime that produced it, with
one path-translation seam rather than two.

**There is deliberately no host-PATH discovery.** The SALOME pipeline offered a
native profile ahead of the working WSL one, and a stray host install silently
pre-empted the distribution that actually worked. A profile here names its
distribution or it does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shlex
from typing import Mapping
from uuid import uuid4


class GmshProfileError(ValueError):
    pass


@dataclass(frozen=True)
class GmshLaunchCommand:
    argv: tuple[str, ...]
    cleanup_argv: tuple[str, ...] = ()
    profile_id: str = ''
    runtime_cwd: str | None = None

    def to_dict(self) -> dict:
        return {
            'argv': list(self.argv),
            'cleanup_argv': list(self.cleanup_argv),
            'profile_id': self.profile_id,
            'runtime_cwd': self.runtime_cwd,
        }


@dataclass(frozen=True)
class GmshLaunchProfile:
    """One qualified Gmsh runtime, always inside a named WSL distribution."""

    profile_id: str
    distribution: str
    user: str | None = 'foamuser'
    python: str = 'python3'
    wsl_executable: str = 'wsl.exe'
    minimum_version: str = '4.11'
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile_id or self.profile_id.strip() != self.profile_id:
            raise GmshProfileError('profile_id must be a non-empty trimmed token')
        if not self.distribution or not str(self.distribution).strip():
            raise GmshProfileError(
                'a Gmsh profile must name a WSL distribution; host-PATH '
                'discovery is not supported')
        if '/' in self.python or '\\' in self.python:
            if not PurePosixPath(self.python).is_absolute():
                raise GmshProfileError(
                    'python must be a plain name or an absolute runtime path')

    # -- identity ---------------------------------------------------------- #

    @property
    def fingerprint(self) -> str:
        """Stable, secret-free identity for plans, manifests, and evidence."""
        document = {
            'profile_id': self.profile_id,
            'distribution': self.distribution,
            'user': self.user,
            'python': self.python,
            'minimum_version': self.minimum_version,
            'environment_keys': sorted(self.environment),
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict:
        return {
            'profile_id': self.profile_id,
            'kind': 'wsl',
            'distribution': self.distribution,
            'user': self.user,
            'python': self.python,
            'minimum_version': self.minimum_version,
            'fingerprint': self.fingerprint,
        }

    # -- paths ------------------------------------------------------------- #

    def translate_host_path(self, path: str | Path) -> str:
        windows = PureWindowsPath(str(path))
        if not windows.is_absolute() or not windows.drive:
            raise GmshProfileError(
                'WSL Gmsh execution requires an absolute Windows drive path')
        if windows.drive.startswith('\\\\'):
            raise GmshProfileError(
                'UNC paths require an explicit local-copy workspace profile')
        drive = windows.drive.rstrip(':').lower()
        parts = [part for part in windows.parts[1:] if part not in {'\\', '/'}]
        return str(PurePosixPath('/mnt', drive, *parts))

    def _wsl_prefix(self) -> tuple[str, ...]:
        result = [self.wsl_executable, '--distribution', str(self.distribution)]
        if self.user:
            result.extend(('--user', str(self.user)))
        result.append('--exec')
        return tuple(result)

    def python_argv(self, script: str, *arguments: str) -> tuple[str, ...]:
        """Run *script* with the profile's interpreter, passing *arguments*."""
        return (*self._wsl_prefix(), self.python, '-c', script, *arguments)

    # -- probes ------------------------------------------------------------ #

    def identity_probe_argv(self) -> tuple[str, ...]:
        """One probe reporting interpreter, Gmsh version, threads and algorithms.

        Everything the capability report needs comes back from a single
        launch, so a slow cold distribution is paid for once.
        """
        script = (
            'import json, sys\n'
            'out = {"python": sys.version.split()[0]}\n'
            'try:\n'
            '    import gmsh\n'
            'except Exception as error:\n'
            '    out["error"] = "%s: %s" % (type(error).__name__, error)\n'
            '    out["category"] = "not_installed"\n'
            'else:\n'
            '    try:\n'
            '        gmsh.initialize()\n'
            '        out["version"] = gmsh.option.getString("General.Version")\n'
            '        out["threads"] = gmsh.option.getNumber("General.NumThreads")\n'
            '        out["has_occ"] = hasattr(gmsh.model, "occ")\n'
            '        out["has_boundary_layer"] = hasattr(\n'
            '            gmsh.model.geo, "extrudeBoundaryLayer")\n'
            '        out["has_periodic"] = hasattr(gmsh.model.mesh, "setPeriodic")\n'
            '        gmsh.finalize()\n'
            '    except Exception as error:\n'
            '        out["error"] = "%s: %s" % (type(error).__name__, error)\n'
            '        out["category"] = "probe_failed"\n'
            'print("__FOAMMESH_GMSH__" + json.dumps(out))\n'
        )
        return (*self._wsl_prefix(), self.python, '-c', script)

    # -- execution --------------------------------------------------------- #

    def runner_argv(self, runner: str | Path, job: str | Path, *,
                    cwd: str | Path) -> GmshLaunchCommand:
        """Launch the runner on one job file, with group cleanup on cancel."""
        runtime_cwd = self.translate_host_path(cwd)
        runtime_runner = self.translate_host_path(runner)
        runtime_job = self.translate_host_path(job)
        token = uuid4().hex
        pid_name = f'.foammesh-gmsh-{token}.pid'
        run = shlex.join((self.python, '-u', runtime_runner, runtime_job))
        exports = ''.join(
            f'export {shlex.quote(key)}={shlex.quote(str(value))}; '
            for key, value in sorted(self.environment.items()))
        grouped = 'setsid bash -c ' + shlex.quote('exec stdbuf -oL -eL ' + run)
        script = (
            f'cd {shlex.quote(runtime_cwd)} && '
            f'{exports}'
            f'rm -f {shlex.quote(pid_name)}; '
            f'{grouped} & child=$!; '
            f'echo "$child" > {shlex.quote(pid_name)}; '
            f'wait "$child"; status=$?; '
            f'rm -f {shlex.quote(pid_name)}; '
            'exit "$status"'
        )
        cleanup = (
            f'cd {shlex.quote(runtime_cwd)} && '
            f'if test -s {shlex.quote(pid_name)}; then '
            f'pid=$(cat {shlex.quote(pid_name)}); '
            'kill -TERM -- "-$pid" 2>/dev/null || true; '
            'sleep 1; kill -KILL -- "-$pid" 2>/dev/null || true; '
            f'rm -f {shlex.quote(pid_name)}; fi'
        )
        prefix = self._wsl_prefix()
        return GmshLaunchCommand(
            (*prefix, 'bash', '-c', script),
            (*prefix, 'bash', '-c', cleanup),
            self.profile_id,
            runtime_cwd,
        )


#: The distribution and user the application's OpenFOAM runtime names. Gmsh
#: runs in the same distribution, so it follows that setting; the
#: ``FOAMMESH_GMSH_WSL_*`` variables still win for a deliberate override.
_shared_runtime: dict[str, str | None] = {'distribution': None, 'user': None}


def use_runtime(distribution: str | None, user: str | None) -> None:
    """Make Gmsh run in *distribution* as *user* (None restores the default)."""
    _shared_runtime['distribution'] = (str(distribution).strip() or None
                                       if distribution else None)
    _shared_runtime['user'] = str(user).strip() or None if user else None


def configured_profiles(environment: Mapping[str, str] | None = None
                        ) -> tuple[GmshLaunchProfile, ...]:
    """The qualified Gmsh profiles for this host.

    Exactly one, in the same distribution as OpenFOAM 13. Returning a tuple
    keeps the shape the capability layer expects and leaves room for a second
    qualified runtime without changing callers.
    """
    env = dict(os.environ if environment is None else environment)
    distribution = (env.get('FOAMMESH_GMSH_WSL_DISTRO')
                    or _shared_runtime['distribution'] or 'OpenFOAM13Runtime')
    user = (env.get('FOAMMESH_GMSH_WSL_USER')
            or _shared_runtime['user'] or 'foamuser')
    if os.name != 'nt' and not env.get('FOAMMESH_GMSH_WSL_DISTRO'):
        # No WSL to launch: report no profile rather than inventing a native
        # one that would re-create the host-PATH precedence defect.
        return ()
    return (GmshLaunchProfile(
        profile_id=env.get('FOAMMESH_GMSH_PROFILE_ID', 'wsl-openfoam13-gmsh'),
        distribution=distribution,
        user=user,
        python=env.get('FOAMMESH_GMSH_PYTHON', 'python3'),
        wsl_executable=env.get('FOAMMESH_WSL_EXECUTABLE', 'wsl.exe'),
        minimum_version=env.get('FOAMMESH_GMSH_MINIMUM_VERSION', '4.11'),
    ),)
