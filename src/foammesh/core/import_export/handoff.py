"""Plan 49 SR6: the FoamMesh -> FoamFlow hand-off sidecar and the final check.

A separate step that runs **after** a finished export and changes nothing the
export wrote (Plan 49 section 12 point 5: no exporter is restructured, no new
polyMesh writer). It reads the exported mesh on disk plus FoamMesh's own
records -- the patch-identity sidecar (``core/quality/patch_identity``, the one
identity authority), the project's surface roles and relations
(``db/surface_intent``) and the couplings the mesher recorded
(``couplings.recorded_couplings``) -- and writes one small, versioned JSON file
beside the exported mesh:

* OpenFOAM: ``<exported case>/constant/foammesh_handoff.json``. FoamFlow's
  import resolves every selection to the exported ``constant/`` folder and
  copies only ``regionProperties`` and the ``polyMesh`` folders from it
  (``plans/evidence/plan49/foamflow-inventory/NOTE_FOR_13.md`` A), so that is
  the one place it can find the file; FoamFlow has to copy it itself.
* SU2: ``<name>.foammesh_handoff.json`` beside ``<name>.su2``.

The same step **verifies** the export: every authored role is on disk with the
expected type, every relation is coupled, every coupled patch has its partner
pointing back, the empty non-conformal patches were kept. A mismatch is a
failure recorded in ``validation`` and raised to the export's caller
(:class:`HandoffVerificationError`); it is never passed over in silence.

The document is described by :data:`JSON_SCHEMA` (``schemaVersion`` 1), and in
words in ``plans/evidence/plan49/sr6/SIDECAR.md``. It holds no absolute
machine path: every path is relative to the exported case root (OpenFOAM) or
is the bare file name (SU2).

Nothing here imports Qt, PyFoam or Gmsh.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterable, Mapping

from foammesh.core.quality import patch_identity as pid

SCHEMA_ID = 'foammesh-handoff'
SCHEMA_VERSION = 1

#: The OpenFOAM sidecar's name, inside the exported ``constant/``.
OPENFOAM_SIDECAR = 'foammesh_handoff.json'
#: The SU2 sidecar is ``<stem>`` + this, beside the ``.su2`` file.
SU2_SIDECAR_SUFFIX = '.foammesh_handoff.json'

#: OpenFOAM's name for the mesh in ``constant/polyMesh`` of a one-region case.
DEFAULT_REGION = 'region0'

#: The polyMesh files whose checksums identify the exported mesh.
MESH_FILES = ('boundary', 'faces', 'owner', 'neighbour', 'points')

#: The sentence every SU2 export result carries (DP-5765, Plan 49 section 4).
MARKERS_ARE_NOT_COUPLING = (
    'SU2 markers are not coupling: the .su2 file is one zone of named '
    'boundary markers, and no region interface, periodic pair or other '
    'coupling is carried by a marker. Any coupling has to be set in the SU2 '
    'configuration.')

#: Mesh patch types that join a patch to a partner, of those FoamMesh's
#: exports write. (``mapped``/``mappedPatch`` are left out: they may sample
#: one-sidedly.)
_PAIRED_TYPES = ('mappedWall', 'cyclic', 'nonConformalCyclic',
                 'nonConformalMappedWall')

#: ``db/surface_intent`` words, repeated so this module reads plain data.
EXTERNAL = 'externalBoundary'
INTERNAL = 'internalFace'
CONFORMAL = 'regionInterfaceConformal'
NON_CONFORMAL = 'regionInterfaceNonConformal'
BAFFLE = 'blockingBaffle'
PERIODIC = 'periodic'
VALID = 'valid'


class HandoffVerificationError(RuntimeError):
    """The exported mesh does not carry what was authored; names each fault.

    Raised after the sidecar is written, so the failures are on disk too.
    """

    def __init__(self, message: str, result: 'HandoffResult'):
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class HandoffResult:
    """One sidecar written, and what its validation found."""

    path: Path
    document: dict = field(default_factory=dict)
    #: Relative to the export destination (OpenFOAM: the case root), or
    #: the bare file name (SU2).
    relative_path: str = ''

    @property
    def failures(self) -> list[dict]:
        return list(self.document.get('validation', {}).get('failures') or ())

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> dict:
        """What an export result names: the file, its schema, its verdict.

        ``path`` is relative to the export destination for OpenFOAM (the
        case root) and is the bare file name for SU2.
        """
        validation = self.document.get('validation') or {}
        return {
            'path': self.relative_path or self.path.name,
            'schema': SCHEMA_ID,
            'schemaVersion': SCHEMA_VERSION,
            'status': validation.get('status', ''),
            'failures': [item.get('message', '') for item in self.failures],
            # Plan 49 SR8 (DP-6007). The final check's codes, so a caller
            # can act on a fault without parsing its sentence.
            'failure_codes': [item.get('code', '') for item in self.failures],
            'failure_items': [
                {key: value for key, value in item.items()}
                for item in self.failures],
            'warnings': list(validation.get('warnings') or ()),
        }


# --------------------------------------------------------------------------- #
# The schema
# --------------------------------------------------------------------------- #

_STR = {'type': 'string'}
_STR_OR_NULL = {'type': ['string', 'null']}
_INT = {'type': 'integer'}
_REF = {'type': 'object', 'required': ['region', 'patch'],
        'properties': {'region': _STR, 'patch': _STR, 'type': _STR}}
_SOURCE = {'type': 'object', 'required': ['patchUuid', 'displayName'],
           'properties': {'patchUuid': _STR_OR_NULL, 'displayName': _STR,
                          'solverName': _STR, 'origin': _STR}}

#: JSON Schema (draft 2020-12) of the sidecar. FoamFlow (SR11) implements
#: against it; ``plans/evidence/plan49/sr6/SIDECAR.md`` explains each field.
JSON_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    '$id': f'urn:foammesh:{SCHEMA_ID}:{SCHEMA_VERSION}',
    'title': 'FoamMesh hand-off sidecar',
    'type': 'object',
    'required': ['schema', 'schemaVersion', 'producer', 'route', 'target',
                 'mesh', 'regions', 'patches', 'surfaces', 'relations',
                 'zones', 'auxiliaryPatches', 'unsupported', 'validation'],
    'properties': {
        'schema': {'const': SCHEMA_ID},
        'schemaVersion': {'const': SCHEMA_VERSION},
        'producer': {'type': 'object', 'required': ['application', 'version'],
                     'properties': {'application': _STR, 'version': _STR,
                                    'exportedAt': _STR,
                                    'identityVersion': _INT}},
        'route': {'enum': ['snappy', 'gmsh', 'unknown']},
        'target': {'enum': ['openfoam', 'su2']},
        'mesh': {
            'type': 'object', 'required': ['format', 'lengthUnit', 'files',
                                           'revision'],
            'properties': {
                'format': {'enum': ['openfoam-polyMesh', 'su2']},
                'lengthUnit': {'const': 'm'},
                'files': {'type': 'object', 'additionalProperties': {
                    'type': 'object', 'required': ['sha256', 'bytes'],
                    'properties': {'sha256': _STR, 'bytes': _INT}}},
                'revision': _STR,
            }},
        'regions': {'type': 'array', 'items': {
            'type': 'object', 'required': ['name', 'fluidSolid', 'meshPath'],
            'properties': {
                'name': _STR, 'id': _STR_OR_NULL,
                'fluidSolid': {'enum': ['fluid', 'solid', 'unknown']},
                'meshPath': _STR, 'cells': {'type': ['integer', 'null']}}}},
        'patches': {'type': 'array', 'items': {
            'type': 'object',
            'required': ['region', 'name', 'type', 'nFaces', 'origin',
                         'sources', 'purpose', 'parameters'],
            'properties': {
                'region': _STR, 'name': _STR, 'type': _STR, 'nFaces': _INT,
                'startFace': {'type': ['integer', 'null']},
                'origin': {'enum': [
                    pid.EXPORT_PUBLISHED, pid.EXPORT_BAFFLE_SECOND_SIDE,
                    pid.EXPORT_COUPLING, pid.EXPORT_COUPLING_ERROR,
                    pid.EXPORT_REGION_SPLIT, pid.EXPORT_UNIDENTIFIED]},
                'identity': _STR, 'side': _STR, 'basedOn': _STR,
                'auxiliary': {'type': 'boolean'},
                'sources': {'type': 'array', 'items': _SOURCE},
                'role': _STR_OR_NULL, 'purpose': _STR_OR_NULL,
                'purposeSource': _STR_OR_NULL,
                'relationIds': {'type': 'array', 'items': _STR},
                'parameters': {'type': 'object',
                               'additionalProperties': _STR}}}},
        'surfaces': {'type': 'array', 'items': {
            'type': 'object', 'required': ['patchUuid', 'displayName',
                                           'exportedPatches'],
            'properties': {'patchUuid': _STR, 'displayName': _STR,
                           'solverName': _STR, 'role': _STR_OR_NULL,
                           'purpose': _STR_OR_NULL,
                           'meshPatchType': _STR_OR_NULL,
                           'exportedPatches': {'type': 'array',
                                               'items': _REF},
                           'faceZones': {'type': 'array', 'items': _STR}}}},
        'relations': {'type': 'array', 'items': {
            'type': 'object', 'required': ['id', 'kind', 'state', 'owner',
                                           'neighbour', 'coupled'],
            'properties': {
                'id': _STR, 'name': _STR,
                'kind': {'enum': [CONFORMAL, NON_CONFORMAL, PERIODIC]},
                'state': _STR, 'coupled': {'type': ['boolean', 'null']},
                'transform': {'type': 'object'},
                'owner': {'type': 'object'}, 'neighbour': {'type': 'object'},
                'exportedPatches': {'type': 'array', 'items': _REF}}}},
        'zones': {'type': 'array', 'items': {
            'type': 'object', 'required': ['region', 'name', 'kind'],
            'properties': {'region': _STR, 'name': _STR,
                           'kind': {'enum': ['faceZone', 'cellZone',
                                             'pointZone']},
                           'size': {'type': ['integer', 'null']},
                           'sources': {'type': 'array', 'items': _SOURCE}}}},
        'auxiliaryPatches': {'type': 'array', 'items': _REF},
        'unsupported': {'type': 'array', 'items': {'type': 'object'}},
        'validation': {
            'type': 'object', 'required': ['status', 'failures'],
            'properties': {
                'status': {'enum': ['passed', 'failed']},
                'checkedAgainst': _STR,
                'failures': {'type': 'array', 'items': {
                    'type': 'object', 'required': ['code', 'message']}},
                'warnings': {'type': 'array', 'items': _STR},
                'unresolved': {'type': 'array', 'items': {'type': 'object'}},
                'sharedValidation': {}}},
    },
}


def schema_problems(document) -> list[str]:
    """Structural problems of *document* against :data:`JSON_SCHEMA`.

    A small reader of the schema's ``required``/``type``/``enum``/``const``
    keywords -- enough for this product's own tests and for a reader with no
    JSON Schema library. It is not a general validator.
    """
    problems: list[str] = []

    def kind_ok(value, expected) -> bool:
        names = expected if isinstance(expected, list) else [expected]
        checks = {'object': lambda v: isinstance(v, dict),
                  'array': lambda v: isinstance(v, list),
                  'string': lambda v: isinstance(v, str),
                  'integer': lambda v: isinstance(v, int)
                  and not isinstance(v, bool),
                  'boolean': lambda v: isinstance(v, bool),
                  'null': lambda v: v is None}
        return any(checks[name](value) for name in names)

    def walk(value, schema, where):
        if 'const' in schema and value != schema['const']:
            problems.append(f'{where}: expected {schema["const"]!r}')
            return
        if 'enum' in schema and value not in schema['enum']:
            problems.append(f'{where}: {value!r} is not one of '
                            f'{schema["enum"]}')
            return
        if 'type' in schema and not kind_ok(value, schema['type']):
            problems.append(f'{where}: expected {schema["type"]}')
            return
        if isinstance(value, dict):
            for key in schema.get('required', ()):
                if key not in value:
                    problems.append(f'{where}: missing {key}')
            for key, sub in (schema.get('properties') or {}).items():
                if key in value:
                    walk(value[key], sub, f'{where}.{key}')
            extra = schema.get('additionalProperties')
            if isinstance(extra, dict):
                known = schema.get('properties') or {}
                for key, item in value.items():
                    if key not in known:
                        walk(item, extra, f'{where}.{key}')
        if isinstance(value, list) and isinstance(schema.get('items'), dict):
            for index, item in enumerate(value):
                walk(item, schema['items'], f'{where}[{index}]')

    walk(document, JSON_SCHEMA, '$')
    return problems


# --------------------------------------------------------------------------- #
# Reading an OpenFOAM mesh on disk
# --------------------------------------------------------------------------- #

def _existing(path: Path) -> Path | None:
    if path.is_file():
        return path
    compressed = path.with_name(path.name + '.gz')
    return compressed if compressed.is_file() else None


def _read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == '.gz' else data


def _strip_comments(text: str) -> str:
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.DOTALL)
    return re.sub(r'//[^\n]*', ' ', text)


def _after_header(text: str) -> str:
    match = re.search(r'FoamFile\s*\{', text)
    if not match:
        return text
    depth, index = 1, match.end()
    while index < len(text) and depth:
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
        index += 1
    return text[index:]


def _statements(body: str) -> dict[str, str]:
    """``key value;`` statements of one flat dictionary body."""
    entries: dict[str, str] = {}
    depth, start = 0, 0
    for index, char in enumerate(body):
        if char in '({':
            depth += 1
        elif char in ')}':
            depth -= 1
        elif char == ';' and depth == 0:
            statement = body[start:index].strip()
            start = index + 1
            if not statement:
                continue
            parts = statement.split(None, 1)
            entries[parts[0]] = (re.sub(r'\s+', ' ', parts[1]).strip()
                                 if len(parts) > 1 else '')
    return entries


def parse_boundary(text: str) -> list[dict]:
    """The patches of a ``polyMesh/boundary`` file, in order.

    Each is ``{'name': ..., <key>: <value text>, ...}``; ``nFaces`` and
    ``startFace`` are integers.
    """
    body = _after_header(_strip_comments(text))
    open_list = body.find('(')
    if open_list < 0:
        return []
    patches = []
    depth, index, word_start = 0, open_list + 1, None
    name = ''
    while index < len(body):
        char = body[index]
        if char == '{':
            if depth == 0:
                name = body[word_start:index].split()[-1] \
                    if word_start is not None else ''
                body_start = index + 1
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                entry = _statements(body[body_start:index])
                record = {'name': name, **entry}
                for key in ('nFaces', 'startFace'):
                    try:
                        record[key] = int(entry.get(key, ''))
                    except ValueError:
                        record[key] = None
                patches.append(record)
                word_start = index + 1
        elif char == ')' and depth == 0:
            break
        elif depth == 0 and word_start is None and not char.isspace():
            word_start = index
        index += 1
    return patches


def read_boundary(poly_mesh: Path) -> list[dict]:
    path = _existing(Path(poly_mesh) / 'boundary')
    if path is None:
        return []
    return parse_boundary(_read_bytes(path).decode('utf-8', 'replace'))


def parse_region_properties(text: str) -> dict[str, str]:
    """``{region: 'fluid' | 'solid' | <other list name>}``, in file order."""
    body = _after_header(_strip_comments(text))
    match = re.search(r'\bregions\s*\((.*)\)\s*;', body, re.DOTALL)
    if not match:
        return {}
    result: dict[str, str] = {}
    for kind, names in re.findall(r'(\w+)\s*\(([^()]*)\)', match.group(1)):
        for name in names.split():
            result[name] = kind
    return result


#: A zone entry: its name on a line of its own, ``{``, then either a
#: ``type`` line (the zones FoamMesh's own writers add) or straight away its
#: label list. OpenFOAM 13 writes no ``type`` line, so the face zone
#: snappyHexMesh made for an internal face was read as no zone at all
#: (DP-6067).
_ZONE = re.compile(
    rb'(?m)^[ \t]*([^\s{}();/]+)[ \t]*\r?\n[ \t]*\{[ \t]*\r?\n[ \t]*'
    rb'(?:type[ \t]+(faceZone|cellZone|pointZone)[ \t]*;'
    rb'|(?:face|cell|point)Labels\b)')
_ZONE_SIZE = re.compile(rb'(?:faceLabels|cellLabels|pointLabels)\s+'
                        rb'List<label>\s+(\d+)')
#: The zone kind a zone file holds, for an entry that does not say.
_ZONE_KIND = {'faceZones': 'faceZone', 'cellZones': 'cellZone',
              'pointZones': 'pointZone'}


def read_zones(poly_mesh: Path, kind: str) -> list[dict]:
    """``[{'name', 'kind', 'size'}]`` of ``faceZones``/``cellZones``.

    Read by pattern, so an ASCII or binary zone file of any size is scanned
    without parsing its label lists.
    """
    path = _existing(Path(poly_mesh) / kind)
    if path is None:
        return []
    data = _read_bytes(path)
    zones = []
    for match in _ZONE.finditer(data):
        size = _ZONE_SIZE.search(data, match.start(), match.end() + 400)
        zone_kind = (match.group(2).decode('ascii') if match.group(2)
                     else _ZONE_KIND.get(path.name.split('.')[0], kind))
        zones.append({'name': match.group(1).decode('utf-8', 'replace'),
                      'kind': zone_kind,
                      'size': int(size.group(1)) if size else None})
    return zones


def _cell_count(poly_mesh: Path) -> int | None:
    path = _existing(Path(poly_mesh) / 'owner')
    if path is None:
        return None
    try:
        if path.suffix == '.gz':
            with gzip.open(path, 'rb') as handle:
                head = handle.read(4096)
        else:
            with path.open('rb') as handle:
                head = handle.read(4096)
    except OSError:
        return None
    match = re.search(rb'nCells:\s*(\d+)', head)
    if match:
        return int(match.group(1))
    return _counted_cells(path, head)


def _counted_cells(path: Path, head: bytes) -> int | None:
    """DP-6070. The cells of an ASCII ``owner`` with no ``note`` header.

    OpenFOAM's own writers put ``nCells:`` in the header; FoamMesh's Gmsh
    publisher and other meshers do not, and the hand-off said no cell count
    for every Gmsh export. Every cell owns a face, so the count is the
    largest owner label plus one. A binary list is not read.
    """
    import numpy as np

    if b'binary' in head.split(b'}', 1)[0]:
        return None
    try:
        data = (gzip.open(path, 'rb').read() if path.suffix == '.gz'
                else path.read_bytes())
    except OSError:
        return None
    body = data.split(b'}', 1)[-1]
    body = re.sub(rb'//[^\n]*', b'', body)
    start, end = body.find(b'('), body.rfind(b')')
    if start < 0 or end <= start:
        return None
    try:
        labels = np.array(body[start + 1:end].split(), dtype=np.int64)
    except ValueError:
        return None
    return int(labels.max()) + 1 if labels.size else 0


def mesh_regions(case_root: Path) -> list[tuple[str, Path, str]]:
    """``(region name, polyMesh folder, regionProperties list)`` per region.

    ``constant/regionProperties`` names the regions of a multi-region case;
    a one-region case is ``constant/polyMesh``, named :data:`DEFAULT_REGION`.
    """
    constant = Path(case_root) / 'constant'
    properties = _existing(constant / 'regionProperties')
    kinds = (parse_region_properties(
        _read_bytes(properties).decode('utf-8', 'replace'))
        if properties else {})
    if kinds:
        return [(name, constant / name / 'polyMesh', kind)
                for name, kind in kinds.items()
                if (constant / name / 'polyMesh').is_dir()]
    if _existing(constant / 'polyMesh' / 'boundary'):
        return [(DEFAULT_REGION, constant / 'polyMesh', '')]
    found = []
    if constant.is_dir():
        for item in sorted(constant.iterdir()):
            if item.is_dir() and _existing(item / 'polyMesh' / 'boundary'):
                found.append((item.name, item / 'polyMesh', ''))
    return found


def file_checksum(path: Path) -> dict:
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return {'sha256': digest.hexdigest(), 'bytes': Path(path).stat().st_size}


def _revision(files: Mapping[str, Mapping]) -> str:
    joined = '\n'.join(f'{name}:{files[name]["sha256"]}'
                       for name in sorted(files))
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()


# --------------------------------------------------------------------------- #
# FoamMesh's own records
# --------------------------------------------------------------------------- #

def _text(value) -> str:
    value = getattr(value, 'value', value)
    return '' if value is None else str(value).strip()


def project_document(db) -> dict:
    """The project document with its surface intent assessed, in memory.

    The same reading the meshers make (``core/gmsh/surface_roles``): a copy,
    migrated, so the project is not touched. ``{}`` for a database that
    holds no document (a test double, a project with no geometry).
    """
    content = getattr(db, 'data', None)
    if not callable(content):
        return {}
    try:
        document = copy.deepcopy(content() or {})
    except Exception:  # noqa: BLE001 - a db with no document has no intent
        return {}
    if not isinstance(document, dict):
        return {}
    try:
        from foammesh.db import surface_intent as si
        si.migrate_surface_intent(document, [])
    except Exception:  # noqa: BLE001 - unreadable intent is reported below
        pass
    return document


def shared_validation(db) -> list[dict] | None:
    """SR7's shared validation result for this project at export time.

    Plan 49 SR7: ``core/engine/surface_capabilities.export_verdict`` -- the
    same verdict the surface dialog shows and the export gate applied, one
    item per issue (subject, route, target, reason, remedy, code, text);
    ``[]`` when nothing is wrong, ``None`` when the case has no meshing
    route to judge (an imported mesh, no engine chosen).
    """
    from foammesh.core.engine.surface_capabilities import export_verdict
    return export_verdict(db, 'openfoam')


def _region_records(db) -> dict[str, dict]:
    """``{region name: {'id', 'type'}}`` of the regions the export split by:
    the project's region table, or on Gmsh its typed solids (DP-6058)."""
    from foammesh.core.import_export.export_regions import export_regions
    try:
        elements = export_regions(db)
    except Exception:  # noqa: BLE001
        return {}
    result = {}
    for key, element in (elements or {}).items():
        try:
            name = _text(element.value('name'))
        except Exception:  # noqa: BLE001
            continue
        try:
            kind = _text(element.value('type'))
        except Exception:  # noqa: BLE001
            kind = ''
        result[name] = {'id': str(key), 'type': kind}
    return result


def _intent_by_uuid(document, identity) -> dict[str, dict]:
    if not document or identity is None:
        return {}
    entries = [{'geometry_id': '', 'patches': [
        {'patch_uuid': item.patch_uuid, 'name': item.display_name}]}
        for item in identity.entries if item.patch_uuid]
    try:
        from foammesh.db import surface_intent as si
        return si.prepared_surface_intent(document, entries)
    except Exception:  # noqa: BLE001
        return {}


def _with_zone_only_surfaces(identity, source_case, document):
    """*identity* plus the internal faces the mesher made a face zone of.

    DP-6067. The patch-identity sidecar joins the mesher's *patches* to the
    prepared groups; a surface authored ``internalFace`` becomes no patch
    (snappyHexMesh writes it as ``faceZone <name>; faceType internal;``),
    so it had no entry, the hand-off listed no surface for it, no zone was
    traced to it and the internal-face check never ran: the authored face
    could vanish with the export passing. The case's own group manifest
    (``foammesh/geometry/group-manifest.json``, written by the case builder)
    names every prepared group with its patch uuid; the groups it lists that
    the sidecar does not, and that are authored ``internalFace``, are added
    here, in memory only (the sidecar on disk is the fidelity check's and
    stays a list of patches).
    """
    if identity is None or source_case is None or not document:
        return identity
    try:
        from foammesh.openfoam.case_builder import group_manifest_path
        manifest = json.loads(group_manifest_path(source_case).read_text(
            encoding='utf-8'))
    except (OSError, ValueError, ImportError):
        return identity
    groups = manifest.get('groups') if isinstance(manifest, dict) else None
    known_uuids = {item.patch_uuid for item in identity.entries
                   if item.patch_uuid}
    known_names = {item.solver_name for item in identity.entries}
    candidates = []
    for group in groups or ():
        if not isinstance(group, dict):
            continue
        token = _text(group.get('patch_uuid'))
        name = _text(group.get('solver_name'))
        if not token or not name or token in known_uuids \
                or name in known_names:
            continue
        candidates.append(pid.PatchIdentity(
            solver_name=name, patch_uuid=token,
            display_name=_text(group.get('display_name')) or name,
            category=_text(group.get('category')), origin='prepared'))
    if not candidates:
        return identity
    try:
        from foammesh.db import surface_intent as si
        intent = si.prepared_surface_intent(document, [
            {'geometry_id': '', 'patches': [
                {'patch_uuid': item.patch_uuid, 'name': item.display_name}]}
            for item in candidates])
    except Exception:  # noqa: BLE001 - unreadable intent adds nothing
        return identity
    added = tuple(item for item in candidates
                  if (intent.get(item.patch_uuid) or {}).get('role')
                  == INTERNAL)
    if not added:
        return identity
    import dataclasses
    return dataclasses.replace(identity, entries=identity.entries + added)


def _foam_word(name) -> str:
    return re.sub(r"""[\s"'/;{}]""", '', str(name)) or str(name)


def _region_name_for(document, reference: str, regions: Iterable[str],
                     bodies=None) -> str:
    """The exported region a relation's ``geometry:<key>`` side names, or
    the one its ``region:<name>`` side is (DP-6062).

    *bodies* is :func:`foammesh.core.geometry.body_regions.body_regions`:
    a body whose name is not a region's is matched by the region it holds."""
    reference = _text(reference)
    regions = list(regions)
    if reference.startswith('region:'):
        name = reference[len('region:'):]
        for candidate in (name, _foam_word(name),
                          re.sub(r'[^A-Za-z0-9_]', '_', name)):
            if candidate and candidate in regions:
                return candidate
        return ''
    if not reference.startswith('geometry:'):
        return ''
    row = ((document or {}).get('geometry') or {}).get(
        reference[len('geometry:'):])
    if not isinstance(row, dict):
        return ''
    name = _text(row.get('name'))
    regions = list(regions)
    for candidate in (name, _foam_word(name),
                      re.sub(r'[^A-Za-z0-9_]', '_', name)):
        if candidate and candidate in regions:
            return candidate
    found = (bodies or {}).get(reference[len('geometry:'):])
    return found if found in regions else ''


# --------------------------------------------------------------------------- #
# Building the OpenFOAM sidecar
# --------------------------------------------------------------------------- #

def _source(item: pid.PatchIdentity) -> dict:
    return {'patchUuid': item.patch_uuid, 'displayName': item.display_name,
            'solverName': item.solver_name, 'origin': item.origin}


def _relation_kind_word(kind: str) -> str:
    return {CONFORMAL: 'region interface (conformal)',
            NON_CONFORMAL: 'region interface (non-conformal)',
            PERIODIC: 'periodic pair'}.get(kind, kind)


def _producer() -> dict:
    try:
        from app_properties import APP_VERSION
    except Exception:  # noqa: BLE001
        APP_VERSION = ''
    return {'application': 'FoamMesh', 'version': str(APP_VERSION),
            'exportedAt': datetime.now(timezone.utc).strftime(
                '%Y-%m-%dT%H:%M:%SZ'),
            'identityVersion': pid.EXPORTED_IDENTITY_VERSION}


def _route(source_case, identity) -> str:
    engine = identity.engine_id if identity is not None else ''
    if not engine and source_case is not None:
        try:
            from foammesh.core.gmsh.manifest import accepted_run_engine
            kept = accepted_run_engine(source_case)
            engine = kept['engine_id'] if kept else ''
        except Exception:  # noqa: BLE001
            engine = ''
    return {'gmsh': 'gmsh', 'snappy': 'snappy',
            'reconstructPar': 'snappy'}.get(str(engine), 'unknown')


def build_openfoam_document(case_root, *, source_case=None, db=None,
                            couplings=None, route: str = '') -> dict:
    """The sidecar document for the exported OpenFOAM case at *case_root*.

    *source_case* is the meshing case (its patch-identity sidecar and run
    records); *db* the project (roles, relations, region table); *couplings*
    what the export coupled after the split. Each may be ``None``: the
    sidecar then describes the patches as they are, and says what it could
    not check.
    """
    case_root = Path(case_root)
    identity = (pid.read_case_identity(source_case)
                if source_case is not None else None)
    document = project_document(db) if db is not None else {}
    identity = _with_zone_only_surfaces(identity, source_case, document)
    intent = _intent_by_uuid(document, identity)
    region_table = _region_records(db) if db is not None else {}
    failures: list[dict] = []
    warnings: list[str] = []

    def fail(code, message, **where):
        failures.append({'code': code, 'message': message, **where})

    layout = mesh_regions(case_root)
    if not layout:
        fail('no_mesh', 'The exported case holds no polyMesh '
             '(constant/polyMesh or constant/<region>/polyMesh).')

    files: dict[str, dict] = {}
    regions, patches, zones = [], [], []
    region_names = [name for name, _path, _kind in layout]
    for name, poly_mesh, kind in layout:
        for item in MESH_FILES:
            found = _existing(poly_mesh / item)
            if found is not None:
                files[found.relative_to(case_root).as_posix()] = \
                    file_checksum(found)
        record = region_table.get(name) or (
            next(iter(region_table.values()))
            if len(layout) == 1 and len(region_table) == 1 else {})
        fluid_solid = (kind if kind in ('fluid', 'solid')
                       else _text(record.get('type')) or 'unknown')
        if fluid_solid not in ('fluid', 'solid'):
            fluid_solid = 'unknown'
        regions.append({
            'name': name, 'id': record.get('id'),
            'fluidSolid': fluid_solid,
            'meshPath': poly_mesh.relative_to(case_root).as_posix(),
            'cells': _cell_count(poly_mesh)})
        seen: set[str] = set()
        for entry in read_boundary(poly_mesh):
            patch_name = entry['name']
            if patch_name in seen:
                fail('duplicate_patch', f'Region {name} names patch '
                     f'{patch_name} twice.', region=name, patch=patch_name)
            seen.add(patch_name)
            patch_type = str(entry.get('type') or '')
            found = pid.exported_identity(
                identity, patch_name, patch_type=patch_type,
                original_patch=str(entry.get('originalPatch') or ''),
                region=name,
                neighbour_region=str(entry.get('neighbourRegion') or ''))
            parameters = {key: str(value) for key, value in entry.items()
                          if key not in ('name', 'type', 'nFaces',
                                         'startFace')}
            patches.append({
                'region': name, 'name': patch_name, 'type': patch_type,
                'nFaces': entry.get('nFaces') if entry.get('nFaces')
                is not None else 0,
                'startFace': entry.get('startFace'),
                'origin': found.generator,
                'side': found.side, 'basedOn': found.based_on,
                'auxiliary': found.auxiliary,
                'sources': [_source(item) for item in found.sources],
                'parameters': parameters,
                '_identity': found})
        for kind_name in ('faceZones', 'cellZones'):
            for zone in read_zones(poly_mesh, kind_name):
                sources = []
                if identity is not None:
                    match = identity.get(zone['name']) or next(
                        (item for item in identity.entries
                         if _foam_word(item.display_name) == zone['name']),
                        None)
                    if match is not None:
                        sources = [_source(match)]
                zones.append({'region': name, 'name': zone['name'],
                              'kind': zone['kind'], 'size': zone['size'],
                              'sources': sources})

    # Purpose and role, from the authored intent (else the published one).
    for patch in patches:
        found = patch.pop('_identity')
        source = found.sources[0] if found.sources else None
        role = purpose = purpose_source = None
        relation_ids: list[str] = []
        if source is not None:
            authored = intent.get(source.patch_uuid or '') or {}
            role = authored.get('role')
            purpose = authored.get('boundary_category') or None
            purpose_source = (authored.get('origin') or None) \
                if purpose else None
            relation_ids = [item['relation_id'] for item
                            in authored.get('relations') or ()
                            if item.get('relation_id')]
            if not purpose and source.category and \
                    found.generator in (pid.EXPORT_PUBLISHED,
                                        pid.EXPORT_BAFFLE_SECOND_SIDE):
                purpose = source.category
                purpose_source = ('background' if source.origin == 'background'
                                  else 'prepared')
        patch['identity'] = (source.origin if source is not None else '')
        patch['role'] = role
        patch['purpose'] = purpose
        patch['purposeSource'] = purpose_source
        patch['relationIds'] = relation_ids

    by_region = {name: {p['name']: p for p in patches if p['region'] == name}
                 for name in region_names}
    _check_pairs(patches, by_region, fail)

    surfaces = _surfaces(identity, intent, patches, zones)
    bodies = {}
    if document and len(region_names) > 1:
        from foammesh.core.geometry.body_regions import body_regions

        try:
            bodies = body_regions(document, source_case)
        except Exception:  # noqa: BLE001 - matched by name alone, as before
            bodies = {}
    relations = _relations(document, identity, couplings or [], patches,
                           region_names, bodies)
    if document or identity is not None:
        _check_intent(surfaces, relations, patches, zones, by_region,
                      couplings or [], fail, warnings)
        _attribute_split_patches(relations, patches, surfaces, identity)
    else:
        warnings.append(
            'No patch identity and no authored intent were available, so '
            'the export was checked as a mesh only (coupled patches and '
            'their partners), not against roles and relations.')
    unresolved = _unresolved(document)
    auxiliary = [{'region': p['region'], 'patch': p['name'],
                  'type': p['type']} for p in patches if p['auxiliary']]
    return {
        'schema': SCHEMA_ID,
        'schemaVersion': SCHEMA_VERSION,
        'producer': _producer(),
        'route': route or _route(source_case, identity),
        'target': 'openfoam',
        'mesh': {'format': 'openfoam-polyMesh', 'lengthUnit': 'm',
                 'files': files, 'revision': _revision(files) if files
                 else ''},
        'regions': regions,
        'patches': patches,
        'surfaces': surfaces,
        'relations': relations,
        'zones': zones,
        'auxiliaryPatches': auxiliary,
        'unsupported': [],
        'validation': {
            'status': 'failed' if failures else 'passed',
            'checkedAgainst': (
                'the authored roles and relations of the project at export, '
                'and the patch identity the mesher published'
                if document or identity is not None else 'the mesh only'),
            'failures': failures,
            'warnings': warnings,
            'unresolved': unresolved,
            'sharedValidation': shared_validation(db),
        },
    }


def _attribute_split_patches(relations, patches, surfaces,
                             identity) -> None:
    """Give ``splitMeshRegions``' mapped walls the surface they were cut at.

    The split names its pair ``<region>_to_<neighbour>`` after the regions,
    not after any surface, so the identity alone leaves them sourceless; the
    conformal relation that coupled them names the surface.
    """
    if identity is None:
        return
    by_uuid = {item.patch_uuid: item for item in identity.entries
               if item.patch_uuid}
    by_id = {relation['id']: relation for relation in relations}
    surfaces_by_uuid = {surface['patchUuid']: surface for surface in surfaces}
    for patch in patches:
        if patch['origin'] != pid.EXPORT_REGION_SPLIT or patch['sources']:
            continue
        for relation_id in patch['relationIds']:
            relation = by_id.get(relation_id)
            if relation is None or relation['kind'] != CONFORMAL:
                continue
            for token in dict.fromkeys(
                    relation[side]['patchUuid']
                    for side in ('owner', 'neighbour')):
                item = by_uuid.get(token or '')
                if item is None:
                    continue
                patch['sources'].append(_source(item))
                surface = surfaces_by_uuid.get(token)
                if surface is not None:
                    surface['exportedPatches'].append(
                        {'region': patch['region'], 'patch': patch['name'],
                         'type': patch['type']})


def _check_pairs(patches, by_region, fail) -> None:
    """Every coupled patch has its partner, and the partner points back."""
    for patch in patches:
        if patch['type'] not in _PAIRED_TYPES:
            continue
        parameters = patch['parameters']
        region = patch['region']
        other_region = (parameters.get('neighbourRegion')
                        or parameters.get('sampleRegion') or region)
        other_name = (parameters.get('neighbourPatch')
                      or parameters.get('samplePatch') or '')
        where = f'{patch["name"]} ({patch["type"]}) in region {region}'
        if not other_name:
            fail('unpaired_coupled_patch',
                 f'Patch {where} names no neighbour patch.',
                 region=region, patch=patch['name'])
            continue
        partner = by_region.get(other_region, {}).get(other_name)
        if partner is None:
            fail('unpaired_coupled_patch',
                 f'Patch {where} is coupled to {other_name} in region '
                 f'{other_region}, which the exported mesh does not hold.',
                 region=region, patch=patch['name'])
            continue
        back = partner['parameters']
        back_region = (back.get('neighbourRegion') or back.get('sampleRegion')
                       or other_region)
        back_name = back.get('neighbourPatch') or back.get('samplePatch') or ''
        if back_name != patch['name'] or back_region != region:
            fail('unpaired_coupled_patch',
                 f'Patch {where} is coupled to {other_name} in region '
                 f'{other_region}, which does not point back to it.',
                 region=region, patch=patch['name'])


def _surfaces(identity, intent, patches, zones) -> list[dict]:
    if identity is None:
        return []
    result = []
    for item in identity.entries:
        if not item.patch_uuid:
            continue
        authored = intent.get(item.patch_uuid) or {}
        exported = [{'region': p['region'], 'patch': p['name'],
                     'type': p['type']} for p in patches
                    if any(s['patchUuid'] == item.patch_uuid
                           for s in p['sources'])]
        face_zones = sorted({f'{z["region"]}/{z["name"]}' for z in zones
                             if z['kind'] == 'faceZone'
                             and any(s['patchUuid'] == item.patch_uuid
                                     for s in z['sources'])})
        result.append({
            'patchUuid': item.patch_uuid, 'displayName': item.display_name,
            'solverName': item.solver_name,
            'role': authored.get('role'),
            'purpose': authored.get('boundary_category') or item.category
            or None,
            'meshPatchType': authored.get('mesh_patch_type'),
            'state': authored.get('state'),
            'exportedPatches': exported, 'faceZones': face_zones})
    return result


def _relation_sides(relation, identity, couplings):
    """The published names of a relation's two sides (owner, neighbour)."""
    names = []
    for side in ('owner', 'neighbour'):
        token = _text(relation.get(f'{side}PatchUuid'))
        found = None
        if identity is not None and token:
            found = next((item for item in identity.entries
                          if item.patch_uuid == token), None)
        names.append(found.solver_name if found is not None else '')
    relation_id = _text(relation.get('relationId'))
    for coupling in couplings:
        if relation_id and relation_id in (
                _text(coupling.get('relation_id')),
                _text(coupling.get('relationId'))):
            names[0] = names[0] or _text(coupling.get('owner_patch'))
            names[1] = names[1] or _text(coupling.get('neighbour_patch'))
    return names


def _transform(relation) -> dict:
    keys = ('transform', 'matchTolerance', 'translationX', 'translationY',
            'translationZ', 'rotationCentreX', 'rotationCentreY',
            'rotationCentreZ', 'rotationAxisX', 'rotationAxisY',
            'rotationAxisZ', 'rotationAngleDegrees')
    return {key: getattr(relation.get(key), 'value', relation.get(key))
            for key in keys if key in relation}


def _relations(document, identity, couplings, patches,
               region_names, bodies=None) -> list[dict]:
    result = []
    for key, relation in sorted(
            ((document or {}).get('surfaceRelations') or {}).items(),
            key=lambda item: str(item[0])):
        if not isinstance(relation, dict):
            continue
        kind = _text(relation.get('kind'))
        if kind not in (CONFORMAL, NON_CONFORMAL, PERIODIC):
            continue
        owner, neighbour = _relation_sides(relation, identity, couplings)
        sides = {}
        for side, name in (('owner', owner), ('neighbour', neighbour)):
            sides[side] = {
                'patchUuid': _text(relation.get(f'{side}PatchUuid')) or None,
                'displayName': _text(relation.get(f'{side}Label')),
                'solverName': name,
                'region': (_region_name_for(
                    document, relation.get(f'{side}Region'), region_names,
                    bodies)
                    or _text(relation.get(f'{side}Region'))),
            }
        result.append({
            'id': _text(relation.get('relationId')) or str(key),
            'name': _text(relation.get('name')) or str(key),
            'kind': kind,
            'state': _text(relation.get('state')) or VALID,
            'shared': bool(getattr(relation.get('sharedSurface'), 'value',
                                   relation.get('sharedSurface'))),
            'transform': _transform(relation) if kind == PERIODIC else {},
            'owner': sides['owner'], 'neighbour': sides['neighbour'],
            'exportedPatches': [],
            'coupled': None,
        })
    return result


def _settle_side_regions(relation, exported, all_regions) -> None:
    """Name each side by the region its exported patches are in.

    DP-6057. A side is authored against the regions SR1 records -- a
    cell-zone volume or ``surrounding`` -- and regions a seed made are
    neither, so a coupled relation could reach the hand-off with sides named
    ``geometry:1`` and ``surrounding``, which no region of the mesh is. Plan
    49 §5 asks for region-qualified sides. A side whose authored region is
    not an exported region takes the region of the pair it was exported as,
    and keeps what was authored as ``authoredRegion``; a side that names an
    exported region keeps it and orients the pair.
    """
    owner, neighbour = relation['owner'], relation['neighbour']
    known = set(all_regions)
    if owner['region'] in known and neighbour['region'] in known:
        return
    if relation['kind'] == NON_CONFORMAL:
        found = {side: {p['region'] for p in exported
                        if p['parameters'].get('originalPatch')
                        == relation[side]['solverName']}
                 for side in ('owner', 'neighbour')}
        pair = {side: next(iter(regions)) for side, regions in found.items()
                if len(regions) == 1}
    else:
        regions = list(dict.fromkeys(p['region'] for p in exported))
        if len(regions) == 1:
            pair = {'owner': regions[0], 'neighbour': regions[0]}
        elif len(regions) == 2:
            first, second = regions
            if neighbour['region'] == first or owner['region'] == second:
                first, second = second, first
            pair = {'owner': first, 'neighbour': second}
        else:
            pair = {}
    for side, record in (('owner', owner), ('neighbour', neighbour)):
        region = pair.get(side)
        if region and record['region'] not in known:
            record['authoredRegion'] = record['region']
            record['region'] = region


def _ncc_on(patches, original: str, types) -> list[dict]:
    return [p for p in patches if p['type'] in types
            and p['parameters'].get('originalPatch') == original]


def _check_intent(surfaces, relations, patches, zones, by_region, couplings,
                  fail, warnings) -> None:
    """Hold the exported mesh against the authored roles and relations."""
    names_anywhere: dict[str, list[dict]] = {}
    for patch in patches:
        names_anywhere.setdefault(patch['name'], []).append(patch)
    periodic_sides = set()
    for relation in relations:
        if relation['kind'] == PERIODIC:
            periodic_sides.update(
                relation[side]['patchUuid'] for side in ('owner', 'neighbour'))

    for surface in surfaces:
        if surface.get('state') not in (None, VALID):
            continue
        role = surface.get('role')
        name = surface['solverName']
        label = surface['displayName'] or name
        found = names_anywhere.get(name, [])
        if role == EXTERNAL:
            if not found:
                fail('missing_boundary',
                     f'Surface {label}: an external boundary, but the '
                     f'exported mesh holds no patch {name}.',
                     surface=surface['patchUuid'], patch=name)
                continue
            expected = surface.get('meshPatchType')
            if not expected and (surface.get('purpose') == 'wall'):
                expected = 'wall'
            if expected and surface['patchUuid'] not in periodic_sides:
                wrong = [p for p in found if p['type'] != expected]
                for patch in wrong:
                    fail('wrong_type',
                         f'Surface {label}: authored as type {expected}, '
                         f'exported as {patch["type"]} ({name} in region '
                         f'{patch["region"]}).',
                         surface=surface['patchUuid'], patch=name,
                         region=patch['region'])
        elif role == BAFFLE:
            second = names_anywhere.get(f'{name}_slave', [])
            regions = {p['region'] for p in found} & \
                {p['region'] for p in second}
            if not found or not second or not regions:
                fail('baffle_incomplete',
                     f'Surface {label}: a blocking baffle needs its two '
                     f'sides {name} and {name}_slave in one region; the '
                     f'exported mesh holds '
                     f'{", ".join(sorted({p["name"] for p in found + second})) or "neither"}.',
                     surface=surface['patchUuid'], patch=name)
                continue
            for patch in found + second:
                if patch['type'] != 'wall':
                    fail('wrong_type',
                         f'Surface {label}: a blocking baffle side must be '
                         f'a wall; {patch["name"]} in region '
                         f'{patch["region"]} is {patch["type"]}.',
                         surface=surface['patchUuid'], patch=patch['name'],
                         region=patch['region'])
        elif role == INTERNAL:
            if found or names_anywhere.get(f'{name}_slave'):
                fail('internal_face_is_boundary',
                     f'Surface {label}: an internal face, but the exported '
                     f'mesh holds it as a boundary patch {name}.',
                     surface=surface['patchUuid'], patch=name)
            if not surface['faceZones']:
                fail('internal_face_missing_zone',
                     f'Surface {label}: an internal face, but the exported '
                     f'mesh holds no face zone for it.',
                     surface=surface['patchUuid'])

    all_regions = list(by_region)
    for relation in relations:
        if relation['state'] != VALID:
            continue
        kind = relation['kind']
        owner = relation['owner']['solverName']
        neighbour = relation['neighbour']['solverName']
        label = (f'{_relation_kind_word(kind)} {relation["name"]}')
        if kind in (NON_CONFORMAL, PERIODIC) and not (owner and neighbour):
            warnings.append(
                f'{label[0].upper()}{label[1:]}: its sides could not be '
                'named from the patch identity or the recorded couplings, '
                'so its coupling was not checked by side.')
            continue
        exported: list[dict] = []
        coupled = False
        if kind == NON_CONFORMAL:
            first = _ncc_on(patches, owner, ('nonConformalMappedWall',))
            second = _ncc_on(patches, neighbour, ('nonConformalMappedWall',))
            exported = first + second
            coupled = bool(first and second and {p['region'] for p in first}
                           .isdisjoint({p['region'] for p in second}))
        elif kind == PERIODIC:
            ncc = (_ncc_on(patches, owner, ('nonConformalCyclic',))
                   + _ncc_on(patches, neighbour, ('nonConformalCyclic',)))
            cyclic = [p for p in patches if p['type'] == 'cyclic'
                      and p['name'] in (owner, neighbour)
                      and p['parameters'].get('neighbourPatch')
                      in (owner, neighbour)]
            exported = ncc or cyclic
            coupled = (len({p['parameters'].get('originalPatch')
                            for p in ncc}) == 2 or len(cyclic) == 2)
        elif kind == CONFORMAL:
            wanted = {relation['owner']['region'],
                      relation['neighbour']['region']} & set(all_regions)
            mapped = [p for p in patches if p['type'] == 'mappedWall'
                      and p['parameters'].get('neighbourRegion')
                      and p['parameters'].get('neighbourRegion')
                      != p['region']]
            # DP-6061. A relation is one pair of regions. With a third, a
            # mapped patch touching one authored region may be another
            # interface's: the steel of the CHT tube meets both fluids, and
            # every relation claimed all four of its patches. Both sides
            # known: that pair only. Otherwise the pairs the known side is
            # in, and only when that is one pair.
            pairs = {frozenset((p['region'],
                                p['parameters'].get('neighbourRegion')))
                     for p in mapped}
            if len(wanted) == 2:
                pairs = {pair for pair in pairs if pair == wanted}
            elif wanted:
                pairs = {pair for pair in pairs if wanted <= pair}
            if len(pairs) > 1:
                fail('relation_ambiguous',
                     f'{label[0].upper()}{label[1:]}: its sides name '
                     + (f'only the region {next(iter(wanted))}'
                        if wanted else 'no exported region')
                     + ', and the exported mesh couples '
                     + ', '.join(sorted(' and '.join(sorted(pair))
                                        for pair in pairs))
                     + '. Name both regions on the relation, then export.',
                     relation=relation['id'])
                relation['exportedPatches'] = []
                relation['coupled'] = False
                continue
            mapped = [p for p in mapped
                      if frozenset((p['region'],
                                    p['parameters'].get('neighbourRegion')))
                      in pairs]
            exported = mapped
            coupled = len(all_regions) > 1 and len(mapped) >= 2
        relation['exportedPatches'] = [
            {'region': p['region'], 'patch': p['name'], 'type': p['type']}
            for p in exported]
        relation['coupled'] = coupled
        if coupled:
            _settle_side_regions(relation, exported, all_regions)
        for patch in exported:
            if relation['id'] not in patch['relationIds']:
                patch['relationIds'].append(relation['id'])
        if not coupled:
            fail('relation_not_coupled',
                 f'{label[0].upper()}{label[1:]}: authored, but the exported '
                 f'mesh does not couple it'
                 + (f' (sides {owner} and {neighbour})'
                    if owner or neighbour else '')
                 + '. '
                 + ('A non-conformal pair is coupled only when OpenFOAM is the '
                    'selected target: select it, mesh the case again with the '
                    'relation in place, then export.'
                    if kind == NON_CONFORMAL else
                    'Mesh the case again with the relation in place, then '
                    'export.'),
                 relation=relation['id'])

    for coupling in couplings:
        kind = _text(coupling.get('kind'))
        name = _text(coupling.get('name')) or 'coupling'
        types = (('nonConformalMappedWall',) if kind == 'non_conformal'
                 else ('nonConformalCyclic',))
        sides = [_text(coupling.get('owner_patch')),
                 _text(coupling.get('neighbour_patch'))]
        missing = [side for side in sides if side
                   and not _ncc_on(patches, side, types)]
        if missing:
            fail('coupling_patch_missing',
                 f'Coupling {name}: the exported mesh holds no '
                 f'{types[0]} patch made from {", ".join(missing)}; the '
                 'coupled patches (empty by design) must be kept.',
                 coupling=name)


def _unresolved(document) -> list[dict]:
    items = []
    for key, row in sorted(((document or {}).get('geometry') or {}).items(),
                           key=lambda item: str(item[0])):
        if isinstance(row, dict) and _text(row.get('intentState')) \
                == 'unresolved':
            items.append({'surface': _text(row.get('name')) or str(key),
                          'patchUuid': _text(row.get('patchUuid')) or None,
                          'reason': _text(row.get('intentIssue'))})
    for key, relation in sorted(
            ((document or {}).get('surfaceRelations') or {}).items(),
            key=lambda item: str(item[0])):
        if isinstance(relation, dict) and _text(relation.get('state')) \
                not in ('', VALID):
            items.append({'relation': _text(relation.get('relationId'))
                          or str(key),
                          'name': _text(relation.get('name')),
                          'kind': _text(relation.get('kind')),
                          'reason': _text(relation.get('stateReason'))})
    return items


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(document, indent=2, sort_keys=False)
                         + '\n', encoding='utf-8', newline='\n')
    os.replace(temporary, path)


def _raise_if_failed(result: HandoffResult, what: str) -> HandoffResult:
    if result.passed:
        return result
    lines = '; '.join(item['message'] for item in result.failures)
    raise HandoffVerificationError(
        f'The {what} does not carry what was authored, so it is not reported '
        f'as exported: {lines} (details in {result.summary()["path"]}).',
        result)


def write_openfoam_handoff(case_root, *, source_case=None, db=None,
                           couplings=None, route: str = '',
                           raise_on_failure: bool = True) -> HandoffResult:
    """Write ``<case_root>/constant/foammesh_handoff.json`` and verify.

    Run once, at the very end of the OpenFOAM export, after the last utility
    and the void-patch sweep, so the checksums are of the files as delivered.
    Raises :class:`HandoffVerificationError` (after writing) when the export
    does not carry the authored intent.
    """
    case_root = Path(case_root)
    document = build_openfoam_document(
        case_root, source_case=source_case, db=db, couplings=couplings,
        route=route)
    path = case_root / 'constant' / OPENFOAM_SIDECAR
    _write_json(path, document)
    result = HandoffResult(path, document, f'constant/{OPENFOAM_SIDECAR}')
    return _raise_if_failed(result, 'exported mesh') \
        if raise_on_failure else result


# --------------------------------------------------------------------------- #
# SU2
# --------------------------------------------------------------------------- #

_MARKER = re.compile(r'MARKER_TAG\s*=\s*(\S+)\s*\r?\n\s*MARKER_ELEMS\s*=\s*'
                     r'(\d+)')


def su2_markers(path: Path) -> list[tuple[str, int]]:
    """``(marker, element count)`` per ``MARKER_TAG`` of an SU2 file."""
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    return [(name, int(count)) for name, count in _MARKER.findall(text)]


def run_surface_intent(run: Mapping | None) -> dict:
    """The ``surfaceIntent`` block of a kept Gmsh run's own ``job.json``."""
    if not run:
        return {}
    root = Path(str(run.get('run_path') or ''))
    try:
        job = json.loads((root / 'job.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    block = ((job.get('intent') or {}).get('surfaceIntent') or {}) \
        if isinstance(job, dict) else {}
    return block if isinstance(block, dict) else {}


def su2_intent_refusal(case_path, run: Mapping | None) -> str:
    """Why the authored intent of the run cannot be exported as SU2, or ``''``.

    DP-5765. SU2's file is one zone of markers (SR0 probe 6): a region
    interface, conformal or not, and an internal face have no form in it,
    so the export is refused and the reason names each one. A periodic pair
    and a blocking baffle are not refused: their markers are written and the
    coupling is the SU2 configuration's (:func:`su2_notes`).
    """
    block = run_surface_intent(run)
    named = []
    for relation in block.get('relations') or ():
        if not isinstance(relation, dict):
            continue
        if relation.get('kind') in (CONFORMAL, NON_CONFORMAL) and \
                relation.get('state', VALID) == VALID:
            named.append(f'{_relation_kind_word(relation["kind"])} '
                         f'{relation.get("name") or relation.get("relationId")}')
    for surface in block.get('surfaces') or ():
        if isinstance(surface, dict) and surface.get('role') == INTERNAL \
                and surface.get('state', VALID) == VALID:
            named.append(f'internal face {surface.get("name")}')
    try:
        from foammesh.core.gmsh.role_publication import recorded_couplings
        recorded = recorded_couplings(case_path) if case_path else []
    except Exception:  # noqa: BLE001
        recorded = []
    for coupling in recorded:
        label = (f'region interface (non-conformal) '
                 f'{coupling.get("name") or coupling.get("relationId")}')
        if coupling.get('kind') == 'non_conformal' and not any(
                str(coupling.get('name') or '') in item for item in named):
            named.append(label)
    if not named:
        return ''
    return ('SU2 cannot express what this mesh was authored with: '
            + ', '.join(dict.fromkeys(named))
            + '. An SU2 file is one zone of boundary markers; a region '
              'interface needs one zone per region and an internal face is '
              'not a boundary, so writing markers for them would drop the '
              'coupling without a word. Export OpenFOAM instead, or remove '
              'the interface / internal face and mesh again for SU2.')


def su2_notes(run: Mapping | None) -> list[str]:
    """What an SU2 export result says beside its markers."""
    notes = [MARKERS_ARE_NOT_COUPLING]
    for relation in run_surface_intent(run).get('relations') or ():
        if not isinstance(relation, dict) or relation.get('kind') != PERIODIC:
            continue
        name = relation.get('name') or relation.get('relationId')
        owner = relation.get('ownerName') or relation.get('ownerScope')
        neighbour = relation.get('neighbourName') \
            or relation.get('neighbourScope')
        centre = ' '.join(f'{v:g}' for v in relation.get('rotationCentre')
                          or (0.0, 0.0, 0.0))
        axis = relation.get('rotationAxis') or (0.0, 0.0, 1.0)
        angle = float(relation.get('rotationAngleDegrees') or 0.0)
        angles = ' '.join(f'{float(v) * angle:g}' for v in axis) \
            if relation.get('transform') in ('rotation', 'rotational') \
            else '0 0 0'
        shift = ' '.join(f'{float(v):g}' for v in relation.get('translation')
                         or (0.0, 0.0, 0.0))
        notes.append(
            f'Periodic pair {name}: the file holds the two markers {owner} '
            f'and {neighbour}, not their coupling. Set it in the SU2 '
            f'configuration: MARKER_PERIODIC= ( {owner}, {neighbour}, '
            f'{centre}, {angles}, {shift} ) (rotation centre, rotation '
            f'angles in degrees, translation).')
    return notes


def write_su2_handoff(su2_path, *, case_path=None, run: Mapping | None = None,
                      notes: Iterable[str] = ()) -> HandoffResult:
    """Write ``<stem>.foammesh_handoff.json`` beside an exported ``.su2``."""
    su2_path = Path(su2_path)
    identity = pid.read_case_identity(case_path) if case_path else None
    block = run_surface_intent(run)
    roles = {str(item.get('name')): item for item in block.get('surfaces')
             or () if isinstance(item, dict)}
    patches, failures = [], []
    names = set()
    for marker, count in su2_markers(su2_path):
        found = pid.exported_identity(identity, marker)
        source = found.sources[0] if found.sources else None
        authored = roles.get(marker) or roles.get(found.based_on) or {}
        if marker in names:
            failures.append({'code': 'duplicate_patch',
                             'message': f'The SU2 file names marker {marker} '
                                        'twice.', 'patch': marker})
        names.add(marker)
        patches.append({
            'region': DEFAULT_REGION, 'name': marker, 'type': 'marker',
            'nFaces': count, 'startFace': None, 'origin': found.generator,
            'identity': source.origin if source is not None else '',
            'side': found.side, 'basedOn': found.based_on,
            'auxiliary': False,
            'sources': [_source(item) for item in found.sources],
            'role': authored.get('role') or None,
            'purpose': (authored.get('boundaryCategory')
                        or (source.category if source is not None else '')
                        or None),
            'purposeSource': ('authored' if authored.get('boundaryCategory')
                              else 'prepared' if source is not None
                              else None),
            'relationIds': [], 'parameters': {}})
    unsupported = []
    for relation in block.get('relations') or ():
        if isinstance(relation, dict) and relation.get('kind') == PERIODIC:
            unsupported.append({
                'relation': relation.get('relationId'),
                'name': relation.get('name'), 'kind': PERIODIC,
                'reason': 'SU2 carries the two markers; the periodic '
                          'coupling is MARKER_PERIODIC in the SU2 '
                          'configuration, which FoamMesh does not write.'})
    unsupported.append({'kind': 'regions', 'reason':
                        'An SU2 file is one zone; volume (region) identity '
                        'is not carried.'})
    relations = []
    for relation in block.get('relations') or ():
        if not isinstance(relation, dict):
            continue
        relations.append({
            'id': str(relation.get('relationId') or ''),
            'name': str(relation.get('name') or ''),
            'kind': relation.get('kind'), 'state': relation.get('state', VALID),
            'transform': {key: relation.get(key) for key in (
                'transform', 'translation', 'rotationCentre', 'rotationAxis',
                'rotationAngleDegrees') if key in relation},
            'owner': {'solverName': relation.get('ownerName'),
                      'patchUuid': relation.get('ownerScope')},
            'neighbour': {'solverName': relation.get('neighbourName'),
                          'patchUuid': relation.get('neighbourScope')},
            'exportedPatches': [], 'coupled': False})
    surfaces = []
    if identity is not None:
        for item in identity.entries:
            if item.patch_uuid:
                surfaces.append({
                    'patchUuid': item.patch_uuid,
                    'displayName': item.display_name,
                    'solverName': item.solver_name,
                    'role': (roles.get(item.solver_name) or {}).get('role'),
                    'purpose': item.category or None,
                    'meshPatchType': None,
                    'exportedPatches': [
                        {'region': DEFAULT_REGION, 'patch': p['name'],
                         'type': 'marker'} for p in patches
                        if any(s['patchUuid'] == item.patch_uuid
                               for s in p['sources'])],
                    'faceZones': []})
    files = {su2_path.name: file_checksum(su2_path)}
    document = {
        'schema': SCHEMA_ID, 'schemaVersion': SCHEMA_VERSION,
        'producer': _producer(), 'route': 'gmsh', 'target': 'su2',
        'mesh': {'format': 'su2', 'lengthUnit': 'm', 'files': files,
                 'revision': _revision(files)},
        'regions': [{'name': DEFAULT_REGION, 'id': None,
                     'fluidSolid': 'unknown', 'meshPath': su2_path.name,
                     'cells': None}],
        'patches': patches, 'surfaces': surfaces, 'relations': relations,
        'zones': [], 'auxiliaryPatches': [], 'unsupported': unsupported,
        'validation': {
            'status': 'failed' if failures else 'passed',
            'checkedAgainst': 'the markers of the SU2 file and the surface '
                              'roles of the Gmsh run that wrote it',
            'failures': failures, 'warnings': list(notes),
            'unresolved': [], 'sharedValidation': None},
    }
    path = su2_path.with_name(su2_path.stem + SU2_SIDECAR_SUFFIX)
    _write_json(path, document)
    return HandoffResult(path, document, path.name)
