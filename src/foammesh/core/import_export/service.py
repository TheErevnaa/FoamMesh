"""One staged, history-recording service boundary for every export format."""
from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from foammesh.core.case import record_artifact_event
from foammesh.core.export import ExportFormat, capability, list_formats, readiness
from foammesh.core.export import gmsh_export as _gmsh
from foammesh.core.export import cgns_export as _cgns
# ``POLY_MESH_FILES`` is re-exported under its own name on purpose: DP-266
# wrote the list here, and the fault its Left standing named was that
# ``validate_case`` did not know it.  The list moved next to that verdict in
# ``core/export/openfoam.py`` -- this module already imports that one, so the
# reverse import would have been a cycle -- and every reader that learned the
# name here still reads the one list rather than a second copy that can drift.
from foammesh.core.export.openfoam import (
    POLY_MESH_FILES as POLY_MESH_FILES,
    missing_poly_mesh_files as missing_poly_mesh_files,
    validate_case,
)
from foammesh.core.export.msh_interchange import write_msh_interchange
from foammesh.core.export.vtk_export import (
    load_case_blocks, load_case_dataset, read_vtu_counts, write_vtu,
)
from foammesh.core.format_registry import format_spec


FLUENT_EXPORT_UTILITY = 'foamMeshToFluent'
FORMAT_CONVERT_UTILITY = 'foamFormatConvert'

#: The formats the export page offers that are written by converting the
#: accepted Gmsh run's own ``mesh.msh``: ``entry_id -> (registry id, suffix)``.
#:
#: Plan 31 FC-F, ledger row ``export-formats-unexposed``. FC-A measured that
#: the Gmsh writer keeps every physical group name in MED, CGNS and UNV, and
#: qualified that against the writer. It could not be asked for from the
#: product: ``ExportPage.MESH_FORMATS`` listed five ids and none of these was
#: one of them.
#:
#: The source is the run's ``.msh`` and not this case's ``constant/polyMesh``,
#: and that is a measurement rather than a preference. MEASURED 2026-09-06 on
#: ``tests/fixtures/cases/single_hex``, whose ``polyMesh/boundary`` names one
#: patch ``walls``: the existing ``case.export.gmsh`` route -- polyMesh, to a
#: VTK dataset, to a legacy ``.vtk``, to Gmsh -- produced a ``.msh`` of 8
#: nodes, 1 hexahedron and **zero physical groups**. That is the same loss
#: FC-A refused ``.vtk`` as a writer target for, so a MED derived that way
#: would carry no patch identity either. The accepted run's ``mesh.msh`` does
#: carry it, which is why it is what gets converted. See DP-23.
GMSH_CONVERSION_FORMATS = {
    'med': ('mesh.med.export', '.med'),
    'unv': ('mesh.unv.export', '.unv'),
    'cgns': ('mesh.cgns.gmsh.export', '.cgns'),
}

#: Why the Gmsh conversion route cannot be offered for a case that has no
#: accepted Gmsh run. A snappyHexMesh case never has one, and this is the
#: sentence its export page shows rather than a bare "not available".
NO_ACCEPTED_RUN = (
    'this format is written by converting the mesh the accepted Gmsh run '
    'produced, and this case has no accepted Gmsh run to convert')


@dataclass(frozen=True)
class ExportEntry:
    """Registry row consumed by the GUI: truthful availability, never hidden."""
    entry_id: str
    label: str
    maturity: str            # 'stable' | 'experimental'
    available: bool
    reason: str              # why unavailable ('' when available)
    destination: str         # 'directory' | 'file' | 'in_case'
    suffix: str
    notes: str = ''
    #: Where the exported file would come from: ``'native'`` when a mesher
    #: already wrote it and the export is a copy, ``'derived'`` when this
    #: service builds it from the published case. Plan 30 WP-07 (F-08): the
    #: Gmsh run writes ``mesh.su2`` and the export page used to re-derive a
    #: different SU2 file through the VTK writer, so a user could be handed a
    #: mesh that was not the one their run produced.
    source: str = 'derived'
    source_path: str = ''

    def to_dict(self) -> dict:
        return {'entry_id': self.entry_id, 'label': self.label,
                'maturity': self.maturity, 'available': self.available,
                'reason': self.reason, 'destination': self.destination,
                'suffix': self.suffix, 'notes': self.notes,
                'source': self.source, 'source_path': self.source_path}


@dataclass(frozen=True)
class ExportOutcome:
    entry_id: str
    source: Path
    destination: Path
    total_bytes: int
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {'entry_id': self.entry_id, 'source': str(self.source),
                'destination': str(self.destination),
                'total_bytes': self.total_bytes, 'warnings': list(self.warnings)}


@dataclass(frozen=True)
class NativeExportResult:
    source: Path
    destination: Path
    warnings: tuple[str, ...]


def _classified(error: RuntimeError) -> ValueError:
    """Carry the converter's classification onto the refusal it becomes.

    EXPORT-01. :mod:`foammesh.core.export.gmsh_export` decides whether a
    conversion was interrupted, wrote something it could not read back, or
    never ran; the callers of this service raise on ValueError, so the word
    would be lost at the boundary unless it is carried across. Reading
    ``error.kind`` tells a caller which of the three it was without parsing
    the sentence.
    """
    refusal = ValueError(str(error))
    refusal.kind = getattr(error, 'kind', 'failed')
    return refusal


def path_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())
    return 0


def record_export_event(case_path, *, entry_id: str, destination, total_bytes: int,
                        warnings=(), status: str = 'applied', details: dict | None = None):
    """Shared export-history hook for Case Tools export and the authored step."""
    payload = {'destination': str(destination), 'total_bytes': int(total_bytes),
               'warnings': [str(item) for item in warnings]}
    payload.update(details or {})
    return record_artifact_event(
        case_path, operation=f'export:{entry_id}', status=status, details=payload)


def _dataset_count(dataset, method: str) -> int:
    """A census read off the dataset that was actually written.

    The VTK reader hands back a dataset, not a document, so the only honest
    count an export can file is the one the object it wrote reports. A build
    whose reader answers neither call files no number rather than a zero that
    would read as an empty mesh.
    """
    call = getattr(dataset, method, None)
    if call is None:
        return 0
    try:
        return int(call())
    except Exception:
        return 0


def _kept_run_id(case_path) -> str:
    """The newest run this case kept, or ``''`` when it kept none.

    ``accepted_run_layout`` answers the same question but returns ``None`` for
    a kept run that recorded no processor layout, which is exactly the case a
    serial mesh leaves behind. The run id is still a fact then, and it is the
    one thing that lets a reader trace an export back to the mesh it came
    from, so it is read on its own here.
    """
    from foammesh.core.gmsh.manifest import (
        EXPORTABLE, run_disposition, run_documents)

    for root, document in run_documents(case_path):
        if run_disposition(document) in EXPORTABLE:
            return str(document.get('run_id') or root.name)
    return ''


def _su2_identity_problems(census, document: dict) -> tuple[list, list]:
    """Hold an SU2 file against the identity the run recorded for it.

    Plan 31 CP-05 item 5. The export used to be accepted on "it has volume
    elements", which a truncated or half-copied file also has. What identifies
    a mesh to the solver that reads it is its boundary: the markers, their
    names, and how many cells they bound.

    MEASURED against archived Gmsh reproductions at first and second order:
    ``NELEM`` equals ``statistics.mesh.cells``, ``NPOIN`` equals
    ``statistics.mesh.nodes``, and the number of ``MARKER_TAG`` blocks equals
    ``statistics.groups.boundarySurfaces`` in every one. Counts that must
    match are refusals; the point count is reported as a warning instead,
    because a mismatch there is a reason to look rather than a reason to
    withhold a mesh the user asked for.

    An older run that recorded no statistics is not a fault: there is simply
    nothing to check it against, and it is exported as before.
    """
    statistics = dict(document.get('statistics') or {})
    mesh = dict(statistics.get('mesh') or {})
    groups = dict(statistics.get('groups') or {})
    problems: list = []
    warnings: list = []

    markers = list(census.markers)
    if not markers:
        problems.append('the copied file names no boundary markers, so the '
                        'mesh has no boundary identity to give the solver')
    blank = [name for name in markers if not str(name).strip()]
    if blank:
        problems.append('the copied file has an unnamed boundary marker')
    duplicated = sorted({name for name in markers if markers.count(name) > 1})
    if duplicated:
        problems.append('the copied file names the boundary '
                        + ', '.join(duplicated) + ' more than once')

    # DP-63. The names the run grouped its boundary into, which is what a
    # marker is. This used to read `boundarySurfaces`, the count of surface
    # *entities*, and a named STL surface is deliberately one patch however
    # many faces it became -- so a pipe from one solid meshed as six surfaces
    # under one name, and every SU2 export of it was refused for carrying one
    # marker instead of six. MEASURED on `test_cases/gmsh/pipe_stl_su2`.
    expected_names = [str(name) for name in (groups.get('boundaryGroupNames') or ())
                      if str(name).strip()]
    if expected_names and markers:
        missing = sorted(set(expected_names) - set(str(name) for name in markers))
        extra = sorted(set(str(name) for name in markers) - set(expected_names))
        if missing:
            problems.append(
                'the run named the boundary ' + ', '.join(missing)
                + ' and the copied file does not carry '
                + ('them' if len(missing) > 1 else 'it'))
        if extra:
            problems.append(
                'the copied file names the boundary ' + ', '.join(extra)
                + ' and the run recorded no such patch')
    else:
        # A manifest from before the names were recorded. A group is one or
        # more surfaces and never none, so more markers than surfaces is
        # impossible and fewer is ordinary; that inequality is the part of
        # the old equality check that was ever true.
        expected_markers = groups.get('boundarySurfaces')
        if (isinstance(expected_markers, int) and expected_markers > 0
                and markers and len(markers) > expected_markers):
            problems.append(
                f'the run meshed {expected_markers} boundary surfaces but the '
                f'copied file carries {len(markers)} markers '
                f'({", ".join(str(name) for name in markers)}), which is more '
                f'boundary than the run had to give it')

    expected_cells = mesh.get('cells')
    if isinstance(expected_cells, int) and expected_cells > 0:
        if census.volume_count != expected_cells:
            problems.append(
                f'the run wrote {expected_cells} cells but the copied file '
                f'holds {census.volume_count}')

    expected_nodes = mesh.get('nodes')
    if isinstance(expected_nodes, int) and expected_nodes > 0:
        if census.point_count != expected_nodes:
            warnings.append(
                f'the run recorded {expected_nodes} nodes and the exported '
                f'file holds {census.point_count}')
    return problems, warnings


class ImportExportService:
    """Single service boundary for menu and authored-workflow export callers."""

    def __init__(self, utilities: dict[str, str] | None = None):
        self._utilities = dict(utilities or {})

    def formats(self, *, case_path=None):
        """Every format, with a readiness report for this case's mesh.

        Plan 28: without ``case_path`` this can only say whether a writer
        exists, and that is what it used to say -- so SU2 read as ready for
        every snappyHexMesh case, whose polyhedra SU2 cannot open. Pass the
        case and the mesh's own census decides.
        """
        census = self._census(case_path)
        return [(fmt, readiness(fmt, census=census)) for fmt in list_formats()]

    @staticmethod
    def _census(case_path):
        """The mesh census, or None when there is nothing to census yet.

        An unreadable or absent mesh is not an error here: the export page is
        shown before a mesh exists, and it must list the formats.
        """
        if case_path is None:
            return None
        from foammesh.core.mesh.census import cell_census
        census = cell_census(case_path)
        return None if census.read_error else census

    def format_summary(self, *, case_path=None) -> tuple[dict, ...]:
        return tuple({
            'format': fmt.value, 'available': report.ok, 'warnings': tuple(report.warnings),
            'errors': tuple(report.errors),
        } for fmt, report in self.formats(case_path=case_path))

    @staticmethod
    def native_su2_artifact(case_path):
        """The ``mesh.su2`` the *accepted* Gmsh run wrote, or ``None``.

        Plan 30 WP-07 (F-08). Gmsh's own SU2 writer is the one SU2 writer: it
        is what the run produced, it is what the manifest records, and it is
        what a second-order mesh only exists as. This is how the export side
        finds it instead of writing a second file of its own.

        Plan 31 CP-05 item 5. It used to ask for the *latest* such file.
        MEASURED with an accepted run A followed by a candidate B that the
        quality gate refused: the export handed the user B's mesh, under A's
        name, with B's boundary markers in it. The question an exporter has to
        ask is which artifact was accepted, and that is what
        :func:`accepted_artifact` answers.
        """
        if case_path is None:
            return None
        try:
            from foammesh.core.gmsh.manifest import accepted_artifact
            return accepted_artifact(Path(case_path), 'su2')
        except Exception:                                    # noqa: BLE001
            return None

    @staticmethod
    def native_msh_artifact(case_path):
        """The ``mesh.msh`` the *accepted* Gmsh run wrote, or ``None``.

        The same question :meth:`native_su2_artifact` asks, about the file the
        conversion formats are written from. Every run writes this one -- it is
        what the polyMesh publisher, the element census and the fidelity check
        all read -- so it is there whenever an accepted Gmsh run is.
        """
        if case_path is None:
            return None
        try:
            from foammesh.core.gmsh.manifest import accepted_artifact
            return accepted_artifact(Path(case_path), 'msh')
        except Exception:                                    # noqa: BLE001
            return None

    def gmsh_conversion_source(self, case_path):
        """``(artifact, reason)`` for the Gmsh conversion route on this case.

        The artifact is ``None`` and the reason says which of the three things
        is missing: a Gmsh writer, an accepted run, or a run whose ``.msh`` is
        still the file it recorded.
        """
        if not _gmsh.is_available():
            return None, ('Gmsh export needs the gmsh module on the host '
                          '(pip install "foammesh[export]") or the qualified '
                          'WSL Gmsh runtime')
        artifact = self.native_msh_artifact(case_path)
        if artifact is None:
            return None, NO_ACCEPTED_RUN
        from foammesh.core.gmsh.manifest import artifact_provenance_error
        problem = artifact_provenance_error(artifact)
        if problem:
            return None, problem
        return artifact, ''

    def _conversion_entry(self, entry_id, case_path, source, reason) -> ExportEntry:
        """One export row for a format the Gmsh writer converts into."""
        spec = format_spec(GMSH_CONVERSION_FORMATS[entry_id][0])
        note = spec.notes
        if source is not None:
            note = (f'Converted from mesh.msh written by Gmsh run '
                    f'{source.get("run_id", "")}. {note}').strip()
        return ExportEntry(
            entry_id=entry_id, label=spec.display_name,
            maturity=spec.maturity.value, available=source is not None,
            reason=reason, destination='file',
            suffix=spec.extensions[0] if spec.extensions else '',
            notes=note, source='native' if source is not None else 'derived',
            source_path=str((source or {}).get('path', '')))

    def export_entries(self, *, case_path=None) -> tuple[ExportEntry, ...]:
        """Every export format, visible with a truthful availability reason."""
        entries = []
        native_su2 = self.native_su2_artifact(case_path)
        conversion_source, conversion_reason = self.gmsh_conversion_source(case_path)
        for fmt, report in self.formats(case_path=case_path):
            spec = capability(fmt)
            entry = ExportEntry(
                entry_id=fmt.value, label=spec.display_name or fmt.value,
                maturity=spec.maturity, available=report.ok,
                reason='; '.join(report.errors),
                destination='directory' if fmt is ExportFormat.OPENFOAM else 'file',
                suffix=spec.suffix, notes=spec.notes)
            if fmt is ExportFormat.SU2 and native_su2 is not None:
                # The file exists. Whatever the polyMesh census says -- and on
                # a second-order run there is no polyMesh to census -- the
                # export is a copy of a mesh that is already on disk.
                #
                # Unless it is no longer that mesh: a run directory is an
                # ordinary folder in the user's case, and the page says so
                # here rather than letting the export discover it (CP-05
                # item 5).
                from foammesh.core.gmsh.manifest import artifact_provenance_error
                problem = artifact_provenance_error(native_su2)
                entry = replace(
                    entry, available=not problem, reason=problem,
                    source='native',
                    source_path=str(native_su2.get('path', '')),
                    notes=(f'Copies mesh.su2 written by Gmsh run '
                           f'{native_su2.get("run_id", "")}.').strip())
            if fmt is ExportFormat.CGNS and conversion_source is not None:
                # Plan 31 FC-F. One CGNS row, two routes, the working one
                # preferred -- rather than a second row that says CGNS again.
                # MEASURED on this host: `vtkmodules.vtkIOCGNSWriter` is not
                # installed, so the VTK route's readiness is an error and this
                # row was permanently `(not available)` in the dialog. The
                # Gmsh writer needs no VTK CGNS module, and FC-A measured that
                # it keeps every group name.
                entry = replace(
                    entry, available=True, reason='', source='native',
                    source_path=str(conversion_source.get('path', '')),
                    notes=self._conversion_entry(
                        'cgns', case_path, conversion_source, '').notes)
            entries.append(entry)
        for entry_id in ('med', 'unv'):
            entries.append(self._conversion_entry(
                entry_id, case_path, conversion_source, conversion_reason))
        entries.append(self._utility_entry(
            'openfoam_format', 'OpenFOAM ASCII/binary conversion (in place)',
            FORMAT_CONVERT_UTILITY, destination='in_case', maturity='stable',
            notes='Rewrites case IO objects via foamFormatConvert using controlDict write settings.'))
        entries.append(self._utility_entry(
            'fluent', 'Fluent mesh (.msh) via foamMeshToFluent',
            FLUENT_EXPORT_UTILITY, destination='in_case', maturity='stable',
            notes='Writes fluentInterface/<case>.msh next to the case; documented Fluent limitations apply.'))
        return tuple(entries)

    def _utility_entry(self, entry_id, label, utility_name, *, destination, maturity,
                       notes) -> ExportEntry:
        registry_spec = format_spec(f'mesh.{entry_id}.export')
        available = bool(self._utilities.get(utility_name))
        return ExportEntry(
            entry_id=entry_id, label=registry_spec.display_name or label,
            maturity=registry_spec.maturity.value or maturity, available=available,
            reason='' if available else
            f'{utility_name} was not found in the configured OpenFOAM environment',
            destination=destination,
            suffix=registry_spec.extensions[0] if registry_spec.extensions else '',
            notes=registry_spec.notes or notes)

    def export_native_case(self, source: str | Path, destination: str | Path) -> NativeExportResult:
        source_path = Path(source).resolve()
        destination_path = Path(destination).resolve()
        if source_path == destination_path or destination_path.is_relative_to(source_path):
            raise ValueError('export destination must not be the source or a child of it')
        if destination_path.exists():
            raise FileExistsError(f'export destination already exists: {destination_path}')
        # A mesh that was written halfway -- a run killed between `owner` and
        # `neighbour`, a copy that ran out of disk -- used to export without a
        # word, and the reader met it as an unreadable case in whatever solver
        # they took it to.  DP-266 named it here, in one of the two callers;
        # the verdict itself now names it, so this route and the native writer
        # and everything else that reads an ``ExportReport`` refuse on the one
        # sentence.  The explicit check that stood here is gone rather than
        # left unreachable behind ``validate_case``.
        report = validate_case(source_path)
        if not report.ok:
            raise ValueError('; '.join(report.errors))
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        staging = destination_path.with_name(f'.{destination_path.name}.foammesh-export-{uuid.uuid4().hex}')
        try:
            staging.mkdir()
            for name in ('0', 'constant', 'system'):
                source_item = source_path / name
                if source_item.is_dir():
                    shutil.copytree(source_item, staging / name, copy_function=shutil.copy2)
            foam_marker = source_path / 'case.foam'
            if foam_marker.is_file():
                shutil.copy2(foam_marker, staging / foam_marker.name)
            os.replace(staging, destination_path)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        record_export_event(
            source_path, entry_id=ExportFormat.OPENFOAM.value,
            destination=destination_path, total_bytes=path_bytes(destination_path),
            warnings=report.warnings)
        return NativeExportResult(source_path, destination_path, tuple(report.warnings))

    def export_vtu(self, case_path: str | Path, destination: str | Path) -> ExportOutcome:
        case = Path(case_path).resolve()
        target = self._prepare_file_destination(destination, '.vtu')
        dataset = load_case_dataset(case)
        expected = (dataset.GetNumberOfPoints(), dataset.GetNumberOfCells())
        with self._staged(target) as staging:
            write_vtu(dataset, staging)
            actual = read_vtu_counts(staging)
            if actual != expected:
                raise ValueError(
                    f'VTU validation failed: wrote {expected} points/cells '
                    f'but read back {actual}')
        outcome = ExportOutcome(ExportFormat.VTK.value, case, target, path_bytes(target))
        record_export_event(case, entry_id=outcome.entry_id, destination=target,
                            total_bytes=outcome.total_bytes,
                            details={'points': actual[0], 'cells': actual[1]})
        return outcome

    @staticmethod
    def _identity_problems(census, *, nodes=None, cells=None, names=(),
                           wrote='the mesh holds',
                           recorded='no group names were recorded for this mesh'
                           ) -> tuple[list, list]:
        """Hold a converted file against the identity it is supposed to have.

        Plan 31 DP-23 split this out of :meth:`_conversion_identity_problems`.
        Two routes now convert a mesh and have to prove the result: the MED,
        UNV and CGNS exports, whose expectation is what the accepted Gmsh run
        recorded, and the Gmsh export, whose expectation is the interchange it
        just wrote from the published polyMesh. The comparison is the same
        one; only where the expectation comes from differs.
        """
        problems: list = []
        warnings: list = []
        expected_names = [str(name).strip() for name in names if str(name).strip()]

        for expected, key, label in ((nodes, 'nodes', 'nodes'),
                                     (cells, 'cells', 'cells')):
            if isinstance(expected, int) and expected > 0 and census.get(key) != expected:
                problems.append(
                    f'{wrote} {expected} {label} and the converted file '
                    f'holds {census.get(key)}')

        if expected_names:
            found = set(census.get('groups') or ())
            lost = sorted(name for name in expected_names if name not in found)
            if lost:
                problems.append(
                    'these names did not survive the conversion: '
                    + ', '.join(lost))
        elif census.get('groups'):
            warnings.append(
                f'{recorded}, so the '
                f'{len(census["groups"])} names in the exported file could '
                'not be held against it')
        else:
            problems.append(
                'the converted file carries no physical group names, so it '
                'has no boundary identity to give the solver that reads it')
        return problems, warnings

    def _conversion_identity_problems(self, census, artifact) -> tuple[list, list]:
        """Hold a converted file against the identity the run recorded.

        The same question :func:`_su2_identity_problems` asks of the SU2 copy,
        put to a file this application converted rather than copied. The run
        recorded what it meshed -- ``statistics.mesh`` -- and what its own
        ``.msh`` read back as, group names included, in
        ``statistics.outputs.msh.identity``. A conversion that dropped a patch
        name is refused here rather than handed over as a mesh whose
        boundaries the receiving solver cannot tell apart.

        A run that recorded nothing to compare against is not a fault: there
        is simply nothing to check, and that is reported as a warning.
        """
        statistics = dict((artifact.get('run_manifest') or {}).get('statistics') or {})
        mesh = dict(statistics.get('mesh') or {})
        identity = dict(
            (dict(statistics.get('outputs') or {}).get('msh') or {}).get('identity') or {})
        expected_names = [
            str(name).strip()
            for name in (dict(identity.get('expected') or {}).get('groups')
                         or identity.get('groups') or ())
            if str(name).strip()]
        return self._identity_problems(
            census, nodes=mesh.get('nodes'), cells=mesh.get('cells'),
            names=expected_names, wrote='the run wrote',
            recorded='the run recorded no group names for its mesh')

    def _export_by_gmsh_conversion(self, case_path, destination, *,
                                   entry_id: str) -> ExportOutcome:
        """Convert the accepted run's ``mesh.msh`` and prove what came out.

        Plan 31 FC-F. The writer is FC-A's; what is added here is the two
        things an export owes the person receiving the file -- that its source
        is the mesh the user accepted (hash and size, not existence), and that
        the file which landed on disk still carries the counts and the patch
        names that mesh had. Both are checked on the artifact, never on the
        call having returned without raising.
        """
        registry_id, suffix = GMSH_CONVERSION_FORMATS[entry_id]
        case = Path(case_path).resolve()
        artifact, reason = self.gmsh_conversion_source(case)
        if artifact is None:
            raise ValueError(f'{entry_id} export is not available: {reason}')
        source = Path(artifact.get('path') or '')
        target = self._prepare_file_destination(destination, suffix)
        with self._staged(target) as staging:
            try:
                census = _gmsh.convert_and_census(source, staging)
            except RuntimeError as error:
                # The caller's contract is OSError/ValueError; a runtime that
                # would not start is a refusal with a reason, not a traceback.
                # EXPORT-01: the word the converter classified this failure
                # with travels with the refusal, so the reader is told the
                # conversion was interrupted rather than that the mesh is bad.
                raise _classified(error) from error
            problems, warnings = self._conversion_identity_problems(census, artifact)
            if problems:
                raise ValueError(
                    f'{entry_id} validation failed: ' + '; '.join(problems))
        outcome = ExportOutcome(entry_id, source, target, path_bytes(target),
                                tuple(warnings))
        # DP-244. The same four layout details DP-229 gave the authored
        # OpenFOAM export and the SU2 copy now files, through the same helper.
        # This is every MED and UNV export and the CGNS exports that take the
        # Gmsh route, so leaving it out made the history answer for the layout
        # on some formats and not others, for no reason a reader can see.
        from foammesh.core.gmsh.manifest import recorded_mesh_layout
        from foammesh.core.import_export.authored import export_layout_details
        layout = recorded_mesh_layout(artifact.get('run_manifest') or {}) or {}
        record_export_event(
            case, entry_id=entry_id, destination=target,
            total_bytes=outcome.total_bytes, warnings=outcome.warnings,
            details={'points': census.get('nodes'), 'cells': census.get('cells'),
                     'groups': list(census.get('groups') or ()),
                     'format': registry_id, 'source': 'native',
                     'run_id': artifact.get('run_id', ''),
                     **export_layout_details(
                         'accepted-run', artifact.get('run_id', ''),
                         layout.get('cores') or 1, layout.get('decomposed'))})
        return outcome

    def export_med(self, case_path: str | Path, destination: str | Path) -> ExportOutcome:
        """MED for SALOME and Code_Aster, converted from the accepted mesh."""
        return self._export_by_gmsh_conversion(case_path, destination, entry_id='med')

    def export_unv(self, case_path: str | Path, destination: str | Path) -> ExportOutcome:
        """I-DEAS Universal, converted from the accepted mesh."""
        return self._export_by_gmsh_conversion(case_path, destination, entry_id='unv')

    def export_cgns(self, case_path: str | Path, destination: str | Path) -> ExportOutcome:
        """CGNS, through whichever of the two writers this host actually has.

        Plan 31 FC-F. The VTK route is the original one and stays the
        fallback; it needs a VTK build carrying ``vtkCGNSWriter``, which
        MEASURED on this host is absent -- ``vtkmodules.vtkIOCGNSWriter`` does
        not resolve, so every CGNS export here failed its readiness check and
        the row sat disabled in the export dialog. The Gmsh writer needs no
        such module and keeps the group names, so when the case has an
        accepted Gmsh run to convert, that is the route taken.
        """
        artifact, _reason = self.gmsh_conversion_source(case_path)
        if artifact is not None:
            return self._export_by_gmsh_conversion(
                case_path, destination, entry_id='cgns')
        case = Path(case_path).resolve()
        report = readiness(ExportFormat.CGNS)
        if not report.ok:
            raise ValueError('; '.join(report.errors))
        target = self._prepare_file_destination(destination, '.cgns')
        dataset = load_case_dataset(case)
        with self._staged(target) as staging:
            _cgns.write_cgns(dataset, staging)
            if path_bytes(staging) == 0:
                raise ValueError('CGNS writer produced an empty file')
        warnings = tuple(report.warnings)
        outcome = ExportOutcome(ExportFormat.CGNS.value, case, target, path_bytes(target), warnings)
        # DP-264 left this standing.  The three other export writers -- the
        # authored OpenFOAM step, the native SU2 copy and the Gmsh conversion
        # -- file the four layout details through
        # :func:`export_layout_details`; this one filed no ``details`` at all,
        # so the one page reading the one history answered for the layout on a
        # MED, a UNV or an SU2 export and answered "not recorded" on a CGNS
        # export written from the same case.
        #
        # What this route reproduces is the case's own ``constant/polyMesh``,
        # read through the VTK OpenFOAM reader.  That mesh is one piece by the
        # time it is read, whatever the run that wrote it did, so the layout
        # filed is one core and undecomposed, and the source is named with the
        # vocabulary ``_exportLayout`` already uses for pieces taken off disk.
        # The run named is the newest one the case kept, so a reader can still
        # trace the file back; a case adopted from outside FoamMesh kept none
        # and names none, which is a fact rather than a gap.
        from foammesh.core.import_export.authored import export_layout_details
        record_export_event(
            case, entry_id=outcome.entry_id, destination=target,
            total_bytes=outcome.total_bytes, warnings=warnings,
            details={'points': _dataset_count(dataset, 'GetNumberOfPoints'),
                     'cells': _dataset_count(dataset, 'GetNumberOfCells'),
                     'format': 'mesh.cgns.export',
                     **export_layout_details(
                         'mesh-on-disk', _kept_run_id(case), 1, False)})
        return outcome

    def _copy_native_su2(self, case: Path, artifact: dict,
                         destination) -> ExportOutcome:
        """Hand over the ``mesh.su2`` the accepted run wrote, checked twice.

        Plan 31 CP-05 item 5. Two things had to become true here. The copy is
        made only when the file still *is* the artifact the run recorded --
        hash and size, not existence -- and the copy is then read back and
        held against the boundary identity and counts the run recorded, rather
        than against "the file is not empty". A truncated copy has volume
        elements and a nonzero size; what it does not have is the run's
        marker set.
        """
        from foammesh.core.gmsh.manifest import (
            artifact_provenance_error, recorded_mesh_layout)
        from foammesh.core.import_export.authored import export_layout_details
        from foammesh.core.mesh.census import su2_element_census

        source = Path(artifact.get('path') or '')
        problem = artifact_provenance_error(artifact)
        if problem:
            raise ValueError(
                f'the accepted Gmsh run\'s SU2 file cannot be exported: '
                f'{problem}')
        target = self._prepare_file_destination(destination, '.su2')
        with self._staged(target) as staging:
            shutil.copy2(source, staging)
            census = su2_element_census(staging)
            if not census.has_volume_elements:
                raise ValueError(
                    'SU2 validation failed: '
                    + (census.read_error
                       or f'{source.name} holds no volume elements'))
            problems, warnings = _su2_identity_problems(
                census, artifact.get('run_manifest') or {})
            if problems:
                raise ValueError('SU2 validation failed: ' + '; '.join(problems))
        outcome = ExportOutcome(ExportFormat.SU2.value, source, target,
                                path_bytes(target),
                                tuple(census.warnings) + tuple(warnings))
        # DP-244. The layout details DP-229 gave the authored OpenFOAM export,
        # filed here too and through the same helper. This writer recorded the
        # run under its own spelling and nothing else, so an SU2 export reached
        # the history with three fewer facts than an OpenFOAM one and the page
        # that reads them had to know two vocabularies. The layout is the one
        # the accepted run recorded; a run that recorded none meshed in one
        # process, which is every Gmsh run.
        layout = recorded_mesh_layout(artifact.get('run_manifest') or {}) or {}
        record_export_event(
            case, entry_id=outcome.entry_id, destination=target,
            total_bytes=outcome.total_bytes, warnings=outcome.warnings,
            details={'points': census.point_count,
                     'elements': census.volume_count,
                     'markers': list(census.markers),
                     'source': 'native',
                     'run_id': artifact.get('run_id', ''),
                     **export_layout_details(
                         'accepted-run', artifact.get('run_id', ''),
                         layout.get('cores') or 1, layout.get('decomposed'))})
        return outcome

    def export_su2(self, case_path: str | Path, destination: str | Path) -> ExportOutcome:
        """Export the case as an SU2 native mesh, boundary markers included.

        Plan 30 WP-07 (F-08). When a Gmsh run wrote ``mesh.su2``, that file is
        the export: it is the mesh the run produced, it is the only form a
        second-order mesh has, and re-deriving one from the published polyMesh
        would hand the user a different mesh under the same name. The VTK
        writer in ``su2_export`` stays as the fallback, which is the only route
        a snappyHexMesh case has.
        """
        from foammesh.core.export.su2_export import (
            Su2ExportError, validate_against, write_su2_from_case,
        )

        case = Path(case_path).resolve()
        native = self.native_su2_artifact(case)
        if native is not None:
            return self._copy_native_su2(case, native, destination)
        target = self._prepare_file_destination(destination, '.su2')
        with self._staged(target) as staging:
            try:
                report = write_su2_from_case(case, staging)
            except Su2ExportError as error:
                raise ValueError(str(error)) from error
            # Read the file back independently; the exporter's own count proves
            # nothing about what reached the disk.
            ok, problems = validate_against(
                staging, points=report.points, cells=report.elements,
                boundary_faces=report.marker_elements)
            if not ok:
                raise ValueError('SU2 validation failed: ' + '; '.join(problems))
        outcome = ExportOutcome(ExportFormat.SU2.value, case, target,
                                path_bytes(target))
        record_export_event(case, entry_id=outcome.entry_id, destination=target,
                            total_bytes=outcome.total_bytes,
                            details={'points': report.points,
                                     'elements': report.elements,
                                     'markers': list(report.markers)})
        return outcome

    def export_gmsh(self, case_path: str | Path, destination: str | Path) -> ExportOutcome:
        """Write the published polyMesh as ``.msh``, patch names included.

        Plan 31 DP-23. This route used to convert the case through a legacy
        ASCII ``.vtk`` intermediate, which has nowhere to put a boundary name.
        MEASURED 2026-09-06 on ``tests/fixtures/cases/single_hex``, whose
        ``polyMesh/boundary`` names one patch ``walls``: the file that landed
        read back as ``{"nodes": 8, "cells": 1, "groups": []}`` and was
        reported as a success with a byte count. The intermediate is now MSH
        2.2 with ``$PhysicalNames`` (see
        :mod:`foammesh.core.export.msh_interchange`), and the same fixture
        comes back ``{"nodes": 8, "cells": 1, "groups": ["walls"]}``.

        The byte count is no longer what the export is checked by. The file is
        reopened in a fresh Gmsh session and held against the counts and the
        patch names that went in -- the guard the MED and UNV exports already
        run, given the expectation a case with no Gmsh run can still supply.
        """
        case = Path(case_path).resolve()
        report = readiness(ExportFormat.GMSH)
        if not report.ok:
            raise ValueError('; '.join(report.errors))
        target = self._prepare_file_destination(destination, '.msh')
        artifact, _reason = self.gmsh_conversion_source(case)
        if artifact is not None:
            return self._copy_native_msh(case, artifact, target, report)
        interior, patches = load_case_blocks(case)
        with self._staged(target) as staged_target:
            with tempfile.TemporaryDirectory(prefix='foammesh-gmsh-') as work:
                written = write_msh_interchange(
                    interior, patches, Path(work) / 'interchange.msh')
                try:
                    census = _gmsh.convert_and_census(
                        written.path, staged_target, save_all=True)
                except RuntimeError as error:
                    # The caller's contract is OSError/ValueError; a runtime
                    # that would not start is a refusal with a reason.
                    raise _classified(error) from error
            if path_bytes(staged_target) == 0:
                raise ValueError('Gmsh writer produced an empty file')
            problems, identity_warnings = self._identity_problems(
                census, nodes=written.nodes, cells=written.cells,
                names=written.groups, wrote='the published mesh holds',
                recorded='the published mesh names no patches')
            if problems:
                raise ValueError('gmsh validation failed: ' + '; '.join(problems))
        warnings = tuple(report.warnings) + tuple(identity_warnings)
        outcome = ExportOutcome(ExportFormat.GMSH.value, case, target, path_bytes(target), warnings)
        record_export_event(case, entry_id=outcome.entry_id, destination=target,
                            total_bytes=outcome.total_bytes, warnings=warnings,
                            details={'points': census.get('nodes'),
                                     'cells': census.get('cells'),
                                     'groups': list(census.get('groups') or ()),
                                     'boundary_faces': written.boundary_faces})
        return outcome

    def _copy_native_msh(self, case: Path, artifact: dict, target: Path,
                         report) -> ExportOutcome:
        """Hand over the accepted run's own ``mesh.msh`` rather than rebuild it.

        EXPORT-01. The run wrote this file, read it back in a fresh session
        and recorded its hash and its byte count;
        :meth:`gmsh_conversion_source` has just held the file on disk against
        both, so the mesh being asked for is the mesh that is already there.
        Rebuilding it -- polyMesh to interchange to a Gmsh process and back --
        is a second chance to fail at something a copy cannot get wrong, and
        the measured export that died at exit 3221225786 died in exactly that
        process. What is filed says ``reused`` so that a reader can tell a
        copy from a conversion without measuring the file.

        The same shape :meth:`_copy_native_su2` files for the SU2 mesh, for
        the same reason: one history, one vocabulary.
        """
        source = Path(artifact.get('path') or '')
        with self._staged(target) as staging:
            shutil.copy2(source, staging)
            if path_bytes(staging) == 0:
                raise ValueError(
                    f'the mesh of the accepted Gmsh run is empty: {source}')
        statistics = dict(
            (artifact.get('run_manifest') or {}).get('statistics') or {})
        mesh = dict(statistics.get('mesh') or {})
        identity = dict((dict(statistics.get('outputs') or {}).get('msh')
                         or {}).get('identity') or {})
        groups = [str(name).strip()
                  for name in (dict(identity.get('expected') or {}).get('groups')
                               or identity.get('groups') or ())
                  if str(name).strip()]
        outcome = ExportOutcome(ExportFormat.GMSH.value, source, target,
                                path_bytes(target), tuple(report.warnings))
        from foammesh.core.gmsh.manifest import recorded_mesh_layout
        from foammesh.core.import_export.authored import export_layout_details
        layout = recorded_mesh_layout(artifact.get('run_manifest') or {}) or {}
        record_export_event(
            case, entry_id=outcome.entry_id, destination=target,
            total_bytes=outcome.total_bytes, warnings=outcome.warnings,
            details={'points': mesh.get('nodes'), 'cells': mesh.get('cells'),
                     'groups': groups, 'source': 'reused',
                     'run_id': artifact.get('run_id', ''),
                     **export_layout_details(
                         'accepted-run', artifact.get('run_id', ''),
                         layout.get('cores') or 1, layout.get('decomposed'))})
        return outcome

    @staticmethod
    @contextmanager
    def _staged(target: Path):
        """Write beside the destination; move it into place only when good.

        Plan 31 CP-05 item 7 -- an export has to be atomic from where the user
        stands. MEASURED before this: every writer wrote straight into the
        path the user named and unlinked it again if validation failed, so a
        rejected or interrupted export left a half-written mesh sitting under
        the name the user chose, looking like the export that never happened.
        The staging file keeps the user's own suffix, because the Gmsh and
        CGNS writers decide their format from it.
        """
        staging = target.with_name(
            f'.{target.stem}.foammesh-export-{uuid.uuid4().hex}{target.suffix}')
        try:
            yield staging
            os.replace(staging, target)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise

    @staticmethod
    def _prepare_file_destination(destination: str | Path, suffix: str) -> Path:
        target = Path(destination).resolve()
        if target.suffix.lower() != suffix:
            raise ValueError(f'export destination must end in {suffix}')
        if target.exists():
            raise FileExistsError(f'export destination already exists: {target}')
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
