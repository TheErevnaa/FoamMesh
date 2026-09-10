"""Capability-gated Fluent mesh export via ``foamMeshToFluent`` (§13.3)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from foammesh.core.case import classify_case, record_artifact_event
from foammesh.core.jobs import JobManager, JobRequest, JobResult, JobStatus


@dataclass(frozen=True)
class FluentExportRun:
    job: JobResult
    output: Path | None
    total_bytes: int = 0
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.job.status is JobStatus.DONE and self.output is not None

    def to_dict(self) -> dict:
        return {'job': self.job.to_dict(),
                'output': str(self.output) if self.output else None,
                'total_bytes': self.total_bytes, 'warnings': list(self.warnings)}


class FluentMeshExportService:
    """Write ``fluentInterface/<case>.msh`` with the target's own converter."""

    def __init__(self, jobs: JobManager | None = None, *,
                 utility: str = 'foamMeshToFluent', launcher=None):
        self._jobs = jobs or JobManager()
        self._utility = utility
        self._launcher = launcher

    async def run(self, case_path: str | Path, *, on_line=None) -> FluentExportRun:
        if not self._utility:
            raise ValueError('foamMeshToFluent is not available')
        case = Path(case_path).resolve()
        if classify_case(case).poly_mesh_path is None:
            raise ValueError('no complete constant/polyMesh was found to export')
        expected = Path('fluentInterface') / f'{case.name}.msh'
        if self._launcher is not None:
            launch = self._launcher('foamMeshToFluent', (), cwd=case)
            command = launch.argv
            cleanup_argv = launch.cleanup_argv
        else:
            command = (self._utility,)
            cleanup_argv = ()
        job_request = JobRequest(
            name='foamMeshToFluent', argv=command, cwd=case, mutation=False,
            log_path=case / 'foammesh' / 'logs' / 'foamMeshToFluent.log',
            timeout=600.0, expected_outputs=(expected,),
            cleanup_argv=cleanup_argv)
        job = (await self._jobs.run(job_request) if on_line is None else
               await self._jobs.run(job_request, on_line=on_line))
        if job.status is not JobStatus.DONE:
            record_artifact_event(
                case, operation='export:fluent', status=job.status.value,
                command=command, details={'expected_output': str(case / expected)})
            return FluentExportRun(job, None)
        output = case / expected
        total = output.stat().st_size
        warnings = tuple(job.warnings) + (
            'Fluent conversion has documented limitations for zones and boundary '
            'semantics; verify the imported mesh in the target tool.',)
        record_artifact_event(
            case, operation='export:fluent', status='applied', command=command,
            details={'destination': str(output), 'total_bytes': total,
                     'warnings': list(warnings)})
        return FluentExportRun(job, output, total, warnings)
