"""Portable case identity and workflow-mode rules.

The legacy UI treats the linear meshing wizard as the only source of a mesh.
That is no longer true once a raw OpenFOAM case or imported mesh can be opened.
This module keeps those concepts separate:

``content`` answers what artifacts exist; ``workflow`` answers whether the
authored Geometry -> Export navigation is valid for the current mesh.

Only the standard library is used so this logic can run during startup and in
headless API/CLI clients.
"""
from __future__ import annotations

import hashlib
import gzip
import json
import os
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


SIDECAR_DIRECTORY = 'foammesh'
CASE_METADATA_FILE = 'project.json'
CASE_METADATA_VERSION = 2
DEFAULT_OPENFOAM_TARGET = 'openfoam-foundation-v13'
_REQUIRED_POLY_MESH_FILES = ('boundary', 'faces', 'owner', 'points')
_OPTIONAL_POLY_MESH_FILES = ('neighbour',)
_FINGERPRINT_POLY_MESH_FILES = ('boundary', 'faces', 'neighbour', 'owner', 'points')


class CaseMetadataError(ValueError):
    """A sidecar metadata document is malformed or unsupported."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class CaseKind(str, Enum):
    EMPTY = 'empty'
    OPENFOAM_CASE = 'openfoam_case'
    RAW_POLY_MESH_CASE = 'raw_poly_mesh_case'
    FOAMMESH_CASE = 'foammesh_case'
    INVALID = 'invalid'


class WorkflowMode(str, Enum):
    NONE = 'none'
    AUTHORED = 'authored'
    MESH_EXTERNAL = 'mesh_external'


class MeshOrigin(str, Enum):
    NONE = 'none'
    GENERATED = 'generated'
    IMPORTED = 'imported'
    OPENED_NATIVE = 'opened_native'
    DERIVED_MUTATION = 'derived_mutation'


class ArtifactState(str, Enum):
    ABSENT = 'absent'
    CURRENT = 'current'
    STALE = 'stale'
    INVALID = 'invalid'
    RECOVERING = 'recovering'


@dataclass(frozen=True)
class MeshFingerprint:
    """Content fingerprint of required and present optional polyMesh files."""

    digest: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {'digest': self.digest, 'files': list(self.files)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'MeshFingerprint':
        try:
            digest = str(value['digest'])
            files = tuple(str(item) for item in value['files'])
        except (KeyError, TypeError) as error:
            raise CaseMetadataError('mesh_fingerprint must contain digest and files') from error
        if len(digest) != 64 or any(char not in '0123456789abcdef' for char in digest.lower()):
            raise CaseMetadataError('mesh_fingerprint.digest must be a SHA-256 hex digest')
        if not files:
            raise CaseMetadataError('mesh_fingerprint.files must not be empty')
        return cls(digest=digest, files=files)


@dataclass(frozen=True)
class CaseMetadata:
    """Persisted, sidecar-owned state needed before the configuration DB opens."""

    format_version: int = CASE_METADATA_VERSION
    case_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    target: str = DEFAULT_OPENFOAM_TARGET
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    workflow: WorkflowMode = WorkflowMode.NONE
    mesh_origin: MeshOrigin = MeshOrigin.NONE
    artifact_state: ArtifactState = ArtifactState.ABSENT
    mesh_fingerprint: MeshFingerprint | None = None
    authored_workflow_suspended: bool = False
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.format_version != CASE_METADATA_VERSION:
            raise CaseMetadataError(
                f'unsupported FoamMesh case metadata version: {self.format_version}')
        try:
            uuid.UUID(self.case_id)
        except (ValueError, AttributeError) as error:
            raise CaseMetadataError('case_id must be a UUID') from error
        if not self.target:
            raise CaseMetadataError('target must not be empty')
        if self.workflow is WorkflowMode.MESH_EXTERNAL and not self.authored_workflow_suspended:
            raise CaseMetadataError(
                'mesh_external workflow requires authored_workflow_suspended=true')
        if self.workflow is WorkflowMode.NONE and self.mesh_origin is not MeshOrigin.NONE:
            raise CaseMetadataError('workflow=none cannot have a mesh origin')
        if self.artifact_state is ArtifactState.ABSENT and self.mesh_fingerprint is not None:
            raise CaseMetadataError('an absent mesh cannot have a fingerprint')

    def to_dict(self) -> dict[str, Any]:
        return {
            'format_version': self.format_version,
            'case_id': self.case_id,
            'target': self.target,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'workflow': self.workflow.value,
            'mesh_origin': self.mesh_origin.value,
            'artifact_state': self.artifact_state.value,
            'mesh_fingerprint': (
                self.mesh_fingerprint.to_dict() if self.mesh_fingerprint else None),
            'authored_workflow_suspended': self.authored_workflow_suspended,
            'provenance': dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> 'CaseMetadata':
        if not isinstance(value, dict):
            raise CaseMetadataError('case metadata must be a JSON object')
        unknown = set(value) - {
            'format_version', 'case_id', 'target', 'created_at', 'updated_at',
            'workflow', 'mesh_origin', 'artifact_state',
            'mesh_fingerprint', 'authored_workflow_suspended', 'provenance',
        }
        if unknown:
            raise CaseMetadataError(f'unknown case metadata fields: {", ".join(sorted(unknown))}')
        try:
            fingerprint_data = value.get('mesh_fingerprint')
            provenance = value.get('provenance', {})
            if not isinstance(provenance, dict) or not all(
                    isinstance(key, str) and isinstance(item, str)
                    for key, item in provenance.items()):
                raise CaseMetadataError('provenance must be a string-to-string object')
            return cls(
                format_version=int(value.get('format_version', CASE_METADATA_VERSION)),
                case_id=str(value.get('case_id', uuid.uuid4())),
                target=str(value.get('target', DEFAULT_OPENFOAM_TARGET)),
                created_at=str(value.get('created_at', _utc_now())),
                updated_at=str(value.get('updated_at', _utc_now())),
                workflow=WorkflowMode(value.get('workflow', WorkflowMode.NONE.value)),
                mesh_origin=MeshOrigin(value.get('mesh_origin', MeshOrigin.NONE.value)),
                artifact_state=ArtifactState(value.get('artifact_state', ArtifactState.ABSENT.value)),
                mesh_fingerprint=(
                    MeshFingerprint.from_dict(fingerprint_data)
                    if fingerprint_data is not None else None),
                authored_workflow_suspended=bool(value.get('authored_workflow_suspended', False)),
                provenance=dict(provenance),
            )
        except (TypeError, ValueError) as error:
            if isinstance(error, CaseMetadataError):
                raise
            raise CaseMetadataError(f'invalid case metadata: {error}') from error

    def evolve(self, **changes) -> 'CaseMetadata':
        """Return updated metadata while preserving case identity and creation time."""
        return replace(self, updated_at=_utc_now(), **changes)

    def with_external_mesh(self, fingerprint: MeshFingerprint, *, origin: MeshOrigin,
                           provenance: dict[str, str] | None = None) -> 'CaseMetadata':
        if origin not in (MeshOrigin.IMPORTED, MeshOrigin.OPENED_NATIVE):
            raise ValueError('external meshes must be imported or opened_native')
        return self.evolve(
            workflow=WorkflowMode.MESH_EXTERNAL,
            mesh_origin=origin,
            artifact_state=ArtifactState.CURRENT,
            mesh_fingerprint=fingerprint,
            authored_workflow_suspended=True,
            provenance={**self.provenance, **dict(provenance or {})},
        )

    def with_generated_mesh(self, fingerprint: MeshFingerprint, *,
                            provenance: dict[str, str] | None = None
                            ) -> 'CaseMetadata':
        """Record that FoamMesh's own meshing workflow produced this mesh.

        The mirror of :meth:`with_external_mesh`.  Without it nothing ever
        wrote ``workflow=authored`` for a mesh we generated ourselves (H9).
        """
        return self.evolve(
            workflow=WorkflowMode.AUTHORED,
            mesh_origin=MeshOrigin.GENERATED,
            artifact_state=ArtifactState.CURRENT,
            mesh_fingerprint=fingerprint,
            authored_workflow_suspended=False,
            provenance={**self.provenance, **dict(provenance or {})},
        )

    @classmethod
    def external_mesh(cls, fingerprint: MeshFingerprint, *, origin: MeshOrigin,
                      provenance: dict[str, str] | None = None) -> 'CaseMetadata':
        if origin not in (MeshOrigin.IMPORTED, MeshOrigin.OPENED_NATIVE):
            raise ValueError('external meshes must be imported or opened_native')
        return cls(
            workflow=WorkflowMode.MESH_EXTERNAL,
            mesh_origin=origin,
            artifact_state=ArtifactState.CURRENT,
            mesh_fingerprint=fingerprint,
            authored_workflow_suspended=True,
            provenance=dict(provenance or {}),
        )


@dataclass(frozen=True)
class CaseClassification:
    kind: CaseKind
    path: Path
    poly_mesh_path: Path | None = None
    metadata_path: Path | None = None
    reasons: tuple[str, ...] = ()

    @property
    def has_mesh(self) -> bool:
        return self.poly_mesh_path is not None


@dataclass(frozen=True)
class WorkflowResolution:
    workflow: WorkflowMode
    mesh_origin: MeshOrigin
    artifact_state: ArtifactState
    reason: str


def _mesh_file(poly_mesh_path: Path, name: str) -> Path | None:
    plain = poly_mesh_path / name
    if plain.is_file():
        return plain
    compressed = poly_mesh_path / f'{name}.gz'
    if compressed.is_file():
        return compressed
    return None


_FOAM_HEADER_RE = re.compile(r'FoamFile\s*\{(?P<body>.*?)\}', re.DOTALL)
_HEADER_FIELD_RE = re.compile(r'\b(?P<key>class|object)\s+(?P<value>[^;\s]+)\s*;')


def _read_mesh_prefix(path: Path, limit: int = 64 * 1024) -> str:
    opener = gzip.open if path.suffix == '.gz' else path.open
    try:
        with opener(path, 'rb') if path.suffix == '.gz' else opener('rb') as stream:
            return stream.read(limit).decode('latin-1', errors='replace')
    except (OSError, EOFError) as error:
        raise ValueError(f'cannot read polyMesh file {path.name}: {error}') from error


def validate_poly_mesh_headers(poly_mesh_path: str | Path, *, strict: bool = True) -> tuple[str, ...]:
    """Validate any OpenFOAM headers present in the core polyMesh files.

    Headerless files remain readable for legacy/test fixtures, while a file
    claiming to contain a ``FoamFile`` header must be structurally valid and
    name the expected object. Real OpenFOAM polyMesh files always take the
    strict branch.
    """
    root = Path(poly_mesh_path)
    warnings: list[str] = []
    for name in (*_REQUIRED_POLY_MESH_FILES, *_OPTIONAL_POLY_MESH_FILES):
        path = _mesh_file(root, name)
        if path is None:
            continue
        prefix = _read_mesh_prefix(path)
        if 'FoamFile' not in prefix:
            if strict:
                raise ValueError(f'{path.name}: FoamFile header is missing')
            warnings.append(f'{path.name}: headerless compatibility file')
            continue
        match = _FOAM_HEADER_RE.search(prefix)
        if match is None:
            raise ValueError(f'{path.name}: malformed FoamFile header')
        fields = {item.group('key'): item.group('value')
                  for item in _HEADER_FIELD_RE.finditer(match.group('body'))}
        if fields.get('object') != name:
            raise ValueError(
                f'{path.name}: FoamFile object is {fields.get("object", "missing")}, expected {name}')
        if 'class' not in fields:
            raise ValueError(f'{path.name}: FoamFile class is missing')
    return tuple(warnings)


def _missing_poly_mesh_files(poly_mesh_path: Path) -> list[str]:
    return [name for name in _REQUIRED_POLY_MESH_FILES if _mesh_file(poly_mesh_path, name) is None]


def _present_poly_mesh_files(poly_mesh_path: Path) -> list[str]:
    return [name for name in (*_REQUIRED_POLY_MESH_FILES, *_OPTIONAL_POLY_MESH_FILES)
            if _mesh_file(poly_mesh_path, name) is not None]


def _poly_mesh_path(case_path: Path) -> Path:
    return case_path / 'constant' / 'polyMesh'


def fingerprint_poly_mesh(poly_mesh_path: Path) -> MeshFingerprint:
    """Return a deterministic SHA-256 fingerprint of a complete polyMesh.

    File names and byte lengths are included to avoid ambiguous concatenation.
    The function reads in chunks, so it does not load large meshes into memory.
    """
    poly_mesh_path = Path(poly_mesh_path)
    missing = _missing_poly_mesh_files(poly_mesh_path)
    if missing:
        raise ValueError(f'not a complete polyMesh: missing {", ".join(missing)}')
    validate_poly_mesh_headers(poly_mesh_path)

    digest = hashlib.sha256()
    selected: list[str] = []
    for name in _FINGERPRINT_POLY_MESH_FILES:
        file_path = _mesh_file(poly_mesh_path, name)
        if file_path is None:
            continue
        relative_name = file_path.name
        selected.append(relative_name)
        digest.update(relative_name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(str(file_path.stat().st_size).encode('ascii'))
        digest.update(b'\0')
        with file_path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
    return MeshFingerprint(digest=digest.hexdigest(), files=tuple(selected))


#: Why a case with our own sidecar still opens with no workflow behind it.
#:
#: Plan 30 WP-13. Measured on 2026-09-05 against three catalogue cases
#: (`test_cases/snappyhexmesh/duct`, `.../elbow`, `test_cases/gmsh/pipe`): each
#: carries a `foammesh/project.json` written before `record_generated_mesh`
#: existed, with `workflow: none`, `mesh_origin: none` and `mesh_fingerprint:
#: null`, beside a complete `constant/polyMesh`. `classify_case` calls that a
#: FoamMesh case -- correctly, the sidecar is ours and valid -- and
#: `resolve_workflow` then falls through to `MESH_EXTERNAL`, so the mesh is
#: shown and the steps that made it are not. Nothing was wrong and nothing said
#: so; the classification now carries the reason.
#: See `docs/user_manual/legacy_case_migration.md`.
LEGACY_SIDECAR_REASON = (
    'legacy sidecar: valid FoamMesh metadata with no workflow mode beside an '
    'existing mesh; the case opens as External mesh with no workflow state')


def classify_case(path: str | Path) -> CaseClassification:
    """Classify a candidate case directory without changing it.

    A valid sidecar has precedence over the native layout.  An invalid sidecar
    is reported as invalid instead of being overwritten, which preserves the
    user's recovery options.
    """
    case_path = Path(path)
    if not case_path.exists():
        return CaseClassification(CaseKind.INVALID, case_path, reasons=('path does not exist',))
    if not case_path.is_dir():
        return CaseClassification(CaseKind.INVALID, case_path, reasons=('path is not a directory',))

    metadata_path = case_path / SIDECAR_DIRECTORY / CASE_METADATA_FILE
    if metadata_path.exists():
        try:
            metadata = load_case_metadata(case_path)
        except CaseMetadataError as error:
            return CaseClassification(
                CaseKind.INVALID, case_path, metadata_path=metadata_path,
                reasons=(f'invalid FoamMesh sidecar: {error}',))
        poly_mesh_path = _poly_mesh_path(case_path)
        missing = _missing_poly_mesh_files(poly_mesh_path)
        if missing and _present_poly_mesh_files(poly_mesh_path):
            return CaseClassification(
                CaseKind.INVALID, case_path, metadata_path=metadata_path,
                reasons=(f'partial constant/polyMesh: missing {", ".join(missing)}',))
        if missing:
            poly_mesh_path = None
        else:
            try:
                validate_poly_mesh_headers(poly_mesh_path)
            except ValueError as error:
                return CaseClassification(
                    CaseKind.INVALID, case_path, metadata_path=metadata_path,
                    reasons=(f'invalid polyMesh: {error}',))
        reasons: tuple[str, ...] = ()
        if poly_mesh_path is not None and metadata.workflow is WorkflowMode.NONE:
            reasons = (LEGACY_SIDECAR_REASON,)
        return CaseClassification(
            CaseKind.FOAMMESH_CASE, case_path, poly_mesh_path, metadata_path,
            reasons=reasons)

    poly_mesh_path = _poly_mesh_path(case_path)
    missing = _missing_poly_mesh_files(poly_mesh_path)
    if not missing:
        try:
            validate_poly_mesh_headers(poly_mesh_path)
        except ValueError as error:
            # An unreadable mesh is not a usable mesh: never advertise
            # poly_mesh_path (and therefore has_mesh) for it.
            return CaseClassification(
                CaseKind.INVALID, case_path,
                reasons=(f'invalid polyMesh: {error}',))
        return CaseClassification(CaseKind.RAW_POLY_MESH_CASE, case_path, poly_mesh_path)

    if _present_poly_mesh_files(poly_mesh_path):
        return CaseClassification(
            CaseKind.INVALID, case_path,
            reasons=(f'partial constant/polyMesh: missing {", ".join(missing)}',))

    if any((case_path / name).exists() for name in ('0', 'constant', 'system')):
        return CaseClassification(
            CaseKind.OPENFOAM_CASE, case_path,
            reasons=(f'no complete constant/polyMesh ({", ".join(missing)})',))

    try:
        is_empty = not any(case_path.iterdir())
    except OSError as error:
        return CaseClassification(CaseKind.INVALID, case_path, reasons=(str(error),))
    if is_empty:
        return CaseClassification(CaseKind.EMPTY, case_path)
    return CaseClassification(
        CaseKind.INVALID, case_path,
        reasons=('directory is neither an empty, OpenFOAM, nor FoamMesh case',))


def load_case_metadata(case_path: str | Path) -> CaseMetadata:
    metadata_path = Path(case_path) / SIDECAR_DIRECTORY / CASE_METADATA_FILE
    try:
        raw = json.loads(metadata_path.read_text(encoding='utf-8'))
    except FileNotFoundError as error:
        raise CaseMetadataError('FoamMesh sidecar metadata is missing') from error
    except (OSError, json.JSONDecodeError) as error:
        raise CaseMetadataError(f'cannot read FoamMesh sidecar metadata: {error}') from error
    version = raw.get('format_version') if isinstance(raw, dict) else None
    if not isinstance(version, int):
        raise CaseMetadataError('format_version must be an integer')
    if version != CASE_METADATA_VERSION:
        raise CaseMetadataError(
            f'unsupported case metadata version {version}; '
            f'expected {CASE_METADATA_VERSION}')
    return CaseMetadata.from_dict(raw)


def save_case_metadata(case_path: str | Path, metadata: CaseMetadata, *,
                       failure_injector=None) -> Path:
    """Atomically persist sidecar metadata and return its final path."""
    sidecar = Path(case_path) / SIDECAR_DIRECTORY
    sidecar.mkdir(parents=True, exist_ok=True)
    destination = sidecar / CASE_METADATA_FILE
    temporary = destination.with_suffix(f'{destination.suffix}.tmp')
    payload = json.dumps(metadata.to_dict(), indent=2, sort_keys=True) + '\n'
    inject = failure_injector or (lambda _stage: None)
    try:
        inject('before_open')
        with temporary.open('w', encoding='utf-8', newline='\n') as output:
            output.write(payload)
            inject('after_write')
            output.flush()
            os.fsync(output.fileno())
            inject('after_fsync')
        inject('before_replace')
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return destination


def resolve_workflow(metadata: CaseMetadata | None,
                     current_fingerprint: MeshFingerprint | None) -> WorkflowResolution:
    """Resolve safe navigation mode from persisted provenance and live mesh data."""
    if current_fingerprint is None:
        return WorkflowResolution(
            WorkflowMode.NONE, MeshOrigin.NONE, ArtifactState.ABSENT,
            'no complete polyMesh is present')

    if metadata is None:
        return WorkflowResolution(
            WorkflowMode.MESH_EXTERNAL, MeshOrigin.OPENED_NATIVE,
            ArtifactState.CURRENT,
            'mesh has no FoamMesh provenance')

    if metadata.workflow is WorkflowMode.AUTHORED:
        if metadata.mesh_fingerprint == current_fingerprint:
            return WorkflowResolution(
                WorkflowMode.AUTHORED, metadata.mesh_origin,
                metadata.artifact_state, 'authored mesh fingerprint matches')
        return WorkflowResolution(
            WorkflowMode.MESH_EXTERNAL, MeshOrigin.OPENED_NATIVE,
            ArtifactState.STALE,
            'mesh fingerprint differs from authored workflow provenance')

    if metadata.workflow is WorkflowMode.MESH_EXTERNAL:
        return WorkflowResolution(
            WorkflowMode.MESH_EXTERNAL, metadata.mesh_origin,
            metadata.artifact_state,
            'mesh is explicitly managed as an external artifact')

    return WorkflowResolution(
        WorkflowMode.MESH_EXTERNAL, MeshOrigin.OPENED_NATIVE,
        ArtifactState.CURRENT,
        'mesh exists but metadata has no workflow mode')


def record_generated_mesh(case_path: str | Path, *,
                          provenance: dict[str, str] | None = None
                          ) -> CaseMetadata | None:
    """Write authored provenance for a mesh this workflow just generated (H9).

    :func:`resolve_workflow` reads the sidecar, and a sidecar carrying
    ``workflow=none`` falls through to `mesh exists but metadata has no
    workflow mode` -- External mesh.  ``workflow=authored`` was written in
    exactly one place, the explicit external-to-authored transition, so a case
    that had only ever meshed with Gmsh or snappyHexMesh had no record of it.
    The first re-resolution after that -- a save, a re-open -- read our own
    fresh mesh as somebody else's import and collapsed the workflow outline to
    its two external rows.  A run that has just written ``constant/polyMesh``
    is the moment the provenance is known, so it is recorded here.

    A sidecar that explicitly says ``mesh_external`` is left alone: that stance
    is the user's, and leaving it is what ``workflow.start_authored`` is for --
    it retains a recovery copy of the external mesh, which this must not skip.

    Returns the metadata written, or ``None`` when there is nothing to record.
    """
    case_path = Path(case_path)
    if not (case_path / SIDECAR_DIRECTORY / CASE_METADATA_FILE).is_file():
        return None
    try:
        fingerprint = fingerprint_poly_mesh(_poly_mesh_path(case_path))
    except (OSError, ValueError):
        return None
    metadata = load_case_metadata(case_path)
    if metadata.workflow is WorkflowMode.MESH_EXTERNAL:
        return None
    if (metadata.workflow is WorkflowMode.AUTHORED
            and metadata.mesh_origin is MeshOrigin.GENERATED
            and metadata.mesh_fingerprint == fingerprint):
        return metadata
    updated = metadata.with_generated_mesh(fingerprint, provenance=provenance)
    save_case_metadata(case_path, updated)
    return updated


def workflow_after_mesh_mutation(
        metadata: CaseMetadata | None) -> tuple[WorkflowMode, bool]:
    """The workflow an in-place mesh mutation records, and its suspension flag.

    A repair or transform rewrites ``constant/polyMesh`` and must then say what
    that mesh is. Carrying the sidecar's stored workflow forward unexamined is
    wrong in one common case: a sidecar written before any workflow decision
    still says ``none``, and :meth:`CaseMetadata.__post_init__` forbids pairing
    ``none`` with a real ``mesh_origin``. Both mutation services used to do
    exactly that, and the two failed differently, neither of them visibly:

    * ``MeshRepairService.run`` treats the raised ``CaseMetadataError`` as a
      post-run validation failure and restores its own recovery point, so a
      renumber that succeeded was undone and reported in a status-bar line.
    * the facade's transform path swallows it, so the mesh moved and its
      provenance was never recorded -- leaving a case whose sidecar still
      claimed no mesh at all, which is what recovery and staleness read.

    :func:`resolve_workflow` already reads "a polyMesh exists but the sidecar
    has no workflow mode" as an externally-managed mesh. This returns the same
    judgement in the form a mutation has to persist, so the running app's view
    of a case and what a mutation writes back cannot disagree.

    An authored case keeps its workflow: the pipeline that produced the mesh
    still owns it, and the caller refreshes the fingerprint alongside.
    """
    if metadata is None or metadata.workflow in (
            WorkflowMode.NONE, WorkflowMode.MESH_EXTERNAL):
        return WorkflowMode.MESH_EXTERNAL, True
    return metadata.workflow, False
