#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Gmsh mesh export: MSH, and the companion formats measured beside it.

Runs through the host ``gmsh`` module when one is installed, and otherwise
through the qualified WSL Gmsh runtime the meshing engine already uses -- the
export is available wherever meshing is, instead of asking for a second Gmsh
on the host.

Gmsh export of arbitrary polyhedral meshes is inherently lossy — see the
registry capability note.

Plan 31 FC-A, ledger row ``export-formats-unexposed``. Gmsh writes 39 distinct
extensions in this build, and this module used to hand whatever suffix it was
given straight to ``gmsh.write``. That is a wider door than it looks: what
matters is not whether a format can be written but whether it can be read
back, because a file this product cannot reopen is one whose identity it can
never check. :data:`WRITER_TARGETS` is therefore the measured list, and a
target outside it is refused rather than quietly produced.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

#: EXPORT-01. Every conversion this module runs, said out loud: what was
#: invoked, where from, what it exited with and every byte it wrote to its own
#: two streams. MEASURED before this: a CGNS export died with exit 3221225786
#: and the application reported a malformed file, with nothing anywhere on
#: disk or in the log saying what had actually been run or what it had said.
logger = logging.getLogger(__name__)

#: The conversion was stopped; it did not disagree with its input.
#: ``3221225786`` is ``0xC000013A``, Windows' ``STATUS_CONTROL_C_EXIT`` -- the
#: code the measured export came back with. ``130`` and ``-2`` are the two
#: spellings of the same event on POSIX.
INTERRUPTED_RETURNCODES = frozenset({3221225786, 130, -2})

#: The three answers a conversion that produced no usable file can give.
INTERRUPTED = 'interrupted'
MALFORMED = 'malformed'
FAILED = 'failed'


class ExportRunError(RuntimeError):
    """A conversion that produced no file, and which of the three it was.

    EXPORT-01. The three are not the same sentence to a reader: `interrupted`
    means nothing is wrong with the mesh and the act can be repeated,
    `malformed` means a file landed and cannot be trusted, and `failed` means
    the conversion refused the input it was given. One message for all three
    sent the reader who had interrupted an export looking for a bad mesh.
    """

    def __init__(self, message: str, kind: str = FAILED):
        super().__init__(message)
        self.kind = kind

#: What this module will write, and what a cold read-back measured it keeps.
#:
#: MEASURED 2026-09-06 with Gmsh 4.15.2 in the ``OpenFOAM13Runtime`` distro --
#: script and output at ``plans/evidence/plan31/fca-format-io/`` -- on one box
#: meshed to 307 nodes and 984 tetrahedra with three named faces and one named
#: volume. Every entry below returned those counts and all four group names
#: when the written file was reopened in a fresh session.
#:
#: ``.vtk`` is writable by the same call and is deliberately not here: it came
#: back with the right element counts and no physical groups at all, so it
#: cannot carry the patch identity this product publishes by.
WRITER_TARGETS = {
    '.msh': 'the native mesh every reader in this application parses',
    '.med': 'group names round-trip, padded by MED to 80 characters',
    '.cgns': 'group names round-trip exactly; needs no VTK CGNS module',
    '.unv': 'group names round-trip exactly',
}


def census(gmsh) -> dict:
    """Nodes, volume elements and physical group names of the open model.

    The census a written file is checked by, rather than its byte count -- a
    number equally happy to describe a mesh with no patch names left in it.

    Group names are stripped: MED pads every one of them to 80 characters on
    the way out and hands the padding straight back on the way in, so an
    unstripped comparison calls a perfectly good file wrong.
    """
    tags, _coords, _params = gmsh.model.mesh.getNodes()
    _types, groups, _nodes = gmsh.model.mesh.getElements(3)
    names = []
    for dim, tag in gmsh.model.getPhysicalGroups():
        label = str(gmsh.model.getPhysicalName(dim, tag)).strip()
        if label:
            names.append(label)
    return {'nodes': len(tags),
            'cells': sum(len(item) for item in groups),
            'groups': sorted(names)}


def supported_target(dest) -> str:
    """The measured note for *dest*'s suffix, raising when there is none."""
    suffix = Path(dest).suffix.lower()
    if suffix not in WRITER_TARGETS:
        named = suffix or 'a file with no suffix'
        raise ValueError(
            f'Gmsh export has no measured writer for {named}; the measured '
            'targets are ' + ', '.join(sorted(WRITER_TARGETS)))
    return WRITER_TARGETS[suffix]


#: Written into the conversion scripts when the caller asks for it.
#:
#: Plan 31 DP-23. Gmsh writes only the elements that belong to a physical
#: group whenever the model has any -- ``Mesh.SaveAll`` is 0 by default -- and
#: the interchange the Gmsh export now converts from names its boundary
#: patches and deliberately does not name the volume. MEASURED 2026-09-06 on
#: ``tests/fixtures/cases/single_hex``: without this option the written file
#: came back ``{"nodes": 8, "cells": 0, "groups": ["walls"]}`` -- the patch
#: name recovered and the one hexahedron gone. With it, ``cells`` is 1.
_SAVE_ALL_LINE = 'gmsh.option.setNumber("Mesh.SaveAll", 1)\n'


def _convert_script(save_all: bool) -> str:
    return (
        'import sys\n'
        'import gmsh\n'
        'gmsh.initialize()\n'
        + (_SAVE_ALL_LINE if save_all else '')
        + 'try:\n'
        '    gmsh.open(sys.argv[1])\n'
        '    gmsh.write(sys.argv[2])\n'
        'finally:\n'
        '    gmsh.finalize()\n')


_CONVERT_SCRIPT = _convert_script(False)


#: The line :func:`convert_and_census` reads its answer off.
CENSUS_MARKER = 'FOAMMESH_EXPORT_CENSUS '

def _convert_and_census_script(save_all: bool) -> str:
    """Convert, then reopen what was written in a *fresh* Gmsh session.

    Plan 31 FC-F. The second ``initialize`` is not decoration. FC-A measured
    (``plans/evidence/plan31/fca-format-io/isolation.json``) that reading a MED
    file back into a session that still holds the model it was written from
    merges it into that model instead of a scratch one -- 307 nodes became 614
    and four physical names became eight -- because Gmsh's MED reader selects
    the model by the mesh name stored in the file. Finalising first is what
    makes the read-back an independent measurement rather than a copy of the
    writer's own opinion, and it costs one interpreter instead of two.
    """
    return (
        'import json, sys\n'
        'import gmsh\n'
        'gmsh.initialize()\n'
        + (_SAVE_ALL_LINE if save_all else '')
        + _CONVERT_AND_CENSUS_TAIL)


_CONVERT_AND_CENSUS_TAIL = (
    'try:\n'
    '    gmsh.open(sys.argv[1])\n'
    '    gmsh.write(sys.argv[2])\n'
    'finally:\n'
    '    gmsh.finalize()\n'
    'gmsh.initialize()\n'
    'try:\n'
    '    gmsh.open(sys.argv[2])\n'
    '    tags, _c, _p = gmsh.model.mesh.getNodes()\n'
    '    _t, cells, _n = gmsh.model.mesh.getElements(3)\n'
    '    names = []\n'
    '    for dim, tag in gmsh.model.getPhysicalGroups():\n'
    '        label = str(gmsh.model.getPhysicalName(dim, tag)).strip()\n'
    '        if label:\n'
    '            names.append(label)\n'
    '    print("' + CENSUS_MARKER + '" + json.dumps({\n'
    '        "nodes": len(tags),\n'
    '        "cells": sum(len(item) for item in cells),\n'
    '        "groups": sorted(names)}))\n'
    'finally:\n'
    '    gmsh.finalize()\n')


def host_available() -> bool:
    return 'gmsh' in sys.modules or importlib.util.find_spec('gmsh') is not None


def runtime_profile():
    """The qualified Gmsh launch profile, or None where there is no runtime."""
    try:
        from foammesh.core.gmsh.launch_profiles import configured_profiles
        profiles = configured_profiles(os.environ)
    except Exception:  # noqa: BLE001 - a profile that cannot be built is no profile
        return None
    return profiles[0] if profiles else None


def is_available() -> bool:
    return host_available() or runtime_profile() is not None


def unavailable_reason() -> str:
    """Why Gmsh export cannot run here -- empty when it can.

    Plan 31 DP-24. Every disabled row in the readiness table used to say the
    same sentence -- ``needs the [export] extra`` -- whatever had actually
    failed to import. This route has two ways of being satisfied and the
    message says which one is missing, because installing the pip extra is
    only one of the two answers.
    """
    if is_available():
        return ''
    return ('Gmsh export needs Gmsh, and neither route is present here: '
            '"import gmsh" fails on the host, and no qualified WSL Gmsh '
            'runtime is configured. Either one fixes it — pip install '
            '"foammesh[export]" for the host module, or configure the WSL '
            'Gmsh runtime the meshing engine already uses.')


def require() -> None:
    if not is_available():
        raise RuntimeError(unavailable_reason())


def staging_path(dest) -> Path:
    """Where a conversion writes before it has earned the reader's own path.

    EXPORT-01. The writer used to be pointed straight at the destination the
    user named, so an interrupted conversion left whatever it had managed to
    put there under that name, indistinguishable from a finished export. The
    staging name keeps the destination's suffix, because Gmsh chooses the
    format it writes from the extension it is handed and a ``.part`` at the
    end of the name would leave it with nothing to choose from.
    """
    dest = Path(dest)
    return dest.with_name(f'{dest.stem}.part{dest.suffix}')


def _discard(staging: Path) -> None:
    """Take the half-written file away, and say so if it will not go."""
    try:
        staging.unlink(missing_ok=True)
    except OSError as error:                                 # noqa: PERF203
        logger.warning('Gmsh export could not remove %s: %s', staging, error)


def _promote(staging: Path, out: Path) -> None:
    os.replace(staging, out)
    logger.info('Gmsh export promoted %s to %s', staging, out)


def _tail(completed) -> str:
    said = (completed.stderr or completed.stdout or '').strip().splitlines()
    return ' | '.join(said[-5:])


def _run_conversion(profile, argv, timeout, staging: Path):
    """Run the conversion, logging what it was asked and what it answered.

    EXPORT-01. Everything a reader outside the application needs in order to
    repeat the run by hand goes through here once: the invocation, the
    directory it was launched from, the return code and both streams. An
    interruption is classified here too, because it is the same event however
    it arrives -- a return code, a timeout, or a keyboard.
    """
    spoken = ' '.join(str(argument) for argument in argv)
    logger.info('Gmsh export through %s runs %s (cwd %s)',
                profile.profile_id, spoken, os.getcwd())
    try:
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=timeout)
    except subprocess.TimeoutExpired as error:
        logger.info('Gmsh export through %s was interrupted: no answer within '
                    '%s seconds (cwd %s, stdout and stderr unread)',
                    profile.profile_id, timeout, os.getcwd())
        _discard(staging)
        raise ExportRunError(
            f'Gmsh export through {profile.profile_id} was interrupted: it '
            f'did not answer within {timeout} seconds. Nothing was written.',
            INTERRUPTED) from error
    except KeyboardInterrupt as error:
        logger.info('Gmsh export through %s was interrupted from the keyboard '
                    '(cwd %s, stdout and stderr unread)',
                    profile.profile_id, os.getcwd())
        _discard(staging)
        raise ExportRunError(
            f'Gmsh export through {profile.profile_id} was interrupted from '
            'the keyboard. Nothing was written.', INTERRUPTED) from error
    except OSError as error:
        logger.info('Gmsh export through %s did not start (cwd %s): %s; '
                    'stdout and stderr unread',
                    profile.profile_id, os.getcwd(), error)
        _discard(staging)
        raise ExportRunError(
            f'Gmsh export through {profile.profile_id} did not run: {error}',
            FAILED) from error
    logger.info('Gmsh export through %s exited %s (cwd %s); stdout: %s; '
                'stderr: %s', profile.profile_id, completed.returncode,
                os.getcwd(), (completed.stdout or '').strip(),
                (completed.stderr or '').strip())
    if completed.returncode in INTERRUPTED_RETURNCODES:
        _discard(staging)
        raise ExportRunError(
            f'Gmsh export through {profile.profile_id} was interrupted '
            f'(exit {completed.returncode}). The mesh is untouched and the '
            'export can be run again.', INTERRUPTED)
    return completed


def convert_with_gmsh(input_mesh, dest) -> Path:
    """Open *input_mesh* (any format gmsh reads) and write *dest*.

    The written format is chosen from *dest*'s suffix, and the suffix has to be
    one :data:`WRITER_TARGETS` names -- see the module docstring for why that
    is the measured list rather than everything Gmsh can write.
    """
    supported_target(dest)
    if host_available():
        import gmsh
        gmsh.initialize()
        try:
            gmsh.open(str(input_mesh))
            out = Path(dest)
            gmsh.write(str(out))
            return out
        finally:
            gmsh.finalize()
    profile = runtime_profile()
    if profile is None:
        require()
    return convert_in_runtime(profile, input_mesh, dest)


def convert_and_census(input_mesh, dest, *, timeout: float = 900,
                       save_all: bool = False) -> dict:
    """Write *dest* from *input_mesh* and report what reading *dest* back gives.

    Returns ``{'nodes': int, 'cells': int, 'groups': [str]}`` measured on the
    file that landed on disk, not on the model that produced it. The export
    side needs this because the byte count it used to report is a number
    equally happy to describe a mesh with every patch name stripped out of it
    -- which is exactly what a ``.vtk`` written by the same call comes back as.

    ``save_all`` writes elements that belong to no physical group as well as
    those that do -- see :data:`_SAVE_ALL_LINE` for what it is worth in cells.
    """
    supported_target(dest)
    out = Path(dest)
    staging = staging_path(out)
    _discard(staging)
    if host_available():
        import gmsh
        # EXPORT-01. The read-back is a check, and a check the destination has
        # already been given the file for is no check at all: what is censused
        # here is the staging file, and only a census that came back promotes
        # it to the path the reader named.
        try:
            gmsh.initialize()
            if save_all:
                gmsh.option.setNumber('Mesh.SaveAll', 1)
            try:
                gmsh.open(str(input_mesh))
                gmsh.write(str(staging))
            finally:
                gmsh.finalize()
            gmsh.initialize()
            try:
                gmsh.open(str(staging))
                answered = census(gmsh)
            finally:
                gmsh.finalize()
        except BaseException:
            _discard(staging)
            raise
        _promote(staging, out)
        return answered
    profile = runtime_profile()
    if profile is None:
        require()
    argv = profile.python_argv(
        _convert_and_census_script(save_all),
        profile.translate_host_path(Path(input_mesh).resolve()),
        profile.translate_host_path(staging.resolve()))
    completed = _run_conversion(profile, argv, timeout, staging)
    answered = next((line for line in (completed.stdout or '').splitlines()
                     if line.startswith(CENSUS_MARKER)), None)
    written = staging.exists()
    if answered is not None and written:
        _promote(staging, out)
        return json.loads(answered[len(CENSUS_MARKER):])
    _discard(staging)
    if answered is not None:
        raise ExportRunError(
            f'Gmsh export through {profile.profile_id} counted a file that is '
            f'not there (exit {completed.returncode}): ' + _tail(completed),
            MALFORMED)
    if written:
        # A file landed and the session that wrote it could not read it back.
        # That is a statement about the file, and the only one of the three
        # that earns the word malformed.
        raise ExportRunError(
            f'Gmsh export through {profile.profile_id} wrote a file it could '
            f'not read back (exit {completed.returncode}): ' + _tail(completed),
            MALFORMED)
    raise ExportRunError(
        f'Gmsh export through {profile.profile_id} did not say what it wrote '
        f'(exit {completed.returncode}): ' + _tail(completed), FAILED)


def convert_in_runtime(profile, input_mesh, dest, *, timeout: float = 600) -> Path:
    """Convert through the runtime's own Gmsh, with paths it can see."""
    out = Path(dest)
    staging = staging_path(out)
    _discard(staging)
    argv = profile.python_argv(
        _CONVERT_SCRIPT,
        profile.translate_host_path(Path(input_mesh).resolve()),
        profile.translate_host_path(staging.resolve()))
    completed = _run_conversion(profile, argv, timeout, staging)
    written = staging.exists()
    if completed.returncode == 0 and written:
        _promote(staging, out)
        return out
    _discard(staging)
    if written:
        raise ExportRunError(
            f'Gmsh export through {profile.profile_id} left a file it had not '
            f'finished (exit {completed.returncode}): ' + _tail(completed),
            MALFORMED)
    raise ExportRunError(
        f'Gmsh export through {profile.profile_id} failed '
        f'(exit {completed.returncode}): ' + _tail(completed), FAILED)
