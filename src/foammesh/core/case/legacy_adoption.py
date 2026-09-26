"""Opening a case written before the Plan 31 contracts, and adopting it.

Plan 31 CP-11 (C31-14). Packages CP-01 to CP-10 changed what a case records:
the Gmsh job now carries source-aware entity identity instead of a
source-local integer index (``ENTITY_SCHEMA_VERSION`` 2), the export block
describes target, MSH version and publisher as one decision, and a result is
named by the artifact its own run wrote rather than by whatever sits at
``constant/polyMesh``. Cases on disk predate all of that.

This module is the reader for those cases. Three rules shape it, and each one
came from something measured on the 42 Gmsh runs and 51 snappy cases under
``test_cases/``:

**Nothing is guessed.** A field that was never written cannot be recovered by
inference. ``run-manifest.json`` carries ``exit_code: null`` on every run in
the history of this repository -- the recorder read a key the payload never
had (fault DP-12) -- so a migration that filled it in from ``status`` would be
inventing the one column that would have shown a process never started. It
stays null here, and the run is marked as carrying evidence that cannot be
completed.

**Nothing is overwritten.** Migration writes new documents beside the old ones,
under ``foammesh/migration/<stamp>/``, or into a copy of the whole case. The
original run directories, the meshes in them and the published
``constant/polyMesh`` are never touched. A migration that loses an accepted
mesh is worse than one that refuses.

**A provenance that cannot be reconstructed is refused, not approximated.** A
legacy scope map is ``{group token: [index]}`` where the index is local to one
imported file and the runner resolved it globally. On a single-source case
those are the same number, the prepared revision can prove it, and the map
lifts to entity IDs exactly. On a multi-source case they are not the same
number and no record on disk says which file an index belonged to: that case
is told to re-prepare rather than handed a mapping nobody checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import shutil
import time

from foammesh.core.quality.checkmesh_service import (
    NATIVE_CHECK, report_path as quality_report_path)
from foammesh.core.quantities import agreeing, count_text

#: The version of the adoption report itself.
ADOPTION_SCHEMA_VERSION = 1

#: Where a migration writes, relative to the case sidecar directory.
MIGRATION_DIRECTORY = 'migration'

# -- dispositions ----------------------------------------------------------- #

#: The record is already at the current contract; nothing to do.
CURRENT = 'current'
#: The record was rewritten to the current contract, with every reconstructed
#: field checked against the prepared revision it came from.
MIGRATED = 'migrated'
#: The artifact is preserved and readable, but evidence attached to it was
#: produced under a superseded contract and cannot be completed from what is
#: on disk. Re-run the check before quoting it.
REVALIDATE = 'revalidate'
#: Provenance cannot be reconstructed reliably. The geometry must be prepared
#: again before scoped controls or their receipts can be trusted.
REPREPARE = 'reprepare'
#: The record is missing or unparseable.
UNREADABLE = 'unreadable'

#: Ordered worst-last, so a case takes the disposition of its worst record.
_SEVERITY = {CURRENT: 0, MIGRATED: 1, REVALIDATE: 2, REPREPARE: 3,
             UNREADABLE: 4}


def current_record_versions() -> dict:
    """What each record type's owning module says the current version is.

    Read from the owners rather than restated here: a version copied into a
    second file is a version that drifts, which is exactly how the MSH
    version and the publisher came to disagree (C31-01).
    """
    from foammesh.core.case.model import CASE_METADATA_VERSION
    from foammesh.core.geometry.prepared import (
        GROUP_SCHEMA_VERSION, PREPARED_SCHEMA_VERSION)
    from foammesh.core.gmsh.execution import (
        ENTITY_SCHEMA_VERSION, JOB_SCHEMA_VERSION)
    from foammesh.core.gmsh.layers import CALCULATION_VERSION as LAYERS_VERSION
    from foammesh.core.gmsh.manifest import SCHEMA_VERSION as MANIFEST_VERSION
    from foammesh.core.gmsh.plan_derivation import EXPORT_VERSION

    return {
        'case_metadata_format': CASE_METADATA_VERSION,
        'gmsh_job_schema': JOB_SCHEMA_VERSION,
        'gmsh_entity_schema': ENTITY_SCHEMA_VERSION,
        'gmsh_run_manifest_schema': MANIFEST_VERSION,
        'gmsh_export_calculation': EXPORT_VERSION,
        'gmsh_layers_calculation': LAYERS_VERSION,
        'prepared_geometry_schema': PREPARED_SCHEMA_VERSION,
        'prepared_group_schema': GROUP_SCHEMA_VERSION,
    }


class AdoptionError(RuntimeError):
    pass


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8')), ''
    except FileNotFoundError:
        return None, 'file is missing'
    except (OSError, ValueError) as error:
        return None, str(error)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _msh_version_of(path: Path) -> str:
    """The version in a ``.msh`` file's own header, or ''.

    The header is the only place a finished run still says which MSH version
    it wrote: the legacy job records the format it asked for only from the
    revision that added an export block, and half the runs on disk predate it.
    """
    try:
        with Path(path).open('r', encoding='utf-8', errors='replace') as msh:
            if msh.readline().strip() != '$MeshFormat':
                return ''
            return msh.readline().split()[0]
    except (OSError, IndexError):
        return ''


@dataclass
class RecordAdoption:
    """One record inside a case, and what can be done with it."""

    kind: str
    path: str
    disposition: str
    reasons: list = field(default_factory=list)
    findings: dict = field(default_factory=dict)
    #: Documents the migration would write, keyed by file name.
    documents: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'path': self.path,
                'disposition': self.disposition,
                'reasons': list(self.reasons), 'findings': dict(self.findings)}


@dataclass
class CaseAdoption:
    """What one case carries, and what adopting it would take."""

    case_path: str
    disposition: str = CURRENT
    records: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    #: Files whose loss would lose a result: they are copied, never rewritten.
    preserved: list = field(default_factory=list)
    #: Written by :func:`migrate_case`.
    migration_path: str = ''
    copied_to: str = ''

    @property
    def stale_evidence(self) -> list:
        return [record for record in self.records
                if record.findings.get('stale_evidence')]

    def to_dict(self) -> dict:
        return {
            'schema_version': ADOPTION_SCHEMA_VERSION,
            'case_path': self.case_path,
            'disposition': self.disposition,
            'current_record_versions': current_record_versions(),
            'reasons': list(self.reasons),
            'records': [record.to_dict() for record in self.records],
            'preserved': list(self.preserved),
            'stale_evidence': [record.path for record in self.stale_evidence],
            'migration_path': self.migration_path,
            'copied_to': self.copied_to,
        }


# -- the case sidecar ------------------------------------------------------- #

def _adopt_sidecar(case_path: Path) -> RecordAdoption:
    from foammesh.core.case.model import (
        CASE_METADATA_FILE, CASE_METADATA_VERSION, SIDECAR_DIRECTORY)

    path = case_path / SIDECAR_DIRECTORY / CASE_METADATA_FILE
    document, error = _read_json(path)
    if document is None:
        return RecordAdoption('case-metadata', str(path), UNREADABLE,
                              [f'cannot read the case sidecar: {error}'])
    version = document.get('format_version')
    findings = {
        'format_version': version,
        'workflow': document.get('workflow'),
        'mesh_origin': document.get('mesh_origin'),
        'mesh_fingerprint': document.get('mesh_fingerprint'),
        'target': document.get('target'),
        'has_mesh': (case_path / 'constant' / 'polyMesh' / 'owner').is_file(),
    }
    reasons = []
    disposition = CURRENT
    if version != CASE_METADATA_VERSION:
        return RecordAdoption(
            'case-metadata', str(path), REPREPARE,
            [f'sidecar format version {version!r} is not the current '
             f'{CASE_METADATA_VERSION}; FoamMesh refuses to read it rather '
             'than guess what it meant'], findings)
    if findings['has_mesh'] and not document.get('mesh_fingerprint'):
        # The reason `classify_case` already gives, restated as a disposition:
        # the mesh is real and usable, the claim that this case authored it is
        # not on disk, and nothing here will invent one.
        disposition = REVALIDATE
        findings['stale_evidence'] = True
        reasons.append(
            'a mesh exists beside a sidecar that records no workflow and no '
            'mesh fingerprint, so the case opens as External mesh: the mesh '
            'is kept and used, and the steps that made it are not claimed')
    return RecordAdoption('case-metadata', str(path), disposition, reasons,
                          findings)


# -- Gmsh runs -------------------------------------------------------------- #

def _prepared_revision_for(case_path: Path, job: dict):
    """The prepared revision a legacy job meshed, read off disk.

    The job records the staged source path in the *runtime's* namespace (a
    ``/mnt/d/...`` path when the run went through WSL), so the revision is
    found by its directory name inside the path rather than by opening it.
    """
    sources = job.get('geometry')
    paths = [sources] if isinstance(sources, str) else list(sources or ())
    revisions = {part for path in paths
                 for part in Path(str(path).replace('\\', '/')).parts
                 if part.startswith('pg-')}
    if len(revisions) != 1:
        return None, '', paths
    revision = revisions.pop()
    root = case_path / 'foammesh' / 'geometry' / 'prepared' / revision
    manifest, _ = _read_json(root / 'prepared-geometry.json')
    groups, _ = _read_json(root / 'group-manifest.json')
    if manifest is None or groups is None:
        return None, revision, paths
    return {'manifest': manifest, 'group_manifest': groups}, revision, paths


def _scoped_controls(intent: dict) -> int:
    """How many authored controls in this job name a scope."""
    count = 0
    for key in ('curveControls', 'volumeControls'):
        count += len(intent.get(key) or ())
    for entry in (intent.get('sizeFields') or {}).get('fields') or ():
        if isinstance(entry, dict) and entry.get('scope'):
            count += 1
    layers = intent.get('layers') or {}
    if layers.get('scope') not in (None, '', 'all_boundary_surfaces'):
        count += 1
    return count


def _layer_policy(job: dict) -> tuple:
    """What this run asked for in boundary layers, and what has to be said.

    R118 gave the layer block a ``patches`` key and a scope that can name the
    patches instead of layering the whole boundary. MEASURED 6 September 2026
    over the 42 Gmsh jobs in the catalogue: 22 record
    ``scope: selected_patches`` and name their patches, 6 record
    ``all_boundary_surfaces`` and carry the key empty, and 14 -- the ones
    written before R118 -- carry no ``patches`` key at all and record
    ``all_boundary_surfaces``. All 42 record ``calculation_version:
    gmsh.layers.v1``.

    So the absent key and the recorded scope agree, and the key is written
    explicitly rather than left to be re-derived from a default that may move
    again. A job that says it layered named patches and then names none is not
    reconstructible from anything on disk, and is sent back for revalidation
    rather than silently read as the whole boundary. A recorded calculation
    version that is not the current one is the same kind of gap: the heights
    were computed by arithmetic this build no longer contains, so they are
    kept as the record of what ran and the run is sent back before they are
    compared with anything.

    Returns ``(findings, normalised layer block or None, reasons,
    disposition)``.
    """
    from foammesh.core.gmsh.layers import (CALCULATION_VERSION, SCOPE,
                                           SCOPE_SELECTED)

    layers = (job.get('intent') or {}).get('layers') or {}
    findings = {
        'layers_recorded': bool(layers),
        'layers_enabled': bool(layers.get('enabled')),
        'layers_scope_recorded': str(layers.get('scope') or ''),
        'layers_patches_recorded': 'patches' in layers,
        'layers_calculation_version': str(
            layers.get('calculation_version') or ''),
    }
    if not layers or not layers.get('enabled'):
        return findings, None, [], CURRENT

    reasons = []
    disposition = CURRENT
    version = findings['layers_calculation_version']
    if version and version != CALCULATION_VERSION:
        disposition = REVALIDATE
        reasons.append(
            f'the layer heights in this job were computed by {version} and '
            f'this build computes {CALCULATION_VERSION}; they are kept as '
            'the record of what ran, and must be re-derived before they are '
            'compared with a current run')

    if findings['layers_patches_recorded']:
        return findings, None, reasons, disposition

    if findings['layers_scope_recorded'] == SCOPE_SELECTED:
        return findings, None, reasons + [
            'this job layered named patches and no record on disk says which '
            'ones, so the scope cannot be reconstructed; author the layer '
            'scope again before re-running'
        ], REVALIDATE

    normalised = dict(layers)
    normalised['scope'] = layers.get('scope') or SCOPE
    normalised['patches'] = []
    return findings, normalised, reasons, disposition


def _with_layer_policy(job: dict, layers) -> dict:
    """A copy of *job* carrying *layers*, or an unchanged copy when None."""
    document = dict(job)
    if layers is None:
        return document
    intent = dict(document.get('intent') or {})
    intent['layers'] = layers
    document['intent'] = intent
    return document


def _migrate_job(case_path: Path, job: dict) -> tuple:
    """Lift one legacy job to the current entity contract, or refuse.

    Returns ``(document or None, disposition, reasons, findings)``.
    """
    from foammesh.core.gmsh import execution

    findings = {
        'job_schema_version': job.get('schema_version'),
        'entity_schema_version': job.get('entitySchemaVersion'),
        'scope_surface_entries': len(job.get('scopeSurfaces') or {}),
        'scope_volume_entries': len(job.get('scopeVolumes') or {}),
        'scoped_controls': _scoped_controls(job.get('intent') or {}),
    }
    (layer_findings, layer_block, layer_reasons,
     layer_disposition) = _layer_policy(job)
    findings.update(layer_findings)

    entity_version = job.get('entitySchemaVersion')
    if entity_version == execution.ENTITY_SCHEMA_VERSION:
        if layer_block is None:
            return None, layer_disposition, layer_reasons, findings
        document = _with_layer_policy(job, layer_block)
        document['migration'] = {
            'schema_version': ADOPTION_SCHEMA_VERSION,
            'from_entity_schema': entity_version,
            'to_entity_schema': entity_version,
            'layers': 'the layer block gained the patches key it predates, '
                      'written empty because this run layered the whole '
                      'boundary',
        }
        return (document,
                max(MIGRATED, layer_disposition, key=_SEVERITY.get),
                layer_reasons, findings)

    prepared, revision, paths = _prepared_revision_for(case_path, job)
    findings['prepared_revision'] = revision
    findings['sources_in_job'] = len(paths)
    if len(paths) > 1:
        return None, REPREPARE, [
            'the job names more than one imported source and its scopes are '
            'source-local indices the runner resolved globally (C31-04), so '
            'no record on disk says which file an index belonged to; prepare '
            'the geometry again and re-run before trusting any scoped control'
        ], findings
    if prepared is None:
        scoped = findings['scoped_controls'] + findings['scope_surface_entries'] \
            + findings['scope_volume_entries']
        if scoped:
            return None, REPREPARE, [
                f'the prepared revision {revision or "(unnamed)"} this run '
                'meshed is no longer in the case, so the scopes it recorded '
                'cannot be attributed to a source; prepare the geometry again'
            ], findings
        return None, REVALIDATE, [
            'the prepared revision this run meshed is no longer in the case; '
            'the mesh and its manifest are kept, and nothing scoped was '
            'recorded that could be mis-attributed'
        ], findings

    # The check that makes this a migration rather than a guess: recompute the
    # version-1 maps from the prepared revision still on disk and require them
    # to equal the ones the job recorded. Equal maps mean the entity IDs
    # derived from the same revision describe the same entities.
    recomputed_surfaces = execution.scope_surface_map(prepared)
    recomputed_volumes = execution.scope_volume_map(prepared)
    mismatches = []
    for name, recorded, recomputed in (
            ('scopeSurfaces', job.get('scopeSurfaces') or {}, recomputed_surfaces),
            ('scopeVolumes', job.get('scopeVolumes') or {}, recomputed_volumes)):
        for token, indices in recorded.items():
            if [int(value) for value in indices] != [
                    int(value) for value in recomputed.get(token, [])]:
                mismatches.append(f'{name}[{token}]')
    findings['recomputed_scope_surfaces'] = len(recomputed_surfaces)
    findings['recomputed_scope_volumes'] = len(recomputed_volumes)
    if mismatches:
        return None, REPREPARE, [
            'the prepared revision no longer places these scopes where the '
            'job recorded them, so the topology changed under the run: '
            + ', '.join(sorted(mismatches))
        ], findings

    sources = execution.source_identity_records(prepared)
    scope_entities = execution.scope_entity_map(prepared)
    entity_names = execution.entity_name_map(prepared)
    named = len(entity_names['surfaces']) + len(entity_names['volumes'])
    covered = set(scope_entities['surfaces']) | set(scope_entities['volumes'])
    recorded_tokens = set(job.get('scopeSurfaces') or {}) | set(
        job.get('scopeVolumes') or {})
    groups = (prepared['group_manifest'].get('groups') or ())
    if (not sources or not all(record.get('source_id') for record in sources)
            or (groups and not named)):
        return None, REPREPARE, [
            'the prepared revision carries no geometry_id for its sources or '
            'their groups, so no entity in this job can be named and the '
            'migrated job would scope nothing; prepare the geometry again'
        ], findings
    if recorded_tokens - covered:
        return None, REPREPARE, [
            'the prepared revision no longer names these scopes, so the '
            'controls authored on them would silently apply to nothing: '
            + ', '.join(sorted(recorded_tokens - covered))
        ], findings

    document = _with_layer_policy(job, layer_block)
    document['entitySchemaVersion'] = execution.ENTITY_SCHEMA_VERSION
    document['sources'] = sources
    document['scopeEntities'] = scope_entities
    document['entityNames'] = entity_names
    document['migration'] = {
        'schema_version': ADOPTION_SCHEMA_VERSION,
        'from_entity_schema': entity_version,
        'to_entity_schema': execution.ENTITY_SCHEMA_VERSION,
        'reconstructed_from': revision,
        'checked': 'the version-1 scope maps recomputed from the prepared '
                   'revision equal the ones this job recorded',
    }
    if layer_block is not None:
        document['migration']['layers'] = (
            'the layer block gained the patches key it predates, written '
            'empty because this run layered the whole boundary')
    return (document, max(MIGRATED, layer_disposition, key=_SEVERITY.get),
            layer_reasons, findings)


def _artifact_rows(run_path: Path) -> list:
    """The run's outputs as they are on disk, measured rather than recalled.

    The manifest gained artifact rows partway through the catalogue: 14 of the
    42 Gmsh runs under ``test_cases/`` have none. The files are still there, so
    the rows are rebuilt from them and marked as rebuilt.
    """
    formats = {'.msh': 'msh', '.su2': 'su2', '.cgns': 'cgns', '.vtk': 'vtk'}
    rows = []
    for path in sorted(run_path.glob('mesh.*')):
        token = formats.get(path.suffix.lower())
        if token is None:
            continue
        rows.append({'format': token, 'name': path.name, 'path': str(path),
                     'exists': True, 'bytes': path.stat().st_size,
                     'sha256': _sha256_of(path), 'reconstructed': True})
    return rows


def _result_handle(run_path: Path, manifest: dict, case_path: Path,
                   artifacts: list) -> dict:
    """The section-4.2 handle for a finished run, from its own artifact.

    C31-03. The legacy manifest records a publication destination of
    ``<case>/constant/polyMesh`` and nothing else, so a reader asking "what did
    this run produce?" was answered with whatever the case root holds now --
    which, after a later run, is a different mesh. The native artifact is the
    file in the run's own directory.
    """
    from foammesh.core import run_result

    native = next((row for row in artifacts if row['format'] == 'msh'), None)
    verdict = run_result.NOTHING
    quality = manifest.get('quality_verdict') or {}
    if quality:
        verdict = (run_result.ACCEPTED if quality.get('accepted')
                   else run_result.REFUSED)
    published = bool((manifest.get('publication') or {}).get('destination'))
    return {
        'run_id': str(manifest.get('run_id') or run_path.name),
        'engine': str(manifest.get('engine_id') or 'gmsh'),
        'artifact_path': native['path'] if native else '',
        'artifact_format': run_result.GMSH_MSH if native else '',
        'artifact_role': run_result.NATIVE,
        'content_id': str(manifest.get('mesh_sha256')
                          or (native or {}).get('sha256') or ''),
        'verdict': verdict,
        'case_root': '',
        'derived': ([{'role': run_result.EXPORTED,
                      'artifact_format': run_result.POLY_MESH,
                      'artifact_path': str(case_path / 'constant' / 'polyMesh'),
                      'note': 'published by this run into the case root; a '
                              'later run may have replaced it, so it is not '
                              'this run\'s identity'}]
                    if published else []),
    }


def _adopt_gmsh_run(case_path: Path, run_path: Path) -> list:
    records = []
    job, error = _read_json(run_path / 'job.json')
    if job is None:
        records.append(RecordAdoption(
            'gmsh-job', str(run_path / 'job.json'), UNREADABLE,
            [f'cannot read the job: {error}']))
        job = {}
        job_disposition = UNREADABLE
    else:
        document, job_disposition, reasons, findings = _migrate_job(case_path, job)
        record = RecordAdoption('gmsh-job', str(run_path / 'job.json'),
                                job_disposition, reasons, findings)
        if document is not None:
            record.documents['job.json'] = document
        records.append(record)

    manifest, error = _read_json(run_path / 'run-manifest.json')
    if manifest is None:
        records.append(RecordAdoption(
            'gmsh-run-manifest', str(run_path / 'run-manifest.json'),
            UNREADABLE, [f'cannot read the run manifest: {error}']))
        return records

    artifacts = _artifact_rows(run_path)
    findings = {
        'manifest_schema_version': manifest.get('schema_version'),
        'status': manifest.get('status'),
        'exit_code_recorded': manifest.get('exit_code') is not None,
        'artifact_rows_recorded': len(manifest.get('artifacts') or ()),
        'artifact_rows_on_disk': len(artifacts),
        'quality_accepted': (manifest.get('quality_verdict') or {}).get('accepted'),
        'mesh_sha256_recorded': bool(manifest.get('mesh_sha256')),
        'export_settings_recorded': bool(
            ((job.get('intent') or {}).get('export'))),
    }
    native = next((row for row in artifacts if row['format'] == 'msh'), None)
    if native:
        findings['msh_version_in_file'] = _msh_version_of(Path(native['path']))
        findings['mesh_sha256_matches'] = (
            manifest.get('mesh_sha256') in (None, '', native['sha256'])
            or manifest.get('mesh_sha256') == native['sha256'])
    reasons = []
    disposition = CURRENT
    document = dict(manifest)
    if not findings['exit_code_recorded']:
        # DP-12: read from a key the payload never had. Every manifest in the
        # catalogue carries null here, succeeded and failed alike.
        disposition = REVALIDATE
        findings['stale_evidence'] = True
        reasons.append(
            'exit_code was never recorded by the build that wrote this run '
            '(fault DP-12), so the manifest cannot say whether a process '
            'exited at all; it is left null rather than inferred from status, '
            'and the run must be repeated to obtain that evidence')
    if not findings['artifact_rows_recorded'] and artifacts:
        disposition = max(disposition, MIGRATED, key=_SEVERITY.get)
        document['artifacts'] = artifacts
        reasons.append(
            f'{count_text(len(artifacts), "output file")} '
            f'{agreeing(len(artifacts), "exists", "exist")} in the run '
            f'directory and {agreeing(len(artifacts), "was", "were")} not '
            'recorded; the rows are rebuilt from the files, hashed as found')
    if 'result_handle' not in manifest and artifacts:
        document['result_handle'] = _result_handle(
            run_path, manifest, case_path, artifacts)
        disposition = max(disposition, MIGRATED, key=_SEVERITY.get)
        reasons.append(
            'the run had no result handle, so its result resolved to whatever '
            'the case root held; the native artifact in this run directory is '
            'recorded as the run\'s own result (C31-03)')
    if findings['mesh_sha256_recorded'] and native and not findings.get(
            'mesh_sha256_matches'):
        disposition = REVALIDATE
        findings['stale_evidence'] = True
        reasons.append(
            'the mesh on disk does not hash to the digest the manifest '
            'recorded; the file is preserved and its recorded evidence is not '
            'attributable to it')
    if document != manifest:
        document['migration'] = {
            'schema_version': ADOPTION_SCHEMA_VERSION,
            'source': str(run_path / 'run-manifest.json'),
            'unreconstructed': ([] if findings['exit_code_recorded']
                                else ['exit_code']),
        }
    record = RecordAdoption('gmsh-run-manifest',
                            str(run_path / 'run-manifest.json'),
                            disposition, reasons, findings)
    if document != manifest:
        record.documents['run-manifest.json'] = document
    records.append(record)
    return records


# -- snappy evidence -------------------------------------------------------- #

def _adopt_snappy_evidence(case_path: Path) -> list:
    """The records a snappy case keeps outside a run directory.

    snappyHexMesh runs through the OpenFOAM utilities and leaves its evidence
    in ``foammesh/dictionaries`` and ``foammesh/quality``, not in a run
    manifest. Neither names a run, so neither can be tied to the mesh beside
    it; both are preserved and reported as evidence to re-take.
    """
    records = []
    dictionaries = case_path / 'foammesh' / 'dictionaries' / 'last-run.json'
    if dictionaries.is_file():
        document, error = _read_json(dictionaries)
        if document is None:
            records.append(RecordAdoption(
                'snappy-dictionaries', str(dictionaries), UNREADABLE,
                [f'cannot read the dictionary snapshot: {error}']))
        else:
            findings = {
                'configuration_sha256': document.get('configuration_sha256'),
                'files': sorted((document.get('files') or {}).keys()),
                'names_a_run': bool(document.get('run_id')),
            }
            records.append(RecordAdoption(
                'snappy-dictionaries', str(dictionaries),
                CURRENT if findings['names_a_run'] else REVALIDATE,
                ([] if findings['names_a_run'] else [
                    'the dictionary snapshot records no run identity, so the '
                    'dictionaries it holds cannot be tied to the mesh in the '
                    'case; they are kept as the record of what was written']),
                findings))
    # Asked for by name rather than spelled out here. MeshCheckService owns
    # where a check lands, and a second literal copy of that path is how a
    # reader ends up inspecting a file the writer stopped using.
    quality = quality_report_path(case_path, NATIVE_CHECK)
    if quality.is_file():
        document, error = _read_json(quality)
        if document is None:
            records.append(RecordAdoption(
                'mesh-check', str(quality), UNREADABLE,
                [f'cannot read the mesh check record: {error}']))
        else:
            recorded = document.get('mesh_fingerprint')
            measured = _poly_mesh_fingerprint(case_path)
            findings = {
                'checked_at': document.get('checked_at'),
                'names_a_run': bool(document.get('run_id')),
                'recorded_mesh_fingerprint': recorded,
                'measured_mesh_fingerprint': measured,
            }
            reasons = []
            if not recorded:
                reasons.append(
                    'the recorded mesh check names neither a run nor a mesh '
                    'fingerprint, so it cannot be shown to describe any mesh '
                    'in the case; run Mesh check again to attach current '
                    'evidence')
            elif not measured:
                reasons.append(
                    'the recorded mesh check names a mesh fingerprint and '
                    'there is no complete polyMesh in the case to compare it '
                    'against, so the check describes a mesh that is not here')
            elif recorded != measured:
                reasons.append(
                    'the mesh in the case now fingerprints differently from '
                    'the one this check measured, so the recorded verdict is '
                    'not evidence about it; the mesh is kept and the check '
                    'must be run again')
            if reasons:
                findings['stale_evidence'] = True
            records.append(RecordAdoption(
                'mesh-check', str(quality),
                REVALIDATE if reasons else CURRENT, reasons, findings))
    return records


def _poly_mesh_fingerprint(case_path: Path) -> str:
    """The fingerprint of the mesh actually in the case, or ''."""
    from foammesh.core.case.model import fingerprint_poly_mesh
    try:
        return fingerprint_poly_mesh(case_path / 'constant' / 'polyMesh').digest
    except (OSError, ValueError):
        return ''


# -- the case --------------------------------------------------------------- #

def _preserved_paths(case_path: Path) -> list:
    """Everything whose loss would lose a result."""
    preserved = []
    poly_mesh = case_path / 'constant' / 'polyMesh'
    if (poly_mesh / 'owner').is_file():
        preserved.append(str(poly_mesh))
    runs = case_path / 'foammesh' / 'runs'
    for run in sorted(runs.glob('*')) if runs.is_dir() else ():
        preserved.extend(str(path) for path in sorted(run.glob('mesh.*')))
    return preserved


def inspect_case(case_path) -> CaseAdoption:
    """Read one case and report what adopting it would take. Changes nothing."""
    case_path = Path(case_path)
    if not case_path.is_dir():
        raise AdoptionError(f'{case_path} is not a directory')
    report = CaseAdoption(str(case_path))
    report.records.append(_adopt_sidecar(case_path))
    runs = case_path / 'foammesh' / 'runs'
    if runs.is_dir():
        for run in sorted(path for path in runs.iterdir() if path.is_dir()):
            report.records.extend(_adopt_gmsh_run(case_path, run))
    report.records.extend(_adopt_snappy_evidence(case_path))
    report.preserved = _preserved_paths(case_path)
    report.disposition = max(
        (record.disposition for record in report.records),
        key=_SEVERITY.get, default=CURRENT)
    report.reasons = [reason for record in report.records
                      for reason in record.reasons]
    return report


def migrate_case(case_path, *, destination=None, stamp: str = '') -> CaseAdoption:
    """Adopt a case, writing new records beside the old ones.

    With ``destination`` the whole case is copied there first and the copy is
    migrated, so the original is not opened for writing at all. Without one,
    the migration is written into ``foammesh/migration/<stamp>/`` inside the
    case: no existing file is read-modify-written, no run directory is
    touched, and the meshes named in :attr:`CaseAdoption.preserved` are exactly
    as they were.
    """
    source = Path(case_path)
    copied_to = ''
    if destination is not None:
        destination = Path(destination)
        if destination.exists():
            raise AdoptionError(f'{destination} already exists; migration '
                                'writes a new copy and never merges into one')
        shutil.copytree(source, destination)
        copied_to = str(destination)
        source = destination

    report = inspect_case(source)
    report.copied_to = copied_to
    stamp = stamp or time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    root = source / 'foammesh' / MIGRATION_DIRECTORY / stamp
    root.mkdir(parents=True, exist_ok=False)
    for record in report.records:
        if not record.documents:
            continue
        target = root / Path(record.path).parent.name
        target.mkdir(parents=True, exist_ok=True)
        for name, document in record.documents.items():
            (target / name).write_text(
                json.dumps(document, indent=2, sort_keys=True) + '\n',
                encoding='utf-8')
    report.migration_path = str(root)
    (root / 'migration-report.json').write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    return report
