"""Job construction and the supervised launch of the Gmsh runner.

The runner is deliberately a subprocess. Gmsh aborts the process on some bad
geometry, and an abort inside the GUI would take the whole application with it.
Licensing does not require the separation (see plan 22 D1); crash isolation
does.
"""

from __future__ import annotations

from pathlib import Path
import json
import re
import uuid

from foammesh.core.engine.base import StageRun

from .manifest import RunBuilder, RunLayout
from .plan_derivation import (
    JobIntent, companion_export_formats, derive_from_native,
)

JOB_SCHEMA_VERSION = 1

#: C31-04. The version of the source-aware entity identity written into the
#: job and echoed back in the runner's per-source import receipt. Version 1 is
#: the source-local integer index that shipped before this: it is still
#: written, and still read by a runner that has nothing better, but it cannot
#: name an entity once a case holds more than one imported file.
ENTITY_SCHEMA_VERSION = 2


class ExecutionError(RuntimeError):
    pass


def runner_path() -> Path:
    """Where the runner script lives on the host filesystem."""
    from resources import resource

    return Path(resource.file('gmsh/runner_v1.py')).resolve()


def _native_section(db) -> dict:
    """Read the ``gmsh`` configuration section out of the project database.

    MEASURED: ``SimpleDB.getValue`` resolves leaf scalars only and raises
    ``LookupError`` for a section path, and SimpleDB has neither ``toDict`` nor
    ``asDict``. Reading the section through those accessors therefore returned
    ``{}`` for every real database, so a run started from the GUI discarded
    every Gmsh control the user had set and then failed deriving a size it
    could have read. ``data()`` is SimpleDB's own accessor for the content
    tree, so it is tried first and the rest are kept only as adapter fallbacks
    for the injected doubles used in unit tests.
    """
    if db is None:
        return {}
    content = getattr(db, 'data', None)
    if callable(content):
        try:
            section = (content() or {}).get('gmsh')
        except Exception:
            section = None
        if isinstance(section, dict):
            return section
    for accessor in ('getValue', 'get'):
        method = getattr(db, accessor, None)
        if method is None:
            continue
        try:
            value = method('gmsh')
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    snapshot = getattr(db, 'toDict', None) or getattr(db, 'asDict', None)
    if snapshot is not None:
        try:
            return dict((snapshot() or {}).get('gmsh') or {})
        except Exception:
            return {}
    return {}


_FACE_NAME = re.compile(r'^face(\d+)$')


def group_surface_indices(group) -> list[int]:
    """The imported surface indices one prepared group covers.

    WP-01 F-01. There is one producer of this ordering -- the geometry store,
    which records a ``source_ref`` per CAD face -- and this is the one reader
    of it. Nothing in the application ever wrote ``surface_indices`` or
    ``surface_index``, so a map that looked only for those keys was empty for
    every real prepared revision: MEASURED on ``duct.step``, six groups and
    ``scope_surface_map() == {}``, which silently scoped every Distance field,
    Threshold, curve control and periodic pair to nothing.

    Plan 28 names a CAD face's staged solid ``face<N>`` where ``N`` counts
    faces across the whole shape, which is the order Gmsh numbers surfaces in.
    ``face_index`` counts within one body and therefore repeats on a
    multi-body import, so the name wins where the store recorded one.
    """
    if not isinstance(group, dict):
        return []
    indices = group.get('surface_indices')
    if indices is None and group.get('surface_index') is not None:
        indices = [group['surface_index']]
    if indices:
        return [int(item) for item in indices]
    derived: list[int] = []
    for reference in group.get('source_refs') or ():
        if not isinstance(reference, dict):
            continue
        match = _FACE_NAME.match(str(reference.get('original_name') or ''))
        if match is not None:
            derived.append(int(match.group(1)))
            continue
        index = reference.get('face_index')
        if isinstance(index, int) and not isinstance(index, bool):
            derived.append(index)
    return derived


def region_volume_indices(region) -> list[int]:
    """The imported volume indices one prepared region covers.

    Same fault as ``group_surface_indices``: the store writes ``solid_index``
    inside the region's ``source_ref`` and never a ``volume_indices`` key, so
    every volume control resolved to nothing.
    """
    if not isinstance(region, dict):
        return []
    indices = region.get('volume_indices')
    if indices is None and region.get('volume_index') is not None:
        indices = [region['volume_index']]
    if indices:
        return [int(item) for item in indices]
    derived: list[int] = []
    for reference in region.get('source_refs') or ():
        if not isinstance(reference, dict):
            continue
        for key in ('solid_index', 'body_index'):
            index = reference.get(key)
            if isinstance(index, int) and not isinstance(index, bool):
                derived.append(index)
                break
    return derived


def entity_id(source_id: str, kind: str, key) -> str:
    """The stable identity of one imported entity: which file, and which of it.

    C31-04. ``face0``, ``Part_1`` and ``volume0`` are labels, and the integer
    beside them is an index into *one* source file. MEASURED on a case holding
    ``duct.step`` and ``pipe.step`` prepared together: ``scope_surface_map``
    returned index ``0`` for the duct's ``face0`` and index ``0`` again for
    the pipe's, ``scope_volume_map`` returned volume index ``0`` for both
    bodies, and ``surface_names`` collapsed to six entries -- the pipe's three
    names overwriting the duct's first three -- for a model with nine
    surfaces. The runner then indexed those into one global tag list, so a
    control authored on the pipe was applied to the duct and nothing said so.

    The identity is the source's ``geometry_id`` (which the prepared revision
    already assigns and stages every file under), the kind of entity, and the
    entity's key *within that source* -- the same number as before, but no
    longer pretending to be global.
    """
    return f'{source_id}:{kind}:{key}'


def _source_of(record) -> str:
    """The ``geometry_id`` a prepared group or region belongs to."""
    if not isinstance(record, dict):
        return ''
    return str(record.get('geometry_id') or '').strip()


def group_surface_entities(group) -> list[str]:
    """The source-aware entity IDs one prepared group covers.

    Empty for a manifest with no ``geometry_id`` -- a hand-built test double,
    or a revision written before the prepared schema carried one -- which
    leaves the caller on the source-local integer path rather than inventing
    an identity that cannot be checked.
    """
    source_id = _source_of(group)
    if not source_id:
        return []
    return [entity_id(source_id, 'surface', index)
            for index in group_surface_indices(group)]


def region_volume_entities(region) -> list[str]:
    """The source-aware entity IDs one prepared region covers."""
    source_id = _source_of(region)
    if not source_id:
        return []
    return [entity_id(source_id, 'volume', index)
            for index in region_volume_indices(region)]


def seed_point(prepared_geometry):
    """The fluid seed the prepared revision recorded, in metres, or ``None``.

    Plan 30 WP-06a/b. The runner reads ``seedPoint`` to decide which shell of
    a tessellated import is the domain; the seed itself has been recorded on
    every prepared revision all along (from the region point the user placed,
    or from the import's own fluid-seed diagnosis) and simply never reached
    the job, so the runner fell back to alternating roles by nesting depth.
    """
    manifest = prepared_manifest(prepared_geometry)
    values = manifest.get('fluid_seed')
    if not values:
        return None
    try:
        seed = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    return seed if len(seed) == 3 else None


def shell_roles(prepared_geometry) -> dict:
    """Staged file stem -> the role its shell was declared to have.

    Only geometries that say something appear here: the runner infers a role
    from nesting for every shell it is told nothing about, and that inference
    is what a box with an obstacle in it needs. A geometry whose regions are
    all excluded is a hole, one whose regions are all solid is a body, and
    the shells of a file that holds several are numbered ``stem#1``, ``#2``
    -- which a whole-file declaration does not name, so the runner reports it
    as unmatched rather than silently applying it to one of them.
    """
    manifest = prepared_manifest(prepared_geometry)
    roles: dict[str, str] = {}
    for record in manifest.get('sources') or ():
        if not isinstance(record, dict):
            continue
        role = str(record.get('shell_role') or '').strip().lower()
        name = str(record.get('prepared_name') or '')
        if not role or not name:
            continue
        roles[Path(name).stem] = role
    return roles


def scope_surface_map(prepared_geometry) -> dict:
    """Prepared scope token -> imported surface indices.

    Gmsh numbers surfaces in import order, and the prepared geometry records
    the same order when it stages the CAD, so an index is a stable handle.
    A scope this cannot place is simply absent, and the runner reports it as
    unresolved rather than refining nothing and calling it done.
    """
    if prepared_geometry is None:
        return {}
    manifest = _group_manifest(prepared_geometry)
    groups = manifest.get('groups') or ()
    mapping: dict[str, list[int]] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        token = str(group.get('patch_uuid') or '').strip()
        if not token:
            continue
        indices = group_surface_indices(group)
        if not indices:
            continue
        mapping[token] = indices
    return mapping


def scope_volume_map(prepared_geometry) -> dict:
    """Prepared region token -> imported volume indices.

    Volume controls scope to regions, not surfaces, so they resolve against a
    separate map. Without it every volume control silently matches nothing.
    """
    if prepared_geometry is None:
        return {}
    manifest = _group_manifest(prepared_geometry)
    mapping: dict[str, list[int]] = {}
    for region in manifest.get('regions') or ():
        if not isinstance(region, dict):
            continue
        token = str(region.get('region_uuid') or '').strip()
        if not token:
            continue
        indices = region_volume_indices(region)
        if indices:
            mapping[token] = indices
    return mapping


def volume_names(prepared_geometry) -> dict:
    """Imported volume tag (1-based, as a string) -> region name."""
    if prepared_geometry is None:
        return {}
    manifest = _group_manifest(prepared_geometry)
    names: dict[str, str] = {}
    for region in manifest.get('regions') or ():
        if not isinstance(region, dict):
            continue
        indices = region_volume_indices(region)
        label = str(region.get('name') or region.get('solver_name') or '').strip()
        if not label:
            continue
        for index in indices:
            names[str(int(index) + 1)] = label
    return names


def scope_entity_map(prepared_geometry) -> dict:
    """Prepared scope token -> the source-aware entity IDs it covers.

    C31-04. This is the map the runner resolves against; ``scope_surface_map``
    and ``scope_volume_map`` remain beside it as the version-1 form, read only
    by a runner or a fixture that has no entity map, and correct only while a
    case holds a single imported file.
    """
    manifest = _group_manifest(prepared_geometry)
    surfaces: dict[str, list[str]] = {}
    for group in manifest.get('groups') or ():
        token = str((group or {}).get('patch_uuid') or '').strip()
        entities = group_surface_entities(group)
        if token and entities:
            surfaces[token] = entities
    volumes: dict[str, list[str]] = {}
    for region in manifest.get('regions') or ():
        token = str((region or {}).get('region_uuid') or '').strip()
        entities = region_volume_entities(region)
        if token and entities:
            volumes[token] = entities
    return {'surfaces': surfaces, 'volumes': volumes}


def entity_name_map(prepared_geometry) -> dict:
    """Entity ID -> the solver name its group or region publishes under.

    The same collision as the scopes: ``surface_names`` keys on a global tag
    computed from a source-local index, so on the two-source duct/pipe case it
    lost three of the duct's six names to the pipe's three and never mentioned
    surfaces 7-9 at all.
    """
    manifest = _group_manifest(prepared_geometry)
    surfaces: dict[str, str] = {}
    for group in manifest.get('groups') or ():
        label = str((group or {}).get('solver_name')
                    or (group or {}).get('name') or '').strip()
        if not label:
            continue
        for identity in group_surface_entities(group):
            surfaces[identity] = label
    volumes: dict[str, str] = {}
    for region in manifest.get('regions') or ():
        label = str((region or {}).get('solver_name')
                    or (region or {}).get('name') or '').strip()
        if not label:
            continue
        for identity in region_volume_entities(region):
            volumes[identity] = label
    return {'surfaces': surfaces, 'volumes': volumes}


def source_identity_records(prepared_geometry) -> list[dict]:
    """One record per staged source, in the order the job lists its geometry.

    The runner receives a list of paths and has no way to tell which prepared
    source each one is; this is that join, and it is what turns "the surfaces
    this file added" into entity IDs the scopes are keyed on. ``confidence``
    is the weakest mapping confidence the prepared revision recorded for the
    source's own groups, carried so the receipt can say how much the mapping
    is worth rather than implying it is exact.
    """
    manifest = prepared_manifest(prepared_geometry)
    groups = _group_manifest(prepared_geometry).get('groups') or ()
    confidence: dict[str, float] = {}
    counted: dict[str, int] = {}
    for group in groups:
        source_id = _source_of(group)
        if not source_id:
            continue
        value = float((group or {}).get('mapping_confidence', 1.0) or 0.0)
        confidence[source_id] = min(confidence.get(source_id, 1.0), value)
        counted[source_id] = counted.get(source_id, 0) + len(
            group_surface_indices(group))
    records = []
    for index, record in enumerate(manifest.get('sources') or ()):
        if not isinstance(record, dict):
            continue
        source_id = str(record.get('geometry_id') or '').strip()
        records.append({
            'index': index,
            'source_id': source_id,
            'prepared_name': str(record.get('prepared_name') or ''),
            'display_name': str(record.get('display_name') or ''),
            'source_format': str(record.get('source_format') or ''),
            'declared_surfaces': counted.get(source_id, 0),
            'mapping_confidence': confidence.get(source_id, 1.0),
        })
    return records


def _group_manifest(prepared_geometry) -> dict:
    """The prepared group manifest, however the caller carries it.

    The facade passes ``prepared.reference`` -- a ``PreparedGeometryRef``,
    which holds ``group_manifest_path`` and *not* the manifest itself. Reading
    only the in-memory attribute therefore found nothing on the single code
    path that matters, so every derived name came back empty and every Gmsh
    patch published as ``face_<tag>``. MEASURED on the finned heat sink: 132 of
    132 patches unrated for fidelity.
    """
    if isinstance(prepared_geometry, dict):
        # R60/R152. `geometry.prepared.current` hands the GUI plain JSON, not
        # a `PreparedGeometryRef`, so a page asking which patch is surface 2
        # got `{}` from every getattr below and had nothing to show but the
        # bare index the dialog already displayed.
        manifest = prepared_geometry.get('group_manifest')
        if isinstance(manifest, dict) and manifest:
            return manifest
        if 'groups' in prepared_geometry or 'regions' in prepared_geometry:
            return prepared_geometry
        return {}
    manifest = getattr(prepared_geometry, 'group_manifest', None)
    if manifest:
        return manifest
    path = getattr(prepared_geometry, 'group_manifest_path', None)
    if path is None:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def surface_names(prepared_geometry) -> dict:
    """Imported surface index (as a string tag) -> solver patch name."""
    if prepared_geometry is None:
        return {}
    manifest = _group_manifest(prepared_geometry)
    names: dict[str, str] = {}
    for group in manifest.get('groups') or ():
        if not isinstance(group, dict):
            continue
        # `group_surface_indices` is the single reader of the store's
        # ordering. Without the `source_refs` fallback it holds, the lookup
        # found nothing for every real manifest, the runner fell back to
        # `face_<tag>`, and every published patch failed the identity join
        # -- so no Gmsh mesh could carry a per-section fidelity verdict
        # (Plan 23 §4). MEASURED on the heat sink: 132/132 unrated.
        indices = group_surface_indices(group)
        label = str(group.get('solver_name') or group.get('name') or '').strip()
        if not label:
            continue
        for index in indices:
            # Gmsh surface tags are 1-based in import order.
            names[str(int(index) + 1)] = label
    return names


def build_job(intent: JobIntent, *, geometry, outputs: dict,
              prepared_geometry=None, run_id: str = '',
              profile=None) -> dict:
    """Assemble the immutable job the runner consumes.

    Paths are written in the runtime's own namespace when a WSL profile is
    given, so the runner never has to translate anything itself.

    WP-01 F-35. ``geometry`` is a *list* of every staged source in prepared
    manifest order. The runner has always accepted a list and imported each
    entry, but the job only ever carried ``sources[0]``, so a case assembled
    from more than one imported file meshed the first file and silently
    dropped the rest. A single path is still accepted here and written as a
    one-entry list, because the job schema now has exactly one shape.
    """
    def translate(path):
        return (profile.translate_host_path(path) if profile is not None
                else str(Path(path)))

    sources = ([geometry] if isinstance(geometry, (str, Path))
               else list(geometry))
    if not sources:
        raise ExecutionError('a Gmsh job needs at least one geometry source')

    return {
        'schema_version': JOB_SCHEMA_VERSION,
        'engine_id': 'gmsh',
        'units': 'm',
        'run_id': run_id,
        'job_digest': intent.digest,
        'geometry': [translate(item) for item in sources],
        'intent': intent.to_dict(),
        # C31-04. The source-aware identity, and the source table the runner
        # needs to build it: which staged file is which prepared source, in
        # the order `geometry` above lists them. The four version-1 maps below
        # stay for a runner that predates this and for a single-source case,
        # where a source-local index and a global one are the same number.
        'entitySchemaVersion': ENTITY_SCHEMA_VERSION,
        'sources': source_identity_records(prepared_geometry),
        'scopeEntities': scope_entity_map(prepared_geometry),
        'entityNames': entity_name_map(prepared_geometry),
        'scopeSurfaces': scope_surface_map(prepared_geometry),
        'scopeVolumes': scope_volume_map(prepared_geometry),
        'surfaceNames': surface_names(prepared_geometry),
        'volumeNames': volume_names(prepared_geometry),
        # WP-06a's shell topology, wired: the seed decides which shell is the
        # domain and a declared role overrides what nesting would infer.
        # Absent means "infer", which is what every job carried until now.
        'seedPoint': seed_point(prepared_geometry),
        'shellRoles': shell_roles(prepared_geometry),
        'output': {key: translate(value) for key, value in outputs.items()},
    }


def write_job(db, bbox, case_path, *, prepared_geometry=None, profile=None,
              run_id: str = '', formats=('msh',)) -> dict:
    """Derive and persist one run's job. Returns the created run manifest."""
    case_path = Path(case_path)
    geometry = prepared_geometry_paths(prepared_geometry, case_path)
    if not geometry:
        raise ExecutionError(
            'Gmsh meshes the prepared CAD directly, so a prepared geometry '
            'revision is required before a job can be written')

    from foammesh.core.engine.registry import configured_target_solver

    intent = derive_from_native(
        _native_section(db), bbox=bbox,
        target_solver=configured_target_solver(db),
        metadata={'engine_id': 'gmsh', 'run_id': run_id})
    if intent.export.mesh_format == 'su2' and 'su2' not in formats:
        # A second-order mesh only survives in Gmsh's own SU2 file, so the run
        # has to be given somewhere to write it.
        formats = (*formats, 'su2')
    # Plan 31 FC-A, row `export-formats-unexposed`. A companion format is an
    # addition and never a replacement: the MSH is what the polyMesh
    # publisher, the element census and the geometry-fidelity check all read,
    # so a run asked for a MED alone would finish having written nothing this
    # application can open. What may be asked for is checked against what was
    # measured to round-trip, rather than passed to a writer whose output
    # nothing here could re-identify.
    companion_export_formats(formats)
    if 'msh' not in formats:
        formats = ('msh', *formats)
    run_id = run_id or f'gmsh-{uuid.uuid4().hex[:16]}'
    builder = RunBuilder(case_path)
    layout = RunLayout(builder.root(run_id))
    job = build_job(
        intent, geometry=geometry, outputs=layout.outputs(formats),
        prepared_geometry=prepared_geometry, run_id=run_id, profile=profile)
    try:
        script = runner_path()
    except Exception:
        script = None
    manifest = builder.create(run_id, job=job, profile=profile,
                              runner_path=script)
    return {
        'run_id': run_id,
        'job': job,
        'manifest': manifest.document,
        'job_path': str(layout.job),
        'run_path': str(layout.root),
        'warnings': list(intent.warnings),
    }


def prepared_manifest(prepared_geometry) -> dict:
    """The prepared-geometry manifest, however the caller carries it.

    A ``PreparedGeometryRef`` holds only ``manifest_path``; the GUI is handed
    the manifest itself as plain JSON. Both have to reach the same reader, or
    the job that the facade writes and the job the pages describe disagree.
    """
    if prepared_geometry is None:
        return {}
    if isinstance(prepared_geometry, dict):
        manifest = prepared_geometry.get('manifest')
        if isinstance(manifest, dict) and manifest:
            return manifest
        return prepared_geometry if 'sources' in prepared_geometry else {}
    manifest = getattr(prepared_geometry, 'manifest', None)
    if isinstance(manifest, dict) and manifest:
        return manifest
    path = getattr(prepared_geometry, 'manifest_path', None)
    if path is None:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def prepared_source_names(prepared_geometry) -> tuple[str, ...]:
    """The staged file name of every prepared source, in manifest order."""
    manifest = prepared_manifest(prepared_geometry)
    return tuple(
        str(record['prepared_name'])
        for record in (manifest.get('sources') or ())
        if isinstance(record, dict) and record.get('prepared_name'))


def prepared_geometry_paths(prepared_geometry, case_path: Path) -> tuple[Path, ...]:
    """Every staged source Gmsh will import, in prepared manifest order.

    WP-01 F-35. The prepared revision stages one file per imported geometry
    under ``<revision>/sources``; the job used to carry only the first of
    them, so an assembly imported as several files meshed as one. The single
    ``geometry_path`` on the reference (which is ``sources[0]``) stays as the
    fallback for a caller that has no manifest to read, such as a test double.
    """
    if prepared_geometry is None:
        return ()
    root = getattr(prepared_geometry, 'root', None)
    names = prepared_source_names(prepared_geometry)
    if root and names:
        staged = [Path(root) / 'sources' / name for name in names]
        if all(path.is_file() for path in staged):
            return tuple(staged)
    single = _prepared_geometry_path(prepared_geometry, case_path)
    return (single,) if single is not None else ()


def _prepared_geometry_path(prepared_geometry, case_path: Path):
    """The staged CAD file Gmsh will import."""
    if prepared_geometry is None:
        return None
    for attribute in ('cad_path', 'geometry_path', 'path'):
        value = getattr(prepared_geometry, attribute, None)
        if value:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = case_path / candidate
            if candidate.is_file():
                return candidate
    return None


def stage_run(engine, stage: str, *, db, case_path: Path, executable: str,
              profile=None, run_id: str = '') -> StageRun:
    """Prepare the supervised launch for one engine stage."""
    definition = engine.validate(stage)
    case_path = Path(case_path)
    if stage != 'compute':
        # publish and checkMesh are driven by the facade, not by argv here.
        return StageRun(definition, (), case_path, None, ())
    if profile is None:
        raise ExecutionError(
            'no qualified Gmsh runtime is configured; Gmsh runs inside a WSL '
            'distribution and none was found')
    run_id = run_id or _latest_run_id(case_path)
    if not run_id:
        raise ExecutionError(
            'no Gmsh job has been written for this case; derive a plan first')
    layout = RunLayout(RunBuilder(case_path).root(run_id))
    if not layout.job.is_file():
        raise ExecutionError(f'run {run_id} has no job file')
    command = profile.runner_argv(runner_path(), layout.job, cwd=case_path)
    return StageRun(
        definition, command.argv, case_path, None,
        tuple(layout.outputs(('msh',)).values()))


def _latest_run_id(case_path: Path) -> str:
    root = Path(case_path) / 'foammesh' / 'runs'
    if not root.is_dir():
        return ''
    candidates = sorted(
        (item for item in root.iterdir() if (item / 'job.json').is_file()),
        key=lambda item: item.stat().st_mtime)
    return candidates[-1].name if candidates else ''
