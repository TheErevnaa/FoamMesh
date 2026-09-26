"""Tolerant parser for Foundation-style ``checkMesh`` logs.

A failed check is not one thing. ``checkMesh -allGeometry -allTopology`` runs
diagnostics the plain tool does not, and those report *quality*, not validity:
a mesh can fail several of them and still run perfectly well.

MEASURED, on the same two meshes, plain against strict:

* Gmsh, ``centrifugal_impeller``: ``Mesh OK.`` -- then 3 cells with a small
  determinant and 4 faces with a small interpolation weight, out of 290,385.
* Snappy, ``torus``: ``Mesh OK.`` -- then 621 concave cells, an artefact of
  hanging nodes at refinement transitions that every cut-cell mesh carries.

Reporting both as "FAIL" says the mesh cannot be run, which is untrue and
hides the cases where it really is. So findings are split: **blocking** ones
mean the solver will not run, and **advisory** ones mean the mesh runs and the
quality is worth looking at.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from foammesh.core.quantities import agreeing, count_text

#: Checks that only ``-allGeometry`` / ``-allTopology`` perform. Failing one
#: means the quality is poor, not that the mesh is unusable -- plain
#: ``checkMesh``, which is what a solver run implies, does not even ask.
ADVISORY_CHECKS = (
    'concave cell',
    'small determinant',
    'small interpolation weight',
    'small volume ratio',
    'twisted face',
    'face tets',
    'underdetermined cell',
    # Plain checkMesh does report these, and they do count towards "Failed N
    # mesh checks" -- but OpenFOAM's own wording is "may impair the quality of
    # the result", not that the case cannot be run. They are graded as quality
    # indicators instead, where the figure is more use than the word.
    'highly skew',
    'severely non-orthogonal',
    'high aspect ratio',
)

#: Findings that genuinely stop a run. Anything not recognised is treated as
#: blocking: an unknown failure is not assumed harmless.
BLOCKING_HINTS = (
    'negative volume',
    'zero volume',
    'open cell',
    'not closed',
    'incorrectly oriented',
    'illegal',
    'unordered face',
    'point not used',
    'edge not used',
    'multiply connected',
)


def classify_finding(detail: str) -> str:
    """``advisory`` when the mesh still runs, ``blocking`` when it does not."""
    text = (detail or '').lower()
    if any(hint in text for hint in ADVISORY_CHECKS):
        return 'advisory'
    return 'blocking'


@dataclass
class CheckMeshResult:
    points: int | None = None
    faces: int | None = None
    internal_faces: int | None = None
    cells: int | None = None
    patches: int | None = None
    max_non_ortho: float | None = None
    avg_non_ortho: float | None = None
    max_skewness: float | None = None
    max_aspect_ratio: float | None = None
    #: Metrics whose *minimum* is the critical end: a single tiny value is the
    #: defect, and the average only says how much healthy mesh surrounds it.
    min_determinant: float | None = None
    avg_determinant: float | None = None
    min_face_weight: float | None = None
    avg_face_weight: float | None = None
    min_volume_ratio: float | None = None
    avg_volume_ratio: float | None = None
    min_cell_volume: float | None = None
    max_cell_volume: float | None = None
    min_face_area: float | None = None
    max_openness: float | None = None
    failed_checks: int | None = None
    mesh_ok: bool | None = None
    severity: str = 'incomplete'
    incomplete: bool = True
    warnings: list[str] = field(default_factory=list)
    failed_check_details: list[str] = field(default_factory=list)
    reported_sets: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    #: Findings that stop a solver run.
    blocking_findings: list[str] = field(default_factory=list)
    #: Findings that describe poor quality on a mesh that still runs.
    advisory_findings: list[str] = field(default_factory=list)
    #: True when nothing blocking was found -- the question a user actually
    #: has, which "failed 2 mesh checks" does not answer.
    runnable: bool | None = None
    #: One line for a status bar or a console: what this mesh is.
    verdict: str = ''

    def to_dict(self) -> dict:
        return {
            'points': self.points, 'faces': self.faces,
            'internal_faces': self.internal_faces, 'cells': self.cells,
            'patches': self.patches,
            'max_non_ortho': self.max_non_ortho,
            'avg_non_ortho': self.avg_non_ortho,
            'max_skewness': self.max_skewness,
            'max_aspect_ratio': self.max_aspect_ratio,
            # The minimum-critical metrics travel with the rest, or a caller
            # reading this dict sees only the maxima and concludes the mesh is
            # fine when its worst cell is flattened.
            'min_determinant': self.min_determinant,
            'avg_determinant': self.avg_determinant,
            'min_face_weight': self.min_face_weight,
            'avg_face_weight': self.avg_face_weight,
            'min_volume_ratio': self.min_volume_ratio,
            'avg_volume_ratio': self.avg_volume_ratio,
            'min_cell_volume': self.min_cell_volume,
            'max_cell_volume': self.max_cell_volume,
            'min_face_area': self.min_face_area,
            'max_openness': self.max_openness,
            'min_orthogonal_quality': self.min_orthogonal_quality,
            'failed_checks': self.failed_checks, 'mesh_ok': self.mesh_ok,
            'severity': self.severity, 'incomplete': self.incomplete,
            'warnings': list(self.warnings),
            'failed_check_details': list(self.failed_check_details),
            'reported_sets': list(self.reported_sets),
            'recommendations': list(self.recommendations),
            'blocking_findings': list(self.blocking_findings),
            'advisory_findings': list(self.advisory_findings),
            'runnable': self.runnable,
            'verdict': self.verdict,
            'quality_grade': self.quality_grade,
            'quality_indicators': self.quality_indicators(),
        }

    @property
    def min_orthogonal_quality(self) -> float | None:
        """Worst orthogonal quality, in the ANSYS/Fluent convention.

        ``checkMesh`` reports the non-orthogonality *angle*, where the maximum
        is the critical end. Orthogonal quality is its cosine, where 1 is
        perfect and the *minimum* is the critical end -- the same geometry, the
        convention most users arrive with, and worth stating in both so a
        number is never read against the wrong scale.
        """
        if self.max_non_ortho is None:
            return None
        return round(math.cos(math.radians(self.max_non_ortho)), 4)

    @property
    def avg_orthogonal_quality(self) -> float | None:
        if self.avg_non_ortho is None:
            return None
        return round(math.cos(math.radians(self.avg_non_ortho)), 4)

    @property
    def quality_grade(self) -> str:
        """``good``, ``marginal`` or ``poor`` for the mesh as a whole."""
        grades = {item['grade'] for item in self.quality_indicators()}
        if 'poor' in grades:
            return 'poor'
        if 'marginal' in grades or self.advisory_findings:
            return 'marginal'
        return 'good'

    @property
    def worst_indicator(self) -> dict | None:
        """The indicator a user should look at first, if any is imperfect."""
        order = {'poor': 0, 'marginal': 1, 'good': 2, 'unknown': 3}
        ranked = sorted(self.quality_indicators(),
                        key=lambda item: order.get(item['grade'], 3))
        worst = ranked[0] if ranked else None
        if worst and worst['grade'] in ('poor', 'marginal'):
            return worst
        return None

    def quality_indicators(self) -> list[dict]:
        """The numbers worth showing next to a verdict.

        Each carries its own grade, so a mesh that runs but is poor says so
        with a figure attached rather than a bare word.
        """
        def grade(value, warn, bad):
            if value is None:
                return 'unknown'
            if value >= bad:
                return 'poor'
            if value >= warn:
                return 'marginal'
            return 'good'

        def floor(value, warn, bad):
            """Grade a metric whose *minimum* is the defect end."""
            if value is None:
                return 'unknown'
            if value <= bad:
                return 'poor'
            if value <= warn:
                return 'marginal'
            return 'good'

        # Every indicator names which end is critical, because it differs by
        # metric: a mesh is spoiled by its *worst* non-orthogonality and by its
        # *smallest* determinant. The average sits beside it, since one says
        # how bad the worst cell is and the other how much of the mesh is like
        # that -- 3 bad cells in 290,000 is a very different mesh from 30,000.
        return [
            {'name': 'non-orthogonality', 'critical_kind': 'max',
             'critical': self.max_non_ortho, 'average': self.avg_non_ortho,
             'value': self.max_non_ortho,
             'grade': grade(self.max_non_ortho, 70, 80),
             'ideal': 'max below 65', 'acceptable': 'max below 70',
             'units': 'deg',
             # One convention on screen -- OpenFOAM's, since that is what
             # judges the mesh. The Fluent equivalent stays available as
             # ``min_orthogonal_quality`` for anyone who needs it.
             'note': 'above 70 the solver needs non-orthogonal correctors'},
            {'name': 'skewness', 'critical_kind': 'max',
             'critical': self.max_skewness, 'average': None,
             'value': self.max_skewness,
             'grade': grade(self.max_skewness, 4, 8),
             'ideal': 'max below 1', 'acceptable': 'max below 4',
             'units': '',
             'note': "4 is OpenFOAM's own limit; checkMesh reports no average"},
            {'name': 'aspect ratio', 'critical_kind': 'max',
             'critical': self.max_aspect_ratio, 'average': None,
             'value': self.max_aspect_ratio,
             'grade': grade(self.max_aspect_ratio, 100, 1000),
             'ideal': 'max below 20', 'acceptable': 'max below 100',
             'units': '',
             'note': 'high ratios slow convergence; layers raise this by design'},
            {'name': 'cell determinant', 'critical_kind': 'min',
             'critical': self.min_determinant, 'average': self.avg_determinant,
             'value': self.min_determinant,
             'grade': floor(self.min_determinant, 0.01, 0.001),
             'ideal': 'min above 0.05', 'acceptable': 'min above 0.001',
             'units': '',
             'note': 'a near-zero determinant is a flattened cell'},
            {'name': 'face interpolation weight', 'critical_kind': 'min',
             'critical': self.min_face_weight, 'average': self.avg_face_weight,
             'value': self.min_face_weight,
             'grade': floor(self.min_face_weight, 0.05, 0.02),
             'ideal': 'min above 0.2', 'acceptable': 'min above 0.05',
             'units': '',
             'note': '0.5 is a perfectly centred face; low values bias gradients'},
            {'name': 'face volume ratio', 'critical_kind': 'min',
             'critical': self.min_volume_ratio, 'average': self.avg_volume_ratio,
             'value': self.min_volume_ratio,
             'grade': floor(self.min_volume_ratio, 0.01, 0.001),
             'ideal': 'min above 0.1', 'acceptable': 'min above 0.01',
             'units': '',
             'note': 'an abrupt volume jump between neighbouring cells'},
            {'name': 'cell volume', 'critical_kind': 'min',
             'critical': self.min_cell_volume, 'average': None,
             'value': self.min_cell_volume,
             'grade': ('poor' if self.min_cell_volume is not None
                       and self.min_cell_volume <= 0 else
                       'unknown' if self.min_cell_volume is None else 'good'),
             'ideal': 'min above 0', 'acceptable': 'min above 0',
             'units': 'm3',
             'note': 'a zero or negative volume stops the solver outright'},
        ]


_INT = r'([0-9]+)'
_FLOAT = r'([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)'


def _search_int(pattern, text):
    match = re.search(pattern, text, re.I)
    return int(match.group(1)) if match else None


def _search_float(pattern, text):
    match = re.search(pattern, text, re.I)
    return float(match.group(1)) if match else None


_FOAM_WARNING_HEADER = re.compile(r'^-->\s*FOAM Warning\s*:?\s*$')

#: What checkMesh says as a matter of course. These stay in the warning list
#: -- they were said -- but they do not make a clean mesh a blemished one.
_HOUSEKEEPING_WARNINGS = (
    re.compile(r'No time specified or available', re.I),
    re.compile(r'^<<\s*Writing\b', re.I),
)


def substantive_warnings(warnings) -> list[str]:
    """The warnings that say something about the mesh, housekeeping excluded."""
    return [
        warning for warning in warnings
        if not any(pattern.search(warning) for pattern in _HOUSEKEEPING_WARNINGS)]


def parse_checkmesh(log: str) -> CheckMeshResult:
    result = CheckMeshResult()
    result.points = _search_int(rf'\bpoints:\s*{_INT}', log)
    result.faces = _search_int(rf'(?m)^\s*faces:\s*{_INT}', log)
    result.internal_faces = _search_int(rf'\binternal faces:\s*{_INT}', log)
    result.cells = _search_int(rf'\bcells:\s*{_INT}', log)
    result.patches = _search_int(rf'\bboundary patches:\s*{_INT}', log)

    non_ortho = re.search(
        rf'non-orthogonality\D*Max:\s*{_FLOAT}\s*average:\s*{_FLOAT}',
        log, re.I)
    if non_ortho:
        result.max_non_ortho = float(non_ortho.group(1))
        result.avg_non_ortho = float(non_ortho.group(2))
    result.max_skewness = _search_float(rf'\bMax skewness\s*=\s*{_FLOAT}', log)
    result.max_aspect_ratio = _search_float(rf'\bMax aspect ratio\s*=\s*{_FLOAT}', log)
    result.max_openness = _search_float(rf'\bMax cell openness\s*=\s*{_FLOAT}', log)
    result.min_face_area = _search_float(rf'\bMinimum face area\s*=\s*{_FLOAT}', log)
    result.min_cell_volume = _search_float(rf'\bMin volume\s*=\s*{_FLOAT}', log)
    result.max_cell_volume = _search_float(rf'\bMax volume\s*=\s*{_FLOAT}', log)

    for attribute, label in (
            ('determinant', r'Cell determinant \(wellposedness\)'),
            ('face_weight', r'Face interpolation weight'),
            ('volume_ratio', r'Face volume ratio')):
        pair = re.search(
            rf'{label}\s*:\s*minimum:\s*{_FLOAT}\s*average:\s*{_FLOAT}',
            log, re.I)
        if pair:
            setattr(result, f'min_{attribute}', float(pair.group(1)))
            setattr(result, f'avg_{attribute}', float(pair.group(2)))

    failed = _search_int(rf'\bFailed\s*{_INT}\s*mesh checks?', log)
    mesh_ok = re.search(r'\bMesh\s+OK\b', log, re.I) is not None
    result.incomplete = failed is None and not mesh_ok
    if failed is not None:
        result.failed_checks = failed
        result.mesh_ok = failed == 0
    elif mesh_ok:
        result.failed_checks = 0
        result.mesh_ok = True

    lines = log.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line:
            continue
        if _FOAM_WARNING_HEADER.match(line):
            # OpenFOAM prints the header on its own line and the message on
            # the lines after it. Keeping just the header recorded a warning
            # that said nothing, and every clean mesh wore a blemish for it.
            body = []
            while index < len(lines) and lines[index].strip():
                body.append(lines[index].strip())
                index += 1
            message = ' '.join(
                part for part in body
                if not part.startswith(('From function', 'in file')))
            result.warnings.append(f'{line} {message}' if message else line)
            continue
        if '***' in line or '<<' in line or re.search(r'\bwarning\b', line, re.I):
            result.warnings.append(line)
        # "Failed 2 mesh checks." is the tally, not a finding. Counting it as
        # one made every failure look like it had an unrecognised -- and so
        # blocking -- cause.
        is_tally = re.search(rf'\bFailed\s*{_INT}\s*mesh checks?', line, re.I)
        if not is_tally and (
                '***' in line or re.search(r'\bfailed\b.*\bcheck', line, re.I)):
            detail = re.sub(r'^[*<\s]+|[*<>\s]+$', '', line)
            if detail:
                result.failed_check_details.append(detail)
        for pattern in (
                r'\b(?:set|Set)\s+["\']?([A-Za-z_][A-Za-z0-9_.-]*)',
                r'\bWriting\s+(?:failed\s+)?(?:cells|faces|points)\s+to\s+([A-Za-z_][A-Za-z0-9_.-]*)'):
            result.reported_sets.extend(re.findall(pattern, line))

    result.warnings = list(dict.fromkeys(result.warnings))
    result.failed_check_details = list(dict.fromkeys(result.failed_check_details))
    result.reported_sets = list(dict.fromkeys(result.reported_sets))
    for detail in result.failed_check_details:
        if classify_finding(detail) == 'advisory':
            result.advisory_findings.append(detail)
        else:
            result.blocking_findings.append(detail)

    # DP-450. Size before faults. checkMesh grades what it looked for and
    # an empty domain is not on its list, so a mesh with no interior arrived
    # here with two advisory findings and left graded runnable.
    empty = empty_mesh_finding(result)
    if empty:
        result.blocking_findings.insert(0, empty)
        if empty not in result.failed_check_details:
            result.failed_check_details.insert(0, empty)

    if result.incomplete:
        result.runnable = None
    else:
        result.runnable = not result.blocking_findings

    result.recommendations = _recommendations(result)
    if result.incomplete:
        result.severity = 'incomplete'
    elif result.blocking_findings:
        result.severity = 'fail'
    elif result.advisory_findings or substantive_warnings(result.warnings):
        # Runs, but the quality is worth a look. Calling this "fail" would say
        # the mesh is unusable, which it is not.
        result.severity = 'warning'
    else:
        result.severity = 'pass'
    result.verdict = _verdict(result)
    return result


def _verdict(result: CheckMeshResult) -> str:
    """One line for a status bar, a console, or a report header.

    Two facts, in the order a user needs them: can this mesh be run, and how
    good is it. Counting findings -- "2 blocking problem(s)" -- says neither,
    so the problems are named and the quality is graded instead.

    DP-137. The second fact is *not* called "quality" here. The verdict strip
    a centimetre above this line is labelled ``Quality limits:`` and carries
    the other scale -- ``pass``/``blemish``/``fail`` -- so a sentence reading
    ``Quality: marginal`` under a strip reading ``Quality limits: blemish``
    put one word over two scales and left a user no way to tell which of the
    two judged the mesh. The grade is named for what it grades instead, and
    it points at the box by the title that box actually wears.
    """
    if result.incomplete:
        return 'Mesh check did not finish — no verdict.'
    if result.blocking_findings:
        named = '; '.join(item.rstrip('.') for item in result.blocking_findings[:2])
        more = (f'; and {len(result.blocking_findings) - 2} more'
                if len(result.blocking_findings) > 2 else '')
        return f'Mesh is NOT runnable — {named}{more}.'

    grade = result.quality_grade
    worst = result.worst_indicator
    if grade == 'good':
        return 'Mesh is runnable. Every metric grades good.'
    if worst is None or worst.get('value') is None:
        # Nothing is bad enough to name -- the grade came from an advisory
        # finding rather than from a number -- so it is the mesh as a whole
        # being graded, and "worst metric" would point at a figure that is
        # fine. The elbow case is exactly this one.
        return (f'Mesh is runnable. The mesh grades {grade}. '
                'See the metrics below.')
    # DP-137. Written the way the strip writes it: direction, figure, unit.
    # The strip said `max 71.9472 deg` and this line said `71.9472`, so the
    # same measurement appeared twice in one band in two different shapes.
    units = str(worst.get('units') or '')
    figure = '{0} {1:g}{2}'.format(
        str(worst.get('critical_kind') or '').strip(), worst['value'],
        ' ' + units if units else '').strip()
    return (f'Mesh is runnable. Worst metric grades {grade} — '
            f'{worst["name"]} {figure}. See the metrics below.'
            + _REMEDY.get(str(worst['name']), ''))


#: DP-781. The one move that lowers each metric, said where the grade is
#: read. MEASURED on mesh campaign 0925: S3 (snappy elbow, base cell 0.015 m)
#: finished at max skewness 4.36 with 2 checkMesh checks failed; S3B, the same
#: case at 0.01 m, read `Mesh OK`, max skewness 3.04. The remedy lived only in
#: the Mesh check dialog, which the guided Quality and Export steps never open.
_FINER = (' Finer cells where the surface bends lower it: a smaller base cell '
          'size or a higher surface refinement level on snappyHexMesh, a '
          'smaller target size on Gmsh.')
_REMEDY = {'skewness': _FINER, 'non-orthogonality': _FINER}


def empty_mesh_finding(result: CheckMeshResult) -> str | None:
    """Why this mesh cannot carry a solve, from its size rather than its faults.

    DP-450, MEASURED on `flat helical_pipe/snappy`: 8 points, 6 faces, 0
    internal faces, 1 cell, 1 patch -- one hexahedron. checkMesh named two
    findings, a small determinant and a concave cell, one cell each; both
    classify advisory, which is correct for what they are. So
    `blocking_findings` was empty, the mesh graded `runnable: true, severity:
    warning`, and the sentence beside it advised the reader on refinement
    transitions and `nCellsBetweenLevels` -- on a mesh with no internal face.
    The paired refined leg published the identical mesh and graded the same.

    A mesh with nothing in it passed by having too little to be wrong with,
    because runnability was decided entirely by which of checkMesh's named
    checks fired. checkMesh is not wrong here and adding a check to it is not
    the repair: it reports the faults it looks for, and "there is no mesh" is
    not one of them. The counts are already parsed a hundred lines above and
    were consulted by nothing.

    `internal_faces` is the discriminator and it is unambiguous. A flux
    crosses internal faces; a domain that has none has no interior, whatever
    its cell count says, so nothing can be solved on it. Zero cells is the
    same statement one step earlier and is named separately, because a reader
    told "no cells" and a reader told "no internal faces" are being told
    different things about what went wrong upstream.

    Returns the sentence to file as a blocking finding, or None when the mesh
    has an interior or when the log did not report the counts -- a count that
    was never parsed is unknown, and grading unknown as empty would be this
    fault inverted.
    """
    if result.cells is not None and result.cells <= 0:
        return ('The mesh has no cells, so there is nothing to solve on it.')
    if result.internal_faces is not None and result.internal_faces <= 0:
        if result.cells is None:
            return ('The mesh has no internal faces, so it has no interior '
                    'for a solver to work across.')
        return (f'The mesh has no internal faces across its '
                f'{count_text(result.cells, "cell")}, so it has no interior '
                f'for a flux to cross; whatever else checkMesh reported, '
                f'nothing can be solved on this.')
    return None


def _recommendations(result: CheckMeshResult) -> list[str]:
    recommendations = []
    joined = ' '.join(result.failed_check_details + result.warnings).lower()
    if result.incomplete:
        recommendations.append('Run Mesh check again; the log ended before a final verdict.')
    if result.max_non_ortho is not None and result.max_non_ortho > 70:
        recommendations.append('Review highly non-orthogonal cells and local refinement or geometry quality.')
    if result.max_skewness is not None and result.max_skewness > 4:
        recommendations.append('Inspect high-skewness sets and adjust snapping or refinement controls.')
    if 'negative' in joined or 'zero volume' in joined:
        recommendations.append('Do not run a solver until negative or zero-volume cells are removed.')
    if 'open' in joined or 'illegal' in joined:
        recommendations.append('Inspect the reported sets and patch topology before meshing again.')
    if result.advisory_findings and not result.blocking_findings:
        recommendations.append(
            count_text(len(result.advisory_findings), 'quality finding')
            + agreeing(len(result.advisory_findings), ' comes', ' come')
            + ' from the exhaustive checks only; the mesh is runnable. '
            'Improve them if the solver struggles, rather than treating the '
            'run as blocked.')
    if 'concave cell' in joined:
        recommendations.append(
            'Concave cells are inherent to cut-cell meshing at refinement '
            'transitions; reduce them with fewer refinement levels or a '
            'higher nCellsBetweenLevels if they matter to your solver.')
    if result.mesh_ok is True and not recommendations:
        recommendations.append('Mesh check passed; review any application-specific quality limits before solving.')
    return recommendations
