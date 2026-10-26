"""Plan 48 GM3: the one execution path, on the local API.

Plain remote control of what the window does, nothing agent-specific:

``gmsh.execution.page_controls``
    The option rows the page controls turn into, for the importer (applied
    before the import) and for the mesh pass. Both the batch run and the
    resident session send exactly these rows.
``gmsh.session.apply_page_controls``
    The window's "Generate step by step" preparation: start the session,
    apply the importer options, import, apply the page controls, the Options
    list and the size fields. Returns the session's ledger.
``gmsh.session.ledger``
    The session's requested-against-effective ledger, and, with ``run_id``,
    where it differs from that batch run's ledger.
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

OPERATIONS = (
    OperationDescriptor(
        'gmsh.execution.page_controls',
        'The option rows the Gmsh page controls become', 'case',
        ImpactClass.READ,
        summary='The importer options (set before the import) and the mesh '
                'options (sizing, algorithms, optimisation, threads, element '
                'order) the batch run and the resident session both apply.'),
    OperationDescriptor(
        'gmsh.session.apply_page_controls',
        'Apply the page controls to the resident Gmsh session', 'case',
        ImpactClass.MESH_MUTATION,
        summary='Start the session if needed, apply the importer options, '
                'import the prepared geometry and apply the page controls, '
                'as Generate step by step does. Returns the ledger.',
        parameters_schema={'type': 'object', 'properties': {}}),
    OperationDescriptor(
        'gmsh.session.ledger',
        'The resident session ledger', 'case', ImpactClass.READ,
        summary='What the session was asked for and what Gmsh holds. With '
                'run_id, the controls where it differs from that batch run.',
        parameters_schema={
            'type': 'object', 'properties': {'run_id': {'type': 'string'}}}),
)


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


def _page_controls(session, command) -> OperationResult:
    controller = _controller(session)
    try:
        paths = tuple(str(Path(p)) for p in controller._geometry_paths())
    except Exception:                                    # noqa: BLE001
        paths = ()
    from foammesh.core.gmsh import execution

    intent = controller.page_intent()
    if not intent:
        raise PreconditionFailedError(
            'the page controls cannot be read yet; ' + (
                controller.warnings[-1] if controller.warnings
                else 'open a project with prepared geometry'))
    tessellated = execution.sources_are_tessellated(paths)
    return _result(session, command, {
        'tessellated': tessellated,
        'importer': [row.to_dict() for row in
                     execution.importer_option_rows(
                         intent, tessellated=tessellated)],
        'page': [row.to_dict() for row in execution.page_option_rows(intent)],
    })


async def _apply_page_controls(session, command) -> OperationResult:
    controller = _controller(session)
    from foammesh.core.gmsh.generate_actions import ActionRefusal

    def work():
        controller.warnings.clear()
        done = controller.ensure_geometry()
        return done, controller.ledger(), list(controller.warnings)

    try:
        done, ledger, warnings = await asyncio.to_thread(work)
    except ActionRefusal as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, {
        'imported': bool(done.get('imported')), 'ledger': ledger,
        'warnings': warnings})


def _ledger(session, command) -> OperationResult:
    from foammesh.core.gmsh import execution

    controller = _controller(session)
    entries = controller.ledger()
    payload = {'ledger': entries, 'warnings': list(controller.warnings)}
    run_id = str(command.parameters.get('run_id') or '').strip()
    if run_id:
        from foammesh.core.gmsh.manifest import RunBuilder, RunManifest

        try:
            document = RunManifest.read(
                RunBuilder(session.case_path).root(run_id)).document
        except Exception as error:                       # noqa: BLE001
            raise ValidationFailedError(
                f'run {run_id} has no readable manifest',
                details={'run_id': run_id}) from error
        payload['run_id'] = run_id
        payload['differences'] = execution.ledger_difference(
            entries, document.get('controls') or ())
    return _result(session, command, payload)


def handlers() -> dict:
    return {
        'gmsh.execution.page_controls': _page_controls,
        'gmsh.session.apply_page_controls': _apply_page_controls,
        'gmsh.session.ledger': _ledger,
    }
