"""The resident Gmsh session (Plan 41 GN1).

One process per open project, started as ``python3 -u session_v1.py`` and
spoken to in JSON lines.  Standard input carries requests, standard output
carries responses and events; Gmsh's own console output is captured from the
file descriptors and forwarded as ``log`` events while an operation runs.

Request   ``{"id": 7, "op": "model.occ.addBox", "args": [0,0,0,1,1,1],
              "kwargs": {}, "timeout": 600}``
Response  ``{"id": 7, "ok": true, "result": 1, "seconds": 0.002}``
          ``{"id": 7, "ok": false, "error": "...", "log": [...]}``
Event     ``{"event": "ready", ...}``, ``{"event": "log", "lines": [...]}``

An ``op`` is either a session operation (``session.*``, ``mesh.generate``) or
a dotted path under the ``gmsh`` module (``model.occ.addBox``,
``option.setNumber``, ``model.mesh.setOrder``).  Every operation that changes
the model is appended to the op log once it succeeds; replaying the log on a
fresh session rebuilds the model after a crash or a reopen.  A request marked
``"replay": true`` is not logged again.

Out of band: ``{"op": "session.abort"}`` is acted on at once by the reader
thread (the process exits 75) because the main thread may be inside a long
``generate``; the client then replays the log.  ``timeout`` and ``--memory-mb``
are per-operation caps enforced the same way.

The hand-over writes ``mesh.vtu`` and ``geometry.vtp`` (VTK XML, appended raw
data) with cell arrays ``gmshElementTag``, ``gmshElementType``,
``gmshEntityDim``, ``gmshEntityTag``, ``gmshPhysical``, ``gmshPartition`` and,
on request, the quality arrays of ``QUALITY_ARRAYS`` (every measure
``getElementQualities`` accepts on 4.15.2: ``gmshSICN``, ``gmshSIGE``,
``gmshGamma``, ``gmshMinIsotropy``, ``gmshMinEdge``, ``gmshMaxEdge``,
``gmshVolume``, ``gmshInnerRadius``, ``gmshOuterRadius``, ``gmshMinDetJac``,
``gmshMaxDetJac``).  ``surfaces`` adds the boundary triangles and quadrangles
to a volume hand-over and ``mesh.names.json`` beside ``mesh.vtu`` names the
physical groups, so a viewport draws patches without any other file.
"""
from __future__ import annotations

import json
import os
import queue
import struct
import sys
import threading
import time
import traceback

import numpy as np

# Plan 48 seam: one mixin per area, each in its own file next to this one.
# Running this file as a script already puts its directory on the path;
# loading it as a module by file does not.
try:
    import session_options_v1  # noqa: F401
except ImportError:  # pragma: no cover - exercised only by the module loader
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from session_options_v1 import SessionOptionsMixin
from session_readers_v1 import SessionReadersMixin
from session_geometry_v1 import SessionGeometryMixin
from session_mesh_v1 import SessionMeshMixin
from session_plugins_v1 import SessionPluginsMixin
from session_export_v1 import SessionExportMixin
from session_fields_v1 import SessionFieldsMixin
from session_cvt_v1 import SessionCvtMixin
# Plan 48 RR (DP-4009): the stage functions the batch runner also plays.
from stages.session import SessionStageOperations

PROTOCOL = 1
EXIT_ABORTED = 75
EXIT_TIMEOUT = 76
EXIT_MEMORY = 77

#: Plan 41 GN13. Hand-over array -> the name ``getElementQualities`` takes.
QUALITY_ARRAYS = (
    ('gmshSICN', 'minSICN'), ('gmshSIGE', 'minSIGE'), ('gmshGamma', 'gamma'),
    ('gmshMinIsotropy', 'minIsotropy'), ('gmshMinEdge', 'minEdge'),
    ('gmshMaxEdge', 'maxEdge'), ('gmshVolume', 'volume'),
    ('gmshInnerRadius', 'innerRadius'), ('gmshOuterRadius', 'outerRadius'),
    ('gmshMinDetJac', 'minDetJac'), ('gmshMaxDetJac', 'maxDetJac'))

# Gmsh element type -> (VTK cell type, node count, node order Gmsh -> VTK).
# The order may list fewer nodes than the element has: the corners of a
# high-order solid, drawn straight (Plan 41 GN17, O-08).
_VTK_CELLS = {
    1: (3, 2, None),
    2: (5, 3, None),
    3: (9, 4, None),
    4: (10, 4, None),
    5: (12, 8, None),
    6: (13, 6, None),
    7: (14, 5, None),
    15: (1, 1, None),
    140: (10, 4, None),          # trihedron (MSH_TRIH_4), a 4-node carrier
    # High-order curves and surfaces keep every node, in VTK's Lagrange (and
    # quadratic quad) cells, whose node order is Gmsh's: corners, edge nodes
    # along each edge, interior. The viewer cuts each edge into
    # ``Mesh.NumSubEdges`` pieces (O-08).
    8: (68, 3, None),            # 3-node line
    26: (68, 4, None),           # 4-node line
    27: (68, 5, None),           # 5-node line
    9: (69, 6, None),            # 6-node triangle
    21: (69, 10, None),          # 10-node triangle
    10: (28, 9, None),           # 9-node quadrangle (VTK biquadratic quad)
    16: (23, 8, None),           # 8-node quadrangle (VTK quadratic quad)
    # High-order solids: the corner nodes only.
    11: (10, 10, [0, 1, 2, 3]),                        # tetrahedron 10
    12: (12, 27, [0, 1, 2, 3, 4, 5, 6, 7]),            # hexahedron 27
    17: (12, 20, [0, 1, 2, 3, 4, 5, 6, 7]),            # hexahedron 20
    13: (13, 18, [0, 1, 2, 3, 4, 5]),                  # prism 18
    18: (13, 15, [0, 1, 2, 3, 4, 5]),                  # prism 15
    14: (14, 14, [0, 1, 2, 3, 4]),                     # pyramid 14
    19: (14, 13, [0, 1, 2, 3, 4]),                     # pyramid 13
}

_READ_PREFIXES = ('get', 'is', 'has', 'list', 'count', 'find', 'probe',
                  'compute', 'check', 'measure')
#: ``session.*`` operations that change the model (everything else under
#: ``session.`` only reads).
_MUTATING_SESSION = ('session.import', 'session.script',
                     'session.geo_lines', 'session.onelab_run',
                     'session.regenerate', 'session.renumber',
                     'session.coherence', 'session.build',
                     'session.fields', 'session.point_sizes',
                     'session.size_field', 'session.size_field_off',
                     'session.open', 'session.reclassify',
                     'session.create_topology', 'session.create_geometry',
                     'session.remesh_surfaces', 'session.plugin_edit',
                     # Plan 48 GM7: the mesh edits, logged and replayed.
                     # (reclassify_edges, list_mesh_elements and
                     # get_mesh_order are reads.)
                     'session.regularize', 'session.remove_elements',
                     'session.delete_entity_mesh',
                     'session.reclassify_subset', 'session.quad_layout',
                     # Plan 48 GM16b: the CVT apply, logged and replayed
                     # (cvt_run only writes files and stays out).
                     'session.cvt_apply',
                     # Plan 48 RR (DP-4009): the shared stages, logged and
                     # replayed (stage_report only reads).
                     'session.stage_import', 'session.stage_apply')
_ROOTS = ('model', 'option', 'merge', 'write', 'clear', 'logger', 'plugin',
          'onelab', 'view')

_proto = None
_proto_lock = threading.Lock()
_log_lines: list[str] = []
_log_cond = threading.Condition()
_op_deadline = {'when': None, 'op': None}
_memory_cap_kb = 0
_log_path = None
_calls_path = None


def emit(message: dict) -> None:
    text = json.dumps(message, separators=(',', ':'), default=_json_default)
    with _proto_lock:
        _proto.write(text + '\n')
        _proto.flush()


def _json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


# -- console capture ------------------------------------------------------- #

def _capture_console() -> None:
    """Send fd 1 and fd 2 (Gmsh's console) into a pipe a thread forwards."""
    read_fd, write_fd = os.pipe()
    os.dup2(write_fd, 1)
    os.dup2(write_fd, 2)
    os.close(write_fd)

    def pump():
        buffer = b''
        with os.fdopen(read_fd, 'rb', buffering=0) as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                buffer += chunk
                *lines, buffer = buffer.split(b'\n')
                if not lines:
                    continue
                text = [line.decode('utf-8', 'replace').rstrip('\r')
                        for line in lines]
                with _log_cond:
                    _log_lines.extend(text)
                    _log_cond.notify_all()
                emit({'event': 'log', 'lines': text})

    threading.Thread(target=pump, name='console', daemon=True).start()


def _drain_console(gmsh) -> list[str]:
    """Wait until the console has shown everything written so far."""
    marker = 'FM_SYNC %d' % time.monotonic_ns()
    try:
        gmsh.logger.write(marker, 'info')
    except Exception:
        return []
    deadline = time.monotonic() + 1.0
    with _log_cond:
        while time.monotonic() < deadline:
            if any(marker in line for line in _log_lines[-50:]):
                break
            _log_cond.wait(0.05)
        lines = [line for line in _log_lines if marker not in line]
        _log_lines.clear()
    return lines


# -- watchers --------------------------------------------------------------- #

def _rss_kb() -> int:
    try:
        with open('/proc/self/status', encoding='ascii') as handle:
            for line in handle:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _watchdog() -> None:
    while True:
        time.sleep(0.5)
        when = _op_deadline['when']
        if when is not None and time.monotonic() > when:
            emit({'event': 'fatal', 'reason': 'timeout',
                  'op': _op_deadline['op']})
            os._exit(EXIT_TIMEOUT)
        if _memory_cap_kb and _rss_kb() > _memory_cap_kb:
            emit({'event': 'fatal', 'reason': 'memory',
                  'op': _op_deadline['op'], 'rss_kb': _rss_kb()})
            os._exit(EXIT_MEMORY)


def _stdin_reader(requests: 'queue.Queue') -> None:
    """Read requests; act on abort at once; EOF means the client is gone."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            requests.put({'id': None, 'op': 'session.bad', 'raw': line[:200]})
            continue
        if request.get('op') == 'session.abort':
            emit({'event': 'fatal', 'reason': 'abort'})
            os._exit(EXIT_ABORTED)
        requests.put(request)
    os._exit(0)


# -- script recorder (Plan 41 GN19) ----------------------------------------- #
#
# Every call that changes the model goes through ``_Recorder`` and is written
# to ``script.calls.jsonl`` beside the op log, as the literal Gmsh API call.
# The op log says what the user asked (``session.build``, ``session.fields``);
# this file says what Gmsh was told, which is what a script in bare Gmsh needs.
# ``core.gmsh.script_export`` turns it into Python and ``.geo``.

import types as _types

_RAW_SKIP_ROOTS = ('view', 'plugin', 'write', 'logger')
#: A single call whose arguments are larger than this is written as a marker:
#: the script it belongs to cannot be rebuilt from the file alone.
_RAW_LIMIT = 2_000_000


def _calls_for(path):
    if not path:
        return None
    return os.path.join(os.path.dirname(path) or '.', 'script.calls.jsonl')


def _truncate_calls() -> None:
    if _calls_path:
        os.makedirs(os.path.dirname(_calls_path) or '.', exist_ok=True)
        with open(_calls_path, 'w', encoding='utf-8'):
            pass


def _raw_is_recorded(path: tuple) -> bool:
    if path[0] in _RAW_SKIP_ROOTS or path[0] in ('initialize', 'finalize'):
        return False
    last = path[-1]
    if path[0] == 'onelab':
        return last.startswith('set') or last == 'clear'
    return not last.startswith(_READ_PREFIXES)


def _record_raw(path: tuple, args, kwargs, result=None) -> None:
    if not _calls_path or not _raw_is_recorded(path):
        return
    entry = {'f': '.'.join(path), 'a': list(args), 'k': dict(kwargs)}
    if (isinstance(result, (int, float, str, list, tuple))
            and len(str(result)) < 10_000):
        entry['r'] = result
    text = json.dumps(entry, separators=(',', ':'), default=_json_default)
    if len(text) > _RAW_LIMIT:
        text = json.dumps({'f': '.'.join(path), 'omitted': len(text)})
    _write_calls(text)


def _write_calls(text: str) -> None:
    if not _calls_path:
        return
    with open(_calls_path, 'a', encoding='utf-8') as handle:
        handle.write(text + '\n')



def _gmsh_last_error(gmsh) -> str:
    """The last error Gmsh logged, '' when it keeps none."""
    try:
        return str(gmsh.logger.getLastError() or '')
    except Exception:                                    # noqa: BLE001
        return ''

class _Recorder:
    """The Gmsh module, with every model-changing call written down."""

    def __init__(self, target, path=()):
        object.__setattr__(self, '_t', target)
        object.__setattr__(self, '_p', path)

    def __getattr__(self, name):
        attr = getattr(self._t, name)
        if name.startswith('_'):
            return attr
        path = self._p + (name,)
        if isinstance(attr, (type, _types.ModuleType)):
            return _Recorder(attr, path)
        if callable(attr):
            if not isinstance(attr, (_types.FunctionType,
                                     _types.BuiltinFunctionType,
                                     _types.MethodType)):
                # A callable object that also carries attributes (a
                # namespace stood in by a test double): keep both halves.
                return _Recorder(attr, path)
            def recorded(*args, **kwargs):
                result = attr(*args, **kwargs)
                _record_raw(path, args, kwargs, result)
                return result
            recorded.__name__ = name
            return recorded
        return attr

    def __call__(self, *args, **kwargs):
        result = self._t(*args, **kwargs)
        _record_raw(self._p, args, kwargs, result)
        return result

    def __setattr__(self, name, value):
        setattr(self._t, name, value)


# -- op log ----------------------------------------------------------------- #

def _is_read(op: str) -> bool:
    if op.startswith('session.'):
        return op not in _MUTATING_SESSION
    last = op.rsplit('.', 1)[-1]
    if op.split('.')[0] in ('write', 'logger', 'onelab'):
        return True
    return last.startswith(_READ_PREFIXES) or last in (
        'getEntities',) or op in ('mesh.generate_dry',)


def _append_log(request: dict) -> None:
    if not _log_path:
        return
    entry = {'op': request['op'], 'args': request.get('args') or [],
             'kwargs': request.get('kwargs') or {}}
    if request.get('again') is not None:
        # DP-5738: a step the controller made again after a re-import; a
        # rebuild from the log runs it, the build list skips it
        entry['again'] = request['again']
    os.makedirs(os.path.dirname(_log_path) or '.', exist_ok=True)
    with open(_log_path, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, separators=(',', ':'),
                                default=_json_default) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


# -- VTK XML writer -------------------------------------------------------- #

def _vtk_type(array: np.ndarray) -> str:
    return {'int32': 'Int32', 'int64': 'Int64', 'uint8': 'UInt8',
            'float64': 'Float64', 'float32': 'Float32'}[array.dtype.name]


def _write_vtk(path: str, kind: str, *, points: np.ndarray, point_data=None,
               cell_data=None, cells=None, lines=None, polys=None) -> None:
    """Write VTK XML (``vtu`` or ``vtp``) with raw appended data."""
    blobs: list[bytes] = []
    cursor = [0]

    def add(array: np.ndarray, name: str, components: int = 1) -> str:
        array = np.ascontiguousarray(array)
        payload = array.tobytes()
        offset = cursor[0]
        blobs.append(struct.pack('<Q', len(payload)) + payload)
        cursor[0] += 8 + len(payload)
        comp = f' NumberOfComponents="{components}"' if components != 1 else ''
        return (f'<DataArray type="{_vtk_type(array)}" Name="{name}"{comp} '
                f'format="appended" offset="{offset}"/>')

    point_xml = ''.join(add(a, n) for n, a in (point_data or {}).items())
    cell_xml = ''.join(add(a, n) for n, a in (cell_data or {}).items())
    pts = add(np.asarray(points, dtype=np.float64).reshape(-1), 'Points', 3)
    if kind == 'vtu':
        conn, offs, types = cells
        n_cells, n_points = len(types), len(points)
        body = (f'<Piece NumberOfPoints="{n_points}" '
                f'NumberOfCells="{n_cells}">'
                f'<PointData>{point_xml}</PointData>'
                f'<CellData>{cell_xml}</CellData>'
                f'<Points>{pts}</Points><Cells>'
                f'{add(conn.astype(np.int64), "connectivity")}'
                f'{add(offs.astype(np.int64), "offsets")}'
                f'{add(types.astype(np.uint8), "types")}'
                f'</Cells></Piece>')
        root = 'UnstructuredGrid'
    else:
        l_conn, l_offs = lines
        p_conn, p_offs = polys
        n_cells = len(l_offs) + len(p_offs)
        body = (f'<Piece NumberOfPoints="{len(points)}" NumberOfVerts="0" '
                f'NumberOfLines="{len(l_offs)}" NumberOfStrips="0" '
                f'NumberOfPolys="{len(p_offs)}">'
                f'<PointData>{point_xml}</PointData>'
                f'<CellData>{cell_xml}</CellData>'
                f'<Points>{pts}</Points>'
                f'<Lines>{add(l_conn.astype(np.int64), "connectivity")}'
                f'{add(l_offs.astype(np.int64), "offsets")}</Lines>'
                f'<Polys>{add(p_conn.astype(np.int64), "connectivity")}'
                f'{add(p_offs.astype(np.int64), "offsets")}</Polys></Piece>')
        root = 'PolyData'
    head = ('<?xml version="1.0"?>\n<VTKFile type="%s" version="1.0" '
            'byte_order="LittleEndian" header_type="UInt64">'
            '<%s>%s</%s><AppendedData encoding="raw">_' % (
                root, root, body, root))
    temporary = path + '.tmp'
    with open(temporary, 'wb') as handle:
        handle.write(head.encode('ascii'))
        for blob in blobs:
            handle.write(blob)
        handle.write(b'</AppendedData></VTKFile>\n')
    os.replace(temporary, path)


# -- mesh read-back ---------------------------------------------------------- #

def _physical_by_entity(gmsh) -> dict:
    table = {}
    for dim, tag in gmsh.model.getPhysicalGroups():
        for entity in gmsh.model.getEntitiesForPhysicalGroup(dim, tag):
            table.setdefault((dim, int(entity)), int(tag))
    return table


def _partitions(gmsh, dim: int, entity: int) -> int:
    try:
        found = gmsh.model.getPartitions(dim, entity)
        return int(found[0]) if len(found) else 0
    except Exception:
        return 0


def _mesh_dims(gmsh) -> list[int]:
    present = []
    for dim in (3, 2, 1):
        if gmsh.model.getEntities(dim) and any(
                len(gmsh.model.mesh.getElements(d, t)[1])
                for d, t in gmsh.model.getEntities(dim)):
            present.append(dim)
    return present


_UNIT_METRES = {'': 1e-3, 'UM': 1e-6, 'MM': 1e-3, 'CM': 1e-2, 'M': 1.0,
                'KM': 1e3, 'FT': 0.3048}


def _metres_per_unit(unit):
    """Metres in one coordinate unit of the model (Plan 42 SG2), from the
    ``Geometry.OCCTargetUnit`` its CAD was read with (Gmsh's own default,
    millimetre, when that was left empty); ``None`` when the model came from
    a file that carries no unit."""
    if unit is None:
        return None
    return _UNIT_METRES.get(str(unit).strip().upper(), 1e-3)


def _occ_unit(gmsh):
    try:
        return str(gmsh.option.getString('Geometry.OCCTargetUnit'))
    except Exception:                                      # noqa: BLE001
        return ''


def _collect(gmsh, dims, *, quality: bool):
    node_tags, coords, _ = gmsh.model.mesh.getNodes()
    node_tags = np.asarray(node_tags, dtype=np.int64)
    coords = np.asarray(coords, dtype=np.float64).reshape(-1, 3)
    if len(node_tags):
        lookup = np.full(int(node_tags.max()) + 1, -1, dtype=np.int64)
        lookup[node_tags] = np.arange(len(node_tags))
    else:
        lookup = np.zeros(1, dtype=np.int64)
    physical = _physical_by_entity(gmsh)
    conn, offsets, types = [], [], []
    columns = {name: [] for name in (
        'gmshElementTag', 'gmshElementType', 'gmshEntityDim',
        'gmshEntityTag', 'gmshPhysical', 'gmshPartition')}
    if quality:
        for name, _key in QUALITY_ARRAYS:
            columns[name] = []
    running = 0
    for dim in dims:
        for _, entity in gmsh.model.getEntities(dim):
            etypes, etags, enodes = gmsh.model.mesh.getElements(dim, entity)
            part = _partitions(gmsh, dim, entity)
            for etype, tags, nodes in zip(etypes, etags, enodes):
                if int(etype) not in _VTK_CELLS:
                    continue
                vtk_type, size, order = _VTK_CELLS[int(etype)]
                tags = np.asarray(tags, dtype=np.int64)
                nodes = np.asarray(nodes, dtype=np.int64).reshape(-1, size)
                indices = lookup[nodes]
                if order is not None:
                    indices = indices[:, order]
                    size = len(order)
                count = len(tags)
                conn.append(indices.reshape(-1))
                offsets.append(running + size * np.arange(1, count + 1))
                running += size * count
                types.append(np.full(count, vtk_type, dtype=np.uint8))
                columns['gmshElementTag'].append(tags.astype(np.int64))
                columns['gmshElementType'].append(
                    np.full(count, int(etype), dtype=np.int32))
                columns['gmshEntityDim'].append(
                    np.full(count, dim, dtype=np.int32))
                columns['gmshEntityTag'].append(
                    np.full(count, entity, dtype=np.int32))
                columns['gmshPhysical'].append(np.full(
                    count, physical.get((dim, entity), 0), dtype=np.int32))
                columns['gmshPartition'].append(
                    np.full(count, part, dtype=np.int32))
                if quality:
                    for name, key in QUALITY_ARRAYS:
                        if dim >= 2:
                            try:
                                values = gmsh.model.mesh.getElementQualities(
                                    tags.tolist(), key)
                            except Exception:
                                values = [float('nan')] * count
                        else:
                            values = [1.0] * count
                        columns[name].append(
                            np.asarray(values, dtype=np.float64))
    if not types:
        return coords, None, {}
    arrays = {name: np.concatenate(parts) for name, parts in columns.items()}
    arrays['__nodeTags'] = node_tags
    return coords, (np.concatenate(conn), np.concatenate(offsets),
                    np.concatenate(types)), arrays


# -- operations -------------------------------------------------------------- #

def _has_elements(gmsh, dim: int) -> bool:
    """Whether any entity of dimension *dim* holds an element."""
    return any(len(t) for _d, e in gmsh.model.getEntities(dim)
               for t in gmsh.model.mesh.getElements(dim, e)[1])


class Session(SessionOptionsMixin, SessionReadersMixin, SessionGeometryMixin,
              SessionMeshMixin, SessionPluginsMixin, SessionExportMixin,
              SessionFieldsMixin, SessionCvtMixin, SessionStageOperations):
    def __init__(self, gmsh):
        #: the Gmsh module itself, for the few calls that must not be recorded
        self.raw = gmsh
        self.gmsh = _Recorder(gmsh)
        self.started = time.time()
        #: Plan 42 SG2. The ``Geometry.OCCTargetUnit`` the CAD in the model
        #: was read with; ``None`` when no unit was ever stated.
        self.model_unit = None

    # session ops ---------------------------------------------------------- #
    def hello(self, **_):
        gmsh = self.gmsh
        build = gmsh.option.getString('General.BuildInfo') if hasattr(
            gmsh.option, 'getString') else ''
        return {
            'protocol': PROTOCOL, 'pid': os.getpid(),
            'python': sys.version.split()[0],
            'version': gmsh.option.getString('General.Version'),
            'threads': gmsh.option.getNumber('General.NumThreads'),
            'has_occ': hasattr(gmsh.model, 'occ'),
            'has_boundary_layer': hasattr(gmsh.model.geo,
                                          'extrudeBoundaryLayer'),
            'has_periodic': hasattr(gmsh.model.mesh, 'setPeriodic'),
            'build': build,
            # Plan 48 GM16a: the file the imported gmsh module came from, so
            # the build kind (FoamMesh's own or the wheel) is read from the
            # resident process, not only from the identity probe.
            'gmsh_file': str(getattr(gmsh, '__file__', '') or ''),
        }

    def set_log(self, path, **_):
        """Point the op log at *path* (an idle probe session joins a project)."""
        global _log_path, _calls_path
        _log_path = path or None
        _calls_path = _calls_for(_log_path)
        _truncate_calls()
        return {'log': _log_path}

    def ping(self, **_):
        return {'pid': os.getpid(), 'uptime': time.time() - self.started}

    def state(self, **_):
        gmsh = self.gmsh
        entities = {str(d): len(gmsh.model.getEntities(d))
                    for d in range(4)}
        nodes = len(gmsh.model.mesh.getNodes()[0])
        elements = {}
        for dim in range(4):
            count = 0
            for d, t in gmsh.model.getEntities(dim):
                for tags in gmsh.model.mesh.getElements(d, t)[1]:
                    count += len(tags)
            elements[str(dim)] = count
        return {'entities': entities, 'nodes': nodes, 'elements': elements,
                'dims_meshed': _mesh_dims(gmsh),
                'name': gmsh.model.getCurrent()}

    def import_(self, path, healing=False, **_):
        gmsh = self.gmsh
        extension = os.path.splitext(path)[1].lower()
        if extension in ('.step', '.stp', '.iges', '.igs', '.brep'):
            self.model_unit = _occ_unit(gmsh)
            created = gmsh.model.occ.importShapes(path)
            gmsh.model.occ.synchronize()
            return {'entities': [list(item) for item in created]}
        gmsh.merge(path)
        return {'entities': [list(item) for item in
                             gmsh.model.getEntities()]}

    def stage_import(self, job, paths=None, **_):
        """Plan 48 RR (DP-4009). Import the job's CAD as the batch run does
        (``stages.GeometryStage``): the healing pass, the duplicate-face
        fusion, the farfield and the scope resolution."""
        self.model_unit = _occ_unit(self.gmsh)
        return self._stages_import(job, paths)

    def generate(self, dim=3, **_):
        started = time.monotonic()
        gmsh = self.gmsh
        if int(dim) > 2 and _has_elements(gmsh, 2) \
                and not _has_elements(gmsh, 3):
            # DP-5742: the surface mesh already there (a CVT replace, the 2D
            # the person made) is what the volumes are meshed from. A surface
            # cleared and filled again (as the CVT replace does) stays marked
            # unmeshed in Gmsh, so a plain generate meshed it once more; only
            # the empty entities are meshed now, one dimension in turn
            # (MEASURED in ``regenerate``: one generate(3) with MeshOnlyEmpty
            # meshes nothing). MEASURED (4.15.2): generate(1) deletes every
            # surface mesh, so the curves, meshed already, are not redone.
            previous = gmsh.option.getNumber('Mesh.MeshOnlyEmpty')
            gmsh.option.setNumber('Mesh.MeshOnlyEmpty', 1)
            try:
                for step in range(2 if _has_elements(gmsh, 1) else 1,
                                  int(dim) + 1):
                    gmsh.model.mesh.generate(step)
            finally:
                gmsh.option.setNumber('Mesh.MeshOnlyEmpty', previous)
        else:
            if int(dim) > 2 and getattr(self, '_stage_run', None) is not None:
                # Plan 48 RR (DP-4009). The batch run always meshes the
                # surfaces in a pass of their own before the volume pass
                # (``GmshRun.generate``); MEASURED on the sphere farfield
                # case, one generate(3) holds 164,569 cells and the two passes
                # 164,661, the batch run's count.
                gmsh.model.mesh.generate(2)
                copy = getattr(self._stage_run, 'copy_periodic_surfaces',
                               None)
                if copy is not None and getattr(self._stage_run,
                                                'periodic_copies', None):
                    # DP-6054. A periodic side the layer stage rebuilt takes
                    # its partner's surface mesh before the volume pass, as
                    # in the batch run.
                    copy()
                gmsh.model.mesh.generate(int(dim))
                strips = getattr(self._stage_run, 'declare_periodic_strips',
                                 None)
                if copy is not None and strips is not None and getattr(
                        self._stage_run, 'periodic_copies', None):
                    # DP-6066. The pairs' layer strips are declared once the
                    # stacks beside them are extruded, as in the batch run.
                    strips()
            else:
                gmsh.model.mesh.generate(int(dim))
        crack =getattr(getattr(self, '_stage_run', None),
                        'crack_baffles', None)
        if int(dim) > 2 and crack is not None:
            # Plan 49 SR5. A blocking baffle is split into its two sides
            # after the volume mesh, as the batch run does.
            crack()
        return {'dim': int(dim), 'seconds': time.monotonic() - started,
                'state': self.state()}

    # Plan 41 GN12: generate actions that need more than one Gmsh call ------ #
    def regenerate(self, entities, dim=3, **_):
        """Redo *entities* and what depends on them, leaving the rest alone.

        Gmsh refuses to clear an entity that lies on the boundary of one that
        still has a mesh, so the entities above the ones named (their
        surfaces, then their volumes) are cleared with them; the rest of the
        mesh is kept and ``Mesh.MeshOnlyEmpty`` makes the generate skip it.
        """
        gmsh = self.gmsh
        chosen = {(int(d), int(t)) for d, t in entities}
        frontier = list(chosen)
        while frontier:
            d, t = frontier.pop()
            upward, _down = gmsh.model.getAdjacencies(d, t)
            for above in upward:
                key = (d + 1, int(above))
                if key not in chosen:
                    chosen.add(key)
                    frontier.append(key)
        ordered = sorted(chosen, key=lambda item: -item[0])
        for item in ordered:
            gmsh.model.mesh.clear([list(item)])
        previous = gmsh.option.getNumber('Mesh.MeshOnlyEmpty')
        gmsh.option.setNumber('Mesh.MeshOnlyEmpty', 1)
        started = time.monotonic()
        try:
            # MEASURED (Gmsh 4.15.2, elbow): one generate(3) with
            # MeshOnlyEmpty after clearing a surface meshes nothing; each
            # dimension in turn does.
            for step in range(max(1, min(d for d, _ in chosen)),
                              int(dim) + 1):
                gmsh.model.mesh.generate(step)
        finally:
            gmsh.option.setNumber('Mesh.MeshOnlyEmpty', previous)
        return {'cleared': [list(item) for item in ordered],
                'seconds': time.monotonic() - started,
                'state': self.state()}

    def renumber(self, method='RCMK', elements=True, **_):
        """Renumber the nodes with *method* (RCMK, Hilbert, Metis)."""
        gmsh = self.gmsh
        old, new = gmsh.model.mesh.computeRenumbering(method)
        if len(old):
            gmsh.model.mesh.renumberNodes(old, new)
        if elements:
            gmsh.model.mesh.renumberElements()
        return {'method': method, 'nodes': len(old)}

    def measure(self, dims=(2, 3), **_):
        """Element counts by type and min/avg/max of SICN, SIGE and gamma."""
        gmsh = self.gmsh
        counts: dict = {}
        quality: dict = {}
        for dim in range(4):
            for d, entity in gmsh.model.getEntities(dim):
                types, tags, _nodes = gmsh.model.mesh.getElements(d, entity)
                for etype, element_tags in zip(types, tags):
                    name = gmsh.model.mesh.getElementProperties(
                        int(etype))[0]
                    counts[name] = counts.get(name, 0) + len(element_tags)
        for dim in dims:
            pools = {'minSICN': [], 'minSIGE': [], 'gamma': []}
            total = 0
            for d, entity in gmsh.model.getEntities(int(dim)):
                for element_tags in gmsh.model.mesh.getElements(d, entity)[1]:
                    chunk = np.asarray(element_tags, dtype=np.uint64)
                    total += len(chunk)
                    for key, pool in pools.items():
                        pool.append(np.asarray(
                            gmsh.model.mesh.getElementQualities(
                                chunk.tolist(), key), dtype=np.float64))
            if not total:
                continue
            quality[str(dim)] = {'elements': total}
            for key, pool in pools.items():
                values = np.concatenate(pool)
                quality[str(dim)][key] = {
                    'min': float(values.min()), 'avg': float(values.mean()),
                    'max': float(values.max())}
        return {'counts': counts, 'quality': quality}

    def analyse(self, dim=3, hiding=None, **_):
        """Run Gmsh's ``AnalyseMeshQuality`` plugin; min / avg / max per output.

        Plan 41 GN14 (P-02). The plugin computes three measures that
        ``getElementQualities`` does not name together: the Jacobian ratio
        (``minJ/maxJ``, Gmsh's distortion), the inverse gradient error (IGE) and
        the inverse condition number (ICN).  It writes them as element-data
        views; those are read back and removed again so the model carries no
        trace.  ``hiding`` sets the plugin's ``HidingThreshold`` (views then
        keep only the elements below it).
        """
        gmsh = self.gmsh
        name = 'AnalyseMeshQuality'
        for key, value in (('JacobianDeterminant', 1), ('IGEMeasure', 1),
                           ('ICNMeasure', 1), ('CreateView', 1),
                           ('Recompute', 1), ('DimensionOfElements', int(dim))):
            gmsh.plugin.setNumber(name, key, value)
        if hiding is not None:
            gmsh.plugin.setNumber(name, 'HidingThreshold', float(hiding))
            gmsh.plugin.setNumber(name, 'ThresholdGreater', 0)
        before = set(gmsh.view.getTags())
        gmsh.plugin.run(name)
        measures = {}
        for tag in [t for t in gmsh.view.getTags() if t not in before]:
            label = gmsh.view.option.getString(tag, 'Name')
            for suffix in (' 3D', ' 2D'):
                if label.endswith(suffix):
                    label = label[:-len(suffix)]
            _kind, element_tags, data, _time, _comp = gmsh.view.getModelData(
                tag, 0)
            values = (np.concatenate([np.asarray(x, dtype=np.float64).ravel()
                                      for x in data]) if len(data) else
                      np.zeros(0))
            if len(values):
                measures[label] = {
                    'min': float(values.min()), 'avg': float(values.mean()),
                    'max': float(values.max()), 'elements': int(len(values))}
            gmsh.view.remove(tag)
        return {'dim': int(dim), 'measures': measures}

    # Plan 41 GN20: Gmsh's plugins on the session ------------------------- #
    def plugin_run(self, name, numbers=None, strings=None, directory=None,
                   view=None, other_view=None, keep=True, **_):
        """Run a plugin; every view it made is written as ``.vtu`` and kept."""
        import plugin_session_v1 as plugins
        return plugins.run(self.gmsh, name, numbers or {}, strings or {},
                           directory, _write_vtk, view=view,
                           other_view=other_view, keep=keep)

    #: The same run, for plugins that change the mesh: the name is in
    #: ``_MUTATING_SESSION`` so the run is logged and replays after a crash.
    plugin_edit = plugin_run

    def plugin_views(self, **_):
        import plugin_session_v1 as plugins
        return plugins.listing(self.gmsh)

    def plugin_remove(self, tag=None, **_):
        import plugin_session_v1 as plugins
        return plugins.remove(self.gmsh, tag)

    def write(self, path, **_):
        self.gmsh.write(path)
        return {'path': path, 'bytes': os.path.getsize(path)}

    def write_surface(self, path, **_):
        """Write the mesh in hand, every element, as ASCII MSH 2.2 (Plan 48
        GM12, DP-4355): after Mesh 2D the file holds the surface mesh alone,
        which ``surface_deviation`` measures straight away. The session's own
        options are put back; the model and the op log are untouched."""
        option = self.gmsh.option
        names = ('Mesh.SaveAll', 'Mesh.MshFileVersion', 'Mesh.Binary')
        before = {name: option.getNumber(name) for name in names}
        try:
            option.setNumber('Mesh.SaveAll', 1)
            option.setNumber('Mesh.MshFileVersion', 2.2)
            option.setNumber('Mesh.Binary', 0)
            self.gmsh.write(path)
        finally:
            for name, value in before.items():
                option.setNumber(name, value)
        return {'path': path, 'bytes': os.path.getsize(path)}

    # Plan 41 GN15: save the session mesh in any format, read it back ------- #
    def export_facts(self, **_):
        """Order, element families, partitions and counts of the mesh in hand."""
        import export_session_v1 as exporter
        return exporter.facts(self.gmsh)

    def export(self, path, **kwargs):
        """Write the model to *path* with the given Gmsh options; verify it."""
        import export_session_v1 as exporter
        return exporter.export(self.gmsh, path, **kwargs)

    def replay(self, path=None, **_):
        """Run the op log on this session; the ops are not logged again."""
        path = path or _log_path
        done = 0
        _truncate_calls()
        if path and os.path.isfile(path):
            with open(path, encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    dispatch(self, dict(entry, replay=True))
                    done += 1
        return {'replayed': done}

    def _entity_extent(self, dim, tag):
        """The bounding box of an entity, also of one with no CAD extent.

        The built-in kernel's boundary-layer curves and surfaces
        (``geo.extrudeBoundaryLayer``) have no geometry of their own, and
        ``getBoundingBox`` raises "Empty bounding box" for them; their mesh
        nodes stand in, else their boundary points, so a model with layers
        does not take the whole entity table down (DP-5709).
        """
        gmsh = self.raw
        try:
            return gmsh.model.getBoundingBox(dim, tag)
        except Exception:                                # noqa: BLE001
            pass
        xyz = []
        try:
            _nodes, coords, _p = gmsh.model.mesh.getNodes(
                dim, tag, includeBoundary=True)
            xyz = [coords[i:i + 3] for i in range(0, len(coords), 3)]
        except Exception:                                # noqa: BLE001
            pass
        if not xyz:
            try:
                points = [(0, tag)] if dim == 0 else [
                    e for e in gmsh.model.getBoundary(
                        [(dim, tag)], combined=False, oriented=False,
                        recursive=True) if e[0] == 0]
            except Exception:                            # noqa: BLE001
                points = []
            for _d, t in points:
                try:
                    xyz.append(gmsh.model.getValue(0, abs(int(t)), []))
                except Exception:                        # noqa: BLE001
                    pass
        if not xyz:
            return [0.0] * 6
        return ([min(p[i] for p in xyz) for i in range(3)]
                + [max(p[i] for p in xyz) for i in range(3)])

    def entities(self, **_):
        """Every entity of the model for picking (Plan 41 GN4).

        Per entity: dimension, tag, name, physical group tags, the centre of
        its bounding box, and for a point its coordinates; ``up`` lists the
        entities of the next dimension it bounds (a surface lists the volumes
        around it, which is how a click on a face picks the volume).
        """
        gmsh = self.gmsh
        groups = {}
        for dim, ptag in gmsh.model.getPhysicalGroups():
            for entity in gmsh.model.getEntitiesForPhysicalGroup(dim, ptag):
                groups.setdefault((dim, int(entity)), []).append(int(ptag))
        rows = []
        for dim, tag in gmsh.model.getEntities():
            extent = self._entity_extent(dim, tag)
            row = {'dim': int(dim), 'tag': int(tag),
                   'name': gmsh.model.getEntityName(dim, tag),
                   'physical': groups.get((dim, int(tag)), []),
                   'centre': [(extent[i] + extent[i + 3]) / 2.0
                              for i in range(3)],
                   'bbox': [float(v) for v in extent]}
            if dim == 0:
                row['xyz'] = [float(v) for v in
                              gmsh.model.getValue(0, tag, [])]
            if dim < 3:
                try:
                    up, _down = gmsh.model.getAdjacencies(dim, tag)
                except Exception:
                    up = []
                row['up'] = [int(v) for v in up]
            rows.append(row)
        return {'entities': rows}

    def physical_groups(self, **_):
        """Every physical group: dimension, tag, name, member entity tags."""
        gmsh = self.gmsh
        return {'groups': [
            {'dim': int(dim), 'tag': int(tag),
             'name': gmsh.model.getPhysicalName(dim, tag),
             'entities': [int(e) for e in
                          gmsh.model.getEntitiesForPhysicalGroup(dim, tag)]}
            for dim, tag in gmsh.model.getPhysicalGroups()]}

    def coherence(self, **_):
        """Geometry > Coherence: remove duplicate entities (Gmsh's button)."""
        gmsh = self.gmsh
        before = {str(d): len(gmsh.model.getEntities(d)) for d in range(4)}
        gmsh.model.occ.synchronize()
        gmsh.model.occ.removeAllDuplicates()
        gmsh.model.occ.synchronize()
        after = {str(d): len(gmsh.model.getEntities(d)) for d in range(4)}
        return {'before': before, 'after': after,
                'removed': {k: before[k] - after[k] for k in before}}

    # Plan 41 GN19: scripts, the macro console, ONELAB parameters ----------- #
    def script(self, code, **_):
        """Run a Python script on the live model (T-08, Q5).

        The script sees ``gmsh`` (the recorded module).  What it does is in
        the op log (it replays) and, call by call, in the script export.
        """
        namespace = {'gmsh': self.gmsh, '__name__': '__foammesh_script__'}
        exec(compile(str(code), '<script>', 'exec'), namespace)
        return {'entities': [len(self.gmsh.model.getEntities(d))
                             for d in range(4)]}

    def geo_lines(self, text, **_):
        """Run Gmsh script (``.geo``) lines on the live model (T-08)."""
        base = os.path.dirname(_log_path) if _log_path else None
        if not base:
            import tempfile
            base = tempfile.gettempdir()
        os.makedirs(base, exist_ok=True)
        path = os.path.join(base, 'geo_lines.geo')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(str(text))
        self.raw.merge(path)
        _write_calls(json.dumps({'f': '#geo', 'a': [str(text)], 'k': {}}))
        return {'entities': [len(self.gmsh.model.getEntities(d))
                             for d in range(4)]}

    def onelab_parameters(self, **_):
        """The ONELAB parameters of the script last opened (S-01)."""
        raw = self.raw.onelab.get()
        try:
            tree = json.loads(raw) if raw else {}
        except ValueError:
            tree = {}
        rows = []
        for item in (tree.get('onelab', {}) or {}).get('parameters', []):
            if item.get('type') not in ('number', 'string'):
                continue
            name = item.get('name', '')
            if name.startswith('Gmsh/') or name.startswith('0Modules/'):
                continue
            rows.append({
                'name': name, 'type': item['type'],
                'values': item.get('values', item.get('value', [])),
                'min': item.get('min'), 'max': item.get('max'),
                'step': item.get('step'),
                'choices': item.get('choices', []),
                'labels': item.get('valueLabels', {}),
                'label': item.get('label', ''),
                'readOnly': bool(item.get('readOnly', False))})
        return {'parameters': rows}

    def onelab_run(self, path, values=None, **_):
        """Set ONELAB parameters, then run the script again (S-01).

        Gmsh's own GUI does the same: the values go into the ONELAB database,
        the model is cleared and the script is read again, so
        ``DefineConstant`` takes them.
        """
        for name, value in dict(values or {}).items():
            if isinstance(value, str):
                self.gmsh.onelab.setString(name, [value])
            else:
                self.gmsh.onelab.setNumber(name, [float(value)])
        result = self.open(path, 'open')
        result['parameters'] = self.onelab_parameters()['parameters']
        return result

    # Plan 41 GN5: File > Open / Merge, Reclassify, Create topology/geometry -- #
    def _importer(self):
        try:
            import import_v1 as importer
        except ImportError:                              # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import import_v1 as importer
        return importer

    def _imported_call(self, name, *args, **kwargs):
        importer = self._importer()
        try:
            return getattr(importer, name)(self.gmsh, *args, **kwargs)
        except importer.ImportError_ as error:
            raise ValueError(str(error)) from error

    def open(self, path, mode='open', **options):
        """File > Open (replace the model) or File > Merge (add to it).

        Plan 48 DP-4050 / DP-4051. A reader ``open_file`` does not list (.nas,
        .neu, .p3d, .diff, .wrl, .vrml, .iv, .geom, .mmed, .rmed, .vis, and
        .dat, which is not refused) and a ``.gz`` of any format go to
        ``read_file`` (``OpenRequest.operation``).
        """
        lowered = str(path).lower()
        if lowered.endswith('.gz') and not lowered.endswith('.nii.gz'):
            return self.read_file(path, mode)
        extension = os.path.splitext(lowered)[1]
        if extension in self._reader_only_extensions():
            return self.read_file(path, mode)
        result = self._imported_call('open_file', path, mode, **options)
        applied = (result.get('applied') or {}).get('unit')
        if applied is not None and (mode == 'open'
                                    or self.model_unit is None):
            self.model_unit = applied
        elif mode == 'open':
            self.model_unit = None
        return result

    def _reader_only_extensions(self):
        """Extensions ``read_file`` takes that ``open_file`` does not list."""
        try:
            import session_readers_v1 as readers
        except ImportError:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import session_readers_v1 as readers
        return set(readers.READERS) - set(self._importer().KINDS)

    def prime_unit(self, **_):
        """Plan 48 DP-4001. Prime the OCC reader so ``Geometry.OCCTargetUnit``
        can be set before an IGES is read (no empty-file trick)."""
        return self._imported_call('prime_unit')

    def reclassify(self, **options):
        """Mesh > Reclassify 2D (every ``classifySurfaces`` argument)."""
        return self._imported_call('reclassify', **options)

    def create_topology(self, **options):
        return self._imported_call('create_topology', **options)

    def create_geometry(self, **options):
        return self._imported_call('create_geometry', **options)

    def remesh_surfaces(self, **options):
        return self._imported_call('remesh_surfaces', **options)

    def build(self, kind, **params):
        """Plan 41 GN3: one Geometry-module operation (see geometry_build_v1).

        The result lists the entities it created; ``state`` is the entity
        count after the operation, so the caller can show what changed.
        """
        try:
            import geometry_build_v1 as builder
        except ImportError:                              # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import geometry_build_v1 as builder
        # DP-5602: synchronize re-reads every OpenCASCADE face, and one of an
        # imported model (the kept pipe's surface 5) logs "Could not fix wire";
        # the native window prints it and adds the box, the API (AbortOnError
        # 2) threw it and lost the step. Run the step as the window does; an
        # operation that really fails still throws, and Gmsh's error is shown.
        gmsh = self.gmsh
        # DP-5716. The stage run's names and scopes follow the build, as the
        # batch run's do at its ``geometry`` hook (runner_parts.geometry).
        run = getattr(self, '_stage_run', None)
        snapshot = (run.build_snapshot()
                    if run is not None and getattr(run, 'gmsh', None) is gmsh
                    and hasattr(run, 'build_snapshot') else None)
        abort = gmsh.option.getNumber('General.AbortOnError')
        last = _gmsh_last_error(gmsh)
        gmsh.option.setNumber('General.AbortOnError', 0)
        try:
            result = builder.apply(gmsh, kind, params)
        except builder.BuildError as error:
            raise ValueError(str(error)) from error
        finally:
            gmsh.option.setNumber('General.AbortOnError', abort)
        if snapshot is not None:
            followed = run.follow_geometry_build(snapshot, kinds=[kind])
            if followed:
                result['followed'] = followed
        error = _gmsh_last_error(gmsh)
        if error and error != last:
            result['gmsh_error'] = error
        result['entities'] = {str(d): len(gmsh.model.getEntities(d))
                              for d in range(4)}
        return result

    # Plan 41 GN6: the size fields the user made -------------------------- #
    def _native(self):
        try:
            import native_fields_v1 as native
        except ImportError:                              # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import native_fields_v1 as native
        return native

    def fields(self, nodes, combiner='min', **_):
        """Replace the fields built earlier with *nodes* and make them the
        background (``native_fields_v1.build``).  An empty list only removes.

        While the automatic size field is active (``size_field``) it is one
        more input of the combiner (Plan 41 GN25).
        """
        native = self._native()
        previous = getattr(self, '_native_tags', [])
        native.remove(self.gmsh, previous)
        self._native_tags = []
        self._user_background = []
        self._user_aniso = False
        self._user_combiner = combiner
        if not nodes:
            chosen = self._compose_background()
            return {'tags': {}, 'background': chosen, 'applied': []}
        try:
            built = native.build(self.gmsh, nodes)
        except native.BuildError as error:
            raise ValueError(str(error)) from error
        self._native_tags = list(built['tags'].values())
        for tag in built['layer']:
            self.gmsh.model.mesh.field.setAsBoundaryLayer(tag)
        self._user_background = list(built['background'])
        self._user_aniso = bool(built['anisotropic'])
        chosen = self._compose_background()
        return {'tags': built['tags'], 'background': chosen,
                'applied': [[a, b, str(c), str(d)] for a, b, c, d
                            in built['applied']]}

    def _compose_background(self):
        """The one background: the user's fields and the automatic field,
        combined as the user chose (``Min`` by default)."""
        native = self._native()
        old = getattr(self, '_combiner_tag', None)
        if old is not None:
            native.remove(self.gmsh, [old])
            self._combiner_tag = None
        tags = list(getattr(self, '_user_background', []))
        # Plan 48 RR: the per-volume sizes of the volume controls, which the
        # batch run assembles into the same background.
        tags.extend(getattr(self, '_stage_background', []))
        auto = getattr(self, '_auto', None)
        if auto is not None:
            tags.append(auto['field'])
        chosen = native.set_background(
            self.gmsh, tags, combiner=getattr(self, '_user_combiner', 'min'),
            anisotropic=getattr(self, '_user_aniso', False))
        if chosen is not None and chosen not in tags:
            self._combiner_tag = chosen
        return chosen

    def _sizefield(self):
        try:
            import sizefield_consumer_v1 as consumer
        except ImportError:                              # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import sizefield_consumer_v1 as consumer
        return consumer

    def size_field(self, directory, octree=False, **_):
        """Plan 41 GN25 (S5): make the field in *directory* (the arrays of
        ``foammesh-sizefield``) a background size field, replacing any made
        before.  ``Mesh.MeshSizeExtendFromBoundary``, ``MeshSizeFromPoints``
        and ``MeshSizeFromCurvature`` are held at 0 until ``size_field_off``.
        """
        consumer = self._sizefield()
        previous = getattr(self, '_auto', None)
        held = previous['held'] if previous else None
        if previous:
            consumer.remove(self.raw, previous)
            self._auto = None
        try:
            built = consumer.build(self.raw, directory, octree=bool(octree))
        except consumer.FieldError as error:
            if previous:
                consumer.release(self.raw, held)
                self._compose_background()
            raise ValueError(str(error)) from error
        built['held'] = consumer.hold(self.raw, held)
        self._auto = built
        chosen = self._compose_background()
        return {k: v for k, v in built.items() if k != 'held'} | {
            'background': chosen,
            'held': {name: 0.0 for name in built['held']},
            'was': dict(built['held'])}

    def size_field_off(self, **_):
        """Remove the automatic field and give the three options back."""
        consumer = self._sizefield()
        auto = getattr(self, '_auto', None)
        if auto is None:
            return {'active': False, 'restored': {}}
        consumer.remove(self.raw, auto)
        restored = consumer.release(self.raw, auto['held'])
        self._auto = None
        chosen = self._compose_background()
        return {'active': False, 'restored': restored, 'background': chosen}

    def point_sizes(self, rows, **_):
        """Plan 41 GN7 (M-01). Replace the sizes at points with *rows*.

        The points sized before are reset, the rows are set with
        ``mesh.setSize`` and read back with ``mesh.getSizes``, and
        ``Mesh.MeshSizeFromPoints`` is switched on (Gmsh ignores point sizes
        otherwise). ``was`` says what it was.
        """
        try:
            import point_sizes_v1 as points
        except ImportError:                              # pragma: no cover
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import point_sizes_v1 as points
        result = points.apply(self.gmsh, rows,
                              previous=getattr(self, '_point_tags', []))
        self._point_tags = list(result['tags'])
        result['fromPoints'] = None
        if result['applied']:
            was, now = points.switch_on_point_sizes(self.gmsh)
            auto = getattr(self, '_auto', None)
            if auto is not None:
                # held by the automatic field: remember the user's 1, keep 0
                auto['held']['Mesh.MeshSizeFromPoints'] = now
                self.raw.option.setNumber('Mesh.MeshSizeFromPoints', 0.0)
                now = 0.0
            result['fromPoints'] = {'was': was, 'now': now}
        return result

    def field_preview(self, dim=2, limit=20000, seed=1, **_):
        """Sizes the background field gives, sampled where Gmsh asks for one.

        Refused while the model has a mesh: the preview meshes the curves (and
        surfaces) with the field alone and clears them again.
        """
        native = self._native()
        if native.mesh_exists(self.gmsh):
            raise ValueError('mesh_exists: clear the mesh first; the preview '
                             'meshes the model with the field alone')
        return native.preview(self.gmsh, dim=int(dim), limit=int(limit),
                              seed=int(seed))

    def handover(self, directory, mesh=True, geometry=True, dims=None,
                 quality=False, surfaces=False, **_):
        gmsh = self.gmsh
        os.makedirs(directory, exist_ok=True)
        written = {}
        meshed = _mesh_dims(gmsh)
        if mesh and meshed:
            use = [int(d) for d in dims] if dims else [meshed[0]]
            if surfaces and not dims and meshed[0] == 3 and 2 in meshed:
                use = [3, 2]
            coords, cells, arrays = _collect(gmsh, use, quality=bool(quality))
            if cells is not None:
                path = os.path.join(directory, 'mesh.vtu')
                node_tags = arrays.pop('__nodeTags')
                _write_vtk(path, 'vtu', points=coords, cells=cells,
                           cell_data=arrays,
                           point_data={'gmshNodeTag': node_tags})
                names = {f'{dim}:{tag}': gmsh.model.getPhysicalName(dim, tag)
                         for dim, tag in gmsh.model.getPhysicalGroups()}
                with open(os.path.join(directory, 'mesh.names.json'), 'w',
                          encoding='utf-8') as handle:
                    note = {'physical': names, 'dims': use}
                    scale = _metres_per_unit(self.model_unit)
                    if scale is not None:
                        note['metres_per_unit'] = scale
                    json.dump(note, handle)
                written['mesh'] = {'path': path, 'cells': len(cells[2]),
                                   'dims': use, 'arrays': sorted(arrays)}
        if geometry:
            written['geometry'] = self._geometry(directory)
        return written

    def reference_surface(self, directory, per_circle=120.0, **_):
        """The model's CAD surface, tessellated finely, for the deviation tool.

        Plan 42 SG2 (D13). The mesh in the session is left as it is: the
        geometry is written to a BREP, read into a second model, meshed there
        with the chordal error of ``per_circle`` elements around a circle and
        removed again. ``reference.vtp`` holds the triangles only.
        """
        gmsh = self.gmsh
        os.makedirs(directory, exist_ok=True)
        brep = os.path.join(directory, 'reference.brep')
        current = gmsh.model.getCurrent()
        saved = {}
        gmsh.write(brep)
        try:
            gmsh.model.add('foammesh_reference')
            gmsh.model.occ.importShapes(brep)
            gmsh.model.occ.synchronize()
            low, high = (gmsh.model.getBoundingBox(-1, -1)[:3],
                         gmsh.model.getBoundingBox(-1, -1)[3:])
            diagonal = float(np.linalg.norm(np.subtract(high, low))) or 1.0
            for name, value in (('Mesh.MeshSizeMax', diagonal / 20.0),
                                ('Mesh.MeshSizeFromCurvature',
                                 float(per_circle))):
                saved[name] = gmsh.option.getNumber(name)
                gmsh.option.setNumber(name, value)
            gmsh.model.mesh.generate(2)
            coords, cells, arrays = _collect(gmsh, [2], quality=False)
        finally:
            gmsh.model.remove()
            gmsh.model.setCurrent(current)
            for name, value in saved.items():
                gmsh.option.setNumber(name, value)
        if cells is None:
            return {'path': None, 'cells': 0}
        conn, offs, types = cells
        path = os.path.join(directory, 'reference.vtp')
        _write_vtk(path, 'vtp', points=coords, cell_data={},
                   lines=(np.zeros(0, np.int64), np.zeros(0, np.int64)),
                   polys=(conn, offs))
        return {'path': path, 'cells': int(len(offs)),
                'metres_per_unit': _metres_per_unit(self.model_unit)}

    def _geometry(self, directory):
        """CAD display polydata with the entity dimension and tag per cell."""
        gmsh = self.gmsh
        temporary = not _mesh_dims(gmsh)
        saved = {}
        if temporary:
            low, high = gmsh.model.getBoundingBox(-1, -1)[:3], \
                gmsh.model.getBoundingBox(-1, -1)[3:]
            diagonal = float(np.linalg.norm(np.subtract(high, low))) or 1.0
            # DP-5732: no curvature sizing here. On a Built-in model a
            # degenerate spline (a straight edge kept as a NURBS) gives Gmsh's
            # curvature sizing no end, and every hand-over waited minutes.
            # Circles and curves get their points from Gmsh's minimums.
            for name, value in (('Mesh.MeshSizeMax', diagonal / 20.0),
                                ('Mesh.MeshSizeFromCurvature', 0.0),
                                ('Mesh.MinimumCirclePoints', 24.0),
                                ('Mesh.MinimumCurvePoints', 8.0)):
                saved[name] = gmsh.option.getNumber(name)
                gmsh.option.setNumber(name, value)
            gmsh.model.mesh.generate(2)
        try:
            coords, cells, arrays = _collect(gmsh, [1, 2], quality=False)
        finally:
            if temporary:
                gmsh.model.mesh.clear()
                for name, value in saved.items():
                    gmsh.option.setNumber(name, value)
        if cells is None:
            return {'path': None, 'cells': 0}
        conn, offs, types = cells
        starts = np.concatenate(([0], offs[:-1]))
        is_line = types == 3
        l_idx, p_idx = np.nonzero(is_line)[0], np.nonzero(~is_line)[0]

        def gather(idx):
            if not len(idx):
                return np.zeros(0, np.int64), np.zeros(0, np.int64)
            sizes = (offs - starts)[idx]
            parts = [conn[starts[i]:offs[i]] for i in idx]
            return np.concatenate(parts), np.cumsum(sizes)

        lines, polys = gather(l_idx), gather(p_idx)
        order = np.concatenate((l_idx, p_idx))
        data = {name: arrays[name][order] for name in (
            'gmshEntityDim', 'gmshEntityTag', 'gmshPhysical')}
        path = os.path.join(directory, 'geometry.vtp')
        _write_vtk(path, 'vtp', points=coords, cell_data=data,
                   lines=lines, polys=polys)
        return {'path': path, 'cells': len(order)}


_MEDIT_RETIRED = ("Medit (.mesh) export was retired in 1.1.4: Gmsh 4.15.2's "
                  'writer crashes; export .msh, .su2, VTK or polyMesh instead.')


def dispatch(session: Session, request: dict):
    op = request['op']
    args = request.get('args') or []
    kwargs = request.get('kwargs') or {}
    if op in ('write', 'session.write', 'session.export'):
        # DP-1437: Gmsh 4.15.2's Medit writer segfaults this process on
        # tet+prism meshes; the Medit export is retired from FoamMesh.
        target = args[0] if args else kwargs.get('path', kwargs.get('fileName'))
        if str(target or '').lower().endswith(('.mesh', '.meshb')):
            raise ValueError(_MEDIT_RETIRED)
    if op == 'session.import':
        return session.import_(*args, **kwargs)
    if op == 'mesh.generate':
        return session.generate(*args, **kwargs)
    if op.startswith('session.'):
        name = op.split('.', 1)[1]
        method = getattr(session, name, None)
        if name.startswith('_') or not callable(method):
            raise ValueError('unknown session operation %s' % op)
        return method(*args, **kwargs)
    parts = op.split('.')
    if parts[0] not in _ROOTS or any(p.startswith('_') for p in parts):
        raise ValueError('operation %s is not allowed' % op)
    target = session.gmsh
    for part in parts:
        target = getattr(target, part)
    if not callable(target):
        raise ValueError('%s is not callable' % op)
    return target(*args, **kwargs)


def main(argv) -> int:
    global _proto, _log_path, _calls_path, _memory_cap_kb
    options = {'--log': None, '--memory-mb': None}
    for flag in options:
        if flag in argv:
            options[flag] = argv[argv.index(flag) + 1]
    _log_path = options['--log']
    _calls_path = _calls_for(_log_path)
    _truncate_calls()
    if options['--memory-mb']:
        _memory_cap_kb = int(float(options['--memory-mb']) * 1024)
    _proto = os.fdopen(os.dup(1), 'w', buffering=1, encoding='utf-8')
    _capture_console()
    import gmsh
    gmsh.initialize(['', '-noenv'], readConfigFiles=False, run=False)
    gmsh.option.setNumber('General.Terminal', 1)
    gmsh.option.setNumber('General.Verbosity', 5)
    gmsh.model.add('foammesh')
    session = Session(gmsh)
    requests: queue.Queue = queue.Queue()
    threading.Thread(target=_stdin_reader, args=(requests,),
                     daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    emit(dict(session.hello(), event='ready'))
    while True:
        request = requests.get()
        identifier = request.get('id')
        op = request.get('op', '')
        if op == 'session.quit':
            emit({'id': identifier, 'ok': True, 'result': None})
            break
        started = time.monotonic()
        timeout = request.get('timeout')
        _op_deadline['op'] = op
        _op_deadline['when'] = (started + float(timeout)) if timeout else None
        try:
            result = dispatch(session, request)
            if not request.get('replay') and not _is_read(op):
                _append_log(request)
            response = {'id': identifier, 'ok': True, 'result': result}
        except BaseException as error:  # noqa: BLE001 - reported to client
            response = {'id': identifier, 'ok': False,
                        'error': '%s: %s' % (type(error).__name__, error),
                        'trace': traceback.format_exc(limit=6)}
        _op_deadline['when'] = None
        response['seconds'] = round(time.monotonic() - started, 4)
        response['log'] = _drain_console(gmsh)[-200:]
        emit(response)
    gmsh.finalize()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
