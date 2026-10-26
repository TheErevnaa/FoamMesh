"""What a mesh is known to have come from, and what was never measured.

Plan 48 GM12, section 4.14 (review point 9).

Several of the common viewport tools need facts a bare file does not hold: the
layers a run asked for, the elements a run failed, the route and settings a
mesh was made with, the CAD it was meshed from.  A ``.msh`` or an SU2 file from
outside has none of it.  Two rules follow, and this module is where both live:

* **The project stores the facts with each mesh it makes** (:func:`record_mesh`),
  keyed by a fingerprint of the mesh file, so a record can never be read for a
  mesh that was changed or replaced afterwards.
* **A fact that was not measured is unavailable, with its reason**
  (:class:`Value`).  It is never a zero and never a guess.  Every tool that
  consumes a fact asks for a :class:`Value`, shows the number when the status is
  ``measured`` and the reason when it is ``unavailable``.

The store is one JSON file, ``<root>/foammesh/provenance.json``; *root* is the
project's case root or its native store root.  No Qt, no Gmsh.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

VERSION = 1
FILE = Path('foammesh') / 'provenance.json'

MEASURED = 'measured'
UNAVAILABLE = 'unavailable'

#: The reasons the tools state when a fact is not there (one sentence each).
NO_RECORD = ('This mesh was not made by this project, so no record of how it '
             'was made exists.')
CHANGED = ('The project\'s record belongs to an earlier version of this file; '
           'the mesh has changed since, so the record is not used.')
NO_LAYER_REQUEST = ('The requested layer count is unknown: the mesh came from '
                    'outside the project (or the run recorded none).')
NO_FAILURE_TAGS = ('The run\'s own failure tags are unavailable: this mesh '
                   'was not made by this project.')
NO_CAD = ('The CAD the mesh was made from is unknown: the mesh came from '
          'outside the project.')


@dataclass(frozen=True)
class Value:
    """One fact: measured (``value``) or unavailable (``reason``), never both."""

    status: str
    value: object = None
    reason: str = ''
    source: str = ''

    @property
    def is_measured(self) -> bool:
        return self.status == MEASURED

    def to_dict(self) -> dict:
        if self.status == MEASURED:
            return {'status': MEASURED, 'value': self.value,
                    'source': self.source}
        return {'status': UNAVAILABLE, 'value': None, 'reason': self.reason}

    def or_text(self, fallback: str | None = None) -> str:
        """A sentence for a status line: the value, or why there is none."""
        if self.status == MEASURED:
            return str(self.value)
        return fallback if fallback is not None else self.reason


def measured(value, source: str = '') -> Value:
    return Value(MEASURED, value, '', source)


def unavailable(reason: str) -> Value:
    if not str(reason or '').strip():
        raise ValueError('an unavailable value must say why')
    return Value(UNAVAILABLE, None, str(reason).strip())


# -- the same two shapes as plain dicts, for the local API ---------------------- #
#
# Plan 48 wave-2 merge. ``core/facade/results.py`` carried a stub of this
# contract for GM13's study routes; it now builds its entries here, so the
# viewport tools and the local API state "measured" and "unavailable" one way.

def measured_entry(value, *, source: str = '', unit: str | None = None,
                   **extra) -> dict:
    """``{'status': 'measured', 'value', 'source'[, 'unit']}`` plus *extra*."""
    entry = {'status': MEASURED, 'value': value, 'source': source}
    if unit:
        entry['unit'] = unit
    entry.update(extra)
    return entry


def unavailable_entry(reason: str, *, source: str = '', **extra) -> dict:
    """``{'status': 'unavailable', 'reason'}`` (and *source* when known)."""
    entry = {'status': UNAVAILABLE, 'reason': str(reason)}
    if source:
        entry['source'] = source
    entry.update(extra)
    return entry


def is_measured_entry(entry) -> bool:
    return isinstance(entry, dict) and entry.get('status') == MEASURED


def finite_or_unavailable(number, reason: str, source: str = '') -> Value:
    """*number* as measured when it is a finite number, else unavailable."""
    try:
        value = float(number)
    except (TypeError, ValueError):
        return unavailable(reason)
    if value != value or value in (float('inf'), float('-inf')):
        return unavailable(reason)
    return measured(value, source)


# -- fingerprints -------------------------------------------------------------- #

_CHUNK = 4 * 1024 * 1024


def fingerprint_file(path) -> dict:
    """``{'path', 'bytes', 'fingerprint'}``: size and the hash of the file's
    first and last 4 MiB, so a large mesh is identified without being read
    whole.  A different size always gives a different fingerprint."""
    path = Path(path)
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode('ascii'))
    with open(path, 'rb') as handle:
        digest.update(handle.read(_CHUNK))
        if size > 2 * _CHUNK:
            handle.seek(size - _CHUNK)
            digest.update(handle.read(_CHUNK))
    return {'path': str(path), 'bytes': size,
            'fingerprint': digest.hexdigest()[:32]}


def settings_fingerprint(settings) -> str:
    """A stable short hash of a settings document."""
    text = json.dumps(settings if settings is not None else {},
                      sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


# -- the store ----------------------------------------------------------------- #

def store_path(root) -> Path:
    return Path(root) / FILE


def _load(root) -> dict:
    try:
        document = json.loads(store_path(root).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'version': VERSION, 'records': {}}
    if not isinstance(document, dict) or not isinstance(
            document.get('records'), dict):
        return {'version': VERSION, 'records': {}}
    return document


def _save(root, document: dict) -> None:
    target = store_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(document, indent=1, sort_keys=True),
                         encoding='utf-8')
    os.replace(temporary, target)


def record_mesh(root, mesh_path, *, route: str, settings=None,
                requested_layers=None, failed_elements=None,
                cad_reference=None) -> dict:
    """Store how the mesh at *mesh_path* was made, and return the record.

    *requested_layers* is ``{patch: layers asked for}``; *failed_elements*
    is ``{source: {...}}`` with the source ``'gmsh'`` or ``'checkMesh'`` (the
    elements or sets that run flagged); *cad_reference* is the path of the CAD
    file (fingerprinted here) or ``None``.  A second record for the same mesh
    replaces the first.
    """
    mesh = fingerprint_file(mesh_path)
    record = {
        'mesh': mesh, 'route': str(route),
        'settings_fingerprint': settings_fingerprint(settings),
        'requested_layers': (
            None if requested_layers is None else
            {str(k): int(v) for k, v in dict(requested_layers).items()}),
        'failed_elements': {
            str(k): v for k, v in dict(failed_elements or {}).items()},
        'cad_reference': None,
        'recorded': time.strftime('%Y-%m-%dT%H:%M:%S'),
    }
    if cad_reference:
        try:
            record['cad_reference'] = fingerprint_file(cad_reference)
        except OSError:
            record['cad_reference'] = {'path': str(cad_reference),
                                       'bytes': None, 'fingerprint': None}
    document = _load(root)
    document['records'][mesh['fingerprint']] = record
    document['latest'] = mesh['fingerprint']
    _save(root, document)
    return record


def add_failed_elements(root, mesh_path, source: str, failed) -> bool:
    """Add one source's failed elements to the mesh's record (a later check).

    ``False`` when the project has no record for exactly this mesh file.
    """
    key = fingerprint_file(mesh_path)['fingerprint']
    document = _load(root)
    record = document['records'].get(key)
    if record is None:
        return False
    record.setdefault('failed_elements', {})[str(source)] = failed
    _save(root, document)
    return True


@dataclass(frozen=True)
class Lookup:
    """The record found for a mesh file, or why none was."""

    record: dict | None
    reason: str = ''
    root: str = ''

    @property
    def found(self) -> bool:
        return self.record is not None


def find(roots, mesh_path) -> Lookup:
    """The record for *mesh_path* among the stores in *roots*.

    Matches on the file's fingerprint: a record for another mesh, or for an
    earlier version of this file, is never returned.  The reason says which.
    """
    try:
        key = fingerprint_file(mesh_path)['fingerprint']
    except OSError:
        return Lookup(None, 'The mesh file cannot be read.')
    name = Path(mesh_path).name
    stale = False
    for root in roots or ():
        if root is None:
            continue
        records = _load(root)['records']
        if key in records:
            return Lookup(records[key], '', str(root))
        for other in records.values():
            if Path(str((other.get('mesh') or {}).get('path', ''))).name == name:
                stale = True
    return Lookup(None, CHANGED if stale else NO_RECORD)


# -- the facts, as measured-or-unavailable ------------------------------------- #

def route_of(lookup: Lookup) -> Value:
    if not lookup.found:
        return unavailable(lookup.reason)
    return measured(lookup.record.get('route'), 'project record')


def requested_layers(lookup: Lookup) -> Value:
    """``{patch: layers asked for}``, or unavailable with the reason."""
    if not lookup.found:
        return unavailable(lookup.reason or NO_LAYER_REQUEST)
    layers = lookup.record.get('requested_layers')
    if layers is None:
        return unavailable(NO_LAYER_REQUEST)
    return measured(dict(layers), 'project record')


def failed_elements(lookup: Lookup, source: str) -> Value:
    """What *source* (``'gmsh'`` or ``'checkMesh'``) flagged in the run."""
    if not lookup.found:
        return unavailable(lookup.reason or NO_FAILURE_TAGS)
    held = (lookup.record.get('failed_elements') or {})
    if source not in held:
        return unavailable(
            'The run recorded no %s failure tags for this mesh.' % source)
    return measured(held[source], 'project record')


def cad_reference(lookup: Lookup) -> Value:
    if not lookup.found:
        return unavailable(lookup.reason or NO_CAD)
    cad = lookup.record.get('cad_reference')
    if not cad or not cad.get('path'):
        return unavailable(NO_CAD)
    return measured(dict(cad), 'project record')


def settings_of(lookup: Lookup) -> Value:
    if not lookup.found:
        return unavailable(lookup.reason)
    return measured(lookup.record.get('settings_fingerprint'),
                    'project record')


def describe(roots, mesh_path) -> dict:
    """Every fact about a mesh's origin, each as measured or unavailable."""
    lookup = find(roots, mesh_path)
    return {
        'mesh': str(mesh_path),
        'recorded': lookup.found,
        'route': route_of(lookup).to_dict(),
        'settings_fingerprint': settings_of(lookup).to_dict(),
        'requested_layers': requested_layers(lookup).to_dict(),
        'failed_elements': {
            source: failed_elements(lookup, source).to_dict()
            for source in ('gmsh', 'checkMesh')},
        'cad_reference': cad_reference(lookup).to_dict(),
    }
