"""Geometry construction before the engine is chosen (Plan 42 SG21, D6).

The user combines the solids they imported, makes primitives and moves things
on the Gmsh OpenCASCADE kernel, and the result is written as STEP and brought
in through the one shared import.  Both engines then see an ordinary imported
geometry, so snappyHexMesh meshes a constructed part too.

Nothing here imports Qt.  The work runs on a session (the Gmsh process in WSL)
handed in by the caller; the operations are those of Plan 41 GN3
(:mod:`foammesh.core.gmsh.geometry_build`), so the numbers, the expressions and
the refusals are the same ones the Gmsh route has.

Rules measured on 2026-10-10 (Gmsh 4.15.2, ``elbow.step``):

* ``Geometry.OCCTargetUnit = M`` makes the model metres, which is the unit the
  case works in; the STEP written afterwards declares ``MILLI METRE`` and holds
  the coordinates converted, so the shared import reads the right size back.
* the STEP writer does not keep face names (every ``ADVANCED_FACE`` is
  unnamed), so a constructed part is imported with the default names.  Names
  typed on a source geometry therefore do not survive; :func:`names_at_risk`
  says which sources carry typed names so the page can warn before building.
* a mesh (STL, OBJ) is a discrete model, which OpenCASCADE cannot combine;
  :class:`ConstructRefusal` says so.
"""
from __future__ import annotations

from pathlib import Path

from foammesh.core.gmsh import geometry_build as gb

#: Files the OpenCASCADE kernel can read as solids.
SOLID_SUFFIXES = ('.step', '.stp', '.iges', '.igs', '.brep')

#: The operations offered, by kind.  Each is a Plan 41 GN3 operation id.
BOOLEANS = ('fuse', 'cut', 'intersect', 'fragment')
PRIMITIVES = ('box', 'sphere', 'cylinder', 'cone', 'torus', 'wedge')
TRANSFORMS = ('translate', 'rotate', 'scale', 'mirror')

#: Phases of the package, for tests and for the page.
PHASES = {'a': BOOLEANS, 'b': PRIMITIVES, 'c': TRANSFORMS}


class ConstructRefusal(ValueError):
    """The construction cannot be done; the message says what to change."""


def is_solid_source(path) -> bool:
    return Path(str(path)).suffix.lower() in SOLID_SUFFIXES


def names_at_risk(entries) -> list:
    """Names of source entries whose surface names were typed by the user.

    *entries* are artifact-store entries.  An entry whose patches carry a name
    that is not the importer's own default is at risk: the constructed part is
    written as STEP, which holds no face names.
    """
    risky = []
    for entry in entries:
        for patch in entry.get('patches') or ():
            name = str(patch.get('name') or '')
            default = str(patch.get('default_name') or
                          patch.get('original_name') or name)
            if name and name != default:
                risky.append(entry.get('name') or entry.get('geometry_id'))
                break
    return risky


def operation_phase(op_id: str) -> str:
    for phase, ops in PHASES.items():
        if op_id in ops:
            return phase
    raise ConstructRefusal('%r is not a construction operation' % op_id)


def operation_fields(op_id: str) -> tuple:
    """The number and yes/no fields of one operation (entity fields are the
    operands the page lets the user tick)."""
    operation_phase(op_id)
    return tuple(f for f in gb.operation(op_id).fields
                 if f.kind != gb.ENTITIES
                 and f.name not in ('remove_object', 'remove_tool', 'copy'))


class Construction:
    """One construction: labelled solids on a session, the steps, the STEP."""

    def __init__(self, session_provider, parameters=None):
        self._provider = session_provider
        self._session = None
        self.parameters = parameters or gb.Parameters()
        self._labels: dict = {}          # label -> tuple of (3, tag)
        self.steps: list = []            # (title, label, ...)
        self._counter = 0

    # -- the session --------------------------------------------------------- #

    def _live(self):
        if self._session is None:
            session = self._provider()
            session.call('clear')
            session.call('model.add', 'construct')
            # metres, the unit the case works in (measured, see the header)
            session.call('option.setString', 'Geometry.OCCTargetUnit', 'M')
            self._session = session
        return self._session

    def labels(self) -> dict:
        return {k: tuple(v) for k, v in self._labels.items()}

    def _volumes(self, session) -> set:
        return {(3, int(t)) for _d, t in
                (tuple(x) for x in session.call('model.getEntities', 3))}

    def _new_label(self, base: str) -> str:
        label = base
        while label in self._labels:
            self._counter += 1
            label = '%s %d' % (base, self._counter + 1)
        return label

    # -- operands ------------------------------------------------------------ #

    def add_source(self, label: str, path) -> str:
        """Bring a solid model (STEP, IGES, BREP) in under *label*."""
        if not is_solid_source(path):
            raise ConstructRefusal(
                '%s is a mesh, which cannot be combined; construction works '
                'on STEP, IGES or BREP solids and on primitives'
                % Path(str(path)).name)
        session = self._live()
        before = self._volumes(session)
        try:
            session.import_geometry(path)
        except Exception as error:                         # noqa: BLE001
            raise ConstructRefusal('could not read %s: %s' % (
                Path(str(path)).name, gb._clean(error))) from error
        made = sorted(self._volumes(session) - before)
        if not made:
            raise ConstructRefusal(
                '%s holds no solid, only surfaces; booleans need solids'
                % Path(str(path)).name)
        label = self._new_label(label)
        self._labels[label] = tuple(made)
        self.steps.append(('Import', label))
        return label

    def _targets(self, names) -> list:
        out = []
        for name in names:
            if name not in self._labels:
                raise ConstructRefusal('%r is not a shape of this construction'
                                       % name)
            out.extend(self._labels[name])
        return out

    # -- the operations ------------------------------------------------------ #

    def _call(self, op_id: str, values: dict):
        try:
            kind, kwargs = gb.plan(op_id, values, self.parameters)
        except gb.BuildRefusal as error:
            raise ConstructRefusal(str(error)) from error
        session = self._live()
        try:
            return session.call('session.build', kind, **kwargs)
        except Exception as error:                         # noqa: BLE001
            raise ConstructRefusal(gb._clean(error)) from error

    def primitive(self, op_id: str, values: dict, label: str | None = None
                  ) -> str:
        if op_id not in PRIMITIVES:
            raise ConstructRefusal('%r is not a primitive' % op_id)
        result = self._call(op_id, dict(values))
        made = [tuple(c) for c in result.get('created', ()) if c[0] == 3]
        if not made:
            raise ConstructRefusal('the %s made no solid' % op_id)
        label = self._new_label(label or gb.operation(op_id).title)
        self._labels[label] = tuple(made)
        self.steps.append((gb.operation(op_id).title, label))
        return label

    def transform(self, op_id: str, names, values: dict, copy: bool = False
                  ) -> list:
        """Move, turn, scale or mirror the named shapes.

        With *copy* the original stays and the copy becomes a new shape named
        after it; otherwise the shapes are changed where they stand.
        """
        if op_id not in TRANSFORMS:
            raise ConstructRefusal('%r is not a transform' % op_id)
        names = list(names)
        if not names:
            raise ConstructRefusal('tick the shapes to %s' % op_id)
        out = []
        for name in names:
            sent = dict(values)
            sent['entities'] = list(self._targets([name]))
            sent['copy'] = bool(copy)
            result = self._call(op_id, sent)
            if copy:
                made = [tuple(c) for c in result.get('created', ())
                        if c[0] == 3]
                new = self._new_label('%s (%s copy)' % (name, op_id))
                self._labels[new] = tuple(made)
                out.append(new)
            else:
                out.append(name)
            self.steps.append((gb.operation(op_id).title, out[-1]))
        return out

    def boolean(self, op_id: str, objects, tools, *, keep_tools: bool = False,
                label: str | None = None) -> str:
        if op_id not in BOOLEANS:
            raise ConstructRefusal('%r is not a boolean' % op_id)
        objects, tools = list(objects), list(tools)
        if not objects or not tools:
            raise ConstructRefusal(
                'tick at least one shape as an object and one as a tool')
        clash = set(objects) & set(tools)
        if clash:
            raise ConstructRefusal('%s is both object and tool'
                                   % ', '.join(sorted(clash)))
        values = {'objects': self._targets(objects),
                  'tools': self._targets(tools),
                  'remove_object': True, 'remove_tool': not keep_tools}
        result = self._call(op_id, values)
        made = [tuple(c) for c in result.get('created', ()) if c[0] == 3]
        if not made:
            raise ConstructRefusal('the %s left no solid' % op_id)
        for name in objects:
            self._labels.pop(name, None)
        if not keep_tools:
            for name in tools:
                self._labels.pop(name, None)
        label = self._new_label(label or gb.operation(op_id).title)
        self._labels[label] = tuple(made)
        self.steps.append((gb.operation(op_id).title, label))
        return label

    def remove(self, names) -> None:
        targets = self._targets(list(names))
        self._call('delete', {'entities': targets, 'recursive': True})
        for name in names:
            self._labels.pop(name, None)

    # -- the result ---------------------------------------------------------- #

    def export(self, path) -> dict:
        """Write what is left as STEP.  Refuses an empty construction."""
        if not self._labels:
            raise ConstructRefusal('there is nothing left to export')
        session = self._live()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        session.export_file(path, kind='geometry')
        if not path.is_file() or path.stat().st_size == 0:
            raise ConstructRefusal('Gmsh wrote no STEP file')
        return {'path': str(path), 'shapes': sorted(self._labels)}

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:                              # noqa: BLE001
                pass


def default_session(work_dir):
    """Start the Gmsh session a construction works on (WSL, as everywhere).

    Its own process, in *work_dir*: the meshing session of the Gmsh route is
    not touched, and nothing about the case's meshing engine is read.
    """
    from foammesh.core.gmsh.launch_profiles import configured_profiles
    from foammesh.core.gmsh.session import GmshSession

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    session = GmshSession(configured_profiles({})[0], work_dir,
                          start_timeout=180)
    session.start()
    return session
