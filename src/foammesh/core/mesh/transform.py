"""Recovery-backed OpenFOAM mesh transform requests."""
from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

from foammesh.core.case import (
    ArtifactState, CaseMetadata, CaseMetadataError, MeshOrigin, classify_case,
    fingerprint_poly_mesh, load_case_metadata, save_case_metadata,
    record_artifact_event, workflow_after_mesh_mutation,
)
from foammesh.core.jobs import JobManager, JobRequest, JobResult, JobStatus

from .recovery import MeshRecoveryPoint, MeshRecoveryService
from .info import MeshBounds, MeshInfo, MeshInfoService


Vector = tuple[float, float, float]


@dataclass(frozen=True)
class TransformPointsProfile:
    expression_contract: bool
    transform_fields: bool = False

    @classmethod
    def from_help(cls, help_text: str) -> 'TransformPointsProfile':
        if not help_text.strip():
            raise ValueError('transformPoints help output is empty')
        lowered = help_text.lower()
        expression = ('transformation' in lowered and
                      ('<transformation' in lowered or 'transformations' in lowered or
                       any(token in help_text for token in ('scale=', 'translate=', 'Rx=', 'Ra='))))
        if not expression:
            raise ValueError(
                'configured transformPoints does not advertise the Foundation-v13 expression contract')
        return cls(expression_contract=True, transform_fields='-rotateFields' in help_text)


def parse_vector(text: str) -> Vector:
    """Parse exactly three locale-independent numeric values for command argv."""
    values = text.replace(',', ' ').split()
    if len(values) != 3:
        raise ValueError('enter exactly three numeric values')
    try:
        return tuple(float(value) for value in values)  # type: ignore[return-value]
    except ValueError as error:
        raise ValueError('enter exactly three numeric values') from error


def parse_scale(text: str) -> Vector:
    """DP-693. One factor scales every direction; three scale X, Y and Z apart."""
    values = text.replace(',', ' ').split()
    if len(values) == 1:
        try:
            factor = float(values[0])
        except ValueError as error:
            raise ValueError('enter one factor or three numeric values') from error
        return (factor, factor, factor)
    if len(values) != 3:
        raise ValueError('enter one factor or three numeric values')
    return parse_vector(text)


@dataclass(frozen=True)
class MeshTransformRequest:
    operation: str
    vector: Vector
    angle_degrees: float | None = None
    pivot: Vector | None = None
    transform_fields: bool = False

    @classmethod
    def scale_units(cls, source_unit: str, target_unit: str) -> 'MeshTransformRequest':
        metres = {'m': 1.0, 'cm': 0.01, 'mm': 0.001, 'um': 1e-6}
        if source_unit not in metres or target_unit not in metres:
            raise ValueError('units must be one of m, cm, mm, or um')
        factor = metres[source_unit] / metres[target_unit]
        return cls('scale', (factor, factor, factor))

    @classmethod
    def rotation(cls, axis: str | Vector, degrees: float,
                 pivot: Vector | None = None) -> 'MeshTransformRequest':
        if isinstance(axis, str):
            key = axis.strip().lower()
            axes = {'x': (1.0, 0.0, 0.0), 'y': (0.0, 1.0, 0.0), 'z': (0.0, 0.0, 1.0)}
            if key not in axes:
                raise ValueError('rotation axis must be X, Y, Z, or a three-value vector')
            vector = axes[key]
        else:
            vector = tuple(axis)
        return cls('rotate', vector, float(degrees), pivot)

    @property
    def has_negative_scale(self) -> bool:
        return self.operation == 'scale' and any(value < 0 for value in self.vector)

    def argv(self, utility: str = 'transformPoints',
             case_path: str | Path | None = None) -> tuple[str, ...]:
        if self.operation not in {'scale', 'translate', 'rotate'}:
            raise ValueError('transform operation must be scale, translate, or rotate')
        if len(self.vector) != 3:
            raise ValueError('a transform vector must contain exactly three values')
        if not all(math.isfinite(value) for value in self.vector):
            raise ValueError('transform values must be finite')
        if self.operation == 'scale' and any(value == 0 for value in self.vector):
            raise ValueError('scale components must be non-zero')
        rendered = _render_vector(self.vector)
        if self.operation in {'scale', 'translate'}:
            expression = f'{self.operation}={rendered}'
        else:
            if self.angle_degrees is None or not math.isfinite(self.angle_degrees):
                raise ValueError('rotation angle must be a finite number of degrees')
            magnitude = math.sqrt(sum(value * value for value in self.vector))
            if magnitude == 0:
                raise ValueError('rotation axis must be non-zero')
            axis = tuple(value / magnitude for value in self.vector)
            cardinal = {
                (1.0, 0.0, 0.0): 'Rx', (0.0, 1.0, 0.0): 'Ry',
                (0.0, 0.0, 1.0): 'Rz',
            }.get(tuple(round(value, 12) for value in axis))
            rotation = (f'{cardinal}={format(self.angle_degrees, ".12g")}' if cardinal else
                        f'Ra={_render_vector(axis)} {format(self.angle_degrees, ".12g")}')
            if self.pivot is None:
                expression = rotation
            else:
                if len(self.pivot) != 3 or not all(math.isfinite(value) for value in self.pivot):
                    raise ValueError('rotation pivot must contain three finite values')
                negative = tuple(-value for value in self.pivot)
                expression = (f'translate={_render_vector(negative)}, {rotation}, '
                              f'translate={_render_vector(self.pivot)}')
        # Foundation v13 accepts one transformation expression argument.
        argv = [utility, expression]
        if case_path is not None:
            argv.extend(('-case', str(Path(case_path))))
        return tuple(argv)

    def preview_bounds(self, bounds: MeshBounds) -> MeshBounds:
        """Return the axis-aligned box after applying this transform."""
        corners = [
            (x, y, z)
            for x in (bounds.minimum[0], bounds.maximum[0])
            for y in (bounds.minimum[1], bounds.maximum[1])
            for z in (bounds.minimum[2], bounds.maximum[2])
        ]
        transformed = [self._transform_point(point) for point in corners]
        minimum = tuple(min(point[index] for point in transformed) for index in range(3))
        maximum = tuple(max(point[index] for point in transformed) for index in range(3))
        return MeshBounds(minimum, maximum,
                          tuple(maximum[index] - minimum[index] for index in range(3)),
                          bounds.unit)

    def _transform_point(self, point: Vector) -> Vector:
        # argv() performs all finite/non-zero validation and axis normalization.
        self.argv()
        if self.operation == 'scale':
            return tuple(point[index] * self.vector[index] for index in range(3))  # type: ignore[return-value]
        if self.operation == 'translate':
            return tuple(point[index] + self.vector[index] for index in range(3))  # type: ignore[return-value]
        pivot = self.pivot or (0.0, 0.0, 0.0)
        axis_length = math.sqrt(sum(value * value for value in self.vector))
        axis = tuple(value / axis_length for value in self.vector)
        angle = math.radians(self.angle_degrees or 0.0)
        relative = tuple(point[index] - pivot[index] for index in range(3))
        cosine, sine = math.cos(angle), math.sin(angle)
        cross = (
            axis[1] * relative[2] - axis[2] * relative[1],
            axis[2] * relative[0] - axis[0] * relative[2],
            axis[0] * relative[1] - axis[1] * relative[0],
        )
        dot = sum(axis[index] * relative[index] for index in range(3))
        return tuple(
            pivot[index] + relative[index] * cosine + cross[index] * sine
            + axis[index] * dot * (1 - cosine)
            for index in range(3))  # type: ignore[return-value]


@dataclass(frozen=True)
class MeshTransformRun:
    job: JobResult
    recovery: MeshRecoveryPoint
    restored: bool
    before: MeshInfo | None = None
    after: MeshInfo | None = None
    validation_error: str | None = None

    def to_dict(self) -> dict:
        return {'job': self.job.to_dict(), 'recovery': self.recovery.to_dict(),
                'restored': self.restored,
                'before': self.before.to_dict() if self.before else None,
                'after': self.after.to_dict() if self.after else None,
                'validation_error': self.validation_error}


class MeshTransformService:
    def __init__(self, jobs: JobManager | None = None, recovery: MeshRecoveryService | None = None,
                 *, utility: str = 'transformPoints', help_text: str | None = None):
        self._jobs = jobs or JobManager()
        self._recovery = recovery or MeshRecoveryService()
        self._utility = utility
        self._profile = (TransformPointsProfile.from_help(help_text)
                         if help_text is not None else TransformPointsProfile(True))
        self._info = MeshInfoService()

    async def run(self, case_path: str | Path, request: MeshTransformRequest,
                  *, on_line=None) -> MeshTransformRun:
        case = Path(case_path).resolve()
        before = fingerprint_poly_mesh(case / 'constant' / 'polyMesh')
        before_info = self._info.inspect(case)
        recovery = self._recovery.snapshot(case, operation=request.operation)
        command = list(request.argv(self._utility, case))
        if request.transform_fields:
            if request.operation != 'rotate':
                raise ValueError('field transformation is supported only for rotation')
            if not self._profile.transform_fields:
                raise ValueError(
                    'configured transformPoints does not advertise -rotateFields')
            command.insert(2, '-rotateFields')
        command = tuple(command)
        job_request = JobRequest(
            name=f'transformPoints {request.operation}', argv=command, cwd=case,
            mutation=True, log_path=case / 'foammesh' / 'logs' / 'transformPoints.log',
            timeout=300.0)
        job = (await self._jobs.run(job_request) if on_line is None else
               await self._jobs.run(job_request, on_line=on_line))
        if job.status is not JobStatus.DONE:
            self._recovery.restore(case, recovery)
            record_artifact_event(
                case, operation=f'transform:{request.operation}', status=job.status.value,
                command=command, before_fingerprint=before.digest,
                after_fingerprint=before.digest, recovery_id=recovery.recovery_id,
                recovery_status='restored')
            return MeshTransformRun(job, recovery, restored=True, before=before_info)
        try:
            self._record_mutation(case, request)
            after_info = self._info.inspect(case)
        except (OSError, ValueError, RuntimeError) as error:
            self._recovery.restore(case, recovery)
            record_artifact_event(
                case, operation=f'transform:{request.operation}', status='validation_failed',
                command=command, before_fingerprint=before.digest,
                after_fingerprint=before.digest, recovery_id=recovery.recovery_id,
                recovery_status='restored', details={'error': str(error)})
            return MeshTransformRun(
                job, recovery, restored=True, before=before_info,
                validation_error=f'post-transform validation failed: {error}')
        self._recovery.mark_available(recovery)
        record_artifact_event(
            case, operation=f'transform:{request.operation}', status='applied',
            command=command, before_fingerprint=before.digest,
            after_fingerprint=after_info.fingerprint, recovery_id=recovery.recovery_id,
            recovery_status='available', details={
                'vector': list(request.vector), 'angle_degrees': request.angle_degrees,
                'pivot': list(request.pivot) if request.pivot else None,
            })
        return MeshTransformRun(
            job, recovery, restored=False, before=before_info, after=after_info)

    @staticmethod
    def _record_mutation(case: Path, request: MeshTransformRequest):
        classification = classify_case(case)
        if classification.poly_mesh_path is None:
            raise RuntimeError('transform completed without a complete polyMesh')
        try:
            previous = load_case_metadata(case)
        except CaseMetadataError:
            # A raw native case is as valid a transform target as it is a
            # repair target, and repair has always allowed it. Letting the
            # sidecar's absence escape here restored the recovery point after
            # transformPoints had already succeeded.
            previous = None
        fingerprint = fingerprint_poly_mesh(classification.poly_mesh_path)
        workflow, suspended = workflow_after_mesh_mutation(previous)
        provenance = dict(previous.provenance) if previous is not None else {}
        provenance['last_mesh_mutation'] = request.operation
        changes = dict(
            workflow=workflow,
            mesh_origin=MeshOrigin.DERIVED_MUTATION,
            artifact_state=ArtifactState.CURRENT,
            mesh_fingerprint=fingerprint,
            authored_workflow_suspended=suspended,
            provenance=provenance,
        )
        save_case_metadata(
            case, previous.evolve(**changes) if previous else CaseMetadata(**changes))


def _render_vector(vector) -> str:
    return '(' + ' '.join(format(value, '.12g') for value in vector) + ')'
