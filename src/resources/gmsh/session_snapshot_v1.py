"""The resident session's mesh snapshot (Plan 50 IP18, GM-4, DP-6323).

A crashed or cancelled session is rebuilt by starting a new Gmsh process and
running the whole op log again (``session.replay``), so every mesh the person
had generated was generated again, which for a large model is most of the
time the session had spent. After each successful generate the session now
writes a snapshot: the position in the op log, a digest of the log up to it,
the entity inventory and every entity's own nodes and elements. A replay that
finds a snapshot for its log, whose digest still matches, runs the log up to
that position without its mesh operations, checks the model against the
inventory, puts the saved mesh back, checks the counts, and runs only the
operations after it. Anything that does not match and the model is emptied
and the whole log runs as before; the reply says which happened and why.

The file is ``<distro temp>/foammesh-gmsh-snapshots/<log digest>.npz``: out
of the case and out of the scratch folder (removed when the process ends),
written to a temporary name and renamed. It is written entity by entity, so
the session never holds a second copy of the whole mesh.

The module must not import ``session_v1`` (it may be ``__main__``); the
session hands it the module it runs in.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import zipfile

import numpy as np

import gmsh_startup_v1
from session_geometry_v1 import _differences, _inventory, _mesh_counts

SNAPSHOT_SCHEMA = 1

#: Logged operations after which a snapshot is written.
SNAPSHOT_AFTER = ('mesh.generate', 'model.mesh.generate', 'session.regenerate')

#: Logged operations that change only the mesh. A restore leaves them out of
#: the part of the log it runs: the saved mesh is what they left.
MESH_ONLY = frozenset((
    'mesh.generate', 'model.mesh.generate', 'session.regenerate',
    'model.mesh.clear', 'model.mesh.refine', 'model.mesh.optimize',
    'model.mesh.setOrder', 'model.mesh.recombine',
    'model.mesh.renumberNodes', 'model.mesh.renumberElements',
    'model.mesh.removeDuplicateNodes', 'model.mesh.removeDuplicateElements',
    'model.mesh.smooth', 'session.renumber', 'session.regularize',
    'session.remove_elements', 'session.delete_entity_mesh',
    'session.cvt_apply'))

#: Operations that may follow a mesh operation in the restored part of the
#: log: they read no mesh, so leaving the mesh operations before them out
#: changes nothing they do. Anything else after a mesh operation (a script,
#: a reclassification, a plugin) might read the mesh, and the whole log is
#: run instead.
NEEDS_NO_MESH = ('option.', 'model.occ.', 'model.geo.', 'model.mesh.field.',
                 'model.mesh.set', 'model.mesh.embed', 'model.add',
                 'model.setCurrent', 'model.addPhysicalGroup',
                 'model.removePhysicalGroups', 'model.setPhysicalName',
                 'model.setEntityName', 'session.build', 'session.fields',
                 'session.point_sizes', 'session.size_field',
                 'session.size_field_off')

#: Snapshots older than this are removed when a new one is written.
KEEP_SECONDS = 7 * 24 * 3600
#: At most this many snapshots are kept (newest first).
KEEP_FILES = 8


def snapshot_path(log_path, root=None) -> str:
    """Where the snapshot of the session logging to *log_path* is kept."""
    digest = hashlib.sha1(os.path.abspath(str(log_path)).encode(
        'utf-8')).hexdigest()
    return os.path.join(gmsh_startup_v1.snapshot_folder(root),
                        digest + '.npz')


def log_digest(lines) -> str:
    """A digest of op-log lines (stripped, blank lines left out)."""
    digest = hashlib.sha1()
    for line in lines:
        digest.update(line.encode('utf-8'))
        digest.update(b'\n')
    return digest.hexdigest()


def read_log_lines(path) -> list:
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding='utf-8') as handle:
        return [line.strip() for line in handle if line.strip()]


def _put_array(archive, name, array) -> None:
    with archive.open(name + '.npy', 'w', force_zip64=True) as handle:
        np.lib.format.write_array(handle, np.asarray(array),
                                  allow_pickle=False)


def _prune(folder, keep) -> None:
    try:
        names = [os.path.join(folder, n) for n in os.listdir(folder)
                 if n.endswith('.npz')]
    except OSError:
        return
    now = time.time()
    dated = []
    for path in names:
        try:
            dated.append((os.path.getmtime(path), path))
        except OSError:
            continue
    dated.sort(reverse=True)
    for index, (when, path) in enumerate(dated):
        if path == keep:
            continue
        if index >= KEEP_FILES or now - when > KEEP_SECONDS:
            try:
                os.remove(path)
            except OSError:
                pass


def write_snapshot(raw, log_path, calls_path=None, *, rss_kb=0,
                   memory_cap_kb=0, root=None) -> dict:
    """Write the snapshot after a generate; returns what was done.

    Never raises: a snapshot that cannot be written leaves the next replay
    to run the whole log, as it always did.
    """
    if not log_path:
        return {'written': False, 'reason': 'the session keeps no op log'}
    try:
        if memory_cap_kb and rss_kb > memory_cap_kb * 0.5:
            return {'written': False,
                    'reason': 'the session is using more than half the '
                              'memory it may, so the mesh is not copied'}
        lines = read_log_lines(log_path)
        path = snapshot_path(log_path, root)
        folder = os.path.dirname(path)
        free = shutil.disk_usage(folder).free
        if rss_kb and free < rss_kb * 1024:
            return {'written': False,
                    'reason': f'{free} bytes free in {folder}, less than the '
                              'session holds'}
        calls = ''
        if calls_path and os.path.isfile(calls_path):
            with open(calls_path, encoding='utf-8') as handle:
                calls = handle.read()
        started = time.monotonic()
        entities = []
        temporary = path + '.writing'
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_STORED,
                             allowZip64=True) as archive:
            for index, (dim, tag) in enumerate(raw.model.getEntities()):
                nodes, coord, param = raw.model.mesh.getNodes(
                    dim, tag, includeBoundary=False,
                    returnParametricCoord=True)
                types, elements, element_nodes = raw.model.mesh.getElements(
                    dim, tag)
                _put_array(archive, f'{index}.n',
                           np.asarray(nodes, dtype=np.uint64))
                _put_array(archive, f'{index}.c',
                           np.asarray(coord, dtype=np.float64))
                _put_array(archive, f'{index}.p',
                           np.asarray(param, dtype=np.float64))
                for k, (tags, connectivity) in enumerate(
                        zip(elements, element_nodes)):
                    _put_array(archive, f'{index}.{k}.e',
                               np.asarray(tags, dtype=np.uint64))
                    _put_array(archive, f'{index}.{k}.c',
                               np.asarray(connectivity, dtype=np.uint64))
                entities.append([int(dim), int(tag),
                                 [int(t) for t in types]])
            meta = {'schema': SNAPSHOT_SCHEMA, 'ops': len(lines),
                    'digest': log_digest(lines), 'entities': entities,
                    'inventory': _inventory(raw),
                    'counts': _mesh_counts(raw), 'calls': calls}
            _put_array(archive, 'meta', np.frombuffer(
                json.dumps(meta).encode('utf-8'), dtype=np.uint8))
        os.replace(temporary, path)
        _prune(folder, path)
        return {'written': True, 'path': path, 'ops': len(lines),
                'seconds': round(time.monotonic() - started, 3)}
    except Exception as error:                           # noqa: BLE001
        try:
            os.remove(path + '.writing')
        except (OSError, UnboundLocalError):
            pass
        return {'written': False, 'reason': f'{type(error).__name__}: {error}'}


def _read_meta(archive) -> dict:
    return json.loads(bytes(archive['meta']).decode('utf-8'))


def usable_prefix(entries) -> str:
    """Why the log up to the snapshot cannot be run without its mesh
    operations, or ``''`` if it can."""
    meshed = False
    for entry in entries:
        op = str(entry.get('op') or '')
        if op in MESH_ONLY:
            meshed = True
            continue
        if meshed and not op.startswith(NEEDS_NO_MESH):
            return (f'{op} follows a mesh operation in the log and may read '
                    'the mesh')
    return ''


def _put_mesh(raw, archive, entities) -> None:
    raw.model.mesh.clear()
    for index, (dim, tag, _types) in enumerate(entities):
        nodes = archive[f'{index}.n']
        if len(nodes):
            raw.model.mesh.addNodes(dim, tag, nodes, archive[f'{index}.c'],
                                    archive[f'{index}.p'])
    for index, (dim, tag, types) in enumerate(entities):
        if not types:
            continue
        raw.model.mesh.addElements(
            dim, tag, list(types),
            [archive[f'{index}.{k}.e'] for k in range(len(types))],
            [archive[f'{index}.{k}.c'] for k in range(len(types))])


def restore(session, home, lines, log_path, root=None):
    """Bring the session to the snapshot; ``(ops done, report)``.

    *lines* are the op log's lines; *home* is the module the session runs
    in (``dispatch``, ``_calls_path``, ``_write_calls``, ``_truncate_calls``).
    ``ops done`` is ``0`` when the snapshot was not used, and the model is
    then empty, ready for the whole log.
    """
    path = snapshot_path(log_path, root) if log_path else ''
    if not path or not os.path.isfile(path):
        return 0, {'used': False, 'reason': 'no snapshot for this op log'}
    try:
        archive = np.load(path, allow_pickle=False)
    except Exception as error:                           # noqa: BLE001
        return 0, {'used': False,
                   'reason': f'the snapshot could not be read ({error})'}
    with archive:
        try:
            meta = _read_meta(archive)
        except Exception as error:                       # noqa: BLE001
            return 0, {'used': False,
                       'reason': f'the snapshot could not be read ({error})'}
        ops = int(meta.get('ops') or 0)
        if meta.get('schema') != SNAPSHOT_SCHEMA:
            return 0, {'used': False,
                       'reason': 'the snapshot was written by another build'}
        if ops > len(lines) or log_digest(lines[:ops]) != meta.get('digest'):
            return 0, {'used': False,
                       'reason': 'the op log has changed since the snapshot'}
        entries = [json.loads(line) for line in lines[:ops]]
        why = usable_prefix(entries)
        if why:
            return 0, {'used': False, 'reason': why}
        calls = home._calls_path
        problems = []
        try:
            home._calls_path = None
            for entry in entries:
                if entry.get('op') in MESH_ONLY:
                    continue
                home.dispatch(session, dict(entry, replay=True))
            run = getattr(session, '_stage_run', None)
            if run is not None and getattr(run, 'periodic_copies', None):
                problems.append('the model has periodic pairs, whose mesh '
                                'constraints the generate sets')
            if not problems:
                problems = _differences(meta['inventory'],
                                        _inventory(session.raw))
            if not problems:
                _put_mesh(session.raw, archive, meta['entities'])
                counts = _mesh_counts(session.raw)
                if counts != meta['counts']:
                    problems.append('mesh: %s saved, %s restored'
                                    % (meta['counts'], counts))
        except Exception as error:                       # noqa: BLE001
            problems.append(f'{type(error).__name__}: {error}')
        finally:
            home._calls_path = calls
    if problems:
        _empty(session, home)
        return 0, {'used': False,
                   'reason': 'the restored model did not match the snapshot ('
                             + '; '.join(problems) + ')'}
    home._truncate_calls()
    if meta.get('calls') and home._calls_path:
        with open(home._calls_path, 'a', encoding='utf-8') as handle:
            handle.write(meta['calls'])
    return ops, {'used': True, 'ops': ops,
                 'skipped': sum(1 for e in entries
                                if e.get('op') in MESH_ONLY)}


def _empty(session, home) -> None:
    """The model emptied, as a fresh process has it, for the whole log."""
    raw = session.raw
    try:
        raw.clear()
        raw.model.add('foammesh')
    except Exception:                                    # noqa: BLE001
        pass
    if hasattr(session, '_stage_run'):
        session._stage_run = None
        session._stage_applied = []
        session._stage_background = []
    if hasattr(session, 'model_unit'):
        session.model_unit = None
    home._truncate_calls()
