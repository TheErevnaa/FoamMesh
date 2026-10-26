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


# --------------------------------------------------------------------------- #
# FoamMesh's own Gmsh build (Plan 48 GM16a, section 4.15)
# --------------------------------------------------------------------------- #
#
# FoamMesh ships its own Gmsh 4.15.2, built with Revoropt on (CVTRemesh) and
# every other option of the official wheel (``scripts/gmsh_build``). It is put
# into the user's own distribution the way ``foammesh-sizefield`` is: one file
# FoamMesh ships, copied on the user's say-so under their home, no root, no
# distribution image, no download.
#
#     ``$HOME/.local/share/foammesh/gmsh/<BUILD_ID>/lib/{gmsh.py,libgmsh.so.4.15}``
#
# Every Python the user's distribution starts (the resident session, the batch
# runner, the export converter, the probe) finds it the same way: a
# ``foammesh_gmsh.pth`` in the user's own site-packages puts that ``lib``
# folder ahead of the system ``dist-packages``, where the official wheel is.
# Nothing is chosen per call, so no path can reach the wheel by accident.
# When the build is not installed, the wheel is used, and the probe says so
# (``GmshRuntimeReport.build == 'wheel'`` and a notice that names the action).

#: Bump with any rebuild (it is the folder name and the identity of the build).
BUILD_ID = 'gmsh-4.15.2-revoropt-6'
BUILD_VERSION = '4.15.2'
#: The build option that proves the CVT remesher is in.
BUILD_REQUIRED_OPTION = 'Revoropt'
#: The file FoamMesh ships, relative to ``src/resources``.
BUILD_BUNDLE_RELATIVE = f'gmsh_runtime/linux-x86_64/{BUILD_ID}.tar.gz'
#: Under the distribution user's home.
BUILD_USER_DIRECTORY = '.local/share/foammesh/gmsh'
BUILD_PTH_NAME = 'foammesh_gmsh.pth'
BUILD_RECORD_NAME = 'installed.json'
#: The action's name, as the card shows it.
BUILD_SETUP_ACTION = "Set up FoamMesh's Gmsh"

#: ``bash -c`` script, run with ``$1`` = the python to ask for the user site.
#: Prints ``key=value`` lines.
_BUILD_STATUS_SCRIPT = (
    'D="$HOME/' + BUILD_USER_DIRECTORY + '/' + BUILD_ID + '"; '
    'echo "user.name=$(id -un)"; echo "home=$HOME"; '
    'if [ -f "$D/lib/libgmsh.so.4.15" ]; then '
    'echo "build.dir=$D"; '
    'echo "build.sha=$(sha256sum "$D/lib/libgmsh.so.4.15" | cut -d" " -f1)"; '
    '[ -f "$D/' + BUILD_RECORD_NAME + '" ] && '
    'echo "build.record=$(tr -d "\\n" < "$D/' + BUILD_RECORD_NAME + '")"; fi; '
    'SP="$("$1" -c "import site;print(site.getusersitepackages())" 2>/dev/null)"; '
    'if [ -n "$SP" ] && [ -f "$SP/' + BUILD_PTH_NAME + '" ]; then '
    'echo "pth.path=$SP/' + BUILD_PTH_NAME + '"; '
    'echo "pth.target=$(head -n 1 "$SP/' + BUILD_PTH_NAME + '")"; fi; '
    'true')

#: ``$1`` the python, ``$2`` the shipped tarball as the distribution sees it,
#: ``$3`` the tarball's sha256 (written to the record).
_BUILD_INSTALL_SCRIPT = (
    'set -e; umask 022; '
    'D="$HOME/' + BUILD_USER_DIRECTORY + '"; mkdir -p "$D"; '
    'T="$D/.incoming.$$"; rm -rf "$T"; mkdir "$T"; '
    'tar -xzf "$2" -C "$T"; '
    '[ -f "$T/' + BUILD_ID + '/lib/libgmsh.so.4.15" ] || '
    '{ echo "the bundle holds no ' + BUILD_ID + '" >&2; rm -rf "$T"; exit 3; }; '
    'rm -rf "$D/' + BUILD_ID + '"; mv "$T/' + BUILD_ID + '" "$D/' + BUILD_ID + '"; '
    'rm -rf "$T"; '
    'SP="$("$1" -c "import site;print(site.getusersitepackages())")"; '
    'mkdir -p "$SP"; echo "$D/' + BUILD_ID + '/lib" > "$SP/' + BUILD_PTH_NAME + '"; '
    'printf \'{"build_id": "%s", "bundle_sha256": "%s", "libgmsh_sha256": "%s"}\\n\' '
    '"' + BUILD_ID + '" "$3" '
    '"$(sha256sum "$D/' + BUILD_ID + '/lib/libgmsh.so.4.15" | cut -d" " -f1)" '
    '> "$D/' + BUILD_ID + '/' + BUILD_RECORD_NAME + '"; '
    'echo "done=$D/' + BUILD_ID + '"')

#: Put the build out of use again (the wheel answers). Keeps the files.
_BUILD_DISABLE_SCRIPT = (
    'SP="$("$1" -c "import site;print(site.getusersitepackages())")"; '
    'rm -f "$SP/' + BUILD_PTH_NAME + '"; echo "disabled"')


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
        # DP-1440. OpenFOAM refuses a space or a quote in a path.
        from foammesh.core.wsl_paths import WslPathError, runtime_path
        try:
            return runtime_path(
                path, distribution=self.distribution,
                wsl_executable=self.wsl_executable)
        except WslPathError as error:
            raise GmshProfileError(str(error)) from error

    def _wsl_prefix(self) -> tuple[str, ...]:
        result = [self.wsl_executable, '--distribution', str(self.distribution)]
        if self.user:
            result.extend(('--user', str(self.user)))
        result.append('--exec')
        return tuple(result)

    def python_argv(self, script: str, *arguments: str) -> tuple[str, ...]:
        """Run *script* with the profile's interpreter, passing *arguments*."""
        return (*self._wsl_prefix(), self.python, '-c', script, *arguments)

    # -- FoamMesh's own Gmsh build ----------------------------------------- #

    def _bash_argv(self, script: str, *arguments: str) -> tuple[str, ...]:
        return (*self._wsl_prefix(), 'bash', '-c', script, '_', *arguments)

    def build_status_argv(self) -> tuple[str, ...]:
        """Where the FoamMesh Gmsh build is in the distribution (key=value)."""
        return self._bash_argv(_BUILD_STATUS_SCRIPT, self.python)

    def build_install_argv(self, runtime_bundle: str,
                           bundle_sha256: str) -> tuple[str, ...]:
        """Unpack the shipped build under the user's home and switch to it."""
        return self._bash_argv(
            _BUILD_INSTALL_SCRIPT + ' 2>&1', self.python, str(runtime_bundle),
            str(bundle_sha256))

    def build_disable_argv(self) -> tuple[str, ...]:
        """Stop using the build (the wheel answers); the files stay."""
        return self._bash_argv(_BUILD_DISABLE_SCRIPT, self.python)

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
            '        out["build"] = gmsh.option.getString("General.BuildInfo")\n'
            '        out["gmsh_file"] = getattr(gmsh, "__file__", "")\n'
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
        # Plan 35 CR5. One wrapper for both engines: run report, lifetime
        # pipe, heartbeat and an exit acknowledged only once no writer is left.
        from foammesh.core.jobs.wsl_wrapper import cleanup_script, wrapped_script
        script = wrapped_script(
            prelude=exports.rstrip().rstrip(';'), runtime_cwd=runtime_cwd,
            run=run, pid_name=pid_name, token=token)
        cleanup = cleanup_script(runtime_cwd=runtime_cwd, pid_name=pid_name)
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
