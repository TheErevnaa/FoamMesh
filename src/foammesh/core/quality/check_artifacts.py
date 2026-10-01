"""What a checkMesh run wrote, kept per mesh revision, and read back bounded.

Plan 37 UF18. checkMesh v13 writes two kinds of file a user can look at
(``plans/evidence/plan37/uf18-v13-checkmesh-sets.md``):

* ``-writeSets`` writes every failing set to ``<instance>/polyMesh/sets``
  and, through the *set* writer, the point sets (``unusedPoints``,
  ``nonAlignedEdges``, ``shortEdges``, ``nearPoints``, ...) as sampled points
  to ``postProcessing/[<region>/]checkMesh/<instance>/<name>.vtk``;
* ``-writeSurfaces`` writes the face and cell sets, through the *surface*
  writer, as surfaces into the same directory (a cell set as its outside
  faces).

``-writeSets`` alone therefore never gives a bad-face surface, and nothing
but ``-writeSets`` gives a point set. checkMesh never clears that directory,
so a run keeps only what it wrote itself (modified at or after its start).

:func:`collect` copies a run's files beside a manifest that names the check,
the mesh revision, the time instance and the region, and says for every set
whether it can be drawn and, when it cannot, why. :func:`read_highlights`
parses the files -- in the mesh worker, never in the window -- within the
budgets below, refusing what is over them with a typed reason.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

POINTS = 'points'
SURFACE = 'surface'

AVAILABLE = 'available'
MISSING = 'missing'
UNSUPPORTED = 'unsupported'
OVER_BUDGET = 'over_budget'

#: Refusal reasons of :func:`read_highlights`.
REFUSED_NOT_AVAILABLE = 'not_available'
REFUSED_FILE_BUDGET = 'over_budget_file'
REFUSED_REQUEST_FILE_BUDGET = 'over_budget_request_files'
REFUSED_GEOMETRY_BUDGET = 'over_budget_geometry'
REFUSED_REQUEST_GEOMETRY_BUDGET = 'over_budget_request_geometry'
REFUSED_UNREADABLE = 'unreadable'
REFUSED_NO_MANIFEST = 'no_manifest'

MIB = 1024 * 1024
#: One file larger than this is not copied and not parsed. A legacy VTK
#: surface costs about 60 bytes a quad in ASCII (``nonOrthoFaces.vtk``,
#: 760 faces, 24,579 bytes, measured), so this is ~0.5 M faces.
MAX_ARTIFACT_BYTES = 32 * MIB
#: The file bytes one highlight request may parse, all sets together.
MAX_REQUEST_BYTES = 96 * MIB
#: Geometry one highlight may carry: points, and polygons for a surface.
MAX_HIGHLIGHT_POINTS = 500_000
MAX_HIGHLIGHT_POLYGONS = 500_000
#: Points one request may carry, all highlights together. Three float64 a
#: point: 24 MB, well inside the worker's 64 MiB result and 384 MiB cap.
MAX_REQUEST_POINTS = 1_000_000

#: How many revisions are kept per check; older ones are pruned.
KEEP_REVISIONS = 4
#: A file modified this long before the run began is still the run's own:
#: the WSL clock and the Windows clock need not agree to the second.
STALE_SLACK_SECONDS = 2.0

#: The point sets v13 writes (``checkTopology.C`` 149/393/523,
#: ``checkGeometry.C`` 580/899/932). Every other set is a face or cell set.
POINT_SET_NAMES = frozenset({
    'unusedPoints', 'multiRegionPoints', 'nonManifoldPoints',
    'nonAlignedEdges', 'shortEdges', 'nearPoints',
})

ARTIFACT_ROOT = Path('foammesh') / 'quality' / 'check-artifacts'


# --------------------------------------------------------------------------- #
# Collection -- after a run, in the process that ran it
# --------------------------------------------------------------------------- #

def _tokens(argv, depth: int = 3) -> list[str]:
    """The command's words, read through any shell script that launches it.

    Plan 37 UF20. On WSL the recorded command is ``wsl.exe ... bash -c
    "<script>"``, and checkMesh's own flags live inside that script (itself
    nesting ``bash -c '...'``), so they are never separate argv items.
    """
    import shlex
    words: list[str] = []
    for item in (str(entry) for entry in argv or ()):
        words.append(item)
        if depth > 0 and any(space in item for space in ' \t\n'):
            try:
                inner = shlex.split(item)
            except ValueError:
                inner = item.split()
            words.extend(_tokens(inner, depth - 1))
    return words


def _option(argv, name: str) -> str:
    argv = _tokens(argv)
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError):
        return ''


def _has(argv, name: str) -> bool:
    return name in _tokens(argv)


def artifact_root(case_path, check: str = 'openfoam') -> Path:
    return Path(case_path) / ARTIFACT_ROOT / check


def _fresh(path: Path, started_at: float | None) -> bool:
    if started_at is None:
        return True
    try:
        return path.stat().st_mtime >= started_at - STALE_SLACK_SECONDS
    except OSError:
        return False


def _set_class(path: Path) -> str:
    try:
        with path.open('rb') as handle:
            head = handle.read(2048).decode('latin-1')
    except OSError:
        return ''
    found = re.search(r'\bclass\s+(\w+)\s*;', head)
    return found.group(1) if found else ''


def _kind_of(name: str, set_class: str = '') -> str:
    if set_class == 'pointSet' or name in POINT_SET_NAMES:
        return POINTS
    return SURFACE


def _set_files(case: Path, region: str, instance: str,
               started_at: float | None) -> dict[str, dict]:
    """``name -> {'class', 'path'}`` of the sets the run wrote, every rank."""
    relative = Path(instance or 'constant')
    if region:
        relative = relative / region
    relative = relative / 'polyMesh' / 'sets'
    roots = [case] + sorted(case.glob('processor*'))
    found: dict[str, dict] = {}
    for root in roots:
        directory = root / relative
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith('.'):
                continue
            if path.suffix in ('.gz',):
                name = path.stem
            else:
                name = path.name
            if not _fresh(path, started_at):
                continue
            found.setdefault(name, {'class': _set_class(path),
                                    'path': str(path)})
    return found


def _output_dirs(case: Path, region: str) -> list[Path]:
    root = case / 'postProcessing'
    if region:
        root = root / region
    root = root / 'checkMesh'
    if not root.is_dir():
        return []
    return sorted(path for path in root.iterdir() if path.is_dir())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _output_name(path: Path) -> str:
    """``nonOrthoFaces`` of ``nonOrthoFaces.vtk`` / ``.000.mesh`` / ``.case``."""
    return path.name.split('.', 1)[0]


def requested_outputs(argv, wanted=None) -> dict:
    """What each output was asked for and whether the command carried it.

    *wanted* is the `CheckMeshRequest` the settings asked for, when known:
    a flag asked for but missing from the command is one the installed
    checkMesh does not offer.
    """
    def entry(flag: str, attribute: str, off_reason: str, what: str) -> dict:
        passed = _has(argv, flag)
        asked = bool(getattr(wanted, attribute, passed)) if wanted else passed
        if passed:
            reason = ''
        elif asked:
            reason = (f'the installed checkMesh does not offer {flag}, so '
                      f'no {what} were written')
        else:
            reason = off_reason
        return {'flag': flag, 'requested': asked, 'passed': passed,
                'reason': reason}

    return {
        POINTS: entry('-writeSets', 'write_sets',
                      "Write failed sets is off, so checkMesh wrote no point "
                      "sets; turn it on and check again", 'point sets'),
        SURFACE: entry('-writeSurfaces', 'write_surfaces',
                       "Write problem-face surfaces is off, so checkMesh "
                       "wrote the face and cell sets as label lists only; "
                       "turn it on and check again to draw them",
                       'surfaces'),
    }


def collect(case_path, *, argv, started_at: float | None = None,
            wanted=None, check: str = 'openfoam', mesh_fingerprint: str = '',
            mesh_revision=None, checked_at: str = '',
            keep: int = KEEP_REVISIONS) -> dict:
    """Keep what this checkMesh run wrote; returns and stores the manifest.

    *argv* is the command that ran (``-region``, ``-parallel`` and the
    write flags are read from it); *started_at* the run's start, as
    ``time.time()``. Files written before it are an earlier run's and are
    left out; nothing under ``postProcessing`` is removed.
    """
    case = Path(case_path)
    region = _option(argv, '-region')
    parallel = _has(argv, '-parallel')
    outputs = requested_outputs(argv, wanted)
    stamp = time.strftime('%Y%m%dT%H%M%S', time.gmtime())
    revision = f'{stamp}-{(mesh_fingerprint or "unknown")[:12]}'
    root = artifact_root(case, check)
    target = root / revision
    files_dir = target / 'files'

    items: list[dict] = []
    instances: list[str] = []
    seen: set[str] = set()
    for directory in _output_dirs(case, region):
        fresh = [path for path in sorted(directory.iterdir())
                 if path.is_file() and _fresh(path, started_at)]
        if not fresh:
            continue
        instance = directory.name
        instances.append(instance)
        sets = _set_files(case, region, instance, started_at)
        grouped: dict[str, list[Path]] = {}
        for path in fresh:
            grouped.setdefault(_output_name(path), []).append(path)
        for name, paths in sorted(grouped.items()):
            vtk = next((path for path in paths if path.suffix == '.vtk'),
                       None)
            chosen = vtk or paths[0]
            kind = _kind_of(name, sets.get(name, {}).get('class', ''))
            size = sum(path.stat().st_size for path in paths)
            item = {'name': name, 'check': name, 'kind': kind,
                    'instance': instance, 'region': region,
                    'format': chosen.suffix.lstrip('.') or 'unknown',
                    'bytes': size, 'source': str(chosen),
                    'status': AVAILABLE, 'reason': '', 'path': ''}
            if not outputs[kind]['passed']:
                # The command never asked for this writer: whatever its file
                # on disk says, this run did not write it.
                item.update(status=MISSING, format='set', reason=(
                    outputs[kind]['reason'] + '. The file on disk is an '
                    "earlier run's and is not shown"))
            elif vtk is None:
                item.update(status=UNSUPPORTED, reason=(
                    f'checkMesh wrote this set as {item["format"]}; only '
                    'legacy VTK output can be drawn'))
            elif size > MAX_ARTIFACT_BYTES:
                item.update(status=OVER_BUDGET, reason=(
                    f'the file is {size / MIB:.1f} MiB; highlights are '
                    f'drawn from files up to {MAX_ARTIFACT_BYTES // MIB} MiB'))
            else:
                files_dir.mkdir(parents=True, exist_ok=True)
                copy = files_dir / f'{instance}__{vtk.name}'
                shutil.copyfile(vtk, copy)
                item.update(path=str(copy.relative_to(target)),
                            sha256=_sha256(copy), bytes=copy.stat().st_size)
            if kind == POINTS and parallel:
                item['note'] = ('parallel run: the positions are exact; the '
                                'pointID values are global indices, not mesh '
                                'point labels')
            items.append(item)
            seen.add(name)
        for name, record in sorted(sets.items()):
            if name in seen:
                continue
            kind = _kind_of(name, record.get('class', ''))
            reason = (outputs[SURFACE]['reason'] if kind == SURFACE
                      and not outputs[SURFACE]['passed'] else
                      'checkMesh wrote this set as a label list only')
            items.append({'name': name, 'check': name, 'kind': kind,
                          'instance': instance, 'region': region,
                          'format': 'set', 'bytes': 0,
                          'source': record.get('path', ''),
                          'status': MISSING, 'reason': reason, 'path': ''})
            seen.add(name)
    if not instances:
        # No output directory was written to: the sets alone, if any.
        instance = 'constant'
        sets = _set_files(case, region, instance, started_at)
        for name, record in sorted(sets.items()):
            kind = _kind_of(name, record.get('class', ''))
            reason = (outputs[kind]['reason'] or
                      'checkMesh wrote this set as a label list only')
            items.append({'name': name, 'check': name, 'kind': kind,
                          'instance': instance, 'region': region,
                          'format': 'set', 'bytes': 0,
                          'source': record.get('path', ''),
                          'status': MISSING, 'reason': reason, 'path': ''})
    manifest = {
        'schema_version': 1,
        'check': check,
        'revision': revision,
        'mesh_fingerprint': mesh_fingerprint,
        'mesh_revision': mesh_revision,
        'checked_at': checked_at,
        'region': region,
        'instances': instances,
        'parallel': parallel,
        'command': [str(item) for item in argv or ()],
        'outputs': outputs,
        'items': items,
        'budgets': budgets(),
    }
    target.mkdir(parents=True, exist_ok=True)
    _write_json(target / 'manifest.json', manifest)
    _write_json(root / 'current.json', manifest)
    _prune(root, keep=keep, current=revision)
    return manifest


def budgets() -> dict:
    return {'max_artifact_bytes': MAX_ARTIFACT_BYTES,
            'max_request_bytes': MAX_REQUEST_BYTES,
            'max_highlight_points': MAX_HIGHLIGHT_POINTS,
            'max_highlight_polygons': MAX_HIGHLIGHT_POLYGONS,
            'max_request_points': MAX_REQUEST_POINTS}


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write('\n')
    os.replace(temporary, path)


def _prune(root: Path, *, keep: int, current: str) -> None:
    revisions = sorted(path for path in root.iterdir()
                       if path.is_dir() and (path / 'manifest.json').is_file())
    for path in revisions[:max(0, len(revisions) - keep)]:
        if path.name != current:
            shutil.rmtree(path, ignore_errors=True)


def load_manifest(case_path, *, check: str = 'openfoam',
                  revision: str | None = None) -> dict | None:
    """The stored manifest, with ``stale`` set when the mesh has changed."""
    root = artifact_root(case_path, check)
    path = (root / revision / 'manifest.json') if revision \
        else root / 'current.json'
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    stale = False
    recorded = str(manifest.get('mesh_fingerprint') or '')
    if recorded and recorded != 'unavailable':
        poly_mesh = Path(case_path) / 'constant'
        if manifest.get('region'):
            poly_mesh = poly_mesh / str(manifest['region'])
        try:
            from foammesh.core.case import fingerprint_poly_mesh

            current = fingerprint_poly_mesh(poly_mesh / 'polyMesh').digest
            stale = current != recorded
        except Exception:                                    # noqa: BLE001
            # No mesh to compare with is a mesh that has gone.
            stale = True
    manifest['stale'] = stale
    if stale:
        for item in manifest.get('items', ()):
            if item.get('status') == AVAILABLE:
                item['status'] = MISSING
                item['reason'] = ('the mesh has changed since this check; '
                                  'check it again')
    return manifest


# --------------------------------------------------------------------------- #
# Reading -- in the mesh worker
# --------------------------------------------------------------------------- #

class HighlightRefused(Exception):
    """One highlight could not be read; ``reason`` is a typed code."""

    def __init__(self, reason: str, message: str, **details):
        super().__init__(message)
        self.reason = reason
        self.details = details


_SECTION = re.compile(
    rb'^(POINTS|VERTICES|POLYGONS|LINES|TRIANGLE_STRIPS|POINT_DATA|'
    rb'CELL_DATA|FIELD|METADATA)\b[^\n]*$', re.MULTILINE)
_DTYPES = {b'float': 4, b'double': 8, b'int': 4, b'vtktypeint32': 4,
           b'vtktypeint64': 8, b'long': 8}


def read_legacy_vtk(path, *, max_points: int = MAX_HIGHLIGHT_POINTS,
                    max_polygons: int = MAX_HIGHLIGHT_POLYGONS) -> dict:
    """``{'points', 'polygons', 'vertices', 'kind'}`` of a legacy VTK POLYDATA.

    ``points`` is ``(n, 3)`` float64; ``polygons`` the VTK flat connectivity
    (``k i0 .. ik-1`` per polygon) as int64. The counts in each section
    header are checked against the budgets *before* anything is allocated.
    ASCII and BINARY (big-endian) are both read, as checkMesh writes both.
    """
    import numpy as np

    data = Path(path).read_bytes()
    lines = data.split(b'\n', 4)
    if len(lines) < 5 or not lines[0].startswith(b'# vtk DataFile'):
        raise HighlightRefused(REFUSED_UNREADABLE,
                               f'{Path(path).name} is not a legacy VTK file')
    binary = lines[2].strip().upper() == b'BINARY'
    if lines[3].split()[-1:] != [b'POLYDATA']:
        raise HighlightRefused(REFUSED_UNREADABLE,
                               f'{Path(path).name} is not VTK POLYDATA')
    offset = sum(len(line) + 1 for line in lines[:4])
    result = {'points': np.zeros((0, 3)), 'polygons': np.zeros(0, np.int64),
              'vertices': 0, 'binary': binary}
    position = offset
    while True:
        found = _SECTION.search(data, position)
        if found is None:
            break
        words = found.group(0).split()
        keyword = words[0]
        body = found.end() + 1
        if keyword in (b'POINT_DATA', b'CELL_DATA', b'FIELD', b'METADATA'):
            break
        if keyword == b'POINTS':
            count = int(words[1])
            if count > max_points:
                raise HighlightRefused(
                    REFUSED_GEOMETRY_BUDGET,
                    f'{count:,} points; a highlight holds at most '
                    f'{max_points:,}', points=count, limit=max_points)
            dtype = words[2].lower() if len(words) > 2 else b'float'
            values, position = _read_block(
                data, body, 3 * count, binary, dtype, float)
            result['points'] = values.reshape(count, 3).astype(np.float64)
            continue
        count, size = int(words[1]), int(words[2])
        if keyword == b'POLYGONS' and count > max_polygons:
            raise HighlightRefused(
                REFUSED_GEOMETRY_BUDGET,
                f'{count:,} faces; a highlight holds at most '
                f'{max_polygons:,}', polygons=count, limit=max_polygons)
        if size > 16 * max(max_points, max_polygons):
            raise HighlightRefused(
                REFUSED_GEOMETRY_BUDGET,
                f'{size:,} connectivity entries is over the highlight budget',
                entries=size)
        values, position = _read_block(data, body, size, binary, b'int', int)
        if keyword == b'POLYGONS':
            result['polygons'] = values.astype(np.int64)
            result['polygon_count'] = count
        elif keyword == b'VERTICES':
            result['vertices'] = count
    polygons = int(result.get('polygon_count', 0))
    result['polygon_count'] = polygons
    result['kind'] = SURFACE if polygons else POINTS
    return result


def _read_block(data: bytes, start: int, count: int, binary: bool,
                dtype: bytes, kind):
    import numpy as np

    if binary:
        width = _DTYPES.get(dtype, 4)
        if kind is float:
            numpy_type = '>f4' if width == 4 else '>f8'
        else:
            numpy_type = '>i4' if width == 4 else '>i8'
        end = start + count * width
        if end > len(data):
            raise HighlightRefused(REFUSED_UNREADABLE,
                                   'the file ends inside a data block')
        values = np.frombuffer(data, dtype=numpy_type, count=count,
                               offset=start)
        return values, end
    following = _SECTION.search(data, start)
    end = following.start() if following else len(data)
    # ``fromstring`` parses in C: ``str.split`` would hold every number as a
    # Python string, several times the file, in a worker capped in memory.
    import warnings

    text = data[start:end].decode('ascii', errors='replace')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            values = (np.fromstring(text, sep=' ', dtype=np.float64
                                    if kind is float else np.int64)
                      if count else np.zeros(0))
    except (ValueError, DeprecationWarning) as error:
        raise HighlightRefused(REFUSED_UNREADABLE,
                               f'a data block does not parse: {error}')
    if values.size < count:
        raise HighlightRefused(REFUSED_UNREADABLE,
                               f'expected {count} values, found {values.size}')
    return values[:count], end


def read_highlights(case_path, names, *, check: str = 'openfoam',
                    revision: str | None = None,
                    max_request_bytes: int = MAX_REQUEST_BYTES,
                    max_request_points: int = MAX_REQUEST_POINTS,
                    max_points: int = MAX_HIGHLIGHT_POINTS,
                    max_polygons: int = MAX_HIGHLIGHT_POLYGONS) -> dict:
    """The named highlights of one check revision, within the budgets.

    Returns ``{'revision', 'highlights', 'refused', 'values'}``: each
    highlight names its set, check, kind, instance and region and the keys
    of its arrays in ``values``; each refusal names the set, a typed
    ``reason`` and a sentence. Nothing is raised for a set that cannot be
    read: the rest are still drawn.
    """
    manifest = load_manifest(case_path, check=check, revision=revision)
    if manifest is None:
        return {'revision': revision or '', 'highlights': [], 'values': {},
                'refused': [{'name': str(name), 'reason': REFUSED_NO_MANIFEST,
                             'message': 'no checkMesh output has been kept '
                                        'for this case; run the check'}
                            for name in names or ()]}
    target = artifact_root(case_path, check) / manifest['revision']
    by_name = {item.get('name'): item for item in manifest.get('items', ())}
    highlights, refused, values = [], [], {}
    spent_bytes = spent_points = 0
    for name in names or ():
        name = str(name)
        item = by_name.get(name)
        if item is None or item.get('status') != AVAILABLE:
            refused.append({'name': name, 'reason': REFUSED_NOT_AVAILABLE,
                            'message': (item or {}).get('reason')
                            or f'checkMesh kept no drawable output named '
                               f'{name!r}'})
            continue
        size = int(item.get('bytes') or 0)
        if size > MAX_ARTIFACT_BYTES:
            refused.append({'name': name, 'reason': REFUSED_FILE_BUDGET,
                            'message': f'{size:,} bytes is over the '
                                       f'{MAX_ARTIFACT_BYTES:,}-byte file '
                                       'budget'})
            continue
        if spent_bytes + size > max_request_bytes:
            refused.append({'name': name,
                            'reason': REFUSED_REQUEST_FILE_BUDGET,
                            'message': 'the highlights already shown use the '
                                       f'{max_request_bytes:,}-byte budget; '
                                       'hide one to show this'})
            continue
        try:
            parsed = read_legacy_vtk(target / item['path'],
                                     max_points=max_points,
                                     max_polygons=max_polygons)
        except HighlightRefused as refusal:
            refused.append({'name': name, 'reason': refusal.reason,
                            'message': str(refusal),
                            'details': dict(refusal.details)})
            continue
        except (OSError, ValueError) as error:
            refused.append({'name': name, 'reason': REFUSED_UNREADABLE,
                            'message': str(error)})
            continue
        count = int(parsed['points'].shape[0])
        if spent_points + count > max_request_points:
            refused.append({'name': name,
                            'reason': REFUSED_REQUEST_GEOMETRY_BUDGET,
                            'message': f'{count:,} more points would pass '
                                       f'the {max_request_points:,}-point '
                                       'budget of the view; hide one to '
                                       'show this'})
            continue
        spent_bytes += size
        spent_points += count
        index = len(highlights)
        values[f'h{index}_points'] = parsed['points']
        if parsed['kind'] == SURFACE:
            values[f'h{index}_polygons'] = parsed['polygons']
        highlights.append({
            'name': name, 'check': item.get('check', name),
            'kind': parsed['kind'], 'declared_kind': item.get('kind'),
            'instance': item.get('instance', ''),
            'region': item.get('region', ''),
            'revision': manifest['revision'],
            'mesh_revision': manifest.get('mesh_revision'),
            'points_key': f'h{index}_points',
            'polygons_key': (f'h{index}_polygons'
                             if parsed['kind'] == SURFACE else ''),
            'point_count': count,
            'polygon_count': int(parsed.get('polygon_count', 0)),
            'note': item.get('note', ''),
        })
    return {'revision': manifest['revision'], 'stale': manifest.get('stale'),
            'highlights': highlights, 'refused': refused, 'values': values}


def run(args: dict) -> dict:
    """The mesh worker's entry (``quality.check_highlights``)."""
    parameters = dict(args.get('parameters') or {})
    names = parameters.get('names') or ()
    if isinstance(names, str):
        names = [names]
    return read_highlights(
        args['case_path'], list(names),
        check=str(parameters.get('check') or 'openfoam'),
        revision=parameters.get('revision') or None)


# --------------------------------------------------------------------------- #
# the rows a check page lists
# --------------------------------------------------------------------------- #

STALE_REASON = ('the mesh has changed since this check; run the check again '
                'to draw what it finds now')


def highlight_rows(manifest: dict | None) -> list[dict]:
    """One row per kept output: what it is, and why it cannot be drawn.

    ``[{name, kind, label, enabled, reason}]``. A row is enabled only when
    its file was kept for this revision and the mesh is still the one that
    was checked; every other row carries the reason in words.
    """
    if not manifest:
        return []
    stale = bool(manifest.get('stale'))
    rows = []
    for item in manifest.get('items') or ():
        kind = item.get('kind') or POINTS
        noun = 'points' if kind == POINTS else 'faces'
        where = [str(value) for value in (item.get('region'),
                                          item.get('instance'))
                 if value and value != 'constant']
        label = f'{item.get("name")} ({noun}' + (
            f', {" / ".join(where)})' if where else ')')
        available = item.get('status') == AVAILABLE
        reason = '' if available else str(item.get('reason') or
                                          'checkMesh wrote no drawable file')
        if stale:
            reason = STALE_REASON
        rows.append({'name': str(item.get('name')), 'kind': kind,
                     'label': label, 'enabled': available and not stale,
                     'reason': reason, 'note': str(item.get('note') or '')})
    return rows
