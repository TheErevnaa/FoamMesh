"""Plan 48 GM8 local-API routes: the native Fields dialog.

Plain remote control of what the Size fields page does. The rows live in the
project (``gmsh.native_fields.rows``) and are already writable through
``configuration.patch``; these routes add what a person gets from the page
around them.

``gmsh.fields.catalogue``
    Every field type of the build with its options, which can be added now
    (the anisotropic ones need BAMG or MMG3D) and the Delta default of the
    model (the model diagonal / 10000).
``gmsh.fields.check``
    The reason the rows cannot be built, or the nodes Gmsh would be given.
``gmsh.fields.add_row``
    Add a row of a type, as the Add field button does; the rows come back.
``gmsh.fields.size_data.scan``
    The datasets, steps and components of a ``.pos`` or ``.msh`` file.
``gmsh.fields.size_data.load``
    Copy a data file into the project and add a PostView row reading one
    dataset at one step in one unit (decision D6); the rows come back.
``gmsh.fields.size_data.set`` / ``.remove``
    Change the unit, mode or time step of loaded data, or take it away.
``gmsh.fields.visualize``
    The sizes ONE field gives on the model, sampled where Gmsh asks, for the
    viewport (the shared scale).
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_ROWS = {'type': 'array', 'items': {'type': 'object'}}

OPERATIONS = (
    OperationDescriptor(
        'gmsh.fields.catalogue', 'Size field types and options', 'case',
        ImpactClass.READ,
        summary='Every Gmsh size-field type with its options (kind, default, '
                'unit, help), whether it can be added with the chosen '
                'algorithms, and the Delta default of the model.'),
    OperationDescriptor(
        'gmsh.fields.check', 'Check the size fields', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object',
                           'properties': {'rows': _ROWS}},
        summary='The nodes the stored (or given) rows build, with the rows '
                'skipped and why; or the reason they cannot be built.'),
    OperationDescriptor(
        'gmsh.fields.add_row', 'Add a size field', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['type'],
                           'properties': {'type': {'type': 'string'},
                                          'name': {'type': 'string'},
                                          'rows': _ROWS}},
        summary='The rows with a new row of the type; send them back with '
                'configuration.patch on gmsh.native_fields.rows to keep them.'),
    OperationDescriptor(
        'gmsh.fields.size_data.scan', 'Read the datasets of a data file',
        'case', ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': {'type': 'string'}}},
        summary='The datasets, time steps and components of a .pos or .msh '
                'file; a binary file is read by the Gmsh session.'),
    OperationDescriptor(
        'gmsh.fields.size_data.load', 'Load size data', 'case',
        ImpactClass.REVERSIBLE_EDIT,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {
                               'path': {'type': 'string'},
                               'dataset': {},
                               'step': {'type': 'integer'},
                               'unit': {'enum': ['m', 'cm', 'mm']},
                               'mode': {'enum': ['auto', 'list', 'hidden']},
                               'name': {'type': 'string'},
                               'rows': _ROWS}},
        summary='Copy a .pos or .msh file into the project and add a '
                'PostView row reading one dataset at one step in one unit. '
                'The rows come back.'),
    OperationDescriptor(
        'gmsh.fields.size_data.set', 'Change loaded size data', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['name'],
                           'properties': {
                               'name': {'type': 'string'},
                               'unit': {'enum': ['m', 'cm', 'mm']},
                               'mode': {'enum': ['auto', 'list', 'hidden']},
                               'step': {'type': 'integer'},
                               'rows': _ROWS}},
        summary='The rows with the unit, mode or time step of the loaded '
                'data on one row changed.'),
    OperationDescriptor(
        'gmsh.fields.size_data.remove', 'Remove loaded size data', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['name'],
                           'properties': {'name': {'type': 'string'},
                                          'rows': _ROWS}},
        summary='The rows without the row that holds loaded data; refused '
                'while another field reads it.'),
    OperationDescriptor(
        'gmsh.fields.visualize', 'Sizes of one field', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema={'type': 'object', 'required': ['name'],
                           'properties': {'name': {'type': 'string'},
                                          'dim': {'type': 'integer'},
                                          'limit': {'type': 'integer'}}},
        summary='Sample one stored size field alone on the model and return '
                'the points and sizes the viewport draws on the shared '
                'scale. Refused while the model has a mesh.'),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _stored(session) -> dict:
    """The stored field rows and the algorithms, as plain values."""
    from foammesh.core.facade import FIELD_REGISTRY
    from foammesh.core.facade.field_adapters import read_value
    from foammesh.core.gmsh import field_catalogue as fc

    configuration = session.configuration()
    ids = (fc.FIELD_ID, 'gmsh.global_sizing.surface', 'gmsh.global_sizing.volume')
    plain = {}
    for field_id in ids:
        try:
            value = read_value(configuration,
                               FIELD_REGISTRY.get(field_id).storage_path)
        except Exception:                                # noqa: BLE001
            value = None
        plain[field_id] = getattr(value, 'value', value)
    return plain


def _rows(session, command, stored=None) -> list:
    from foammesh.core.gmsh import field_catalogue as fc

    given = (command.parameters or {}).get('rows')
    if given is not None:
        if not isinstance(given, list):
            raise ValidationFailedError('rows must be a list')
        return [dict(item) for item in given if isinstance(item, dict)]
    stored = stored or _stored(session)
    try:
        return fc.rows_from_text(stored.get(fc.FIELD_ID))
    except fc.FieldRefusal as error:
        raise ValidationFailedError(str(error)) from error


def _algorithms(stored) -> tuple:
    return (str(stored.get('gmsh.global_sizing.surface') or ''),
            str(stored.get('gmsh.global_sizing.volume') or ''))


def _text(rows) -> str:
    from foammesh.core.gmsh import field_catalogue as fc

    return fc.rows_to_text(rows)


def _option(option, diagonal) -> dict:
    from foammesh.core.gmsh import field_catalogue as fc

    default = fc.default_for(option, diagonal)
    return {'name': option.name, 'kind': option.kind, 'default': default,
            'gmsh_default': option.default, 'unit': option.unit,
            'help': option.help, 'required': option.required,
            'choices': [list(c) for c in option.choices],
            'model_scale': option.model_scale}


async def _catalogue(session, command) -> OperationResult:
    from foammesh.core.gmsh import field_catalogue as fc

    stored = _stored(session)
    surface, volume = _algorithms(stored)
    diagonal = await asyncio.to_thread(fc.model_diagonal, session.case_path)
    types = []
    for kind, usable, note in fc.listed_types(surface, volume):
        types.append({
            'type': kind.gmsh, 'title': kind.title, 'family': kind.family,
            'help': kind.help, 'anisotropic': kind.anisotropic,
            'can_add': usable, 'note': note,
            'options': [_option(o, diagonal) for o in kind.shown_options]})
    return _result(session, command, {
        'types': types, 'surface_algorithm': surface,
        'volume_algorithm': volume, 'model_diagonal': diagonal,
        'delta_default': (diagonal * 1e-4 if diagonal else None),
        'anisotropic_note': fc.ANISOTROPIC_NOTE})


async def _check(session, command) -> OperationResult:
    from foammesh.core.gmsh import field_catalogue as fc

    stored = _stored(session)
    surface, volume = _algorithms(stored)
    rows = fc.rebase_size_data(_rows(session, command, stored),
                               session.case_path)
    try:
        built = fc.validate(rows, surface_algorithm=surface,
                            volume_algorithm=volume)
    except fc.FieldRefusal as error:
        return _result(session, command, {'ok': False, 'reason': str(error),
                                          'nodes': [], 'warnings': []})
    return _result(session, command, {
        'ok': True, 'reason': '', 'nodes': built.to_list(),
        'order': list(built.order), 'warnings': list(built.warnings)})


async def _add_row(session, command) -> OperationResult:
    from foammesh.core.gmsh import field_catalogue as fc

    parameters = command.parameters or {}
    kind = str(parameters.get('type') or '')
    if kind not in fc.TYPES:
        raise ValidationFailedError(
            f'{kind!r} is not a Gmsh size-field type',
            details={'types': list(fc.TYPES)})
    stored = _stored(session)
    surface, volume = _algorithms(stored)
    reason = fc.inactive_reason(kind, surface, volume)
    if reason and fc.TYPES[kind].anisotropic:
        raise ValidationFailedError(reason, details={'type': kind})
    rows = _rows(session, command, stored)
    from foammesh.core.facade.plan48.pa3_routes import next_name
    given = str(parameters.get('name') or '')
    names = {r.get('name') for r in rows}
    if given:
        chosen, number = given, 1
        while chosen in names:
            number += 1
            chosen = f'{given}{number}'
    else:
        # Plan 48 PA3: unnamed, the row is named as the Add field button
        # names it (ball1, ball2, ...).
        chosen = next_name(rows, kind[0].lower() + kind[1:])
    row = fc.new_row(kind, chosen)
    rows.append(row)
    return _result(session, command, {'row': row, 'rows': rows,
                                      'rows_text': _text(rows)})


def _scan(path: str, session) -> dict:
    from foammesh.core.gmsh import field_catalogue as fc

    scan = fc.scan_size_data(path)
    if scan.get('binary'):
        controller = _controller(session)
        live = controller.session
        if live is None or not live.alive:
            scan['error'] = ('a binary file is read by the Gmsh session; '
                             'generate or open the model first')
            return scan
        try:
            got = live.call('session.size_data_scan',
                            path=fc.runtime_path(path))
            scan['datasets'] = got.get('datasets') or []
            scan['error'] = '' if scan['datasets'] else \
                'the file holds no data view'
        except Exception as error:                       # noqa: BLE001
            scan['error'] = str(error)
    return scan


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


async def _size_data_scan(session, command) -> OperationResult:
    path = str((command.parameters or {}).get('path') or '')
    if not path:
        raise ValidationFailedError('path is required')
    scan = await asyncio.to_thread(_scan, path, session)
    return _result(session, command, scan)


async def _size_data_load(session, command) -> OperationResult:
    from foammesh.core.gmsh import field_catalogue as fc

    parameters = command.parameters or {}
    path = str(parameters.get('path') or '')
    if not path:
        raise ValidationFailedError('path is required')
    scan = await asyncio.to_thread(_scan, path, session)
    datasets = scan.get('datasets') or []
    if not datasets:
        raise ValidationFailedError(
            scan.get('error') or 'the file holds no data view')
    wanted = parameters.get('dataset')
    chosen = None
    for item in datasets:
        if wanted is None and item.get('scalar'):
            chosen = item
        elif wanted is not None and wanted in (item['index'], item['name']):
            chosen = item
        if chosen is not None:
            break
    if chosen is None:
        raise ValidationFailedError(
            f'the file has no scalar dataset {wanted!r}',
            details={'datasets': [d['name'] for d in datasets]})
    try:
        copy = await asyncio.to_thread(fc.copy_size_data, path,
                                       session.case_path)
        ref = fc.make_size_data_ref(
            copy, chosen, step=int(parameters.get('step') or 0),
            unit=str(parameters.get('unit') or 'm'),
            mode=str(parameters.get('mode') or 'auto'),
            name=str(parameters.get('name') or ''))
        rows, row = fc.add_size_data_row(
            _rows(session, command), ref, str(parameters.get('name') or ''))
    except fc.FieldRefusal as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, {
        'row': row, 'rows': rows, 'rows_text': _text(rows),
        'copy': copy['file'], 'dataset': chosen['name']})


async def _size_data_set(session, command) -> OperationResult:
    from foammesh.core.gmsh import field_catalogue as fc

    parameters = command.parameters or {}
    name = str(parameters.get('name') or '')
    rows = _rows(session, command)
    target = next((r for r in rows if r.get('name') == name), None)
    if target is None or not target.get('data'):
        raise ValidationFailedError(f'{name!r} has no loaded size data')
    ref = dict(target['data'])
    for key in ('unit', 'mode', 'step'):
        if parameters.get(key) is not None:
            ref[key] = parameters[key]
    try:
        fc.size_data_node(ref, name, strict=False)
    except fc.FieldRefusal as error:
        raise ValidationFailedError(str(error)) from error
    target['data'] = ref
    return _result(session, command, {'row': target, 'rows': rows,
                                      'rows_text': _text(rows)})


async def _size_data_remove(session, command) -> OperationResult:
    from foammesh.core.gmsh import field_catalogue as fc

    name = str((command.parameters or {}).get('name') or '')
    try:
        rows = fc.remove_size_data(_rows(session, command), name)
    except fc.FieldRefusal as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, {'rows': rows, 'rows_text': _text(rows)})


async def _visualize(session, command) -> OperationResult:
    from foammesh.core.gmsh import field_catalogue as fc

    parameters = command.parameters or {}
    name = str(parameters.get('name') or '')
    rows = fc.rebase_size_data(_rows(session, command), session.case_path)
    row = next((r for r in rows if r.get('name') == name), None)
    if row is None:
        raise ValidationFailedError(f'there is no size field {name!r}')
    kind = fc.TYPES[row['type']]
    if kind.gmsh == 'BoundaryLayer' or kind.anisotropic:
        raise ValidationFailedError(
            f'a {kind.gmsh} field has no single size to show')
    if not row.get('enabled', True):
        raise ValidationFailedError(f'{name!r} is switched off')
    from foammesh.core.gmsh.generate_actions import ActionRefusal
    controller = _controller(session)
    try:
        answer = await asyncio.to_thread(
            controller.sample_field, name,
            int(parameters.get('dim') or 2),
            int(parameters.get('limit') or 20000))
    except (ValueError, ActionRefusal) as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, answer)


def handlers() -> dict:
    return {
        'gmsh.fields.catalogue': _catalogue,
        'gmsh.fields.check': _check,
        'gmsh.fields.add_row': _add_row,
        'gmsh.fields.size_data.scan': _size_data_scan,
        'gmsh.fields.size_data.load': _size_data_load,
        'gmsh.fields.size_data.set': _size_data_set,
        'gmsh.fields.size_data.remove': _size_data_remove,
        'gmsh.fields.visualize': _visualize,
    }
