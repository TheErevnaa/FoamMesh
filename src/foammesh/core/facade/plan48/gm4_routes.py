"""Plan 48 GM4: the Export page's honesty controls, on the local API.

Plain remote control of what a person has on the Export page, nothing
agent-specific:

``gmsh.export.matrix``
    The measured target compatibility matrix the page reads (section 4.9.1):
    its rows, and for one writer the cell of a mesh (SUPPORTED, CONVERTED or
    REFUSED, with the reason, the conversions and what it drops).
``gmsh.export.check``
    What the Export page says before a write: the refusal, the conversions the
    user may choose, and what the write leaves out.
``gmsh.export.msh_choice``
    The ``.msh`` options as chosen: the file Gmsh would write, or why it
    refuses (version, binary, SaveParametric, partition split).
``gmsh.export.unit_pins``
    The two import-unit pins with who set them, and the ``configuration.patch``
    that unlocks or locks them.
``gmsh.export.publisher_copy``
    The hidden MSH 2.2 copy OpenFOAM is built from, beside a ``.msh``.
``gmsh.export.write``
    Write the session mesh as the Export page does.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_OBJECT = {'type': 'object'}

OPERATIONS = (
    OperationDescriptor(
        'gmsh.export.matrix', 'Target compatibility matrix', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'properties': {
            'writer': {'type': 'string'}, 'options': _OBJECT,
            'facts': _OBJECT}},
        summary='The measured matrix of what each writer does with a mesh of '
                'each kind. With a writer and the mesh facts (the open '
                'session when omitted), the cell for that mesh.'),
    OperationDescriptor(
        'gmsh.export.check', 'What an export says before it writes', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['writer'],
                           'properties': {
                               'writer': {'type': 'string'},
                               'options': _OBJECT,
                               'convert': {'type': 'array',
                                           'items': {'type': 'string'}},
                               'facts': _OBJECT}},
        summary='The refusal (or none), the conversions that can be chosen '
                'and what the write leaves out, for the open session mesh or '
                'the facts given.'),
    OperationDescriptor(
        'gmsh.export.msh_choice', 'The .msh as chosen', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'properties': {
            'options': _OBJECT}},
        summary='The five MSH versions Gmsh writes, and for the options given '
                'the file label, the matrix row and the measured refusal.'),
    OperationDescriptor(
        'gmsh.export.unit_pins', 'Import-unit pins', 'case', ImpactClass.READ,
        parameters_schema={'type': 'object', 'properties': {
            'unit': {'type': 'string'}, 'scaling': {}}},
        summary='Geometry.OCCTargetUnit and Geometry.OCCScaling with who set '
                'them, and the configuration.patch that unlocks (at unit and '
                'scaling) or locks them again.'),
    OperationDescriptor(
        'gmsh.export.publisher_copy', 'The hidden publisher copy', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['path'],
                           'properties': {'path': {'type': 'string'}}},
        summary='Where the hidden MSH 2.2 copy of a .msh sits, whether it '
                'exists, and which file the OpenFOAM publisher reads.'),
    OperationDescriptor(
        'gmsh.export.write', 'Write the session mesh', 'case',
        ImpactClass.FILE_PRODUCING,
        parameters_schema={'type': 'object', 'required': ['writer', 'path'],
                           'properties': {
                               'writer': {'type': 'string'},
                               'path': {'type': 'string'},
                               'options': _OBJECT,
                               'convert': {'type': 'array',
                                           'items': {'type': 'string'}},
                               'overwrite': {'type': 'boolean'},
                               'extrusion': _OBJECT,
                               'check_mesh': {'type': 'boolean'}}},
        summary='Write the open Gmsh session mesh in a format, as the Export '
                'page does: the options as given, a chosen conversion, the '
                'result read back.'),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _writers():
    from foammesh.core.gmsh import export_writers
    return export_writers


def _writer(name):
    writers = _writers()
    sentence = writers.retired(name)
    if sentence:
        # DP-1437: Medit (.mesh) export is retired; same error code as an
        # unknown writer, with the retirement sentence and ``retired: true``.
        raise ValidationFailedError(
            sentence,
            details={'writer': str(name), 'retired': True,
                     'writers': [item.id for item in writers.WRITERS]})
    try:
        return writers.writer(str(name))
    except KeyError as error:
        raise ValidationFailedError(
            f'{name!r} is not a writer',
            details={'writers': [item.id for item in writers.WRITERS]}
        ) from error


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _stored_options(session) -> dict:
    from foammesh.core.gmsh import option_catalogue as catalogue
    try:
        controller = _controller(session)
        text = controller._overrides() if controller._overrides else ''
        return catalogue.overrides_from_text(text)
    except Exception:                                        # noqa: BLE001
        return {}


def _facts(session, params):
    writers = _writers()
    given = params.get('facts')
    if given:
        return writers.MeshFacts.from_dict(given)
    controller = _controller(session)
    live = getattr(controller, 'session', None)
    if live is None or not getattr(live, 'alive', False):
        raise PreconditionFailedError(
            'there is no Gmsh session with a mesh yet: generate one first, '
            'or pass the mesh facts')
    return writers.MeshFacts.from_dict(live.export_facts())


def _cell(writers, item, facts, options) -> dict | None:
    cell = writers.cell_for(item, facts, options)
    if cell is None:
        return None
    return {'state': cell.state, 'reason': cell.reason,
            'drops': list(cell.drops), 'conversions': list(cell.conversions),
            'evidence': cell.evidence}


def _matrix(session, command) -> OperationResult:
    writers = _writers()
    params = command.parameters
    document = writers.load_matrix()
    payload = {
        'present': bool(document),
        'path': document.get('_path', ''),
        'gmsh': document.get('gmsh', ''),
        'measured_cases': document.get('measured_cases', 0),
        'key': document.get('key', ''),
        'axes': document.get('axes', {}),
        'rows': sorted((document.get('rows') or {})),
        'conversions': {name: {'label': label, 'description': text}
                        for name, (label, text) in
                        writers.CONVERSIONS.items()},
    }
    if params.get('writer'):
        item = _writer(params['writer'])
        options = dict(params.get('options') or {})
        row = writers.row_id(item, options)
        payload['writer'] = item.id
        payload['row'] = row
        payload['cells'] = ((document.get('rows') or {}).get(row) or {}
                            ).get('cells', {})
        try:
            facts = _facts(session, params)
        except PreconditionFailedError:
            facts = None
        if facts is not None:
            payload['mesh_class'] = writers.mesh_class(facts)
            payload['cell'] = _cell(writers, item, facts, options)
    return _result(session, command, payload)


def _check(session, command) -> OperationResult:
    writers = _writers()
    params = command.parameters
    item = _writer(params['writer'])
    options = dict(params.get('options') or {})
    convert = list(params.get('convert') or ())
    unknown = [name for name in convert if name not in writers.CONVERSIONS]
    if unknown:
        raise ValidationFailedError(
            f'unknown conversion {unknown[0]!r}',
            details={'conversions': sorted(writers.CONVERSIONS)})
    facts = _facts(session, params)
    return _result(session, command, {
        'writer': item.id,
        'row': writers.row_id(item, options),
        'refusal': writers.refusal(item.id, facts, options, convert=convert),
        'conversions': writers.conversions(item, facts, options),
        'drops': writers.stated_drops(item, facts, options),
        'cell': _cell(writers, item, facts, options),
    })


def _msh_choice(session, command) -> OperationResult:
    writers = _writers()
    options = dict(command.parameters.get('options') or {})
    choice = writers.MshChoice.from_options(options)
    return _result(session, command, {
        'versions': [{'version': number, 'label': name}
                     for number, name in writers.MSH_VERSIONS],
        'label': choice.label, 'row': choice.row,
        'refusal': choice.refusal(),
        'drops': writers.msh_choice_drops(options),
    })


def _unit_pins(session, command) -> OperationResult:
    from foammesh.core.gmsh import option_catalogue as catalogue
    writers = _writers()
    params = command.parameters
    stored = _stored_options(session)
    payload = {
        'rows': writers.unit_pin_rows(stored),
        'metres_per_unit': writers.metres_per_unit(stored),
        'field': catalogue.OVERRIDES_FIELD,
    }
    try:
        unlocked = writers.with_unit_pins(
            stored, unlock=True, unit=params.get('unit'),
            scaling=params.get('scaling'))
    except catalogue.OptionRefusal as error:
        raise ValidationFailedError(str(error)) from error
    payload['unlock_patch'] = {
        catalogue.OVERRIDES_FIELD: catalogue.overrides_to_text(unlocked)}
    payload['lock_patch'] = {
        catalogue.OVERRIDES_FIELD: catalogue.overrides_to_text(
            writers.with_unit_pins(stored, unlock=False))}
    return _result(session, command, payload)


def _publisher_copy(session, command) -> OperationResult:
    from foammesh.core.gmsh import publish
    path = Path(str(command.parameters['path']))
    copy = publish.publisher_copy_path(path)
    source = publish.publisher_source(path)
    return _result(session, command, {
        'msh': str(path), 'copy': str(copy), 'copy_exists': copy.is_file(),
        'publisher_reads': str(source), 'hidden': True,
        'copy_format': {'version': 2.2, 'binary': False},
    })


async def _write(session, command) -> OperationResult:
    from foammesh.core.gmsh.export_service import ExportRefused, MeshExporter
    params = command.parameters
    writers = _writers()
    item = _writer(params['writer'])
    controller = _controller(session)
    exporter = MeshExporter(
        lambda: controller.session,
        overrides=lambda: _stored_options(session))
    options = dict(params.get('options') or {})
    convert = list(params.get('convert') or ())
    destination = str(Path(str(params['path'])).expanduser().resolve())

    def work():
        return exporter.write(
            item.id, destination, options,
            overwrite=bool(params.get('overwrite')),
            check_mesh=bool(params.get('check_mesh')),
            convert=convert, extrusion=params.get('extrusion'))

    try:
        outcome = await asyncio.to_thread(work)
    except ExportRefused as error:
        raise PreconditionFailedError(str(error)) from error
    except writers.catalogue.OptionRefusal as error:
        raise ValidationFailedError(str(error)) from error
    return _result(session, command, outcome.to_dict())


def handlers() -> dict:
    return {
        'gmsh.export.matrix': _matrix,
        'gmsh.export.check': _check,
        'gmsh.export.msh_choice': _msh_choice,
        'gmsh.export.unit_pins': _unit_pins,
        'gmsh.export.publisher_copy': _publisher_copy,
        'gmsh.export.write': _write,
    }
