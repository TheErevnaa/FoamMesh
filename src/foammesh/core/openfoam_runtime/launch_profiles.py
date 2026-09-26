"""Native and Windows/WSL OpenFOAM command construction.

The profile owns host-to-runtime path translation, environment activation,
line buffering, and the WSL-side process-group cleanup command.  Callers pass
semantic utility arguments and never construct shell text themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shlex
import shutil
from typing import Mapping
from uuid import uuid4


class LaunchProfileError(ValueError):
    pass


# DP-45. Settings handed to `source <bashrc>` as arguments, which OpenFOAM
# evaluates in `_foamParams "$@"` after its own exports have run -- the
# upstream-sanctioned way to override a shipped default without editing the
# distribution.
#
# `ParaView_TYPE=none` is here because the block the shipped `system` value
# enables runs `pvserver --version` on every source of the environment. On a
# loaded machine that call does not return, and a probe asking whether
# `snappyHexMesh` exists then times out and reports a working OpenFOAM as
# absent. Nothing FoamMesh runs needs ParaView; the product does not offer it
# at all.
SOURCE_SETTINGS: tuple[str, ...] = ('ParaView_TYPE=none',)


@dataclass(frozen=True)
class LaunchCommand:
    argv: tuple[str, ...]
    cleanup_argv: tuple[str, ...] = ()
    profile_id: str = 'native-path'
    runtime_cwd: str | None = None


@dataclass(frozen=True)
class OpenFoamLaunchProfile:
    profile_id: str
    kind: str
    distribution: str | None = None
    user: str | None = None
    bashrc: str = '/opt/openfoam13/etc/bashrc'
    wsl_executable: str = 'wsl.exe'
    environment: Mapping[str, str] = field(default_factory=dict)
    expected_project: str = 'OpenFOAM'
    expected_version: str = '13'

    def __post_init__(self):
        if self.kind not in {'native', 'wsl'}:
            raise LaunchProfileError('OpenFOAM profile kind must be native or wsl')
        if not self.profile_id or self.profile_id.strip() != self.profile_id:
            raise LaunchProfileError('profile_id must be a non-empty trimmed token')
        if self.kind == 'wsl' and not self.distribution:
            raise LaunchProfileError('WSL OpenFOAM profile requires a distribution')
        if not self.expected_project or not self.expected_version:
            raise LaunchProfileError(
                'OpenFOAM profile requires an expected project and version')
        object.__setattr__(self, 'environment', dict(self.environment))

    def export_clause(self) -> str:
        """The profile's declared environment, as shell exports.

        DP-48. `environment` was a declared field that `__post_init__`
        normalised and `fingerprint` recorded and no builder ever read, so
        setting it moved a profile's identity without changing a single
        command -- a change that looks applied and is not. It is applied
        here, ahead of the source, and the empty case adds nothing.

        The form is copied from the Gmsh backend, which has applied its own
        `environment` this way in `runner_argv` since it was written. The two
        profiles were meant to be built alike; only one of them was.
        """
        if not self.environment:
            return ''
        return ''.join(
            f'export {shlex.quote(name)}={shlex.quote(str(value))}; '
            for name, value in sorted(self.environment.items()))

    def source_clause(self, *, quiet: bool = False) -> str:
        """Build the `source <bashrc>` clause every runtime command opens with.

        One place, so a setting added for detection is also carried by the
        script that actually runs the mesher -- a probe and a run that source
        different environments are two different runtimes.

        Order is deliberate and the two halves are not interchangeable. The
        profile's own environment goes first as exports; then the bashrc,
        with SOURCE_SETTINGS passed as *arguments* to `source`, because an
        exported value would not survive it -- line 112 of OpenFOAM 13's
        bashrc exports ParaView_TYPE unconditionally, and the parameter form
        is the override its own `_foamParams` provides.
        """
        clause = ' '.join(
            ('source', shlex.quote(self.bashrc), *SOURCE_SETTINGS))
        if quiet:
            clause = f'{clause} >/dev/null 2>&1'
        return f'{self.export_clause()}{clause}'

    def translate_host_path(self, path: str | Path) -> str:
        if self.kind == 'native':
            return str(Path(path).resolve())
        windows = PureWindowsPath(str(path))
        if not windows.is_absolute() or not windows.drive:
            raise LaunchProfileError(
                'WSL OpenFOAM execution requires an absolute Windows drive path')
        if windows.drive.startswith('\\\\'):
            raise LaunchProfileError(
                'UNC paths require an explicit local-copy workspace profile')
        drive = windows.drive.rstrip(':').lower()
        parts = [part for part in windows.parts[1:] if part not in {'\\', '/'}]
        return str(PurePosixPath('/mnt', drive, *parts))

    @property
    def fingerprint(self) -> str:
        """Stable, secret-free identity for plans, allocations, and evidence."""
        document = {
            'profile_id': self.profile_id,
            'kind': self.kind,
            'distribution': self.distribution,
            'user': self.user,
            'bashrc': self.bashrc,
            'expected_project': self.expected_project,
            'expected_version': self.expected_version,
            'environment': {name: str(value) for name, value in sorted(self.environment.items())},
            'source_settings': list(SOURCE_SETTINGS),
        }
        encoded = json.dumps(
            document, sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()

    def command(self, utility: str, arguments=(), *, cwd: str | Path) -> LaunchCommand:
        utility = str(utility).strip()
        if not utility or '/' in utility or '\\' in utility or '\x00' in utility:
            raise LaunchProfileError('utility must be a plain executable name')
        suffix = tuple(str(item) for item in arguments)
        if any('\x00' in item for item in suffix):
            raise LaunchProfileError('OpenFOAM arguments cannot contain NUL')
        if self.kind == 'native':
            executable = shutil.which(utility) or utility
            return LaunchCommand(
                (executable, *suffix), profile_id=self.profile_id,
                runtime_cwd=str(Path(cwd).resolve()))

        runtime_cwd = self.translate_host_path(cwd)
        suffix = tuple(self._translate_argument(item) for item in suffix)
        token = uuid4().hex
        pid_name = f'.foammesh-openfoam-{token}.pid'
        exit_name = f'.foammesh-openfoam-{token}.exit'
        run = shlex.join((utility, *suffix))
        grouped = 'setsid bash -c ' + shlex.quote('exec stdbuf -oL -eL ' + run)
        script = (
            f'{self.source_clause()} && '
            f'cd {shlex.quote(runtime_cwd)} && '
            f'rm -f {shlex.quote(pid_name)} {shlex.quote(exit_name)}; '
            f'{grouped} & child=$!; '
            f'echo "$child" > {shlex.quote(pid_name)}; '
            f'wait "$child"; status=$?; '
            f'rm -f {shlex.quote(pid_name)} {shlex.quote(exit_name)}; '
            'exit "$status"'
        )
        prefix = self._wsl_prefix()
        cleanup_script = (
            f'cd {shlex.quote(runtime_cwd)} && '
            f'if test -s {shlex.quote(pid_name)}; then '
            f'pid=$(cat {shlex.quote(pid_name)}); '
            'kill -TERM -- "-$pid" 2>/dev/null || true; '
            'sleep 1; kill -KILL -- "-$pid" 2>/dev/null || true; '
            f'rm -f {shlex.quote(pid_name)} {shlex.quote(exit_name)}; fi'
        )
        return LaunchCommand(
            (*prefix, 'bash', '-c', script),
            (*prefix, 'bash', '-c', cleanup_script),
            self.profile_id,
            runtime_cwd,
        )

    def probe_argv(self, utility: str) -> tuple[str, ...]:
        if self.kind == 'native':
            executable = shutil.which(utility)
            return (executable or utility, '-help')
        script = (
            f'{self.source_clause(quiet=True)} && '
            f'command -v {shlex.quote(utility)}'
        )
        return (*self._wsl_prefix(), 'bash', '-c', script)

    def identity_probe_argv(self, utilities=()) -> tuple[str, ...]:
        """Build one exact-runtime probe for identity, utilities, and MPI."""
        names = tuple(dict.fromkeys(str(item) for item in utilities))
        if any(not item or '/' in item or '\\' in item for item in names):
            raise LaunchProfileError('probe utilities must be plain names')
        lines = [
            f'test -r {shlex.quote(self.bashrc)} || '
            f'{{ echo "__FOAMMESH_ERROR__=missing bashrc {self.bashrc}"; exit 40; }}',
            f'{self.source_clause(quiet=True)} || '
            f'{{ echo "__FOAMMESH_ERROR__=could not source {self.bashrc}"; exit 41; }}',
            'echo "__FOAMMESH_PROJECT__=$WM_PROJECT"',
            'echo "__FOAMMESH_VERSION__=$WM_PROJECT_VERSION"',
            'echo "__FOAMMESH_OPTIONS__=$WM_OPTIONS"',
            # Plan 31 CP-07. Open MPI refuses to start a single rank as root
            # unless told to, so the launcher has to know who the runtime is
            # before it builds an ``mpirun`` line, not after it has failed.
            'echo "__FOAMMESH_UID__=$(id -u 2>/dev/null)"',
        ]
        for name in names:
            quoted = shlex.quote(name)
            lines.append(
                f'path=$(command -v {quoted} 2>/dev/null || true); '
                f'echo "__FOAMMESH_UTILITY__{name}=$path"')
        lines.append(
            'mpi=$(mpirun --version 2>&1 | head -n 1 || true); '
            'echo "__FOAMMESH_MPI__=$mpi"')
        script = '; '.join(lines)
        if self.kind == 'wsl':
            return (*self._wsl_prefix(), 'bash', '-c', script)
        return ('bash', '-lc', script)

    def decomposition_probe_argv(self) -> tuple[str, ...]:
        """Ask the selected runtime which decomposition libraries it has.

        Plan 31 CP-07 item 4. A method the page offers and the runtime cannot
        load is a control that fails after the user has committed to a run, so
        the answer has to come from the runtime rather than from a list
        compiled when this product was written.

        The subtlety is ``lib/dummy``. OpenFOAM builds stub versions of the
        optional decomposition libraries there so that a case naming one links
        and then aborts with a message; on this machine's OpenFOAM 13,
        ``libmetisDecomp.so`` exists *only* under ``lib/dummy``. A probe that
        matched on filename would report metis as available and be wrong every
        time, so each hit is reported with the directory it was found in and
        the caller decides.
        """
        script = '; '.join([
            f'{self.source_clause(quiet=True)} || '
            '{ echo "__FOAMMESH_ERROR__=could not source the runtime"; exit 41; }',
            'root="$FOAM_LIBBIN"',
            'test -n "$root" || '
            'root="$WM_PROJECT_DIR/platforms/$WM_OPTIONS/lib"',
            'echo "__FOAMMESH_LIBDIR__=$root"',
            'for f in $(find "$root" "$root/$FOAM_MPI" -maxdepth 2 '
            '-name "lib*Decomp.so" 2>/dev/null); do '
            'echo "__FOAMMESH_DECOMP__$(basename "$f")=$(dirname "$f")"; done',
        ])
        if self.kind == 'wsl':
            return (*self._wsl_prefix(), 'bash', '-c', script)
        return ('bash', '-lc', script)

    def _translate_argument(self, argument: str) -> str:
        windows = PureWindowsPath(argument)
        if windows.is_absolute() and windows.drive:
            return self.translate_host_path(argument)
        return argument

    def _wsl_prefix(self) -> tuple[str, ...]:
        result = [self.wsl_executable, '--distribution', str(self.distribution)]
        if self.user:
            result.extend(('--user', self.user))
        result.append('--exec')
        return tuple(result)


def configured_profiles(environment: Mapping[str, str] | None = None
                        ) -> tuple[OpenFoamLaunchProfile, ...]:
    env = dict(os.environ if environment is None else environment)
    profiles = []
    requested_kind = env.get('FOAMMESH_OPENFOAM_KIND', '').strip().lower()
    if not requested_kind:
        requested_kind = 'wsl' if os.name == 'nt' else 'native'
    if requested_kind == 'native':
        profiles.append(OpenFoamLaunchProfile(
            env.get('FOAMMESH_OPENFOAM_PROFILE_ID', 'native-path-openfoam'),
            'native',
            bashrc=env.get('FOAMMESH_OPENFOAM_BASHRC',
                           '/opt/openfoam13/etc/bashrc')))
    if requested_kind == 'wsl' and os.name == 'nt':
        profiles.append(OpenFoamLaunchProfile(
            env.get('FOAMMESH_OPENFOAM_PROFILE_ID',
                    'wsl-ubuntu22-openfoam-13'),
            'wsl',
            distribution=env.get(
                'FOAMMESH_OPENFOAM_WSL_DISTRO', 'OpenFOAM13Runtime'),
            user=env.get('FOAMMESH_OPENFOAM_WSL_USER', 'foamuser'),
            bashrc=env.get(
                'FOAMMESH_OPENFOAM_BASHRC', '/opt/openfoam13/etc/bashrc'),
            wsl_executable=env.get('FOAMMESH_WSL_EXECUTABLE', 'wsl.exe'),
        ))
    if requested_kind and not profiles:
        raise LaunchProfileError(
            f'OpenFOAM profile kind {requested_kind!r} is unavailable on this host')
    return tuple(profiles)
