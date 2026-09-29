"""Which detection an apply is applying: Plan 36 RP13 #3.

"How many fluid regions?" is two calls: detect answers spaces by number, and
apply writes the ones picked. Between them the geometry, the domain box or
the solids can change, and the numbers then name other spaces -- or none.
RP7's apply re-labelled the domain and took the ids at face value, so a
stale pick wrote a seed into whatever space now had that number.

Every detect answer now carries a ``detection_id``: a hash of the case id,
the fingerprint of what was labelled (the surfaces' content and the box, or
on Gmsh the solid topology), the box, the voxel size and ``external``. The
answer is kept as a small JSON sidecar beside the cached ``.npz``, where the
CLI can read it too. An apply by ``ids`` must name the detection; it is
refused with ``detection_stale`` when the case's inputs no longer match the
fingerprint or the labelled result is gone. Explicit points (and Gmsh
solids by ``region_uuid``) name their own target and need no id.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

#: The ``error`` a refused apply carries.
DETECTION_STALE = 'detection_stale'
DETECTION_ID_REQUIRED = 'detection_id_required'

#: Why a detection is stale.
UNKNOWN = 'unknown_detection'
FINGERPRINT_CHANGED = 'inputs_changed'
RESULT_GONE = 'result_gone'

#: The sources a detection is answered from (RP7 voxels, RP11 solids).
VOXELS = 'fluid_spaces'
SOLIDS = 'solids'

RECORD_VERSION = 1
_PREFIX = 'fluid_regions-'


class DetectionStale(Exception):
    """An apply named a detection that no longer describes the case."""

    def __init__(self, reason: str, detection_id: str | None):
        self.reason = reason
        self.detection_id = detection_id
        super().__init__({
            UNKNOWN: 'no detection with that id was kept for this case',
            FINGERPRINT_CHANGED: 'the geometry or the domain changed since '
                                 'that detection; detect again',
            RESULT_GONE: 'the labelled result of that detection is gone; '
                         'detect again',
        }.get(reason, reason))


def _numbers(values):
    if values is None:
        return None
    return [repr(float(value)) for value in values]


def detection_id(case_id, fingerprint, box, h, external) -> str:
    """The id of one detection: 16 hex digits."""
    payload = json.dumps([RECORD_VERSION, str(case_id or ''),
                          str(fingerprint), _numbers(box),
                          None if h is None else repr(float(h)),
                          bool(external)])
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def voxel_fingerprint(surface_key, box) -> str:
    """The labelled inputs: the surfaces' content hash and the domain box."""
    payload = json.dumps([VOXELS, str(surface_key), _numbers(box)])
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]


def solid_fingerprint(found) -> str:
    """The solid topology a Gmsh detection answered from.

    Each solid's identity (``geometry_id``, ``region_uuid``), how and how big
    it was measured, and its bounds, in a stable order; the regions that were
    not solids too, since one closing or opening changes which solids exist.
    """
    def number(value):
        return format(float(value), '.9g')

    solids = sorted(
        (str(solid.geometry_id), str(solid.region_uuid), str(solid.measured),
         number(solid.volume), [number(value) for value in solid.bounds])
        for solid in found.solids)
    payload = json.dumps([SOLIDS, solids,
                          sorted(str(name) for name in found.open_regions),
                          sorted(str(name) for name in found.not_solids)])
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:24]


def record_path(cache_dir, identifier) -> Path:
    return Path(cache_dir) / f'{_PREFIX}{identifier}.json'


def write(cache_dir, *, case_id, source, fingerprint, box=None, h=None,
          external=False, field_key=None, spaces=()) -> dict:
    """Keep one detection beside the cache and answer its record.

    *spaces* are the detect rows (``id`` and ``seed`` or ``region_uuid``);
    *field_key* is the cached field's key (voxels), whose ``.npz`` must still
    be there for an apply to use the detection.
    """
    identifier = detection_id(case_id, fingerprint, box, h, external)
    record = {
        'version': RECORD_VERSION, 'detection_id': identifier,
        'case_id': str(case_id or ''), 'source': source,
        'fingerprint': fingerprint,
        'box': None if box is None else [float(value) for value in box],
        'h': None if h is None else float(h), 'external': bool(external),
        'field_key': field_key,
        'npz': None if field_key is None else f'fluid_spaces-{field_key}.npz',
        'created': time.time(),
        'spaces': [_space(row) for row in spaces],
    }
    path = record_path(cache_dir, identifier)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.part')
        temporary.write_text(json.dumps(record, indent=1) + '\n',
                             encoding='utf-8')
        temporary.replace(path)
    except OSError:
        # Unkept, a later apply by ids is refused as unknown; never an error.
        pass
    return record


def _space(row) -> dict:
    kept = {'id': int(row['id'])}
    for key in ('seed', 'region_uuid', 'outside', 'volume'):
        if row.get(key) is not None:
            kept[key] = row[key]
    return kept


def read(cache_dir, identifier) -> dict | None:
    """The kept record of *identifier*, or ``None``."""
    identifier = str(identifier or '').strip()
    if not identifier or not all(char in '0123456789abcdef'
                                 for char in identifier):
        return None
    try:
        record = json.loads(record_path(cache_dir, identifier)
                            .read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if record.get('version') != RECORD_VERSION or \
            record.get('detection_id') != identifier:
        return None
    return record


def check(cache_dir, identifier, *, case_id, fingerprint,
          result_present=None) -> dict:
    """The record of *identifier* if it still describes the case.

    *fingerprint* is the case's fingerprint now, in the record's source;
    *result_present* answers whether the labelled result a voxel record
    points at is still held. Raises `DetectionStale`.
    """
    record = read(cache_dir, identifier)
    if record is None or record.get('case_id') != str(case_id or ''):
        raise DetectionStale(UNKNOWN, identifier)
    if record.get('fingerprint') != fingerprint:
        raise DetectionStale(FINGERPRINT_CHANGED, identifier)
    if result_present is not None and not result_present(record):
        raise DetectionStale(RESULT_GONE, identifier)
    return record
