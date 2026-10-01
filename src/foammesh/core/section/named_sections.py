#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Named sections saved with the case (Plan 37 UF11).

A section the user names is a *definition*, never a picture: the planes (the
canonical ``PlaneState`` of each, its pivot and its name), the mode, the
units the offsets read in, the colour and mask preferences, which mesh the
section is meant for, and -- when the user froze a cell layer -- the source
cell IDs and the mesh revision they belong to. Nothing the section worker
produced (a ``section.vtp``, a ``cells.npy``, a job directory) is ever
written here; loading a section asks for it again.

The file is ``<case>/foammesh/sections.json``, beside ``views.json``::

    {"schema": "foammesh.sections", "version": 1,
     "sections": [{"id": ..., "name": ..., "planes": [...], ...}, ...]}

Every length is in metres. A file written by a newer FoamMesh (a version
this one does not know) is read, as far as it can be, *read-only*, with the
reason: saving over it would drop whatever the newer version added. A file
that is not JSON at all is set aside (``sections.json.unreadable``) the first
time something is saved, never silently overwritten.

No Qt here: the panel, the CLI and the tests all read it.
"""
from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

from .modes import SectionMode
from .plane_state import PlaneState

__all__ = [
    'SECTIONS_FILENAME', 'SCHEMA', 'VERSION', 'MAX_PLANES', 'UNITS',
    'NamedPlane', 'FrozenSelection', 'StagePolicy', 'SectionDefinition',
    'SectionsDocument', 'SectionStoreError', 'sections_path',
    'load_sections', 'save_sections', 'put_section', 'delete_section',
    'pack_ids', 'unpack_ids',
]

SECTIONS_FILENAME = Path('foammesh') / 'sections.json'
SCHEMA = 'foammesh.sections'
VERSION = 1
#: The panel's ceiling (§4.2): six planes, each named.
MAX_PLANES = 6
#: How offsets are shown. ``model``: the unit the model's own size reads
#: best in (``quantities.unit_for``). The stored numbers are always metres.
UNITS = ('model', 'm', 'mm', 'um')
STAGES = ('blockMesh', 'castellation', 'snap', 'layers')


class SectionStoreError(RuntimeError):
    """Named sections could not be written (or not safely)."""


def _vector(values, what):
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{what} is not three numbers') from error
    if len(result) != 3 or not all(math.isfinite(v) for v in result):
        raise ValueError(f'{what} is not three finite numbers')
    return result


def _text(value, what):
    text = str(value if value is not None else '').strip()
    if not text:
        raise ValueError(f'{what} is empty')
    return text


# -- frozen IDs, packed as runs --------------------------------------------- #

def pack_ids(ids) -> list[list[int]]:
    """Sorted unique IDs as inclusive ``[first, last]`` runs.

    A frozen layer of a large mesh is a few runs of consecutive cells; a
    plain list of a million integers is what "never serialize a buffer"
    rules out.
    """
    values = sorted({int(value) for value in ids})
    runs: list[list[int]] = []
    for value in values:
        if value < 0:
            raise ValueError('a source cell ID is negative')
        if runs and value == runs[-1][1] + 1:
            runs[-1][1] = value
        else:
            runs.append([value, value])
    return runs


def unpack_ids(runs) -> tuple[int, ...]:
    result = []
    last = -1
    for run in runs or ():
        first, end = (int(run[0]), int(run[1]))
        if first < 0 or end < first or first <= last:
            raise ValueError('frozen cell runs are not sorted and disjoint')
        result.extend(range(first, end + 1))
        last = end
    return tuple(result)


# -- the parts --------------------------------------------------------------- #

@dataclass(frozen=True)
class NamedPlane:
    name: str
    state: PlaneState
    pivot: tuple = (0.0, 0.0, 0.0)
    enabled: bool = True

    def to_dict(self) -> dict:
        return {'name': self.name, 'enabled': bool(self.enabled),
                'pivot': list(self.pivot), **self.state.to_dict()}

    @classmethod
    def from_dict(cls, data: dict, fallback_name='Plane') -> 'NamedPlane':
        if not isinstance(data, dict):
            raise ValueError('a plane is not an object')
        name = str(data.get('name') or '').strip() or fallback_name
        state = PlaneState.from_dict(data)
        pivot = data.get('pivot')
        pivot = state.project(_vector(pivot, 'pivot')) if pivot else \
            state.origin
        return cls(name, state, tuple(pivot), bool(data.get('enabled', True)))


@dataclass(frozen=True)
class StagePolicy:
    """Which mesh the section is for.

    ``live``: whatever mesh is loaded. ``stage``: one stage's immutable
    snapshot (``revision`` ``None`` = the newest revision that kept it).
    ``compare``: the same plane on each of ``stages``.
    """
    kind: str = 'live'
    stages: tuple = ()
    revision: str | None = None

    def __post_init__(self):
        if self.kind not in ('live', 'stage', 'compare'):
            raise ValueError(f'unknown target stage policy {self.kind!r}')
        unknown = [s for s in self.stages if s not in STAGES]
        if unknown:
            raise ValueError(f'unknown stage {unknown[0]!r}')
        if self.kind == 'stage' and len(self.stages) != 1:
            raise ValueError('a stage target names exactly one stage')
        if self.kind == 'compare' and not self.stages:
            raise ValueError('a comparison names at least one stage')
        if self.kind == 'live' and self.stages:
            raise ValueError('the live mesh names no stage')

    def to_dict(self) -> dict:
        result = {'kind': self.kind}
        if self.stages:
            result['stages'] = list(self.stages)
        if self.revision is not None:
            result['revision'] = self.revision
        return result

    @classmethod
    def from_dict(cls, data) -> 'StagePolicy':
        if not data:
            return cls()
        if not isinstance(data, dict):
            raise ValueError('the target stage policy is not an object')
        revision = data.get('revision')
        return cls(str(data.get('kind', 'live')),
                   tuple(str(s) for s in data.get('stages') or ()),
                   None if revision is None else str(revision))


@dataclass(frozen=True)
class FrozenSelection:
    """Source cell IDs bound to the one mesh revision they came from.

    ``mesh_identity`` is ``task_state_store.mesh_identity`` of the polyMesh
    they were read from; ``snapshot`` (``{stage, revision, digest}``) is the
    immutable stage snapshot that mesh was, when it was one. Nothing else
    identifies these cells: a re-meshed case has other cells under the same
    numbers, and no nearest-index remapping is ever made.
    """
    cells: tuple
    mesh_identity: str
    snapshot: dict | None = None
    mode: str = SectionMode.CUT_CELLS.value
    plane: int = 0

    def to_dict(self) -> dict:
        result = {'cells': pack_ids(self.cells), 'count': len(self.cells),
                  'mesh_identity': self.mesh_identity, 'mode': self.mode,
                  'plane': int(self.plane)}
        if self.snapshot:
            result['snapshot'] = {key: self.snapshot[key] for key in
                                  ('stage', 'revision', 'digest')
                                  if key in self.snapshot}
        return result

    @classmethod
    def from_dict(cls, data) -> 'FrozenSelection':
        if not isinstance(data, dict):
            raise ValueError('the frozen selection is not an object')
        cells = unpack_ids(data.get('cells'))
        count = data.get('count')
        if count is not None and int(count) != len(cells):
            raise ValueError('the frozen selection lost cells')
        snapshot = data.get('snapshot')
        if snapshot is not None and not isinstance(snapshot, dict):
            raise ValueError('the frozen snapshot is not an object')
        SectionMode(str(data.get('mode', SectionMode.CUT_CELLS.value)))
        return cls(cells, _text(data.get('mesh_identity'), 'mesh identity'),
                   dict(snapshot) if snapshot else None,
                   str(data.get('mode', SectionMode.CUT_CELLS.value)),
                   int(data.get('plane', 0)))


@dataclass(frozen=True)
class SectionDefinition:
    id: str
    name: str
    planes: tuple
    active: int = 0
    mode: SectionMode = SectionMode.CLIP
    units: str = 'model'
    colour: str = 'none'
    #: ``caps``: draw the caps a clip leaves (they are never coloured).
    mask: dict = field(default_factory=lambda: {'caps': True})
    target: StagePolicy = field(default_factory=StagePolicy)
    frozen: FrozenSelection | None = None

    def __post_init__(self):
        if not self.planes:
            raise ValueError('a section has at least one plane')
        if len(self.planes) > MAX_PLANES:
            raise ValueError(f'a section has at most {MAX_PLANES} planes')
        if not 0 <= int(self.active) < len(self.planes):
            raise ValueError('the active plane is not one of the planes')
        if self.units not in UNITS:
            raise ValueError(f'unknown units policy {self.units!r}')
        if not isinstance(self.mode, SectionMode):
            raise ValueError('the mode is not a section mode')

    @classmethod
    def new(cls, name, planes, **kwargs) -> 'SectionDefinition':
        return cls(uuid.uuid4().hex, _text(name, 'the section name'),
                   tuple(planes), **kwargs)

    def renamed(self, name) -> 'SectionDefinition':
        return replace(self, name=_text(name, 'the section name'))

    @property
    def keep_sides(self) -> tuple:
        return tuple(plane.state.keep for plane in self.planes)

    def to_dict(self) -> dict:
        """Only the definition: a whitelist, so a buffer cannot slip in."""
        result = {
            'id': self.id, 'name': self.name,
            'planes': [plane.to_dict() for plane in self.planes],
            'active': int(self.active), 'mode': self.mode.value,
            'keep': list(self.keep_sides),
            'units': self.units,
            'colour': {'key': str(self.colour)},
            'mask': {'caps': bool(self.mask.get('caps', True))},
            'target': self.target.to_dict(),
        }
        if self.frozen is not None:
            result['frozen'] = self.frozen.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: dict) -> 'SectionDefinition':
        if not isinstance(data, dict):
            raise ValueError('a section is not an object')
        planes = data.get('planes')
        if not isinstance(planes, list):
            raise ValueError('a section has no plane list')
        named = tuple(NamedPlane.from_dict(plane, f'Plane {index + 1}')
                      for index, plane in enumerate(planes))
        colour = data.get('colour') or {}
        mask = data.get('mask') or {}
        frozen = data.get('frozen')
        return cls(
            _text(data.get('id'), 'the section ID'),
            _text(data.get('name'), 'the section name'),
            named, int(data.get('active', 0)),
            SectionMode(str(data.get('mode', SectionMode.CLIP.value))),
            str(data.get('units', 'model')),
            str(colour.get('key', 'none')) if isinstance(colour, dict)
            else 'none',
            {'caps': bool(mask.get('caps', True))} if isinstance(mask, dict)
            else {'caps': True},
            StagePolicy.from_dict(data.get('target')),
            FrozenSelection.from_dict(frozen) if frozen else None)


# -- the file ------------------------------------------------------------ #

@dataclass(frozen=True)
class SectionsDocument:
    sections: tuple = ()
    #: Saving is refused, and ``reason`` says why.
    read_only: bool = False
    reason: str = ''
    #: ``(index, reason)`` for each entry that could not be read.
    skipped: tuple = ()
    #: The file is not JSON; a save sets it aside first.
    unreadable: bool = False

    def by_id(self, section_id) -> SectionDefinition | None:
        for section in self.sections:
            if section.id == section_id:
                return section
        return None


def sections_path(case_root) -> Path:
    return Path(case_root) / SECTIONS_FILENAME


def load_sections(case_root) -> SectionsDocument:
    path = sections_path(case_root)
    if not path.is_file():
        return SectionsDocument()
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return SectionsDocument(
            reason=f'{SECTIONS_FILENAME.as_posix()} could not be read '
                   f'({error.__class__.__name__}); it is set aside when a '
                   'section is next saved', unreadable=True)
    if not isinstance(document, dict) or document.get('schema') != SCHEMA:
        return SectionsDocument(
            read_only=True,
            reason=f'{SECTIONS_FILENAME.as_posix()} is not a FoamMesh '
                   'sections file; it is left as it is')
    version = document.get('version')
    newer = not isinstance(version, int) or version > VERSION
    entries = document.get('sections')
    if not isinstance(entries, list):
        entries = []
    sections, skipped = [], []
    for index, entry in enumerate(entries):
        try:
            sections.append(SectionDefinition.from_dict(entry))
        except (ValueError, TypeError, KeyError) as error:
            skipped.append((index, str(error) or error.__class__.__name__))
    if newer:
        return SectionsDocument(
            tuple(sections), read_only=True,
            reason=f'the named sections were saved by a newer FoamMesh '
                   f'(sections version {version!r}, this one reads '
                   f'{VERSION}); they are shown read-only so that saving '
                   'cannot drop what the newer version added',
            skipped=tuple(skipped))
    return SectionsDocument(tuple(sections), skipped=tuple(skipped))


def save_sections(case_root, sections) -> SectionsDocument:
    """Write *sections* (definitions only). Refused over a read-only file."""
    current = load_sections(case_root)
    if current.read_only:
        raise SectionStoreError(current.reason)
    ids = [section.id for section in sections]
    if len(set(ids)) != len(ids):
        raise SectionStoreError('two sections share one ID')
    path = sections_path(case_root)
    entries = [s.to_dict() for s in sections]
    # An entry this version could not read is kept as it was, not dropped.
    entries += _raw_entries(path, [index for index, _ in current.skipped])
    path.parent.mkdir(parents=True, exist_ok=True)
    if current.unreadable and path.exists():
        aside = path.with_name(path.name + '.unreadable')
        path.replace(aside)
    text = json.dumps({'schema': SCHEMA, 'version': VERSION,
                       'sections': entries},
                      indent=2, sort_keys=True, allow_nan=False)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text + '\n', encoding='utf-8', newline='\n')
    temporary.replace(path)
    return load_sections(case_root)


def _raw_entries(path, indexes):
    if not indexes:
        return []
    entries = json.loads(path.read_text(encoding='utf-8'))['sections']
    return [entries[index] for index in indexes]


def put_section(case_root, section: SectionDefinition) -> SectionsDocument:
    """Add *section*, or replace the one with its ID (its place kept)."""
    current = load_sections(case_root)
    if current.read_only:
        raise SectionStoreError(current.reason)
    sections = list(current.sections)
    for index, existing in enumerate(sections):
        if existing.id == section.id:
            sections[index] = section
            break
    else:
        sections.append(section)
    return save_sections(case_root, sections)


def delete_section(case_root, section_id) -> SectionsDocument:
    current = load_sections(case_root)
    if current.read_only:
        raise SectionStoreError(current.reason)
    return save_sections(case_root, [s for s in current.sections
                                     if s.id != section_id])
