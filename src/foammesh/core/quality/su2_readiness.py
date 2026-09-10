"""Is this mesh ready for SU2, judged without asking OpenFOAM.

Plan 28 WP4. Quality assurance in this application meant one thing: run
OpenFOAM's ``checkMesh``. That is the right question for an OpenFOAM case and
the wrong one for an SU2 case, in two separate ways.

It asks too little. ``checkMesh`` is content with polyhedral cells and polygon
faces, because OpenFOAM is a polyhedral code. A mesh it calls "OK" can be one
SU2's reader rejects outright, and the user found that out at export time, or
later, from SU2 itself.

It also asks too much of the machine. A user meshing with Gmsh for SU2 needs no
OpenFOAM installation at all, and on a machine without one the QA row never
produced a verdict -- the one row that decides whether the mesh is usable sat
permanently blank.

So this module answers the SU2 question from the mesh itself. Everything here
is arithmetic over ``constant/polyMesh``:

* the cell census (:mod:`foammesh.core.mesh.census`), which is what decides
  readability -- SU2 reads tetrahedra, hexahedra, prisms and pyramids only;
* the boundary markers, because SU2 applies conditions per marker and a mesh
  with one marker imports and cannot be set up;
* cell volumes, face non-orthogonality and face skewness, computed the way
  OpenFOAM computes them, so a user who has read a ``checkMesh`` report
  recognises the numbers.

The verdict is persisted through the same ``persist_result`` path the
``checkMesh`` report uses, in the same schema, so the verdict strip, the
Quality tab and the run summary read it with no special case -- but into this
check's own slot. Plan 31 CP-05 item 2: it shared ``checkMesh``'s single
report file, and an SU2 readiness check therefore destroyed the OpenFOAM
verdict on a mesh nobody had changed.

Plan 31 DP-19 corrects the premise of the paragraph above it. "Everything here
is arithmetic over ``constant/polyMesh``" was written when a Gmsh user meshing
for SU2 still went through a published polyMesh. Plan 28 WP7 and Plan 31 CP-01
then made that route *native*: on an SU2 target Gmsh writes ``mesh.su2`` and
the pipeline publishes no polyMesh at all, deliberately. So the one check that
exists for SU2 could not run on an SU2 project -- MEASURED as ``severity
incomplete``, ``problems ['no polyMesh directory at .../constant/polyMesh']``,
and nothing filed, because ``_persist`` fingerprinted a polyMesh that was not
there and swallowed the failure.

A case with a ``constant/polyMesh`` is still judged exactly as before, to the
byte: that is the export-to-SU2 route and it works. A case without one is
judged from the artifact the accepted run actually wrote, found the way the
exporter finds it (:meth:`ImportExportService.native_su2_artifact`, which asks
which artifact was *accepted* rather than which is newest), and counted by
:func:`foammesh.core.mesh.census.element_census`, which reads the SU2 file.

What that costs is stated rather than hidden. The element list in a ``.su2``
carries no face connectivity, and this module's non-orthogonality, skewness
and cell-volume arithmetic is built on the owner/neighbour face addressing a
polyMesh has and this file does not. Those metrics are therefore *absent* from
a native verdict, and the verdict line says so. A number invented for a mesh
nobody measured would be worse than the defect this closes.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from foammesh.core.mesh.census import cell_census, element_census
from foammesh.core.mesh.poly_mesh_boundary import (
    PolyMeshReadError, face_area_vectors, read_poly_mesh,
)

from .checkmesh_parser import CheckMeshResult
from .checkmesh_service import SU2_READINESS_CHECK, MeshCheckService
from .policy import QualityPolicy

#: Degrees. OpenFOAM's own ``checkMesh`` calls a face severely non-orthogonal
#: past 70; SU2's gradient reconstruction has the same difficulty for the same
#: geometric reason, so the threshold carries over. Plan 30 F-25: this used to
#: be a second literal copy of the OpenFOAM acceptance limits with nothing
#: tying it to them, so a change to one would not have reached the other.
_SU2_POLICY = QualityPolicy.for_solver('su2')
NON_ORTHO_LIMIT = _SU2_POLICY.acceptance.max_non_ortho
#: Dimensionless, as ``checkMesh`` reports it.
SKEWNESS_LIMIT = _SU2_POLICY.acceptance.max_skewness

_COMMAND = ('su2-readiness',)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _face_centres_and_areas(mesh):
    """Face centroids and area vectors, as OpenFOAM computes them.

    The centroid of a polygon is not the average of its vertices once it has
    more than three, and using the average would flatter exactly the warped
    faces this check exists to find.
    """
    offsets, flat, points = mesh.face_offsets, mesh.face_vertices, mesh.points
    count = mesh.face_count
    centres = np.zeros((count, 3), dtype=np.float64)
    for face_id in range(count):
        loop = points[flat[offsets[face_id]:offsets[face_id + 1]]]
        if loop.shape[0] == 3:
            centres[face_id] = loop.mean(axis=0)
            continue
        apex = loop.mean(axis=0)
        following = np.roll(loop, -1, axis=0)
        # Sub-triangle areas about the fan apex, weighting each sub-centroid.
        vectors = 0.5 * np.cross(loop - apex, following - apex)
        magnitudes = np.linalg.norm(vectors, axis=1)
        total = magnitudes.sum()
        if total <= 0.0:
            centres[face_id] = apex
            continue
        sub_centres = (loop + following + apex) / 3.0
        centres[face_id] = (
            (sub_centres * magnitudes[:, None]).sum(axis=0) / total)
    return centres, face_area_vectors(mesh, np.arange(count, dtype=np.int64))


def _cell_geometry(mesh, centres, areas):
    """Cell centroids and volumes by pyramid decomposition, as OpenFOAM does."""
    cells = mesh.cell_count
    estimate = np.zeros((cells, 3), dtype=np.float64)
    face_tally = np.zeros(cells, dtype=np.int64)
    for owners in (mesh.owner, mesh.neighbour):
        if owners.size == 0:
            continue
        np.add.at(estimate, owners, centres[:owners.size])
        np.add.at(face_tally, owners, 1)
    estimate /= np.maximum(face_tally, 1)[:, None]

    volumes = np.zeros(cells, dtype=np.float64)
    moments = np.zeros((cells, 3), dtype=np.float64)
    for owners, sign in ((mesh.owner, 1.0), (mesh.neighbour, -1.0)):
        if owners.size == 0:
            continue
        span = owners.size
        arms = centres[:span] - estimate[owners]
        pyramid = sign * (areas[:span] * arms).sum(axis=1) / 3.0
        apexes = 0.75 * centres[:span] + 0.25 * estimate[owners]
        np.add.at(volumes, owners, pyramid)
        np.add.at(moments, owners, pyramid[:, None] * apexes)
    safe = np.where(np.abs(volumes) > 0.0, volumes, 1.0)
    cell_centres = moments / safe[:, None]
    degenerate = np.abs(volumes) <= 0.0
    cell_centres[degenerate] = estimate[degenerate]
    return cell_centres, volumes


def _internal_face_metrics(mesh, centres, areas, cell_centres):
    """Non-orthogonality in degrees and skewness, per internal face."""
    span = mesh.neighbour.size
    if span == 0:
        return np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64)
    owner_centres = cell_centres[mesh.owner[:span]]
    neighbour_centres = cell_centres[mesh.neighbour]
    delta = neighbour_centres - owner_centres
    delta_length = np.linalg.norm(delta, axis=1)
    normals = areas[:span]
    normal_length = np.linalg.norm(normals, axis=1)
    usable = (delta_length > 0.0) & (normal_length > 0.0)

    cosine = np.ones(span, dtype=np.float64)
    cosine[usable] = (
        (delta[usable] * normals[usable]).sum(axis=1)
        / (delta_length[usable] * normal_length[usable]))
    non_ortho = np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))

    # Skewness: how far from the face's own centroid the owner-neighbour line
    # crosses it, as a fraction of the cell-to-cell distance.
    skewness = np.zeros(span, dtype=np.float64)
    projection = np.zeros(span, dtype=np.float64)
    projection[usable] = (delta[usable] * normals[usable]).sum(axis=1)
    crossing = np.abs(projection) > 0.0
    if crossing.any():
        face_centres = centres[:span][crossing]
        offset = face_centres - owner_centres[crossing]
        fraction = ((offset * normals[crossing]).sum(axis=1)
                    / projection[crossing])
        intersection = (owner_centres[crossing]
                        + fraction[:, None] * delta[crossing])
        skewness[crossing] = (
            np.linalg.norm(face_centres - intersection, axis=1)
            / delta_length[crossing])
    return non_ortho, skewness


def check_su2_readiness(case_path, *, persist: bool = True,
                        layout_expectation: str = 'reconstructed') -> dict:
    """Judge the mesh at ``case_path`` for SU2, and return the verdict.

    The returned mapping carries the same keys every quality consumer in this
    application already reads -- ``mesh_ok``, ``severity``, ``incomplete`` and
    the findings -- so nothing downstream has to know which check ran.
    """
    case = Path(case_path)
    native = _native_mesh(case)
    if native is not None:
        return _check_native(case, native, persist=persist)
    census = cell_census(case, layout_expectation=layout_expectation)

    verdict = {
        'check': 'su2-readiness',
        'case_path': str(case),
        'checked_at': _now(),
        'mesh_ok': False,
        'severity': 'incomplete',
        'incomplete': True,
        'problems': [],
        'warnings': list(census.warnings),
        'counts': {},
        'metrics': {},
        'markers': [],
        'census': census.to_dict(),
        'verdict': '',
        'report_path': '',
    }

    if census.read_error:
        verdict['problems'] = [census.read_error]
        verdict['verdict'] = census.read_error
        if persist:
            verdict['report_path'] = _persist(case, verdict)
        return verdict

    try:
        mesh = read_poly_mesh(case, layout_expectation=layout_expectation)
    except PolyMeshReadError as error:                       # pragma: no cover
        verdict['problems'] = [str(error)]
        verdict['verdict'] = str(error)
        if persist:
            verdict['report_path'] = _persist(case, verdict)
        return verdict

    centres, areas = _face_centres_and_areas(mesh)
    cell_centres, volumes = _cell_geometry(mesh, centres, areas)
    non_ortho, skewness = _internal_face_metrics(
        mesh, centres, areas, cell_centres)

    problems = []
    warnings = list(census.warnings)

    if not census.su2_readable:
        problems.append(census.reason)

    negative = int((volumes <= 0.0).sum())
    if negative:
        problems.append(
            '{0} of {1} cells {2} zero or negative volume, which no solver '
            'can integrate'.format(
                negative, mesh.cell_count,
                'has' if negative == 1 else 'have'))

    if census.marker_count == 0:
        problems.append(
            'the mesh carries no boundary markers, so SU2 has nothing to '
            'apply boundary conditions to')

    worst_non_ortho = float(non_ortho.max()) if non_ortho.size else 0.0
    worst_skewness = float(skewness.max()) if skewness.size else 0.0
    if worst_non_ortho > NON_ORTHO_LIMIT:
        severe = int((non_ortho > NON_ORTHO_LIMIT).sum())
        warnings.append(
            '{0} internal faces exceed {1:g} degrees of non-orthogonality, '
            'worst {2:.1f}; expect slower convergence, and consider more '
            'mesh normal to the boundary'.format(
                severe, NON_ORTHO_LIMIT, worst_non_ortho))
    if worst_skewness > SKEWNESS_LIMIT:
        severe = int((skewness > SKEWNESS_LIMIT).sum())
        warnings.append(
            '{0} internal faces are skewed beyond {1:g}, worst {2:.2f}'.format(
                severe, SKEWNESS_LIMIT, worst_skewness))

    verdict['counts'] = {
        'points': int(mesh.points.shape[0]),
        'faces': int(mesh.face_count),
        'internal_faces': int(mesh.neighbour.size),
        'cells': int(mesh.cell_count),
        'markers': int(census.marker_count),
        'polygon_faces': int(census.polygon_faces),
        'polyhedral_cells': int(census.polyhedral_cells),
        'cells_by_family': dict(census.cells_by_family),
        'negative_volume_cells': negative,
    }
    verdict['metrics'] = {
        'max_non_ortho': worst_non_ortho,
        'avg_non_ortho': float(non_ortho.mean()) if non_ortho.size else 0.0,
        'max_skewness': worst_skewness,
        'min_cell_volume': float(volumes.min()) if volumes.size else 0.0,
        'max_cell_volume': float(volumes.max()) if volumes.size else 0.0,
        'total_volume': float(volumes.sum()) if volumes.size else 0.0,
    }
    verdict['markers'] = [
        {'name': patch.name, 'type': patch.patch_type, 'faces': patch.n_faces}
        for patch in mesh.patches]
    verdict['problems'] = problems
    verdict['warnings'] = warnings
    verdict['mesh_ok'] = not problems
    verdict['incomplete'] = False
    verdict['severity'] = ('fail' if problems
                           else 'warning' if warnings else 'pass')
    verdict['verdict'] = _one_line(verdict)
    if persist:
        verdict['report_path'] = _persist(case, verdict)
    return verdict


#: Why a native verdict carries no geometry numbers. Written out rather than
#: left to an empty ``metrics`` map, because a metric that is missing and a
#: metric that is fine look identical to a reader who is only told the check
#: passed (DP-19).
NOT_MEASURED = (
    'non-orthogonality, skewness and cell volumes were not measured: this '
    'check computes them from the face addressing in constant/polyMesh, and '
    'an SU2 run publishes none')

#: The metric names a polyMesh verdict carries and a native one cannot.
UNMEASURED_METRICS = ('max_non_ortho', 'avg_non_ortho', 'max_skewness',
                      'min_cell_volume', 'max_cell_volume', 'total_volume')


def native_mesh_path(case_path) -> Path | None:
    """The ``mesh.su2`` this case is qualified *by*, when it has no polyMesh.

    ``None`` when the case has a polyMesh -- which is the mesh the check has
    always read, and reads unchanged -- or when no accepted run recorded a
    native SU2 artifact, which is a case with no mesh at all rather than a
    case to judge from a file nobody accepted.
    """
    case = Path(case_path)
    poly_mesh = case if case.name == 'polyMesh' else case / 'constant' / 'polyMesh'
    if poly_mesh.is_dir():
        return None
    return _native_mesh(case)


def _native_mesh(case: Path) -> Path | None:
    poly_mesh = case if case.name == 'polyMesh' else case / 'constant' / 'polyMesh'
    if poly_mesh.is_dir():
        return None
    # Asked the way the exporter asks it. CP-05 item 5 measured what globbing
    # for the newest .su2 costs: with an accepted run A followed by a
    # candidate B the gate refused, the export handed the user B's mesh under
    # A's name. A verdict built on the file nobody accepted would be the same
    # mistake with a quality stamp on it.
    from foammesh.core.import_export.service import ImportExportService

    artifact = ImportExportService.native_su2_artifact(case)
    if not artifact:
        return None
    path = Path(str(artifact.get('path') or ''))
    return path if path.is_file() else None


def _check_native(case: Path, mesh_file: Path, *, persist: bool) -> dict:
    """Judge the native SU2 artifact, in the shape every consumer reads.

    Same keys as the polyMesh verdict, and the same rules where the file can
    answer them: which element families are present, whether SU2 reads them,
    and how many markers there are -- SU2 applies boundary conditions per
    marker, so a mesh with fewer than two imports and cannot be set up. Where
    the file cannot answer, the key is left out and :data:`NOT_MEASURED` says
    which question went unanswered.
    """
    census = element_census(mesh_file)
    markers = [str(name) for name in census.markers]
    verdict = {
        'check': 'su2-readiness',
        'case_path': str(case),
        'mesh_path': str(mesh_file),
        'mesh_source': 'native',
        'checked_at': _now(),
        'mesh_ok': False,
        'severity': 'incomplete',
        'incomplete': True,
        'problems': [],
        'warnings': list(census.warnings),
        'counts': {},
        'metrics': {},
        'markers': [],
        'census': census.to_dict(),
        'not_measured': list(UNMEASURED_METRICS),
        'verdict': '',
        'report_path': '',
    }

    if census.read_error:
        verdict['problems'] = [census.read_error]
        verdict['verdict'] = census.read_error
        if persist:
            verdict['report_path'] = _persist(case, verdict,
                                              mesh_source=mesh_file)
        return verdict

    problems = []
    warnings = list(census.warnings)
    if not census.has_volume_elements:
        problems.append(
            '{0} holds no volume elements, so there is nothing for SU2 to '
            'solve in'.format(mesh_file.name))
    if not markers:
        problems.append(
            'the mesh carries no boundary markers, so SU2 has nothing to '
            'apply boundary conditions to')
    elif len(markers) < 2:
        # The same rule the polyMesh census applies, in its own words.
        warnings.append(
            'the mesh has only one boundary marker, so every boundary would '
            'take the same SU2 condition; split it before setting up the case')

    verdict['counts'] = {
        'points': int(census.point_count),
        'cells': int(census.volume_count),
        'markers': len(markers),
        'boundary_elements': int(census.surface_count),
        'cells_by_family': {name: int(count) for name, count
                            in census.volume_by_family.items()},
        'element_order': int(census.element_order),
    }
    # Deliberately empty. Every consumer reads `metrics` with `.get`, and an
    # absent key renders as unrated; a zero would render as a perfect mesh.
    verdict['metrics'] = {}
    verdict['markers'] = [
        # Face counts are per-marker in the file and not attributed by the
        # element census, so the count is left out rather than guessed at.
        {'name': name, 'type': '', 'faces': None} for name in markers]
    verdict['problems'] = problems
    verdict['warnings'] = warnings
    verdict['mesh_ok'] = not problems
    verdict['incomplete'] = False
    verdict['severity'] = ('fail' if problems
                           else 'warning' if warnings else 'pass')
    verdict['verdict'] = _one_line_native(verdict, mesh_file)
    if persist:
        verdict['report_path'] = _persist(case, verdict, mesh_source=mesh_file)
    return verdict


def _one_line_native(verdict: dict, mesh_file: Path) -> str:
    """One sentence for a status bar, ending in what was *not* measured."""
    if verdict['problems']:
        return 'SU2 cannot read this mesh: ' + verdict['problems'][0]
    counts = verdict['counts']
    shapes = ', '.join(
        '{0} {1}{2}'.format(count, name, '' if count == 1 else 's')
        for name, count in sorted(counts.get('cells_by_family', {}).items())
        if count)
    cells = counts.get('cells', 0)
    markers = counts.get('markers', 0)
    head = '{0} element{1} ({2}) in {3}, {4} boundary marker{5}, all element ' \
           'types SU2 reads'.format(
               cells, '' if cells == 1 else 's', shapes, mesh_file.name,
               markers, '' if markers == 1 else 's')
    if verdict['warnings']:
        head = '{0}; {1} advisory findings'.format(head,
                                                   len(verdict['warnings']))
    return '{0}. {1}'.format(head, NOT_MEASURED)


def _one_line(verdict: dict) -> str:
    """One sentence for a status bar: what this mesh is, not what was run."""
    if verdict['problems']:
        return 'SU2 cannot read this mesh: ' + verdict['problems'][0]
    counts = verdict['counts']
    head = '{0} cells, {1} boundary markers, readable by SU2'.format(
        counts.get('cells', 0), counts.get('markers', 0))
    if verdict['warnings']:
        return '{0}; {1} advisory findings'.format(
            head, len(verdict['warnings']))
    return head


def _persist(case: Path, verdict: dict, *, mesh_source=None) -> str:
    """Write the verdict where every quality consumer already looks.

    Best-effort by design: a readiness verdict the user can see is worth more
    than an exception raised because the report could not be filed, and the
    caller holds the verdict either way.

    That design is also how DP-19 stayed invisible. ``persist_result``
    fingerprinted ``constant/polyMesh`` unconditionally, so on the one project
    shape this check exists for it raised, the failure was swallowed here, and
    ``load_report`` returned ``None`` forever with nothing on screen saying
    why. ``mesh_source`` is the file the verdict was reached from when it was
    not a polyMesh; left unset, the fingerprint is the polyMesh digest it has
    always been.
    """
    try:
        log_path = case / 'foammesh' / 'quality' / 'su2_readiness.json'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(verdict, indent=2, sort_keys=True, default=str) + '\n',
            encoding='utf-8')
        counts = verdict['counts']
        metrics = verdict['metrics']
        result = CheckMeshResult(
            points=counts.get('points'), faces=counts.get('faces'),
            internal_faces=counts.get('internal_faces'),
            cells=counts.get('cells'), patches=counts.get('markers'),
            max_non_ortho=metrics.get('max_non_ortho'),
            avg_non_ortho=metrics.get('avg_non_ortho'),
            max_skewness=metrics.get('max_skewness'),
            min_cell_volume=metrics.get('min_cell_volume'),
            max_cell_volume=metrics.get('max_cell_volume'),
            failed_checks=len(verdict['problems']),
            mesh_ok=verdict['mesh_ok'], severity=verdict['severity'],
            incomplete=verdict['incomplete'],
            warnings=list(verdict['warnings']),
            failed_check_details=list(verdict['problems']),
            blocking_findings=list(verdict['problems']),
            advisory_findings=list(verdict['warnings']),
            runnable=verdict['mesh_ok'],
            verdict=verdict.get('verdict', ''))
        # CP-05 item 2. Filed under this check's own name. It used to share
        # `checkMesh`'s single slot, so asking the SU2 question about an
        # unchanged mesh erased the OpenFOAM answer about that same mesh.
        stored, _report = MeshCheckService.persist_result(
            case, result, command=_COMMAND, log_path=log_path,
            check=SU2_READINESS_CHECK, mesh_source=mesh_source)
        return str(stored)
    except Exception as error:                              # pragma: no cover
        verdict.setdefault('warnings', []).append(
            'the readiness report could not be filed: {0}'.format(error))
        return ''
