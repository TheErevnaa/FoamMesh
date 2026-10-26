"""Plan 48 GM12 local-API routes: the common viewport tools on a Gmsh mesh.

Plain remote control for what a person has in the window: the quality of a mesh
file with no case, the surface deviation of a Gmsh mesh from its CAD (trimmed
faces, curved elements sampled to a declared tolerance), the layers grown on a
Gmsh mesh, the failed cells a file shows, the cell fields a section can be
coloured by, and the project's provenance record for a mesh.

Every number is *measured* or *unavailable with its reason* (``core.provenance``):
a value that was not measured is never a zero. The result functions are plain
importable functions (``quality_of_file``, ``deviation_of``, ``layers_of``,
``failed_cells_of``, ``section_fields_of``, ``provenance_of``,
``record_provenance``) so a package that builds on them (GM13, the study-facing
results) can call them without the facade.
"""
from __future__ import annotations

from pathlib import Path

from foammesh.core.facade.errors import (
    CapabilityUnavailableError, ValidationFailedError)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_PATH = {'type': 'string'}
_ROOTS = {'type': 'array', 'items': {'type': 'string'}}

OPERATIONS = (
    OperationDescriptor(
        'gmsh.quality.file', 'Quality of a mesh file', 'case', ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {
                               'path': _PATH,
                               'rank_n': {'type': 'integer', 'minimum': 0}}},
        summary='Gmsh quality (SICN, SIGE, gamma, isotropy, Jacobians, volume) '
                'of a .msh or SU2 file with no case: statistics, histograms, '
                'physical groups, and the worst elements when rank_n is given.'),
    OperationDescriptor(
        'gmsh.deviation.measure', 'Surface deviation of a Gmsh mesh', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['mesh', 'reference'],
                           'properties': {
                               'mesh': _PATH, 'reference': _PATH,
                               'tolerance_mm': {'type': 'number'},
                               'unit': {'type': ['string', 'number']},
                               'max_level': {'type': 'integer', 'minimum': 1,
                                             'maximum': 8},
                               'only': {'type': 'array',
                                        'items': {'type': 'string'}},
                               'include_values': {'type': 'boolean'}}},
        summary='Distance of the surface elements of a .msh/SU2 mesh to the '
                'trimmed faces of a CAD file (isInside check, boundary-curve '
                'fallback), curved elements sampled until two levels differ by '
                'less than the declared sampling tolerance. Works on the 2D '
                'mesh straight after Mesh 2D.'),
    OperationDescriptor(
        'gmsh.deviation.gate', 'Is Surface deviation available', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'properties': {
            'has_mesh': {'type': 'boolean'},
            'session_reference': {'type': 'boolean'},
            'stored_report': {'type': 'boolean'},
            'geometry_reference': {'type': 'boolean'},
            'cad_reference': {'type': 'boolean'}}},
        summary='Whether the deviation tool can run and, when it cannot, the '
                'sentence that says what to do.'),
    OperationDescriptor(
        'gmsh.deviation.report', 'Newest stored fidelity report', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'properties': {
            'roots': _ROOTS}},
        summary='The newest stored geometry-fidelity report across the case '
                'root and the native store root.'),
    OperationDescriptor(
        'gmsh.layers.coverage', 'Layer coverage of a Gmsh mesh', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': _PATH, 'roots': _ROOTS}},
        summary='Layers present per wall patch, counted on the mesh from each '
                'wall face, against the requested count from the project '
                'record (unavailable, with the reason, when there is none).'),
    OperationDescriptor(
        'gmsh.failed_cells', 'Failed cells of a mesh file', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {
                               'path': _PATH, 'roots': _ROOTS,
                               'sicn_limit': {'type': 'number'}}},
        summary='Cells that fail the checks that can run now (non-positive '
                'Jacobian, SICN below a limit); the run\'s own failure tags '
                'are returned as unavailable when the project has no record.'),
    OperationDescriptor(
        'gmsh.section.fields', 'Gmsh cell fields for a section', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {
                               'path': _PATH,
                               'keys': {'type': 'array',
                                        'items': {'type': 'string'}}}},
        summary='The Gmsh colourings of a section (quality, physical group, '
                'entity, partition, element type) and, for each categorical '
                'one, its codes and labels over the cells of a mesh file.'),
    OperationDescriptor(
        'gmsh.provenance.read', 'Provenance of a mesh', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': _PATH, 'roots': _ROOTS}},
        summary='How a mesh was made (route, settings, requested layers, '
                'failed elements, CAD) as the project recorded it; each fact '
                'is measured or unavailable with the reason.'),
    OperationDescriptor(
        'gmsh.provenance.record', 'Record how a mesh was made', 'case',
        ImpactClass.FILE_PRODUCING,
        parameters_schema={'type': 'object', 'required': ['path', 'route'],
                           'properties': {
                               'path': _PATH, 'route': {'type': 'string'},
                               'root': _PATH, 'settings': {'type': 'object'},
                               'requested_layers': {'type': 'object'},
                               'failed_elements': {'type': 'object'},
                               'cad_reference': _PATH}},
        artifact_contract=('foammesh/provenance.json',),
        summary='Store the route, settings fingerprint, requested layers per '
                'patch, failed-element sets and CAD reference of a mesh the '
                'project made, keyed by the mesh file\'s fingerprint.'),
)


# -- the result functions (importable; GM13 builds on these) -------------------- #

def _roots(session, parameters) -> list:
    roots = [r for r in (parameters.get('roots') or []) if r]
    if not roots and getattr(session, 'case_path', None):
        roots = [str(session.case_path)]
    return roots


def _need(parameters, name):
    value = parameters.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailedError('%s is required' % name)
    return value


def quality_of_file(path, rank_n: int = 0, **options) -> dict:
    """``gmsh.quality.file``: statistics of *path* (and the worst elements)."""
    from foammesh.core.gmsh import element_quality as eq

    try:
        arrays = eq.measure_file(path, **options)
    except eq.QualityFileError as error:
        raise ValidationFailedError(str(error)) from error
    out = eq.statistics(arrays)
    out['source'] = dict(arrays['__source'])
    out['measured_by'] = 'Gmsh getElementQualities on the file'
    if rank_n and int(rank_n) > 0:
        out['ranked'] = eq.rank_worst(arrays, int(rank_n))
    return out


def deviation_of(mesh, reference, *, include_values: bool = False,
                 **options) -> dict:
    """``gmsh.deviation.measure``: deviation of *mesh* from *reference*."""
    from foammesh.core.gmsh import quality

    try:
        result = quality.surface_deviation(mesh, reference, **options)
    except quality.DeviationError as error:
        raise ValidationFailedError(str(error)) from error
    out = {
        'unit': result['unit'], 'distance': result['distance'],
        'signed': result['signed'], 'sampling': result['sampling'],
        'cad': result['cad'], 'mesh': result['mesh'],
        'measured_by': result['measured_by'],
        'tolerance_mm': result['tolerance_mm'],
        'summary': quality.deviation_summary(result),
    }
    if include_values:
        out['values'] = {name: [None if v != v else float(v) for v in values]
                         for name, values in result['fields'].items()}
        out['elements'] = {k: v.tolist()
                           for k, v in result['elements'].items()}
    return out


def layers_of(path, roots=(), **options) -> dict:
    """``gmsh.layers.coverage``: layers measured on the mesh at *path*."""
    from foammesh.core.gmsh import element_quality as eq
    from foammesh.rendering import layers_grown

    try:
        out = layers_grown.gmsh_layer_coverage(path, roots, **options)
    except eq.QualityFileError as error:
        raise ValidationFailedError(str(error)) from error
    out.pop('objects', None)
    return out


def failed_cells_of(path, roots=(), *, sicn_limit=None, **options) -> dict:
    """``gmsh.failed_cells``: the failed cells of the mesh at *path*."""
    from foammesh.core import provenance
    from foammesh.core.gmsh import element_quality as eq
    from foammesh.rendering import check_highlights

    try:
        arrays = eq.measure_file(path, **options)
    except eq.QualityFileError as error:
        raise ValidationFailedError(str(error)) from error
    lookup = provenance.find(roots, path)
    limit = (check_highlights.DEFAULT_SICN_LIMIT if sicn_limit is None
             else float(sicn_limit))
    return check_highlights.gmsh_failed_cells(arrays, lookup, sicn_limit=limit)


def section_fields_of(path, keys=None, **options) -> dict:
    """``gmsh.section.fields``: the Gmsh section colourings of the mesh."""
    from foammesh.core import provenance
    from foammesh.core.gmsh import element_quality as eq
    from foammesh.core.section import section_colour
    from foammesh.rendering import section_colour_map
    from foammesh.view.display_control import section_case_fields as fields

    try:
        arrays = eq.measure_file(path, **options)
    except eq.QualityFileError as error:
        raise ValidationFailedError(str(error)) from error
    revision = provenance.fingerprint_file(path)['fingerprint']
    wanted = list(keys) if keys else list(section_colour_map.GMSH_CATEGORY_KEYS)
    categorical = {}
    for key in wanted:
        if not section_colour_map.isGmshCategory(key):
            continue
        field = fields.read_gmsh_field(arrays, key, revision)
        if field.status != fields.READ:
            categorical[key] = provenance.unavailable(field.reason).to_dict()
            continue
        mapped = section_colour_map.gmshMapping(key, field.values, field.names)
        categorical[key] = provenance.measured({
            'cells': field.cells,
            'categories': [{'code': c, 'name': n}
                           for c, n in mapped['categories']]},
            'file').to_dict()
    return {
        'revision': revision,
        'quality': [{'key': section_colour.GMSH_PREFIX + name, 'label': label}
                    for name, label in section_colour.GMSH_QUALITY_FIELDS],
        'categorical': categorical,
        'choices': [{'key': k, 'label': lab, 'array': arr}
                    for k, lab, arr in section_colour_map.GMSH_CATEGORIES],
    }


def provenance_of(path, roots=()) -> dict:
    from foammesh.core import provenance

    if not Path(path).is_file():
        raise ValidationFailedError('%s: no such file' % path)
    return provenance.describe(roots, path)


def record_provenance(root, path, **fields) -> dict:
    from foammesh.core import provenance

    if not Path(path).is_file():
        raise ValidationFailedError('%s: no such file' % path)
    record = provenance.record_mesh(root, path, **fields)
    return {'recorded': True, 'store': str(provenance.store_path(root)),
            'record': record}


# -- handlers --------------------------------------------------------------------- #

def _result(session, command, payload: dict) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _guard(call):
    from foammesh.core.gmsh.element_quality import QualityFileError

    try:
        return call()
    except (QualityFileError, FileNotFoundError) as error:
        raise CapabilityUnavailableError(str(error)) from error


def _quality(session, command):
    p = command.parameters or {}
    return _result(session, command, _guard(lambda: quality_of_file(
        _need(p, 'path'), int(p.get('rank_n') or 0))))


def _deviation(session, command):
    p = command.parameters or {}
    options = {}
    for name in ('tolerance_mm', 'unit', 'max_level', 'only'):
        if p.get(name) is not None:
            options[name] = p[name]
    return _result(session, command, _guard(lambda: deviation_of(
        _need(p, 'mesh'), _need(p, 'reference'),
        include_values=bool(p.get('include_values')), **options)))


def _gate(session, command):
    from foammesh.core.gmsh import quality

    p = command.parameters or {}
    enabled, reason = quality.deviation_availability(
        has_mesh=bool(p.get('has_mesh')),
        session_reference=bool(p.get('session_reference')),
        stored_report=bool(p.get('stored_report')),
        geometry_reference=bool(p.get('geometry_reference')),
        cad_reference=bool(p.get('cad_reference')))
    return _result(session, command, {'enabled': enabled, 'reason': reason})


def _report(session, command):
    from foammesh.core.gmsh import quality

    roots = _roots(session, command.parameters or {})
    found = quality.latest_report(roots)
    return _result(session, command, {
        'roots': roots, 'found': found is not None, 'report': found})


def _layers(session, command):
    p = command.parameters or {}
    return _result(session, command, _guard(lambda: layers_of(
        _need(p, 'path'), _roots(session, p))))


def _failed(session, command):
    p = command.parameters or {}
    return _result(session, command, _guard(lambda: failed_cells_of(
        _need(p, 'path'), _roots(session, p), sicn_limit=p.get('sicn_limit'))))


def _section(session, command):
    p = command.parameters or {}
    return _result(session, command, _guard(lambda: section_fields_of(
        _need(p, 'path'), p.get('keys'))))


def _provenance_read(session, command):
    p = command.parameters or {}
    return _result(session, command, provenance_of(
        _need(p, 'path'), _roots(session, p)))


def _provenance_record(session, command):
    p = command.parameters or {}
    root = p.get('root') or getattr(session, 'case_path', None)
    if not root:
        raise ValidationFailedError('root is required (no case is open)')
    return _result(session, command, record_provenance(
        root, _need(p, 'path'), route=_need(p, 'route'),
        settings=p.get('settings'),
        requested_layers=p.get('requested_layers'),
        failed_elements=p.get('failed_elements'),
        cad_reference=p.get('cad_reference')))


def handlers() -> dict:
    return {
        'gmsh.quality.file': _quality,
        'gmsh.deviation.measure': _deviation,
        'gmsh.deviation.gate': _gate,
        'gmsh.deviation.report': _report,
        'gmsh.layers.coverage': _layers,
        'gmsh.failed_cells': _failed,
        'gmsh.section.fields': _section,
        'gmsh.provenance.read': _provenance_read,
        'gmsh.provenance.record': _provenance_record,
    }
