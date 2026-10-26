"""Session operations for the geometry area (Plan 48 seam, owner GM5/GM6).

A mixin of ``session_v1.Session``. ``dispatch()`` finds a public method by
name (``session.<name>``), so a method added here is reachable as the
operation ``session.<name>`` with no other change. The mixin must not import
``session_v1``: it is mixed into ``Session``, and uses ``self.gmsh`` (the
recording wrapper) and ``self.raw`` (the bare module) from it, and reaches
the module's op-log helpers through ``sys.modules`` (the module may be
``__main__``).

GM5 adds the checkpoints behind "Remove last script command": the op log
(``session.log``) is the history, a checkpoint is a position in it plus an
inventory of the model, and going back empties the model and runs the kept
part of the log again.  The result is checked against the inventory.
"""
from __future__ import annotations

import json
import os
import sys

#: ``session.*`` operations "Remove last script command" may take back: the
#: ones that put geometry in the model from a script.
_GEOMETRY_OPS = ('session.build', 'session.script', 'session.geo_lines')

#: name -> {'ops': number of log lines, 'inventory': ...} (one Gmsh process).
_CHECKPOINTS = {}


def _inventory(raw) -> dict:
    tags = {str(d): sorted(int(t) for _d, t in raw.model.getEntities(d))
            for d in range(4)}
    groups = sorted([int(d), int(t)] for d, t in raw.model.getPhysicalGroups())
    return {'counts': {d: len(v) for d, v in tags.items()}, 'tags': tags,
            'physical_groups': groups}


def _mesh_copy(raw) -> list:
    """Every entity's own nodes and elements, as Gmsh hands them out."""
    out = []
    for dim, tag in raw.model.getEntities():
        nodes, coord, param = raw.model.mesh.getNodes(
            dim, tag, includeBoundary=False, returnParametricCoord=True)
        types, elements, element_nodes = raw.model.mesh.getElements(dim, tag)
        out.append((dim, tag, nodes, coord, param, types, elements,
                    element_nodes))
    return out


def _mesh_put(raw, saved: list) -> None:
    """Replace the mesh by *saved* (the entities must be the saved ones)."""
    raw.model.mesh.clear()
    for dim, tag, nodes, coord, param, *_rest in saved:
        if len(nodes):
            raw.model.mesh.addNodes(dim, tag, nodes, coord, param)
    for dim, tag, *_nodes, types, elements, element_nodes in saved:
        if len(types):
            raw.model.mesh.addElements(dim, tag, types, elements,
                                       element_nodes)


def _mesh_counts(raw) -> dict:
    elements = {}
    for dim in range(4):
        elements[str(dim)] = sum(
            len(e) for _d, t in raw.model.getEntities(dim)
            for e in raw.model.mesh.getElements(dim, t)[1])
    return {'nodes': len(raw.model.mesh.getNodes()[0]),
            'elements': elements}


def _differences(before: dict, after: dict) -> list:
    out = []
    for dim in sorted(before['tags']):
        if before['tags'][dim] != after['tags'].get(dim):
            out.append('dimension %s: %d entities before, %d after'
                       % (dim, len(before['tags'][dim]),
                          len(after['tags'].get(dim) or [])))
    if before['physical_groups'] != after['physical_groups']:
        out.append('physical groups differ')
    return out


class SessionGeometryMixin:
    """Operations of the geometry area (GM5 checkpoints, GM6 structure)."""

    __slots__ = ()

    def _home(self):
        return sys.modules[type(self).__module__]

    def _log_entries(self) -> list:
        path = self._home()._log_path
        if not path or not os.path.isfile(path):
            return []
        with open(path, encoding='utf-8') as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def _write_log(self, entries: list) -> None:
        home = self._home()
        path = home._log_path
        if not path:
            return
        temp = path + '.tmp'
        with open(temp, 'w', encoding='utf-8', newline='\n') as handle:
            for entry in entries:
                handle.write(json.dumps(entry, separators=(',', ':'),
                                        default=home._json_default) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)

    def _rebuild(self, entries: list) -> None:
        """Empty the model and run *entries* again (they are not re-logged)."""
        home = self._home()
        self.raw.clear()
        self.raw.model.add('foammesh')
        home._truncate_calls()
        for entry in entries:
            home.dispatch(self, dict(entry, replay=True))

    def inventory(self, **_):
        """Every entity tag by dimension (what a checkpoint is checked by)."""
        return _inventory(self.raw)

    def script_position(self, **_):
        """How many commands the op log and the script hold, and the last."""
        entries = self._log_entries()
        calls = self._home()._calls_path
        count = 0
        if calls and os.path.isfile(calls):
            with open(calls, encoding='utf-8') as handle:
                count = sum(1 for line in handle if line.strip())
        last = None
        removable = 0
        for entry in entries:
            if entry.get('op') in _GEOMETRY_OPS and 'again' not in entry:
                removable += 1
                last = entry
        return {'ops': len(entries), 'calls': count, 'removable': removable,
                'last': None if last is None else {
                    'op': last['op'],
                    'kind': (last.get('args') or [None])[0]}}

    def script_trim(self, count=1, **_):
        """Remove the last *count* geometry commands and rebuild the model.

        The model is emptied and the rest of the op log is run again; the
        result lists what was removed and the entity counts before and after.
        If the rebuild fails the model is rebuilt from the whole log again
        and the error is raised, so a refused trim leaves the model as it was.
        """
        count = int(count)
        if count < 1:
            raise ValueError('remove at least one command')
        entries = self._log_entries()
        drop = [i for i, e in enumerate(entries)
                if e.get('op') in _GEOMETRY_OPS and 'again' not in e][-count:]
        if len(drop) < count:
            raise ValueError('there are only %d geometry commands to remove'
                             % len(drop))
        before = _inventory(self.raw)
        gone = set(drop)
        # DP-5738: a build step removed takes with it the copies a re-import
        # made again (marked with the step's place among the builds)
        builds = [i for i, e in enumerate(entries)
                  if e.get('op') == 'session.build' and 'again' not in e]
        places = {builds.index(i) for i in drop if i in builds}
        gone.update(i for i, e in enumerate(entries)
                    if e.get('op') == 'session.build'
                    and e.get('again') in places)
        kept = [e for i, e in enumerate(entries) if i not in gone]
        removed = [{'op': entries[i]['op'],
                    'kind': (entries[i].get('args') or [None])[0]}
                   for i in drop]
        try:
            self._rebuild(kept)
        except BaseException:
            self._rebuild(entries)
            raise
        self._write_log(kept)
        after = _inventory(self.raw)
        reply = {'removed': removed, 'ops': len(kept),
                 'before': before['counts'], 'after': after['counts'],
                 'entities': after['counts']}
        # DP-5716: the rebuild followed the kept builds again; what they
        # replaced comes back with the reply.
        run = getattr(self, '_stage_run', None)
        statistics = getattr(run, 'statistics', None) or {}
        if statistics.get('geometryBuild'):
            reply['followed'] = dict(statistics['geometryBuild'])
        return reply

    def checkpoint_save(self, name, mesh=False, **_):
        """Remember the model now (a position in the op log + an inventory).

        DP-5741: with *mesh* the mesh itself is kept too and the restore puts
        it back. Replaying the log meshes again, and that mesh is not the one
        a remesh was undone to (the walk measured 4,752 triangles before, then
        4,754 and 4,750 after two Undos).
        """
        name = str(name or '').strip()
        if not name:
            raise ValueError('a checkpoint needs a name')
        entries = self._log_entries()
        _CHECKPOINTS[name] = {'ops': len(entries),
                              'inventory': _inventory(self.raw)}
        if mesh:
            _CHECKPOINTS[name]['mesh'] = _mesh_copy(self.raw)
            _CHECKPOINTS[name]['mesh_counts'] = _mesh_counts(self.raw)
        return {'name': name, 'ops': len(entries),
                'counts': _CHECKPOINTS[name]['inventory']['counts'],
                'names': sorted(_CHECKPOINTS)}

    def checkpoint_restore(self, name, **_):
        """Go back to a saved checkpoint and verify the model matches it."""
        if name not in _CHECKPOINTS:
            raise ValueError('there is no checkpoint named %r' % (name,))
        saved = _CHECKPOINTS[name]
        entries = self._log_entries()
        if saved['ops'] > len(entries):
            raise ValueError('checkpoint %r is newer than the op log' % name)
        kept = entries[:saved['ops']]
        try:
            self._rebuild(kept)
        except BaseException:
            self._rebuild(entries)
            raise
        self._write_log(kept)
        after = _inventory(self.raw)
        problems = _differences(saved['inventory'], after)
        if 'mesh' in saved and not problems:
            _mesh_put(self.raw, saved['mesh'])
            counts = _mesh_counts(self.raw)
            if counts != saved['mesh_counts']:
                problems.append('mesh: %s before, %s after'
                                % (saved['mesh_counts'], counts))
        for other in [n for n, c in _CHECKPOINTS.items()
                      if c['ops'] > saved['ops']]:
            del _CHECKPOINTS[other]
        return {'name': name, 'ops': len(kept), 'verified': not problems,
                'differences': problems, 'counts': after['counts'],
                'entities': after['counts']}

    def checkpoint_list(self, **_):
        """The saved checkpoints, oldest first."""
        return {'checkpoints': [
            {'name': n, 'ops': c['ops'], 'counts': c['inventory']['counts']}
            for n, c in sorted(_CHECKPOINTS.items(),
                               key=lambda i: i[1]['ops'])]}

    def restart_model(self, **_):
        """Start the model again (a kernel change): the log is emptied, and
        the stage run of the import that went with it is dropped (DP-5730)."""
        self._rebuild([])
        self._write_log([])
        _CHECKPOINTS.clear()
        if hasattr(self, '_stage_run'):
            self._stage_run = None
            self._stage_applied = []
            self._stage_background = []
        return {'ops': 0, 'entities': _inventory(self.raw)['counts']}

    # -- Plan 48 GM6: sweeps and structured meshing ------------------------- #
    def _runtime(self, name):
        import importlib
        try:
            return importlib.import_module(name)
        except ImportError:                              # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            return importlib.import_module(name)

    def sweep_preview(self, kind, **params):
        """Where the layers of a sweep fall (no model change)."""
        builder = self._runtime('geometry_build_v1')
        try:
            return builder.sweep_preview(self.raw, kind, params)
        except builder.BuildError as error:
            raise ValueError(str(error)) from error

    def structure_precheck(self, dim, tags, corners=(), **_):
        """Can each of *tags* be transfinite, and if not, why not.

        A read. "Make structured" and "Hex-dominant" change the model, so the
        host runs ``structure_v1.make_structured`` / ``hex_dominant`` over the
        session's generic Gmsh calls (which the op log records and replays).
        """
        runtime = self._runtime('structure_v1')
        return {'entities': [runtime.precheck(self.raw, dim, int(t), corners)
                             for t in tags]}
