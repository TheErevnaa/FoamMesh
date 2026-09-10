"""Staged, capability-gated OpenFOAM mesh-converter adapters.

Converters write ``constant/polyMesh`` into their working directory.  This
service therefore treats them as recoverable mesh mutations: it snapshots a
current mesh, runs an argv-only command, validates the resulting polyMesh, and
only then commits external-mesh provenance.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4

from foammesh.core.case import (
    CaseMetadata, CaseMetadataError, MeshOrigin, classify_case,
    fingerprint_poly_mesh, load_case_metadata, record_artifact_event,
    save_case_metadata,
)
from foammesh.core.jobs import JobManager, JobRequest, JobResult, JobStatus
from foammesh.core.mesh import MeshRecoveryPoint, MeshRecoveryService


class ConverterFormat(str, Enum):
    FLUENT = 'fluent'
    GMSH = 'gmsh'
    GAMBIT = 'gambit'
    IDEAS_UNV = 'ideas_unv'
    ANSYS = 'ansys'
    CFX4 = 'cfx4'
    STAR_CD = 'star_cd'
    PLOT3D = 'plot3d'

    @property
    def spec(self):
        from foammesh.core.format_registry import converter_spec
        return converter_spec(self.value)

    @property
    def utility_name(self) -> str:
        return self.spec.capability.removeprefix('utility:')

    @property
    def label(self) -> str:
        return self.spec.display_name

    @property
    def extensions(self) -> tuple[str, ...]:
        return self.spec.extensions

    def file_filter(self) -> str:
        patterns = ' '.join(f'*{extension}' for extension in self.extensions)
        return f'{self.label} ({patterns})'


@dataclass(frozen=True)
class ConverterRequest:
    format: ConverterFormat
    source: Path

    def validate(self):
        if not self.source.is_file():
            raise FileNotFoundError(f'converter source does not exist: {self.source}')
        if self.source.suffix.lower() not in self.format.extensions:
            allowed = ', '.join(self.format.extensions)
            raise ValueError(f'{self.format.label} requires one of: {allowed}')

    def argv(self, utility: str) -> tuple[str, ...]:
        if not utility:
            raise ValueError(f'{self.format.utility_name} is not available')
        self.validate()
        return utility, str(self.source)


@dataclass(frozen=True)
class ConverterImportResult:
    request: ConverterRequest
    target_case: Path
    job: JobResult
    recovery: MeshRecoveryPoint | None
    restored: bool
    fingerprint: str | None = None
    validation_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.job.status is JobStatus.DONE and not self.restored and self.fingerprint is not None

    def to_dict(self) -> dict:
        return {
            'format': self.request.format.value,
            'source': str(self.request.source),
            'target_case': str(self.target_case),
            'job': self.job.to_dict(),
            'recovery_id': self.recovery.recovery_id if self.recovery else None,
            'restored': self.restored,
            'fingerprint': self.fingerprint,
            'validation_error': self.validation_error,
        }


@dataclass(frozen=True)
class MeshImportEntry:
    """Chooser row: every format stays visible with a truthful reason (§13.4)."""
    entry_id: str
    label: str
    available: bool
    reason: str = ''
    file_filter: str = ''
    maturity: str = 'stable'
    provider: str = ''

    def to_dict(self) -> dict:
        return {'entry_id': self.entry_id, 'label': self.label,
                'available': self.available, 'reason': self.reason,
                'file_filter': self.file_filter, 'maturity': self.maturity,
                'provider': self.provider}


def extract_converter_warnings(output: str, *, limit: int = 8) -> tuple[str, ...]:
    """Surface converter warning lines (v13 notes ASCII/conversion limitations)."""
    warnings = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith(('--> foam warning', 'warning', 'foam warning')) or ' warning:' in lowered:
            warnings.append(stripped)
    deduplicated = list(dict.fromkeys(warnings))
    if len(deduplicated) > limit:
        return tuple(deduplicated[:limit] + [f'... and {len(deduplicated) - limit} more warnings (see log)'])
    return tuple(deduplicated)


class ConverterImportService:
    """Import a neutral mesh using a discovered OpenFOAM converter utility."""

    def __init__(self, utilities: dict[str, str] | None = None,
                 jobs: JobManager | None = None, recovery: MeshRecoveryService | None = None,
                 *, launcher=None):
        self._utilities = dict(utilities or {})
        self._jobs = jobs or JobManager()
        self._recovery = recovery or MeshRecoveryService()
        self._launcher = launcher

    def available_formats(self) -> tuple[ConverterFormat, ...]:
        return tuple(fmt for fmt in ConverterFormat if self._utilities.get(fmt.utility_name))

    def import_entries(self) -> tuple[MeshImportEntry, ...]:
        """Native import plus every converter, disabled with a reason when absent."""
        from foammesh.core.format_registry import format_spec
        native = format_spec('mesh.native.import')
        entries = [MeshImportEntry(
            'native', native.display_name, True, maturity=native.maturity.value,
            provider=native.provider)]
        for fmt in ConverterFormat:
            available = bool(self._utilities.get(fmt.utility_name))
            spec = fmt.spec
            entries.append(MeshImportEntry(
                fmt.value, fmt.label, available,
                '' if available else
                f'{fmt.utility_name} was not found in the configured OpenFOAM environment',
                fmt.file_filter(), spec.maturity.value, spec.provider))
        return tuple(entries)

    async def import_file(self, target_case: str | Path, request: ConverterRequest) -> ConverterImportResult:
        target = Path(target_case).resolve()
        if not target.is_dir():
            raise FileNotFoundError(f'target case does not exist: {target}')
        request = ConverterRequest(request.format, Path(request.source).resolve())
        utility = self._utilities.get(request.format.utility_name)
        semantic = request.argv(request.format.utility_name)[1:]
        mesh = target / 'constant' / 'polyMesh'
        had_mesh = classify_case(target).has_mesh
        before = fingerprint_poly_mesh(mesh).digest if had_mesh else None
        recovery = self._recovery.snapshot(target, operation=f'converter:{request.format.value}') if had_mesh else None
        staging = target / f'.foammesh-converter-{uuid4().hex}'
        (staging / 'constant').mkdir(parents=True)
        (staging / 'system').mkdir()
        if self._launcher is not None:
            launch = self._launcher(
                request.format.utility_name, semantic, cwd=staging)
            argv = launch.argv
            cleanup_argv = launch.cleanup_argv
        else:
            argv = request.argv(utility or '')
            cleanup_argv = ()
        job = await self._jobs.run(JobRequest(
            name=f'import {request.format.value}', argv=argv, cwd=staging, mutation=True,
            log_path=target / 'foammesh' / 'logs' / f'import-{request.format.value}.log',
            cleanup_argv=cleanup_argv))
        if job.status is not JobStatus.DONE:
            shutil.rmtree(staging, ignore_errors=True)
            # A well-behaved converter only touched staging; restore anyway so
            # a faulty wrapper or utility cannot leak a target-case mutation.
            self._rollback(target, mesh, recovery)
            record_artifact_event(
                target, operation=f'import:{request.format.value}', status=job.status.value,
                command=argv, before_fingerprint=before, after_fingerprint=before,
                recovery_id=recovery.recovery_id if recovery else None,
                recovery_status='restored' if recovery else 'not_required',
                details={'source_file': str(request.source)})
            return ConverterImportResult(request, target, job, recovery, recovery is not None)

        try:
            classification = classify_case(staging)
            if classification.poly_mesh_path is None:
                raise ValueError('converter completed without a complete constant/polyMesh')
            staged_mesh = classification.poly_mesh_path
            fingerprint = fingerprint_poly_mesh(staged_mesh)
            self._promote_mesh(mesh, staged_mesh)
            provenance = {
                'source_file': str(request.source),
                'source_sha256': _sha256(request.source),
                'converter': request.format.utility_name,
                'converter_format': request.format.value,
            }
            try:
                previous = load_case_metadata(target)
            except CaseMetadataError:
                metadata = CaseMetadata.external_mesh(
                    fingerprint, origin=MeshOrigin.IMPORTED, provenance=provenance)
            else:
                metadata = previous.with_external_mesh(
                    fingerprint, origin=MeshOrigin.IMPORTED, provenance=provenance)
            save_case_metadata(target, metadata)
        except (OSError, ValueError) as error:
            shutil.rmtree(staging, ignore_errors=True)
            self._rollback(target, mesh, recovery)
            record_artifact_event(
                target, operation=f'import:{request.format.value}', status='validation_failed',
                command=argv, before_fingerprint=before, after_fingerprint=before,
                recovery_id=recovery.recovery_id if recovery else None,
                recovery_status='restored' if recovery else 'not_required',
                details={'source_file': str(request.source), 'error': str(error)})
            return ConverterImportResult(
                request, target, job, recovery, recovery is not None,
                validation_error=str(error))
        shutil.rmtree(staging, ignore_errors=True)
        record_artifact_event(
            target, operation=f'import:{request.format.value}', status='applied',
            command=argv, before_fingerprint=before, after_fingerprint=fingerprint.digest,
            recovery_id=recovery.recovery_id if recovery else None,
            recovery_status='available' if recovery else 'not_required',
            details={'source_file': str(request.source)})
        return ConverterImportResult(request, target, job, recovery, False, fingerprint.digest)

    @staticmethod
    def _promote_mesh(target_mesh: Path, staged_mesh: Path) -> None:
        target_mesh.parent.mkdir(parents=True, exist_ok=True)
        displaced = target_mesh.with_name(f'.polyMesh-displaced-{uuid4().hex}')
        moved_old = False
        try:
            if target_mesh.exists():
                os.replace(target_mesh, displaced)
                moved_old = True
            os.replace(staged_mesh, target_mesh)
        except Exception:
            if moved_old and displaced.exists() and not target_mesh.exists():
                os.replace(displaced, target_mesh)
            raise
        finally:
            shutil.rmtree(displaced, ignore_errors=True)

    def _rollback(self, target: Path, mesh: Path, recovery: MeshRecoveryPoint | None):
        if recovery is not None:
            self._recovery.restore(target, recovery)
        elif mesh.exists():
            shutil.rmtree(mesh, ignore_errors=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()
