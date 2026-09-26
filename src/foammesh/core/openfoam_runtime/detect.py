"""Find an OpenFOAM 13 + Gmsh runtime among the WSL distributions on this host.

A user who already has WSL with OpenFOAM 13 and Gmsh should not have to know
that FoamMesh expected a distribution called ``OpenFOAM13Runtime`` and a user
called ``foamuser``. This looks for them instead.

The order is the one the product ships with. The configured distribution and
user are tried first (out of the box, ``OpenFOAM13Runtime`` as ``foamuser``).
Every other distribution is then tried as ``foamuser`` first, and as its own
default user after that. The first one that has both an OpenFOAM 13 bashrc and
an importable ``gmsh`` wins; failing that, the first one with OpenFOAM 13 alone.

Nothing here is started on the GUI thread: each probe starts a distribution,
which can take 20 s when WSL is cold.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import subprocess
from typing import Callable, Iterable, Sequence

PREFERRED_USER = 'foamuser'

#: Where OpenFOAM Foundation 13 puts its bashrc: the Ubuntu package, then a
#: source build in the user's home.
BASHRC_CANDIDATES = (
    '/opt/openfoam13/etc/bashrc',
    '$HOME/OpenFOAM/OpenFOAM-13/etc/bashrc',
)

#: Distributions that are WSL plumbing, not places a user installs OpenFOAM.
_IGNORED_PREFIXES = ('docker-desktop', 'rancher-desktop', 'podman-machine')

_PROBE_SCRIPT = (
    'echo "user=$(id -un)"; echo "home=$HOME"; '
    'for f in ' + ' '.join(f'"{path}"' for path in BASHRC_CANDIDATES) + '; do '
    '[ -f "$f" ] && { echo "bashrc=$f"; break; }; done; '
    'python3 -c "import gmsh; print(\'gmsh=\' + str(getattr(gmsh, \'__version__\', '
    'None) or gmsh.GMSH_API_VERSION))" 2>/dev/null; true'
)

Runner = Callable[[Sequence[str], float], 'tuple[int, bytes]']


@dataclass(frozen=True)
class DetectedRuntime:
    distribution: str
    user: str
    bashrc: str = ''
    gmsh_version: str = ''

    @property
    def has_openfoam(self) -> bool:
        return bool(self.bashrc)

    @property
    def has_gmsh(self) -> bool:
        return bool(self.gmsh_version)

    def describe(self) -> str:
        parts = ['OpenFOAM 13' if self.has_openfoam else 'no OpenFOAM 13',
                 f'Gmsh {self.gmsh_version}' if self.has_gmsh else 'no Gmsh']
        return f'{self.distribution} as {self.user}: ' + ', '.join(parts)


def _run(argv: Sequence[str], timeout: float) -> tuple[int, bytes]:
    environment = dict(os.environ, WSL_UTF8='1')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, timeout=timeout,
            env=environment, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        return -1, b''
    return completed.returncode, completed.stdout


def _text(output: bytes) -> str:
    # wsl.exe answers in UTF-16 unless WSL_UTF8 is honoured; a NUL between
    # every character is the sign.
    if b'\x00' in output:
        try:
            return output.decode('utf-16-le')
        except UnicodeDecodeError:
            return output.replace(b'\x00', b'').decode('utf-8', 'replace')
    return output.decode('utf-8', 'replace')


def list_distributions(*, wsl: str = 'wsl.exe', runner: Runner = _run,
                       timeout: float = 30.0) -> list[str]:
    code, output = runner((wsl, '--list', '--quiet'), timeout)
    if code != 0:
        return []
    names = []
    for line in _text(output).splitlines():
        name = line.strip().lstrip('﻿').strip()
        if name and not name.lower().startswith(_IGNORED_PREFIXES):
            names.append(name)
    return names


def probe(distribution: str, user: str | None, *, wsl: str = 'wsl.exe',
          runner: Runner = _run, timeout: float = 60.0
          ) -> DetectedRuntime | None:
    """What *distribution* has when entered as *user* (its default if None)."""
    argv = [wsl, '--distribution', distribution]
    if user:
        argv += ['--user', user]
    argv += ['--exec', 'bash', '-c', _PROBE_SCRIPT]
    code, output = runner(tuple(argv), timeout)
    values = {}
    for line in _text(output).splitlines():
        key, separator, value = line.strip().partition('=')
        if separator and key not in values:
            values[key] = value.strip()
    if code != 0 or not values.get('user'):
        return None   # no such user, or the distribution would not start
    bashrc = values.get('bashrc', '')
    home = values.get('home', '')
    if bashrc.startswith('$HOME') and home:
        bashrc = home + bashrc[len('$HOME'):]
    return DetectedRuntime(distribution, values['user'], bashrc,
                           values.get('gmsh', ''))


def candidates(configured_distribution: str, configured_user: str,
               distributions: Iterable[str]) -> list[tuple[str, str | None]]:
    """The (distribution, user) pairs to try, in order; None = default user."""
    order: list[tuple[str, str | None]] = []

    def add(pair):
        if pair[0] and pair not in order:
            order.append(pair)

    add((configured_distribution, configured_user or PREFERRED_USER))
    listed = list(distributions)
    # The configured distribution keeps its place at the head of the list.
    if configured_distribution in listed:
        listed.remove(configured_distribution)
        listed.insert(0, configured_distribution)
    for distribution in listed:
        add((distribution, PREFERRED_USER))
        add((distribution, None))
    return order


def detect(configured_distribution: str = 'OpenFOAM13Runtime',
           configured_user: str = PREFERRED_USER, *, wsl: str = 'wsl.exe',
           runner: Runner = _run, timeout: float = 60.0
           ) -> DetectedRuntime | None:
    """The best OpenFOAM 13 runtime on this host, or None if there is none."""
    if os.name != 'nt' and runner is _run:
        return None
    distributions = list_distributions(wsl=wsl, runner=runner)
    if not distributions:
        return None
    fallback = None
    for distribution, user in candidates(
            configured_distribution, configured_user, distributions):
        if distribution not in distributions:
            continue
        found = probe(distribution, user, wsl=wsl, runner=runner,
                      timeout=timeout)
        if found is None or not found.has_openfoam:
            continue
        if found.has_gmsh:
            return found
        if fallback is None:
            fallback = found
    return fallback
