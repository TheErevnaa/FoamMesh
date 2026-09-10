"""Build, run, stream, and persist structured ``checkMesh`` reports."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from foammesh.core.case import fingerprint_poly_mesh
from foammesh.core.jobs import JobManager, JobRequest, JobResult, JobStatus

from .checkmesh_parser import CheckMeshResult, parse_checkmesh
from .failed_cells import discover_check_surfaces, discover_set_details


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


#: OpenFOAM 13's own reporting thresholds, read from ``checkMesh -help``:
#: "Threshold in degrees for reporting non-orthogonality errors, default: 70"
#: and "Threshold for reporting non-orthogonality errors, default: 4" (the
#: second help string is the release's own copy-paste; the option is
#: ``-skewThreshold`` and the number is a skewness).
DEFAULT_NON_ORTH_THRESHOLD = 70.0
DEFAULT_SKEW_THRESHOLD = 4.0

#: ``-surfaceFormat`` is never passed, and there is no control for it.
#:
#: MEASURED on OpenFOAM 13 build ``13-58ed5c2046ef``, then confirmed in the
#: source: ``checkMesh.C:164-168`` reads the *same* option into both writers
#:
#:     word surfaceFormat(vtkSurfaceWriter::typeName);
#:     args.optionReadIfPresent("surfaceFormat", surfaceFormat);
#:     word setFormat(vtkSetWriter::typeName);
#:     args.optionReadIfPresent("surfaceFormat", setFormat);   // <- bug
#:
#: so ``-setFormat`` is declared and never read (which is why passing it a
#: nonsense value is silently ignored), and ``-surfaceFormat`` has to name a
#: format both writers have. ``checkMesh -allTopology -allGeometry -writeSets
#: -writeSurfaces -surfaceFormat obj`` exits 1 with "Unknown write type obj /
#: Valid write types : (csv ensight gnuplot none raw vtk)" -- the *set*
#: writer's list. This product always passes ``-writeSets``, so the only
#: value that could ever be passed here is the default, and passing the
#: default is passing nothing.
DEFAULT_SURFACE_FORMAT = 'vtk'


def _is_default(value, default: float) -> bool:
    """Whether a threshold is OpenFOAM's own, and so need not be passed."""
    if value is None or value == '':
        return True
    try:
        return abs(float(value) - default) < 1e-9
    except (TypeError, ValueError):
        return True


def _number(value) -> str:
    """A threshold as OpenFOAM's argument parser wants to read it."""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


@dataclass(frozen=True)
class CheckMeshProfile:
    all_topology: bool = True
    all_geometry: bool = True
    write_sets: bool = False
    write_sets_needs_format: bool = False
    #: Whether this checkMesh can write per-cell quality fields. Plan 26 WP6.5.
    write_all_fields: bool = False
    #: Plan 31 (checkmesh.write_surfaces). ``-writeSurfaces`` reconstructs the
    #: faceSets and cellSets of the problem faces as a surface under
    #: ``postProcessing/checkMesh/``. MEASURED on OpenFOAM 13 build
    #: ``13-58ed5c2046ef`` to be strictly separate from ``-writeSets``: with
    #: only ``-writeSets`` the sets appear in ``constant/polyMesh/sets`` and
    #: ``postProcessing`` stays empty, and with only ``-writeSurfaces`` the
    #: reverse. Neither implies the other.
    write_surfaces: bool = False
    #: Plan 31 (checkmesh.thresholds_and_region). The reporting thresholds and
    #: the two checks that can be switched on the command line.
    non_orth_threshold_option: bool = False
    skew_threshold_option: bool = False
    mesh_quality_option: bool = False
    no_topology_option: bool = False
    source: str = 'Foundation-v13 baseline'

    @classmethod
    def from_help(cls, help_text: str) -> 'CheckMeshProfile':
        if not help_text.strip():
            raise ValueError('checkMesh help output is empty')
        topology = '-allTopology' in help_text
        geometry = '-allGeometry' in help_text
        if not topology or not geometry:
            raise ValueError('configured checkMesh does not advertise -allTopology and -allGeometry')
        write_line = next((line for line in help_text.splitlines() if '-writeSets' in line), '')
        return cls(
            all_topology=True, all_geometry=True, write_sets=bool(write_line),
            write_sets_needs_format=bool(re.search(r'-writeSets\s*[<\[]', write_line)),
            # Plan 26 WP6.5. Probed rather than assumed: the Display Control
            # selector colours by cellAspectRatio, nonOrthoAngle, skewness and
            # cellVolume, and nothing wrote those arrays, so it offered four
            # metrics backed by fields that did not exist.
            write_all_fields='-writeAllFields' in help_text,
            # Plan 31. Probed, never assumed: this product has been bitten
            # three times by a flag that exists in ESI's checkMesh and not in
            # the Foundation release, and a flag the binary does not know
            # fails the entire check rather than losing one artefact.
            write_surfaces='-writeSurfaces' in help_text,
            non_orth_threshold_option='-nonOrthThreshold' in help_text,
            skew_threshold_option='-skewThreshold' in help_text,
            mesh_quality_option='-meshQuality' in help_text,
            no_topology_option='-noTopology' in help_text,
            source='configured utility help')

    @classmethod
    def fallback(cls) -> 'CheckMeshProfile':
        """The Foundation-v13 ``-allTopology -allGeometry -writeSets`` flags.

        Used when the configured utility's ``-help`` cannot be probed so Mesh
        Check still runs rather than failing closed. Verified live on
        OpenFOAM-13: ``-writeSets`` takes no argument (the set format defaults
        to vtk), so no positional format is appended — appending one raises
        "Wrong number of arguments".
        """
        # ``write_all_fields`` stays False here on purpose: the fallback exists
        # because the utility could not be probed, and passing a flag an
        # unprobed binary may not accept would fail the whole check rather
        # than merely lose the colouring it feeds.
        # Plan 31. Every one of these was read off ``checkMesh -help`` on
        # OpenFOAM 13 build ``13-58ed5c2046ef`` and then exercised: each flag
        # was run against a sheared block and its effect recorded. They are
        # part of the baseline because they are part of the release, unlike
        # ``-writeAllFields`` above, which is ESI's.
        return cls(all_topology=True, all_geometry=True, write_sets=True,
                   write_sets_needs_format=False, write_all_fields=False,
                   write_surfaces=True,
                   non_orth_threshold_option=True, skew_threshold_option=True,
                   mesh_quality_option=True, no_topology_option=True,
                   source='baseline fallback')


@dataclass(frozen=True)
class CheckMeshRequest:
    write_sets: bool = True
    set_format: str = 'vtk'
    timeout: float | None = 300.0
    extended_topology: bool = True
    extended_geometry: bool = True
    #: Plan 26 WP6.5. Writes the per-cell quality fields the Display Control
    #: selector already colours by. It offered four metrics -- aspect ratio,
    #: non-orthogonal angle, skewness, cell volume -- against arrays nothing
    #: ever wrote, so every one of them coloured by a field that did not
    #: exist. This is a feed-the-pipe gap, not a build.
    write_all_fields: bool = True
    #: Plan 31 (checkmesh.write_surfaces). Off by default so the command line
    #: an untouched case runs is the one it has always run; the QA page turns
    #: it on. The sets in ``constant/polyMesh/sets`` are what the viewport
    #: reads, so this is the extra, not the essential, artefact.
    write_surfaces: bool = False
    #: Plan 31 (checkmesh.thresholds_and_region). OpenFOAM 13's own defaults,
    #: from ``checkMesh -help``. A value equal to the default writes no flag,
    #: which is what keeps every existing command line byte-identical.
    non_orth_threshold: float = DEFAULT_NON_ORTH_THRESHOLD
    skew_threshold: float = DEFAULT_SKEW_THRESHOLD
    #: ``-meshQuality``: judge the mesh against ``system/meshQualityDict``.
    #: Naming it when the file is absent aborts the check, so the caller is
    #: responsible for only setting it when the dictionary exists -- see
    #: :func:`checkmesh_request`.
    mesh_quality: bool = False
    #: ``-noTopology``: skip the topology checks entirely.
    skip_topology: bool = False

    def argv(self, utility: str, profile: CheckMeshProfile) -> tuple[str, ...]:
        if not utility:
            raise ValueError('checkMesh is not available')
        argv = [utility]
        if self.extended_topology and profile.all_topology:
            argv.append('-allTopology')
        if self.extended_geometry and profile.all_geometry:
            argv.append('-allGeometry')
        if self.write_sets and profile.write_sets:
            argv.append('-writeSets')
            if profile.write_sets_needs_format:
                if not re.fullmatch(r'[A-Za-z0-9_+-]+', self.set_format):
                    raise ValueError('checkMesh set format contains unsafe characters')
                argv.append(self.set_format)
        # Both sides must agree: the request asks and the probed utility
        # confirms. Asking a binary that does not advertise the flag would
        # fail the whole check rather than lose the colouring it feeds.
        if self.write_all_fields and profile.write_all_fields:
            argv.append('-writeAllFields')
        if self.write_surfaces and profile.write_surfaces:
            # No ``-surfaceFormat`` beside it, deliberately: see
            # DEFAULT_SURFACE_FORMAT above. On v13 that option also selects
            # the *set* writer, so any value but the default aborts the run
            # this product always makes.
            argv.append('-writeSurfaces')
        # A threshold equal to OpenFOAM's own is not passed at all. That is
        # not tidiness: two gates in this repository compare the DAG's
        # checkMesh argv with ``mesh.check``'s, and a case nobody has touched
        # must still produce the command line it produced before this
        # capability existed.
        if (profile.non_orth_threshold_option
                and not _is_default(self.non_orth_threshold,
                                    DEFAULT_NON_ORTH_THRESHOLD)):
            argv.extend(('-nonOrthThreshold', _number(self.non_orth_threshold)))
        if (profile.skew_threshold_option
                and not _is_default(self.skew_threshold,
                                    DEFAULT_SKEW_THRESHOLD)):
            argv.extend(('-skewThreshold', _number(self.skew_threshold)))
        if self.mesh_quality and profile.mesh_quality_option:
            argv.append('-meshQuality')
        if self.skip_topology and profile.no_topology_option:
            argv.append('-noTopology')
        return tuple(argv)


#: Where ``-meshQuality`` reads its user-defined criteria from. Naming the
#: flag without this file present aborts the check, so it is looked for on
#: disk rather than inferred from the setting alone.
MESH_QUALITY_DICT = Path('system') / 'meshQualityDict'


def checkmesh_request(db=None, case_path: str | Path | None = None,
                      **overrides) -> CheckMeshRequest:
    """One request, built from the project, for every launcher of checkMesh.

    Plan 31 (``checkmesh.thresholds_and_region``, ``checkmesh.write_surfaces``).
    ``mesh.check`` built its request from operation parameters and the meshing
    DAG built none at all, so a threshold a user set on the QA page could only
    ever reach one of the two -- and both write the same report to the same
    path. The settings live in the configuration, so this reads them there and
    both callers use it.

    ``mesh_quality`` is the one setting that is *not* taken at face value:
    ``-meshQuality`` reads ``system/meshQualityDict`` and OpenFOAM 13 aborts
    the whole check if the file is absent. The case writer emits it whenever
    the setting is on, but a project whose dictionaries were written before
    the setting was turned on has the setting and not the file. So the flag is
    passed only when the file is actually there, and turning the setting on
    then regenerating the case is what makes it take effect.
    """
    def value(path: str, fallback):
        if db is None:
            return fallback
        try:
            raw = db.getValue(path)
        except Exception:                                    # noqa: BLE001
            return fallback
        return fallback if raw is None or raw == '' else raw

    def flag(path: str) -> bool:
        raw = value(path, False)
        if isinstance(raw, str):
            return raw.strip().lower() in ('true', '1', 'yes', 'on')
        return bool(raw)

    def number(path: str, fallback: float) -> float:
        try:
            return float(value(path, fallback))
        except (TypeError, ValueError):
            return fallback

    wants_mesh_quality = flag('meshCheck/userDefinedChecks')
    if wants_mesh_quality and case_path is not None:
        wants_mesh_quality = (Path(case_path) / MESH_QUALITY_DICT).is_file()
    elif wants_mesh_quality:
        wants_mesh_quality = False

    request = CheckMeshRequest(
        write_surfaces=flag('meshCheck/writeSurfaces'),
        non_orth_threshold=number('meshCheck/nonOrthThreshold',
                                  DEFAULT_NON_ORTH_THRESHOLD),
        skew_threshold=number('meshCheck/skewThreshold',
                              DEFAULT_SKEW_THRESHOLD),
        mesh_quality=wants_mesh_quality,
        skip_topology=flag('meshCheck/skipTopology'))
    if overrides:
        from dataclasses import replace
        request = replace(request, **overrides)
    return request


def checkmesh_flags(*, profile: CheckMeshProfile | None = None,
                    request: CheckMeshRequest | None = None,
                    ) -> tuple[str, ...]:
    """The flags every checkMesh run carries, whoever launches it.

    Plan 30 WP-04 (F-04). Separated from :func:`checkmesh_command` because
    ``mesh.check`` hands its flags to the runtime registry, which prepends its
    own launcher (``wsl -d ... checkMesh``); the flags must still be the ones
    the DAG uses or the two produce different reports at the same path.
    """
    profile = profile if profile is not None else CheckMeshProfile.fallback()
    request = request or CheckMeshRequest()
    return tuple(request.argv('checkMesh', profile)[1:])


def checkmesh_command(case_path: str | Path, *,
                      profile: CheckMeshProfile | None = None,
                      request: CheckMeshRequest | None = None,
                      ranks: int = 1, mpirun: str = 'mpirun',
                      mpi_options: tuple[str, ...] = (),
                      ) -> tuple[str, ...]:
    """The one ``checkMesh`` command line, serial or MPI.

    Plan 30 WP-04 (F-04). There were two checkMesh policies. ``mesh.check``
    built its flags here -- ``-allTopology -allGeometry -writeSets`` and, when
    the probed utility advertises it, ``-writeAllFields`` -- while the meshing
    DAG ran a bare ``checkMesh -case`` (or ``mpirun -np N checkMesh -parallel
    -case``). Both persist to the same ``foammesh/quality/latest.json``, so
    which report the QA page showed depended on which of the two had run last:
    a pipeline run overwrote a full report with a thin one, and the cell sets
    and per-cell fields the Display Control colours by simply vanished.

    One builder, one flag set, one report. ``profile`` is the probed
    capability of the configured utility; without it the verified
    Foundation-13 baseline applies, which is the same fallback ``mesh.check``
    uses when the probe is unavailable.

    ``mpi_options`` carries whatever the *probed* runtime needs before
    ``-np`` -- on this machine's root-user WSL distribution that is
    ``--allow-run-as-root``, without which Open MPI starts no ranks at all
    (Plan 31 CP-07).
    """
    flags = checkmesh_flags(profile=profile, request=request)
    case = str(Path(case_path))
    if ranks > 1:
        return (mpirun, *mpi_options, '-np', str(ranks), 'checkMesh', *flags,
                '-parallel', '-case', case)
    return ('checkMesh', *flags, '-case', case)


#: The OpenFOAM check: ``checkMesh``'s opinion of an OpenFOAM mesh. Its slot
#: keeps the name every reader in this codebase already opens.
NATIVE_CHECK = 'checkmesh'

#: The SU2 check: can SU2 read this mesh (:mod:`core.quality.su2_readiness`).
#: An *export-target* question, asked of the same native mesh.
SU2_READINESS_CHECK = 'su2-readiness'

#: Where each check files its verdict, relative to the case.
#:
#: Plan 31 CP-05 item 2. There was one slot, and the readiness check wrote
#: through the same ``persist_result`` as ``checkMesh`` -- so asking the SU2
#: question about an unchanged mesh destroyed the OpenFOAM answer about it,
#: and switching the target back found no native evidence for a mesh nobody
#: had touched. An export-target change may invalidate export-specific
#: qualification; it may not invalidate the native mesh's own.
CHECK_REPORTS = {
    NATIVE_CHECK: Path('foammesh') / 'quality' / 'latest.json',
    SU2_READINESS_CHECK: (
        Path('foammesh') / 'quality' / 'su2-readiness-check.json'),
}

#: Which check each QA operation writes. Read from here rather than derived
#: from the operation name: a lookup keyed on something its producer does not
#: write is how this codebase has repeatedly lost a report.
OPERATION_CHECKS = {
    'mesh.check': NATIVE_CHECK,
    'quality.su2_readiness': SU2_READINESS_CHECK,
}


def check_for(operation: str) -> str:
    """The check *operation* writes. Unknown means the OpenFOAM check."""
    return OPERATION_CHECKS.get(str(operation or ''), NATIVE_CHECK)


def report_path(case_path: str | Path, check: str = NATIVE_CHECK) -> Path:
    """Where *check* files its verdict on this case."""
    return Path(case_path) / CHECK_REPORTS.get(
        check, CHECK_REPORTS[NATIVE_CHECK])


def fingerprint_mesh_file(path: str | Path) -> str:
    """A digest of one mesh *file*, for a check that read a file not a case.

    Plan 31 DP-19. The same shape as :func:`fingerprint_poly_mesh` -- name,
    byte length, then content, so a rename or a truncation cannot collide with
    the original -- for the meshes that are a single file. A native SU2 run
    has no polyMesh to fingerprint, and a verdict filed against no fingerprint
    at all could never be told apart from a verdict about a mesh since
    replaced.
    """
    path = Path(path)
    digest = hashlib.sha256()
    digest.update(path.name.encode('utf-8'))
    digest.update(b'\0')
    digest.update(str(path.stat().st_size).encode('ascii'))
    digest.update(b'\0')
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class QualityReport:
    result: CheckMeshResult
    mesh_fingerprint: str
    checked_at: str
    command: tuple[str, ...]
    log_path: Path
    sets: tuple[dict, ...] = ()
    #: Plan 31 (checkmesh.write_surfaces). What ``-writeSurfaces`` wrote, if
    #: it was asked to. Kept apart from ``sets``: these live under
    #: ``postProcessing/checkMesh`` rather than ``constant/polyMesh/sets``,
    #: they are surfaces rather than label lists, and nothing that reads the
    #: sets directory can see them at all.
    surfaces: tuple[dict, ...] = ()
    stale: bool = False
    schema_version: int = 2
    #: Which mesh ``mesh_fingerprint`` covers. Empty means the case's
    #: ``constant/polyMesh``, which is what every OpenFOAM report says and
    #: what every report written before Plan 31 DP-19 says.
    #:
    #: A native SU2 run publishes no polyMesh, so its readiness verdict is
    #: fingerprinted against the ``mesh.su2`` the accepted run wrote and names
    #: it here. Staleness is then still a comparison against the same bytes
    #: the check read, rather than the permanent "STALE" a missing polyMesh
    #: would otherwise force on every SU2 project.
    mesh_source: str = ''

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'mesh_fingerprint': self.mesh_fingerprint,
            'mesh_source': self.mesh_source,
            'checked_at': self.checked_at,
            'command': list(self.command),
            'log_path': str(self.log_path),
            'sets': [dict(item) for item in self.sets],
            'surfaces': [dict(item) for item in self.surfaces],
            'stale': self.stale,
            'result': self.result.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'QualityReport':
        allowed = CheckMeshResult.__dataclass_fields__
        result_data = data.get('result') or {}
        return cls(
            result=CheckMeshResult(**{
                key: value for key, value in result_data.items() if key in allowed}),
            mesh_fingerprint=str(data.get('mesh_fingerprint', '')),
            checked_at=str(data.get('checked_at', '')),
            command=tuple(str(item) for item in data.get('command', ())),
            log_path=Path(data.get('log_path', '')),
            sets=tuple(dict(item) for item in data.get('sets', ())),
            surfaces=tuple(dict(item) for item in data.get('surfaces', ())),
            stale=bool(data.get('stale', False)),
            schema_version=int(data.get('schema_version', 2)),
            mesh_source=str(data.get('mesh_source', '')),
        )


COMPARISON_METRICS = ('max_non_ortho', 'avg_non_ortho', 'max_skewness',
                      'max_aspect_ratio', 'failed_checks')


def compare_reports(baseline: QualityReport, current: QualityReport) -> dict:
    """Pure per-metric diff of two fingerprinted quality reports.

    Lower is better for every compared metric, so a negative delta is
    ``improved`` and a positive delta is ``regressed``.
    """
    comparison = {}
    for metric in COMPARISON_METRICS:
        before = getattr(baseline.result, metric)
        after = getattr(current.result, metric)
        delta = after - before if before is not None and after is not None else None
        comparison[metric] = {
            'baseline': before, 'current': after, 'delta': delta,
            'trend': ('unchanged' if delta == 0 else
                      'improved' if delta is not None and delta < 0 else
                      'regressed' if delta is not None else 'unavailable')}
    return {
        'baseline_fingerprint': baseline.mesh_fingerprint,
        'current_fingerprint': current.mesh_fingerprint,
        'baseline_verdict': baseline.result.severity,
        'current_verdict': current.result.severity,
        'metrics': comparison}


@dataclass(frozen=True)
class MeshCheckRun:
    job: JobResult
    result: CheckMeshResult | None
    report_path: Path | None
    report: QualityReport | None = None


class MeshCheckService:
    def __init__(self, jobs: JobManager | None = None, *, utility: str = 'checkMesh',
                 help_text: str | None = None, launcher=None):
        self._jobs = jobs or JobManager()
        self._utility = utility
        self._profile = (CheckMeshProfile.from_help(help_text)
                         if help_text is not None else CheckMeshProfile())
        self._launcher = launcher

    async def run(self, case_path: str | Path, *, request: CheckMeshRequest | None = None,
                  on_line=None) -> MeshCheckRun:
        case = Path(case_path)
        mesh = case / 'constant' / 'polyMesh'
        try:
            fingerprint_digest = fingerprint_poly_mesh(mesh).digest
        except ValueError:
            # Preserve compatibility with callers that parse an externally
            # supplied log in a report-only directory.  Real case actions are
            # policy-gated on a complete polyMesh.
            fingerprint_digest = 'unavailable'
        request = request or CheckMeshRequest()
        if self._launcher is not None:
            semantic = request.argv('checkMesh', self._profile)[1:]
            launch = self._launcher('checkMesh', semantic, cwd=case)
            command = launch.argv
            cleanup_argv = launch.cleanup_argv
        else:
            command = request.argv(self._utility, self._profile)
            cleanup_argv = ()
        report_path = case / 'foammesh' / 'quality' / 'latest.json'
        log_path = case / 'foammesh' / 'logs' / 'checkMesh.log'
        job_request = JobRequest(
            name='checkMesh', argv=command, cwd=case, mutation=False,
            log_path=log_path, timeout=request.timeout,
            cleanup_argv=cleanup_argv)
        if on_line is None:
            job = await self._jobs.run(job_request)
        else:
            job = await self._jobs.run(job_request, on_line=on_line)
        if not job.output and job.status is not JobStatus.DONE:
            return MeshCheckRun(job, None, None, None)

        result = parse_checkmesh(job.output)
        if job.status is JobStatus.CANCELLED:
            result.incomplete = True
            result.severity = 'incomplete'
            result.recommendations.insert(0, 'Mesh Check was cancelled; run it again for a current verdict.')
        elif job.status is JobStatus.TIMED_OUT:
            result.incomplete = True
            result.severity = 'incomplete'
            result.recommendations.insert(0, 'Mesh Check timed out; review the log and retry with a longer timeout.')
        sets = tuple(discover_set_details(case))
        for item in sets:
            if item['name'] not in result.reported_sets:
                result.reported_sets.append(item['name'])
        report = QualityReport(
            result=result, mesh_fingerprint=fingerprint_digest,
            checked_at=_now(), command=command, log_path=log_path, sets=sets,
            surfaces=tuple(discover_check_surfaces(case)))
        self._save_report(report_path, report)
        return MeshCheckRun(job, result, report_path, report)

    @classmethod
    def persist_result(cls, case_path: str | Path, result: CheckMeshResult, *,
                       command: tuple[str, ...], log_path: Path,
                       check: str = NATIVE_CHECK,
                       mesh_source: str | Path | None = None
                       ) -> tuple[Path, QualityReport]:
        """Persist an executor-produced result using the canonical report schema.

        ``check`` names which question was answered. Each check owns its own
        slot, so an export-target check cannot overwrite the native mesh's
        OpenFOAM verdict (CP-05 item 2).

        ``mesh_source`` names the mesh file the verdict was reached from, for
        a check that did not read ``constant/polyMesh``. Plan 31 DP-19: this
        method fingerprinted the polyMesh unconditionally, so filing a verdict
        about a native ``mesh.su2`` -- the only mesh an SU2 project has --
        raised, and ``su2_readiness._persist`` is best-effort, so the verdict
        was silently never filed and the QA row could never be answered.
        Left unset it is exactly what it always was: an OpenFOAM case
        fingerprints its polyMesh and gets the same digest as before, so no
        report on disk becomes stale on upgrade.
        """
        case = Path(case_path)
        if mesh_source is None:
            fingerprint_digest = fingerprint_poly_mesh(
                case / 'constant' / 'polyMesh').digest
            source = ''
        else:
            fingerprint_digest = fingerprint_mesh_file(Path(mesh_source))
            source = str(mesh_source)
        sets = tuple(discover_set_details(case))
        for item in sets:
            if item['name'] not in result.reported_sets:
                result.reported_sets.append(item['name'])
        report = QualityReport(
            result=result, mesh_fingerprint=fingerprint_digest,
            checked_at=_now(), command=command, log_path=log_path, sets=sets,
            surfaces=tuple(discover_check_surfaces(case)),
            mesh_source=source)
        target = report_path(case, check)
        cls._save_report(target, report)
        return target, report

    @staticmethod
    def _save_report(path: Path, report: QualityReport):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f'{path.suffix}.tmp')
        try:
            with temporary.open('w', encoding='utf-8', newline='\n') as output:
                json.dump(report.to_dict(), output, indent=2, sort_keys=True)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def load_report(case_path: str | Path,
                    check: str = NATIVE_CHECK) -> QualityReport | None:
        """The stored verdict of *check* on this case, or ``None``."""
        case = Path(case_path)
        path = report_path(case, check)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding='utf-8'))
        default_log = case / 'foammesh' / 'logs' / 'checkMesh.log'
        if data.get('schema_version') == 2 and isinstance(data.get('result'), dict):
            document = dict(data)
            document['log_path'] = data.get('log_path') or str(default_log)
        else:  # schema-1 compatibility: the result was the whole document
            document = {
                'schema_version': 2,
                'result': data,
                'mesh_fingerprint': data.get('mesh_fingerprint', ''),
                'checked_at': data.get('checked_at', ''),
                'command': [],
                'log_path': str(default_log),
            }
        # `from_dict` is the rehydration, and it is the one that knows every
        # field. This used to hand-roll a second one, field by field, and it
        # never learned about `surfaces`: a user could turn on
        # `-writeSurfaces`, checkMesh would write the failing faces,
        # `persist_result` would record where they landed, and the read back
        # in here would drop them on the floor every time (DP-20).
        report = QualityReport.from_dict(document)
        stale = True
        try:
            # A report that names the file it read is re-checked against that
            # file. Everything else is a polyMesh report, and is compared the
            # way it always was -- including the reports written before
            # `mesh_source` existed, which name nothing (DP-19).
            current = (fingerprint_mesh_file(report.mesh_source)
                       if report.mesh_source
                       else fingerprint_poly_mesh(
                           case / 'constant' / 'polyMesh').digest)
            stale = current != report.mesh_fingerprint
        except (OSError, ValueError):
            pass
        return replace(report, stale=stale)

    @staticmethod
    def load_latest(case_path: str | Path) -> CheckMeshResult | None:
        report = MeshCheckService.load_report(case_path)
        return report.result if report else None
