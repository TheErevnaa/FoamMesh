"""Immutable, mesher-independent prepared geometry and group manifests."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import struct
import re
import shutil
from typing import Iterable, Mapping

from foammesh.core.engine.contracts import PreparedGeometryRef
from foammesh.core.quantities import count_text

from . import domain_topology
from .store import (GeometryArtifactStore, is_cad_entry,
                    ordered_entries as _ordered_sources,
                    read_back_unit, stored_tessellation)


PREPARED_SCHEMA_VERSION = 1
GROUP_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r'[^A-Za-z0-9_]+')


class PreparedGeometryError(ValueError):
    pass


#: What preparing with nothing said means: take the geometry as imported.
DEFAULT_PREPARATION = {'decision': 'as_is'}


def _declared_shell_role(entry: Mapping) -> str | None:
    """What the geometry says its body is, or ``None`` to let nesting decide.

    Plan 30 WP-06a/b. The Gmsh runner reads a shell role per staged file and
    infers one from nesting depth for every shell it is told nothing about --
    which is right for an obstacle inside a box and wrong for a geometry the
    user has already excluded or declared solid. Only a geometry that says
    something appears here, so the inference keeps every case that has not.
    """
    regions = entry.get('regions') or ()
    if not regions:
        return None
    included = [item for item in regions if item.get('included', True)]
    if not included:
        return 'void'
    kinds = {str(item.get('region_type') or 'fluid').strip().lower()
             for item in included}
    return 'solid' if kinds == {'solid'} else None


#: Classifying a geometry means reading every triangle in it, and readiness is
#: asked on every page draw. Keyed on the artifact paths plus their size and
#: mtime, so an edited file is re-read and an untouched one is not.
_TOPOLOGY_CACHE: dict = {}
_TOPOLOGY_CACHE_LIMIT = 32


def _topology_key(entries) -> tuple:
    key = []
    for entry in entries or ():
        path = domain_topology.entry_path(entry)
        if not path:
            continue
        try:
            stat = os.stat(path)
        except OSError:
            key.append((path, None, None))
        else:
            key.append((path, stat.st_size, stat.st_mtime_ns))
    return tuple(key)


def domain_topology_report(entries) -> dict:
    """What the imported surfaces are: shells, bodies, voids, and a domain.

    Plan 31 CP-04 (C31-06). This runs the *same* classifier the Gmsh runner
    uses, on the host, before either engine is launched -- so the answer the
    user is shown on the geometry page and the answer the mesher acts on are
    one calculation, not two that agree by luck.

    A geometry this cannot read is not an error here. Readiness must never be
    the thing that fails; an unreadable file is recorded as such and left
    permissive, and the importer or the engine reports it in its own words.
    """
    key = _topology_key(entries)
    cached = _TOPOLOGY_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        report = domain_topology.classify_entries(entries)
    except (OSError, ValueError, struct.error) as error:
        report = {
            'schema_version': domain_topology.SCHEMA_VERSION,
            'representation': 'unreadable',
            'sources': [domain_topology.entry_path(item)
                        for item in (entries or ())],
            'shells': [], 'domains': [], 'bodies': [], 'voids': [],
            'volumes': 0, 'void_count': 0,
            # Nothing was measured, so nothing is claimed.
            'volumes_source': 'unknown',
            'bounds_domain': True,
            'refusal': None,
            'warnings': [f'the geometry could not be classified: {error}'],
        }
    if len(_TOPOLOGY_CACHE) >= _TOPOLOGY_CACHE_LIMIT:
        _TOPOLOGY_CACHE.clear()
    _TOPOLOGY_CACHE[key] = report
    return report


def _gmsh_shared_face_refusal(topology) -> str | None:
    """Why Gmsh cannot take a one-surface multi-region STL, or ``None``.

    DP-860. The host classifier reads a surface whose regions share a face as
    the regions it bounds, each closed on its own, and snappyHexMesh meshes
    it as it is -- MEASURED in the Plan 36 campaign on jacketed_pipe,
    coaxial_ducts, finned_plate_duct and baffled_chamber, each into exactly
    the regions detection proposed. The Gmsh runner builds its volumes from
    the surfaces ``classifySurfaces`` cuts, and a face written into one file
    for two regions is not a surface it can hand to both (DP-368/DP-369), so
    that route is still refused -- here, before WSL is booted (DP-366), and
    not by the runner halfway through the job.
    """
    split = (topology or {}).get('shared_face_regions') or ()
    if not split:
        return None
    first = split[0]
    source = first.get('source') or 'the import'
    return (f'{source} holds {count_text(int(first.get("regions", 0)), "region")} '
            f'that share {count_text(int(first.get("shared_faces", 0)), "face")} '
            'in one surface. snappyHexMesh meshes that as it is; Gmsh needs '
            'each region as a closed shell of its own, so import the regions '
            'as separate STL files or as a STEP of solids, or mesh this '
            'geometry with snappyHexMesh.')


def prepare_readiness(store, *, engine_id: str | None = None) -> dict:
    """Whether the case is ready to mesh, and if not, whether it can be.

    F-12. One predicate, one answer, whichever engine is asking -- which is
    why *engine_id* is taken and never read: it used to matter. snappy
    prepared the geometry for the user with defaults and Gmsh refused to run
    until the user had done it by hand, so the same case was ready in one
    engine and blocked in the other, and the Prepare step meant two
    different things depending on which branch of the outline you were in.

    DP-860. *engine_id* is read for one geometry only: a single surface whose
    regions share a face. snappyHexMesh meshes it and the Gmsh runner cannot
    (:func:`_gmsh_shared_face_refusal`), so the Gmsh run seam passes its id
    and is refused before the job; every other answer is the same for both.
    """
    entries = store.source.entries()
    try:
        current = store.current()
    except PreparedGeometryError:
        current = None
    topology = domain_topology_report(entries) if entries else None
    bounds = topology.get('bounds_domain') if topology else None
    blocked = None if entries else 'no geometry has been imported'
    if entries and bounds is False:
        # Plan 31 CP-04. Say it on the page that owns the geometry, in the
        # classifier's own words, instead of letting a mesher discover it.
        blocked = topology.get('refusal') or (
            'the imported surfaces cannot bound a volume, so there is no '
            'domain to mesh')
    elif entries and engine_id == 'gmsh' and bounds:
        gmsh_refusal = _gmsh_shared_face_refusal(topology)
        if gmsh_refusal:
            bounds, blocked = False, gmsh_refusal
    return {
        'prepared': current is not None,
        'revision_id': current.reference.revision_id if current else None,
        # Still preparable: that is how a broken surface gets repaired or
        # wrapped. What changes is that the user is told first.
        'can_prepare': bool(entries),
        'blocked': blocked,
        'bounds_domain': bounds,
        # DP-52/DP-53. One volume count, published under one key whichever
        # route imported the geometry, so no caller has to know there are
        # two. `volumes_source` is what lets a reader tell a measured zero
        # from an import that never counted.
        'volumes': (topology or {}).get('volumes', 0),
        'volumes_source': (topology or {}).get('volumes_source', 'unknown'),
        'topology': topology,
        'topology_sentence': (domain_topology.summary_sentence(topology)
                              if topology else None),
    }


def preparation_with_topology(readiness: dict, preparation) -> dict:
    """*preparation* with the classifier's topology recorded on it.

    DP-122. Plan 31 CP-04 put the topology block on the revision so the run
    seams and the Boundary Layers page could read the volume count instead of
    recomputing it, and DP-52/DP-53/DP-115 are all built on that read. It was
    written by :func:`ensure_prepared` only -- the automatic route -- and the
    explicit one, `geometry.prepared.create`, passed the caller's dict through
    untouched. The GUI's Prepare step sends `{'decision': 'as_is'}`.

    MEASURED on the `9f6abca1` sweep: **0 of 40 prepared revisions**, twenty
    models across both engines, carry a topology block. Every one of those
    pre-flights therefore read `volumes_source: unknown`, and an unknown count
    refuses nothing, so the whole chain was inert on the route users take.

    A caller that supplies its own topology keeps it; nothing else is
    rewritten.
    """
    preparation = dict(preparation or DEFAULT_PREPARATION)
    if preparation.get('topology'):
        return preparation
    topology = readiness.get('topology') or {}
    preparation['topology'] = {
        'bounds_domain': topology.get('bounds_domain'),
        'domains': list(topology.get('domains') or ()),
        'bodies': list(topology.get('bodies') or ()),
        'voids': list(topology.get('voids') or ()),
        'volumes': topology.get('volumes', 0),
        'volumes_source': topology.get('volumes_source', 'unknown'),
        'solids': list(topology.get('solids') or ()),
        'refusal': topology.get('refusal'),
        'representation': topology.get('representation'),
        'calculation_version': topology.get('calculation_version'),
    }
    return preparation


def ensure_prepared(store, *, producer: str, boundary_categories=None,
                    fluid_seed=None):
    """The current prepared revision, prepared with defaults if there is none.

    Returns ``None`` only when there is nothing to prepare. Every engine
    reaches its geometry through this, so no engine can be readier than
    another on the same case.
    """
    readiness = prepare_readiness(store)
    if readiness['prepared']:
        return store.current()
    if not readiness['can_prepare']:
        return None
    # Plan 31 CP-04. Auto-preparation used to say only "as_is", so the
    # revision both engines mesh from carried no record of what the geometry
    # was. It records the classification now -- and deliberately does not
    # refuse on it, because refusing here would put repair and wrapping out
    # of reach of the very cases that need them. The run seams refuse.
    preparation = preparation_with_topology(
        readiness, {**DEFAULT_PREPARATION, 'producer': producer})
    return store.materialize(
        preparation=preparation,
        boundary_categories=boundary_categories, fluid_seed=fluid_seed)


def domain_refusal(store, *, engine_id: str | None = None) -> str | None:
    """Why these surfaces cannot bound a domain, or ``None`` if they can.

    Plan 31 CP-04 (C31-06). Measured before this existed: Gmsh refused an
    open shell by name, but only after the job had been written and WSL had
    booted; snappyHexMesh did not refuse at all -- its own watertightness
    check reports ``evaluated: False`` on a surface that is not closed, so
    the seed test it guards never ran, and the mesher was handed a surface
    with a hole in it.

    An already-prepared revision is never second-guessed. The user may have
    repaired, wrapped, or knowingly accepted the geometry, and that decision
    is recorded on the revision; re-deciding it here would undo the
    acknowledgement.
    """
    readiness = prepare_readiness(store, engine_id=engine_id)
    if readiness['prepared'] or not readiness['can_prepare']:
        return None
    if readiness.get('bounds_domain') is False:
        return readiness.get('blocked')
    return None


@dataclass(frozen=True)
class PreparedGroup:
    patch_uuid: str
    native_token: str
    display_name: str
    solver_name: str
    category: str
    geometry_id: str
    source_refs: tuple[dict, ...]
    mapping_confidence: float
    confirmation_required: bool
    # DP-92. The face answers the boundary-layer page needs and had no way to
    # get: whether an uncovered patch is flat (DP-90), which patches share an
    # edge and so may not grow layers in opposite directions (DP-91), and
    # which two patches are one interface written twice, healed into the one
    # the walk saw first. Omitted from the manifest where nobody measured --
    # the STL route does not, and neither did any revision written before
    # this -- so the schema is unchanged and absence still reads as absence.
    planar: bool | None = None
    area: float | None = None
    face_order: int | None = None
    adjacent_patch_uuids: tuple[str, ...] = ()
    interface_patch_uuid: str = ''

    def to_dict(self) -> dict:
        measured = {}
        if self.planar is not None:
            measured['planar'] = bool(self.planar)
        if self.area is not None:
            measured['area'] = float(self.area)
        if self.face_order is not None:
            measured['face_order'] = int(self.face_order)
        if self.adjacent_patch_uuids:
            measured['adjacent_patch_uuids'] = list(self.adjacent_patch_uuids)
        if self.interface_patch_uuid:
            measured['interface_patch_uuid'] = self.interface_patch_uuid
        return {
            'patch_uuid': self.patch_uuid,
            'native_token': self.native_token,
            'display_name': self.display_name,
            'solver_name': self.solver_name,
            'category': self.category,
            'geometry_id': self.geometry_id,
            'source_refs': list(self.source_refs),
            'mapping_confidence': self.mapping_confidence,
            'confirmation_required': self.confirmation_required,
            **measured,
        }


@dataclass(frozen=True)
class PreparedRegion:
    region_uuid: str
    native_token: str
    display_name: str
    solver_name: str
    region_type: str
    geometry_id: str
    source_refs: tuple[dict, ...]
    boundary_patch_uuids: tuple[str, ...]
    included: bool = True

    def to_dict(self) -> dict:
        return {
            'region_uuid': self.region_uuid,
            'native_token': self.native_token,
            'display_name': self.display_name,
            'solver_name': self.solver_name,
            'region_type': self.region_type,
            'geometry_id': self.geometry_id,
            'source_refs': list(self.source_refs),
            'boundary_patch_uuids': list(self.boundary_patch_uuids),
            'included': self.included,
        }


@dataclass(frozen=True)
class PreparedGeometryResult:
    reference: PreparedGeometryRef
    manifest: dict
    group_manifest: dict
    created: bool

    def to_dict(self) -> dict:
        return {
            'reference': self.reference.to_dict(),
            'manifest': self.manifest,
            'group_manifest': self.group_manifest,
            'created': self.created,
        }


class PreparedGeometryStore:
    def __init__(self, case_path: str | Path):
        self.case_path = Path(case_path).resolve()
        self.root = self.case_path / 'foammesh' / 'geometry' / 'prepared'
        self.current_path = self.root / 'current.json'
        self.source = GeometryArtifactStore(self.case_path)

    def materialize(self, *, preparation: Mapping | None = None,
                    boundary_categories: Mapping[str, str] | None = None,
                    fluid_seed: Iterable[float] | None = None,
                    transform: Iterable[float] | None = None) -> PreparedGeometryResult:
        entries = self.source.entries()
        if not entries:
            raise PreparedGeometryError('no imported geometry is available to prepare')
        categories = dict(boundary_categories or {})
        groups = self._groups(entries, categories)
        # DP-419. The groups first, because a region publishes beside them:
        # a Gmsh model names its physical volumes and its physical surfaces in
        # one model, so a region that would take a name a patch already has
        # keeps the disambiguated form.
        regions = self._regions(
            entries, reserved={item.solver_name for item in groups})
        self._validate_groups(groups)
        self._validate_regions(regions)
        sources = self._source_records(entries)
        geometry_fingerprint = self.source.geometry_fingerprint()
        manifest_seed = {
            'schema_version': PREPARED_SCHEMA_VERSION,
            'source_geometry_fingerprint': geometry_fingerprint,
            'units': 'm',
            'transform': list(transform or (1, 0, 0, 0,
                                             0, 1, 0, 0,
                                             0, 0, 1, 0,
                                             0, 0, 0, 1)),
            'fluid_seed': self._seed(fluid_seed),
            'preparation': dict(preparation or {}),
            'sources': sources,
            'group_digest': _json_digest([group.to_dict() for group in groups]),
            'region_digest': _json_digest([region.to_dict() for region in regions]),
        }
        revision_digest = _json_digest(manifest_seed)
        revision_id = f'pg-{revision_digest[:16]}'
        destination = self.root / revision_id
        manifest_path = destination / 'prepared-geometry.json'
        group_path = destination / 'group-manifest.json'
        manifest = {
            **manifest_seed,
            'revision_id': revision_id,
            'prepared_fingerprint': revision_digest,
        }
        group_manifest = {
            'schema_version': GROUP_SCHEMA_VERSION,
            'prepared_revision_id': revision_id,
            'groups': [group.to_dict() for group in groups],
            'regions': [region.to_dict() for region in regions],
        }

        if destination.exists():
            self._verify_existing(manifest_path, group_path, manifest, group_manifest)
            primary = self._primary_geometry_path(destination, sources)
            result = PreparedGeometryResult(
                self._reference(revision_id, destination, primary,
                                manifest_path, group_path, revision_digest),
                manifest, group_manifest, False)
            self._publish_current(result)
            return result

        temporary = self.root / f'.{revision_id}.tmp-{os.getpid()}'
        if temporary.exists():
            raise PreparedGeometryError(f'prepared geometry staging path already exists: {temporary}')
        source_stage = temporary / 'sources'
        source_stage.mkdir(parents=True)
        try:
            for record in sources:
                for representation in self._representations(record):
                    source_path = Path(representation['source_path'])
                    target = source_stage / representation['prepared_name']
                    if not target.exists():
                        shutil.copy2(source_path, target)
                    if _file_sha256(target) != representation['sha256']:
                        raise PreparedGeometryError(
                            f'checksum changed while preparing geometry: {source_path}')
            _write_json_atomic(temporary / 'prepared-geometry.json', manifest)
            _write_json_atomic(temporary / 'group-manifest.json', group_manifest)
            self.root.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        primary = self._primary_geometry_path(destination, sources)
        result = PreparedGeometryResult(
            self._reference(revision_id, destination, primary,
                            manifest_path, group_path, revision_digest),
            manifest, group_manifest, True)
        self._publish_current(result)
        return result

    def current(self, *, require_source_match: bool = True) -> PreparedGeometryResult | None:
        """Return the atomically published prepared revision, if it is current.

        A stale publication is never silently reused after geometry import,
        transform, healing, or revision changes.
        """
        if not self.current_path.is_file():
            return None
        document = _read_json(self.current_path)
        if document.get('schema_version') != CURRENT_SCHEMA_VERSION:
            raise PreparedGeometryError('unsupported current prepared geometry schema')
        if require_source_match and document.get(
                'source_geometry_fingerprint') != self.source.geometry_fingerprint():
            return None
        result = self.load(str(document.get('revision_id') or ''))
        if result.reference.fingerprint != document.get('prepared_fingerprint'):
            raise PreparedGeometryError('current prepared geometry fingerprint mismatch')
        return result

    def select(self, revision_id: str, *, require_source_match: bool = True
               ) -> PreparedGeometryResult:
        result = self.load(revision_id)
        if (require_source_match and result.manifest.get(
                'source_geometry_fingerprint') != self.source.geometry_fingerprint()):
            raise PreparedGeometryError(
                'prepared geometry revision is stale for the active geometry')
        self._publish_current(result)
        return result

    def load(self, revision_id: str) -> PreparedGeometryResult:
        if not revision_id.startswith('pg-') or '/' in revision_id or '\\' in revision_id:
            raise PreparedGeometryError('invalid prepared geometry revision ID')
        destination = (self.root / revision_id).resolve()
        manifest_path = destination / 'prepared-geometry.json'
        group_path = destination / 'group-manifest.json'
        if not manifest_path.is_file() or not group_path.is_file():
            raise FileNotFoundError(revision_id)
        manifest = _read_json(manifest_path)
        groups = _read_json(group_path)
        digest = manifest.get('prepared_fingerprint', '')
        sources = manifest.get('sources', [])
        primary = self._primary_geometry_path(destination, sources)
        result = PreparedGeometryResult(
            self._reference(revision_id, destination, primary,
                            manifest_path, group_path, digest),
            manifest, groups, False)
        self.validate(result)
        return result

    def validate(self, result: PreparedGeometryResult) -> None:
        manifest = result.manifest
        groups = result.group_manifest
        if manifest.get('schema_version') != PREPARED_SCHEMA_VERSION:
            raise PreparedGeometryError('unsupported prepared geometry schema version')
        if groups.get('schema_version') != GROUP_SCHEMA_VERSION:
            raise PreparedGeometryError('unsupported group manifest schema version')
        if groups.get('prepared_revision_id') != manifest.get('revision_id'):
            raise PreparedGeometryError('group manifest belongs to another revision')
        if manifest.get('units') != 'm':
            raise PreparedGeometryError('prepared geometry units must be metres')
        for record in manifest.get('sources', []):
            for representation in self._representations(record):
                path = result.reference.root / 'sources' / representation['prepared_name']
                if not path.is_file() or _file_sha256(path) != representation['sha256']:
                    raise PreparedGeometryError(
                        'prepared source checksum mismatch: '
                        f'{representation.get("prepared_name")}')
        parsed = tuple(self._group_from_dict(item) for item in groups.get('groups', []))
        self._validate_groups(parsed)
        if _json_digest([item.to_dict() for item in parsed]) != manifest.get('group_digest'):
            raise PreparedGeometryError('prepared group digest mismatch')
        regions = tuple(
            self._region_from_dict(item) for item in groups.get('regions', []))
        self._validate_regions(regions)
        if _json_digest([item.to_dict() for item in regions]) != manifest.get(
                'region_digest', _json_digest([])):
            raise PreparedGeometryError('prepared region digest mismatch')

    @staticmethod
    def _seed(values: Iterable[float] | None) -> list[float] | None:
        if values is None:
            return None
        result = [float(value) for value in values]
        if len(result) != 3 or not all(value == value and abs(value) != float('inf')
                                       for value in result):
            raise PreparedGeometryError('fluid_seed must contain three finite coordinates')
        return result

    @staticmethod
    def _source_records(entries: list[dict]) -> list[dict]:
        records = []
        used_names = set()
        for entry in _ordered_sources(entries):
            surface = Path(entry['artifact']).resolve()
            # A wrapped or feature-split CAD entry keeps its `cad_artifact`
            # as provenance, but the surface beside it is what it now means.
            cad = entry['cad_artifact'] if is_cad_entry(entry) else None
            source = Path(cad or surface).resolve()
            suffix = source.suffix.lower() or '.geometry'
            prepared_name = f"{entry['geometry_id']}{suffix}"
            if prepared_name in used_names:
                raise PreparedGeometryError(f'duplicate prepared source name: {prepared_name}')
            used_names.add(prepared_name)
            surface_name = f"{entry['geometry_id']}.surface{surface.suffix.lower() or '.stl'}"
            record = {
                'geometry_id': entry['geometry_id'],
                'display_name': entry.get('name') or entry['geometry_id'],
                'source_path': str(source),
                'prepared_name': prepared_name,
                'source_format': entry.get('format', suffix.lstrip('.')),
                'revision': int(entry.get('revision', 1)),
                'sha256': _file_sha256(source),
                'surface_source_path': str(surface),
                'surface_prepared_name': surface_name,
                'surface_sha256': _file_sha256(surface),
                'bbox': entry.get('bbox'),
            }
            if entry.get('cad_artifact') and not cad:
                # Kept so the prepared set can still say what it came from,
                # named apart from the staged representations because the
                # solid model is no longer one of them.
                record['cad_superseded_by'] = str(entry.get('cad_superseded_by'))
                record['cad_origin_path'] = str(entry['cad_artifact'])
            if cad:
                record.update({
                    'cad_source_path': str(source),
                    'cad_prepared_name': prepared_name,
                    'cad_sha256': record['sha256'],
                    # R207. The surface beside it was converted to metres on
                    # import; the CAD file is copied byte for byte, so
                    # re-reading it hands back whatever the OCCT cascade unit
                    # makes of it. The manifest declares `units: 'm'` for the
                    # whole prepared set, so anything that re-tessellates this
                    # copy has to be told what it will get. DP-08: that is the
                    # read-back unit, which is the fact `entry['unit']` used
                    # to carry under a comment claiming otherwise.
                    'cad_unit': read_back_unit(entry),
                    # F-11. The unit the staged CAD file is actually written
                    # in, for whoever reads it back: `Geometry.OCCTargetUnit`
                    # converts what a STEP or IGES header declares, so a
                    # BREP -- which declares nothing -- can only be believed
                    # if something says what it holds. Copied into the Gmsh
                    # job as `geometry_unit`. DP-08: this is the artifact's
                    # own unit and was `mm` on every metre-declaring STEP
                    # until the two facts were recorded apart.
                    'geometry_unit': str(entry.get('unit') or 'm'),
                    'cad_declared_unit': str(entry.get('declared_unit') or ''),
                    # DP-520. The linear deflection in metres, whenever
                    # the entry was written.
                    'tessellation': stored_tessellation(entry),
                })
            role = _declared_shell_role(entry)
            if role:
                record['shell_role'] = role
            records.append(record)
        return records

    @staticmethod
    def _representations(record: Mapping) -> tuple[dict, ...]:
        """Normalize additive representation fields, including legacy manifests."""
        values = [{
            'kind': 'primary',
            'source_path': record['source_path'],
            'prepared_name': record['prepared_name'],
            'sha256': record['sha256'],
        }]
        if record.get('surface_prepared_name'):
            values.append({
                'kind': 'surface',
                'source_path': record['surface_source_path'],
                'prepared_name': record['surface_prepared_name'],
                'sha256': record['surface_sha256'],
            })
        unique = {}
        for value in values:
            unique.setdefault(value['prepared_name'], value)
        return tuple(unique.values())

    def _publish_current(self, result: PreparedGeometryResult) -> None:
        _write_json_atomic(self.current_path, {
            'schema_version': CURRENT_SCHEMA_VERSION,
            'revision_id': result.reference.revision_id,
            'prepared_fingerprint': result.reference.fingerprint,
            'source_geometry_fingerprint':
                result.manifest['source_geometry_fingerprint'],
        })

    @staticmethod
    def _groups(entries: list[dict], categories: Mapping[str, str]) -> tuple[PreparedGroup, ...]:
        groups = []
        for entry in _ordered_sources(entries):
            patches = entry.get('patches') or ({
                'patch_uuid': entry.get('patch_uuid'),
                'name': entry.get('name') or entry['geometry_id'],
                'source_ref': entry.get('source_ref', {}),
            },)
            for patch in patches:
                patch_uuid = str(patch.get('patch_uuid') or '').strip()
                if not patch_uuid:
                    raise PreparedGeometryError(
                        f"geometry {entry['geometry_id']} has a patch without stable UUID")
                display = str(patch.get('name') or entry.get('name') or patch_uuid)
                groups.append(PreparedGroup(
                    patch_uuid=patch_uuid,
                    native_token=_native_token(patch_uuid),
                    display_name=display,
                    solver_name=_solver_name(display, patch_uuid),
                    category=str(categories.get(patch_uuid, 'unclassified')),
                    geometry_id=entry['geometry_id'],
                    # Plan 28 WP5. A merged patch covers several imported
                    # sub-surfaces and names all of them; an unmerged one has
                    # the single ref it always had. Losing the extras would
                    # make a merged boundary untraceable back to the CAD faces
                    # it came from, which is the one thing a merge must not
                    # cost.
                    source_refs=tuple(
                        dict(item) for item in patch['source_refs'])
                    if patch.get('source_refs')
                    else (dict(patch.get('source_ref') or {}),),
                    mapping_confidence=float(patch.get('mapping_confidence', 1.0)),
                    confirmation_required=bool(patch.get('confirmation_required', False)),
                    planar=(None if patch.get('planar') is None
                            else bool(patch['planar'])),
                    area=(None if patch.get('area') is None
                          else float(patch['area'])),
                    face_order=(None if patch.get('face_order') is None
                                else int(patch['face_order'])),
                    adjacent_patch_uuids=tuple(
                        str(value) for value in
                        patch.get('adjacent_patch_uuids') or ()),
                    interface_patch_uuid=str(
                        patch.get('interface_patch_uuid') or ''),
                ))
        return _with_solver_names(groups)

    @staticmethod
    def _regions(entries: list[dict], *,
                 reserved: set | None = None) -> tuple[PreparedRegion, ...]:
        regions = []
        for entry in _ordered_sources(entries):
            source_regions = entry.get('regions') or ({
                'region_uuid': (
                    'region-' + hashlib.sha256(
                        str(entry['geometry_id']).encode()).hexdigest()[:24]),
                'name': entry.get('name') or entry['geometry_id'],
                'region_type': 'fluid',
                'source_ref': {'body_index': 0, 'solid_index': 0},
                'boundary_patch_uuids': [
                    str(patch.get('patch_uuid') or '')
                    for patch in entry.get('patches', ())
                    if patch.get('patch_uuid')],
            },)
            for index, item in enumerate(source_regions):
                region_uuid = str(item.get('region_uuid') or '').strip()
                if not region_uuid:
                    raise PreparedGeometryError(
                        f"geometry {entry['geometry_id']} has a region without stable UUID")
                display = str(
                    item.get('name') or f"{entry.get('name', 'Body')} {index + 1}")
                regions.append(PreparedRegion(
                    region_uuid=region_uuid,
                    native_token=_native_token(region_uuid, prefix='r'),
                    display_name=display,
                    solver_name=_solver_name(display, region_uuid),
                    region_type=str(item.get('region_type') or 'fluid'),
                    geometry_id=str(entry['geometry_id']),
                    source_refs=(dict(item.get('source_ref') or {
                        'body_index': index, 'solid_index': index}),),
                    boundary_patch_uuids=tuple(
                        str(value) for value in
                        item.get('boundary_patch_uuids', ())),
                    included=bool(item.get('included', True)),
                ))
        return _with_region_solver_names(regions, reserved or set())

    @staticmethod
    def _validate_groups(groups: Iterable[PreparedGroup]) -> None:
        groups = tuple(groups)
        if not groups:
            raise PreparedGeometryError('prepared geometry requires at least one group')
        for attribute in ('patch_uuid', 'native_token', 'solver_name'):
            values = [getattr(item, attribute) for item in groups]
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise PreparedGeometryError(
                    f'duplicate prepared group {attribute}: {duplicates}')
        for item in groups:
            if not 0.0 <= item.mapping_confidence <= 1.0:
                raise PreparedGeometryError('mapping confidence must be between zero and one')

    @staticmethod
    def _validate_regions(regions: Iterable[PreparedRegion]) -> None:
        regions = tuple(regions)
        if not regions:
            raise PreparedGeometryError('prepared geometry requires at least one region')
        for attribute in ('region_uuid', 'native_token', 'solver_name'):
            values = [getattr(item, attribute) for item in regions]
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise PreparedGeometryError(
                    f'duplicate prepared region {attribute}: {duplicates}')
        allowed = {'fluid', 'solid', 'dead', 'construction'}
        invalid = sorted({
            item.region_type for item in regions
            if item.region_type not in allowed})
        if invalid:
            raise PreparedGeometryError(
                f'unsupported prepared region types: {invalid}')

    @staticmethod
    def _group_from_dict(item: dict) -> PreparedGroup:
        return PreparedGroup(
            patch_uuid=item['patch_uuid'], native_token=item['native_token'],
            display_name=item['display_name'], solver_name=item['solver_name'],
            category=item['category'], geometry_id=item['geometry_id'],
            source_refs=tuple(item.get('source_refs', ())),
            mapping_confidence=float(item['mapping_confidence']),
            confirmation_required=bool(item['confirmation_required']),
            # DP-92. The digest is taken over `to_dict()`, so a field the
            # writer emits and the reader drops makes every revision fail
            # validation the next time it is loaded.
            planar=(None if item.get('planar') is None
                    else bool(item['planar'])),
            area=(None if item.get('area') is None else float(item['area'])),
            face_order=(None if item.get('face_order') is None
                        else int(item['face_order'])),
            adjacent_patch_uuids=tuple(
                str(value) for value in item.get('adjacent_patch_uuids') or ()),
            interface_patch_uuid=str(item.get('interface_patch_uuid') or ''))

    @staticmethod
    def _region_from_dict(item: dict) -> PreparedRegion:
        return PreparedRegion(
            region_uuid=item['region_uuid'],
            native_token=item['native_token'],
            display_name=item['display_name'],
            solver_name=item['solver_name'],
            region_type=item['region_type'],
            geometry_id=item['geometry_id'],
            source_refs=tuple(item.get('source_refs', ())),
            boundary_patch_uuids=tuple(item.get('boundary_patch_uuids', ())),
            included=bool(item.get('included', True)),
        )

    @staticmethod
    def _primary_geometry_path(destination: Path, sources: list[dict]) -> Path:
        if not sources:
            raise PreparedGeometryError('prepared manifest has no source geometry')
        return destination / 'sources' / sources[0]['prepared_name']

    @staticmethod
    def _reference(revision_id, root, geometry_path, manifest_path, group_path,
                   digest) -> PreparedGeometryRef:
        return PreparedGeometryRef(
            revision_id, root, geometry_path, manifest_path, group_path, digest)

    @staticmethod
    def _verify_existing(manifest_path, group_path, manifest, group_manifest) -> None:
        if not manifest_path.is_file() or not group_path.is_file():
            raise PreparedGeometryError('immutable prepared revision is incomplete')
        if _read_json(manifest_path) != manifest or _read_json(group_path) != group_manifest:
            raise PreparedGeometryError('prepared revision ID collides with different content')


def _native_token(patch_uuid: str, *, prefix: str = 'p') -> str:
    digest = hashlib.sha256(patch_uuid.encode('utf-8')).hexdigest()[:24]
    return f'{prefix}_{digest}'


def solver_stem(display_name: str) -> str:
    """The name the user typed, made safe for a solver dictionary.

    R97. This is the name a boundary condition will be written against, so it
    is the user's own word for the surface with only the characters OpenFOAM
    cannot carry replaced.
    """
    value = _SAFE_NAME.sub('_', display_name.strip()).strip('_')
    if not value or not value[0].isalpha():
        value = f'patch_{value}' if value else 'patch'
    return value[:48]


def _solver_name(display_name: str, patch_uuid: str) -> str:
    """The disambiguated form: the safe name plus the UUID's fingerprint.

    Reserved for the patches that actually collide. Two surfaces may both be
    called ``wall``, and two boundaries with one name would be one boundary in
    the delivered mesh, so the losing pair -- and only that pair -- carries the
    suffix that tells them apart.
    """
    return f'{solver_stem(display_name)}_{hashlib.sha256(patch_uuid.encode()).hexdigest()[:8]}'


def _with_solver_names(groups: Iterable[PreparedGroup]) -> tuple[PreparedGroup, ...]:
    """Name every prepared patch, disambiguating only where names clash.

    R97. Every patch used to be minted ``<display name>_<sha256(uuid)[:8]>``,
    so the venturi case delivered ``wall_converging_9b4145a3``,
    ``wall_diverging_3c8f7777``, ``inlet_9fdbebb0`` and ``outlet_205fed8c``
    into ``constant/polyMesh/boundary`` -- names the user never typed, never
    saw on any page, and had to write their boundary conditions against, while
    ``quality/geometry/patch-identity.json`` recorded the clean
    ``display_name`` beside each one all along.

    The suffix was defending against a real thing: solver names must be unique
    or two boundaries become one, and ``_validate_groups`` refuses duplicates.
    So it is kept exactly where it earns its place -- a name shared by two
    patches suffixes both of them -- and dropped where it never did.
    """
    from .patches.ops import RESERVED_NAMES

    groups = list(groups)
    stems = [solver_stem(item.display_name) for item in groups]
    # A name OpenFOAM keeps for itself was harmless while every name carried a
    # suffix; unsuffixed it would collide with a polyMesh file, so those keep
    # the disambiguated form.
    names = [stem if stems.count(stem) == 1 and stem not in RESERVED_NAMES
             else _solver_name(item.display_name, item.patch_uuid)
             for stem, item in zip(stems, groups)]
    # A display name that already reads like a disambiguated one ("wall_9b41")
    # could land on a suffixed name. Rare, but the collision it would cause is
    # the merge of two boundaries, so suffix every member of the clash.
    clashing = {name for name in names if names.count(name) > 1}
    return tuple(
        replace(item, solver_name=(
            _solver_name(item.display_name, item.patch_uuid)
            if name in clashing else name))
        for item, name in zip(groups, names))


def _with_region_solver_names(
        regions: Iterable[PreparedRegion],
        reserved: set) -> tuple[PreparedRegion, ...]:
    """Name every prepared region, disambiguating only where names clash.

    DP-419. The same rule R97 gave the patches, one field along. A region is
    what becomes a cell zone -- ``Part_1_solid_1`` in the tree, in the Gmsh
    physical group and in ``constant/polyMesh/cellZones`` -- and it was minted
    ``<display name>_<sha256(uuid)[:8]>`` unconditionally, so all four
    multiregion models this campaign meshed on Gmsh published zones called
    `Part_1_solid_1_11ff99c5` and `Part_1_solid_2_aaa75d09`. A user who runs
    ``splitMeshRegions -cellZones`` on that mesh gets directories by those
    names. Nothing was disambiguated: the two names differ in their own words.

    ``reserved`` is what the patches of the same revision publish under, so a
    region cannot take a name a boundary already has -- they share one Gmsh
    model and would be two physical groups with one name.
    """
    from .patches.ops import RESERVED_NAMES

    regions = list(regions)
    stems = [solver_stem(item.display_name) for item in regions]
    names = [stem if (stems.count(stem) == 1
                      and stem not in RESERVED_NAMES
                      and stem not in reserved)
             else _solver_name(item.display_name, item.region_uuid)
             for stem, item in zip(stems, regions)]
    clashing = {name for name in names if names.count(name) > 1}
    return tuple(
        replace(item, solver_name=(
            _solver_name(item.display_name, item.region_uuid)
            if name in clashing else name))
        for item, name in zip(regions, names))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _json_digest(value) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _write_json_atomic(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(
        json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise PreparedGeometryError(f'invalid prepared manifest: {path}: {error}') from error
    if not isinstance(value, dict):
        raise PreparedGeometryError(f'prepared manifest must be an object: {path}')
    return value
