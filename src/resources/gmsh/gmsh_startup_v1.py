"""How every Gmsh process FoamMesh starts begins and ends (Plan 50 IP6).

Two things every one of them shares, kept in one place so they cannot drift:

* **GM-7, one** ``gmsh.initialize``. The resident session has always started
  Gmsh with ``-noenv`` and ``readConfigFiles=False``; the batch runner and its
  probes started it with no arguments, so a ``~/.gmshrc`` or
  ``~/.gmsh-options`` in the distro changed a batch mesh and not the session's
  (DP-6250). Every process now starts Gmsh through :func:`initialize`, and a
  ``python -c`` probe, which cannot import this file, through
  :data:`INITIALIZE_SOURCE`.
* **GM-6, a scratch working directory**. Gmsh writes debug side files
  (``*.pos``, ``_Cut.msh``, plugin by-products) into the current directory.
  The session started wherever FoamMesh had been launched and the batch runner
  in the user's case folder, so those files landed there (DP-6251). Each
  process now works in a private folder under the distro's temp directory,
  removed when the process ends; a folder left by a process that was killed
  is removed by the next process that starts, once its owner is gone. Every
  path the processes are given is absolute, and :func:`absolute` resolves the
  few that might not be against the folder the process was started in.

Pure standard library: the runner, the session and their workers import it
by name from this folder; tests import it from the file.
"""
from __future__ import annotations

import os
import shutil
import tempfile

#: The command line Gmsh is started with: no environment variables read.
INITIALIZE_ARGV = ('', '-noenv')

#: ``gmsh.initialize`` as one line of source, for ``python -c`` probes.
INITIALIZE_SOURCE = (
    "gmsh.initialize(['', '-noenv'], readConfigFiles=False, run=False)\n")

#: Scratch folders are named ``<prefix><pid>-<random>``.
SCRATCH_PREFIX = 'foammesh-gmsh-cwd-'


#: Plan 50 IP18 (DP-6323). The resident session's mesh snapshots live here,
#: under the distro's temp directory: outside the case (the person's folder
#: holds only what they made) and outside the scratch folder (removed when
#: the process ends, and a snapshot exists to outlive a crashed process).
SNAPSHOT_FOLDER_NAME = 'foammesh-gmsh-snapshots'


def snapshot_folder(root=None) -> str:
    """The folder session snapshots are written in, made if missing."""
    folder = os.path.join(root or tempfile.gettempdir(), SNAPSHOT_FOLDER_NAME)
    os.makedirs(folder, exist_ok=True)
    return folder


def initialize(gmsh, argv=None):
    """Start Gmsh without reading the user's environment or config files."""
    arguments = list(INITIALIZE_ARGV) + [str(item) for item in argv or ()]
    gmsh.initialize(arguments, readConfigFiles=False, run=False)


def absolute(path, origin):
    """*path* as an absolute path, a relative one taken from *origin*."""
    text = str(path)
    if not text or os.path.isabs(text):
        return text
    return os.path.normpath(os.path.join(str(origin), text))


def _owner_alive(pid: int) -> bool:
    """Whether process *pid* still runs (Linux; on Windows always True).

    ``os.kill(pid, 0)`` on Windows terminates the process rather than probing
    it, so outside Linux nothing is judged dead and nothing is swept.
    """
    if os.name == 'nt' or pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # Permission refused, or anything else: the owner may be alive.
        return True
    return True


def sweep_stale(root=None, alive=None) -> list:
    """Remove scratch folders whose owning process has ended."""
    root = root or tempfile.gettempdir()
    alive = alive or _owner_alive
    removed = []
    try:
        names = os.listdir(root)
    except OSError:
        return removed
    for name in names:
        if not name.startswith(SCRATCH_PREFIX):
            continue
        pid_text = name[len(SCRATCH_PREFIX):].split('-', 1)[0]
        if not pid_text.isdigit():
            continue
        if alive(int(pid_text)):
            continue
        folder = os.path.join(root, name)
        if os.path.isdir(folder) and not os.path.islink(folder):
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder)
    return removed


class ScratchDirectory:
    """Work in a private temp folder; return and remove it on :meth:`leave`.

    ``origin`` is the folder the process was started in, for :func:`absolute`.
    """

    def __init__(self, root=None):
        self.root = root or tempfile.gettempdir()
        self.origin = os.getcwd()
        self.path = ''

    def enter(self) -> 'ScratchDirectory':
        sweep_stale(self.root)
        self.path = tempfile.mkdtemp(
            prefix=f'{SCRATCH_PREFIX}{os.getpid()}-', dir=self.root)
        os.chdir(self.path)
        return self

    def leave(self) -> None:
        try:
            os.chdir(self.origin)
        except OSError:
            os.chdir(self.root)
        if self.path:
            shutil.rmtree(self.path, ignore_errors=True)
            self.path = ''

    def __enter__(self):
        return self.enter()

    def __exit__(self, *_exc):
        self.leave()
        return False
