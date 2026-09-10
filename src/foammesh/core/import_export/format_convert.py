"""ASCII/binary/compressed conversion via ``foamFormatConvert`` (§13.3).

``foamFormatConvert`` rewrites case IO objects according to the case write
settings; it is not a format picker.  This service therefore edits
``system/controlDict`` explicitly, runs the utility as a recoverable mesh
mutation, and afterwards either restores the user's original settings or
commits the change — never leaving an implicit, undocumented edit behind.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from foammesh.core.case import (
    ArtifactState, CaseMetadataError, classify_case, fingerprint_poly_mesh,
    load_case_metadata, record_artifact_event, save_case_metadata,
)
from foammesh.core.jobs import JobManager, JobRequest, JobResult, JobStatus
from foammesh.core.mesh import MeshRecoveryPoint, MeshRecoveryService


WRITE_FORMATS = ('ascii', 'binary')


def set_write_settings(control_dict_text: str, write_format: str, compression: bool) -> str:
    """Return controlDict text with explicit writeFormat/writeCompression entries.

    Existing entries are replaced in place; missing entries are appended before
    the closing OpenFOAM footer so the rest of the file stays byte-identical
    and the original text can simply be written back to restore it.
    """
    if write_format not in WRITE_FORMATS:
        raise ValueError(f'write format must be one of: {", ".join(WRITE_FORMATS)}')
    values = {'writeFormat': write_format,
              'writeCompression': 'on' if compression else 'off'}
    text = control_dict_text
    for keyword, value in values.items():
        pattern = re.compile(rf'(?m)^(\s*{keyword}\s+)[^;\s]+(\s*;)')
        replacement = rf'\g<1>{value}\g<2>'
        if pattern.search(text):
            text = pattern.sub(replacement, text, count=1)
            continue
        entry = f'{keyword}     {value};\n'
        footer = re.search(r'(?m)^// \*+ //\s*$', text)
        if footer:
            text = text[:footer.start()] + entry + '\n' + text[footer.start():]
        else:
            text = text + ('' if text.endswith('\n') else '\n') + entry
    return text


@dataclass(frozen=True)
class FormatConvertRun:
    job: JobResult
    recovery: MeshRecoveryPoint
    restored: bool
    write_format: str
    compression: bool
    settings_committed: bool
    before_fingerprint: str
    after_fingerprint: str | None = None
    validation_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return (self.job.status is JobStatus.DONE and not self.restored
                and self.validation_error is None)

    def to_dict(self) -> dict:
        return {'job': self.job.to_dict(), 'recovery': self.recovery.to_dict(),
                'restored': self.restored, 'write_format': self.write_format,
                'compression': self.compression,
                'settings_committed': self.settings_committed,
                'before_fingerprint': self.before_fingerprint,
                'after_fingerprint': self.after_fingerprint,
                'validation_error': self.validation_error}


class FoamFormatConvertService:
    def __init__(self, jobs: JobManager | None = None,
                 recovery: MeshRecoveryService | None = None,
                 *, utility: str = 'foamFormatConvert', launcher=None):
        self._jobs = jobs or JobManager()
        self._recovery = recovery or MeshRecoveryService()
        self._utility = utility
        self._launcher = launcher

    async def run(self, case_path: str | Path, *, write_format: str,
                  compression: bool = False, commit_settings: bool = False,
                  on_line=None) -> FormatConvertRun:
        if not self._utility:
            raise ValueError('foamFormatConvert is not available')
        case = Path(case_path).resolve()
        control_dict = case / 'system' / 'controlDict'
        if not control_dict.is_file():
            raise ValueError('this case has no system/controlDict to define write settings')
        original_text = control_dict.read_text(encoding='utf-8')
        modified_text = set_write_settings(original_text, write_format, compression)

        before = fingerprint_poly_mesh(case / 'constant' / 'polyMesh').digest
        recovery = self._recovery.snapshot(case, operation=f'format_convert:{write_format}')
        control_dict.write_text(modified_text, encoding='utf-8', newline='\n')
        if self._launcher is not None:
            launch = self._launcher('foamFormatConvert', (), cwd=case)
            command = launch.argv
            cleanup_argv = launch.cleanup_argv
        else:
            command = (self._utility,)
            cleanup_argv = ()
        job_request = JobRequest(
            name=f'foamFormatConvert {write_format}', argv=command, cwd=case,
            mutation=True, log_path=case / 'foammesh' / 'logs' / 'foamFormatConvert.log',
            timeout=600.0, cleanup_argv=cleanup_argv)
        job = (await self._jobs.run(job_request) if on_line is None else
               await self._jobs.run(job_request, on_line=on_line))

        if job.status is not JobStatus.DONE:
            control_dict.write_text(original_text, encoding='utf-8', newline='\n')
            self._recovery.restore(case, recovery)
            record_artifact_event(
                case, operation=f'export:openfoam_format:{write_format}',
                status=job.status.value, command=command,
                before_fingerprint=before, after_fingerprint=before,
                recovery_id=recovery.recovery_id, recovery_status='restored')
            return FormatConvertRun(job, recovery, True, write_format, compression,
                                    False, before)
        try:
            classification = classify_case(case)
            if classification.poly_mesh_path is None:
                raise ValueError('conversion completed without a complete constant/polyMesh')
            after = fingerprint_poly_mesh(classification.poly_mesh_path)
        except (OSError, ValueError) as error:
            control_dict.write_text(original_text, encoding='utf-8', newline='\n')
            self._recovery.restore(case, recovery)
            record_artifact_event(
                case, operation=f'export:openfoam_format:{write_format}',
                status='validation_failed', command=command,
                before_fingerprint=before, after_fingerprint=before,
                recovery_id=recovery.recovery_id, recovery_status='restored',
                details={'error': str(error)})
            return FormatConvertRun(job, recovery, True, write_format, compression,
                                    False, before, validation_error=str(error))
        if not commit_settings:
            control_dict.write_text(original_text, encoding='utf-8', newline='\n')
        self._refresh_metadata(case, after, write_format)
        self._recovery.mark_available(recovery)
        record_artifact_event(
            case, operation=f'export:openfoam_format:{write_format}', status='applied',
            command=command, before_fingerprint=before, after_fingerprint=after.digest,
            recovery_id=recovery.recovery_id, recovery_status='available',
            details={'write_format': write_format, 'compression': compression,
                     'settings_committed': commit_settings})
        return FormatConvertRun(job, recovery, False, write_format, compression,
                                commit_settings, before, after.digest)

    @staticmethod
    def _refresh_metadata(case: Path, fingerprint, write_format: str):
        try:
            previous = load_case_metadata(case)
        except CaseMetadataError:
            return  # raw native case: nothing to keep consistent
        provenance = dict(previous.provenance)
        provenance['last_mesh_mutation'] = f'format_convert:{write_format}'
        save_case_metadata(case, previous.evolve(
            artifact_state=ArtifactState.CURRENT,
            mesh_fingerprint=fingerprint, provenance=provenance))
