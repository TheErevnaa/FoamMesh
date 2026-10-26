"""Plan 48 ST local-API route: the "Reproducible (single-thread)" box of the
Gmsh Global sizing page (DP-4605).

A plain remote control for the box beside the thread count. Ticked, a Gmsh
run uses one thread for every stage (``General.NumThreads`` and
``Mesh.MaxNumThreads1D/2D/3D`` are 1), in the batch runner and in the
resident session, because Gmsh 4.15.2 with more than one thread can mesh one
model into a different number of elements each run.

The value is the stored field ``gmsh.global_sizing.reproducible``;
``configuration.patch`` reads and writes it without a window. This route is
the window's own control: it ticks the box and applies, as a click on the box
and on Apply do, and answers what the box and the store hold. Without a
window it answers ``precondition_failed``.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import PreconditionFailedError
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.plan48.pa3_routes import _need_panel, _result
from foammesh.core.facade.policy import ImpactClass

OPERATIONS = (
    OperationDescriptor(
        'gmsh.global_sizing.reproducible',
        'Reproducible (single-thread) box', 'case', ImpactClass.READ,
        parameters_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {'enabled': {'type': 'boolean'}}},
        summary='The "Reproducible (single-thread)" box next to Threads on '
                'the Gmsh Global sizing page. With enabled it ticks or '
                'unticks the box and applies, as a click and Apply do; '
                'without, it reads. Answers reproducible (the box), stored '
                '(the case), pending and whether the thread note shows. '
                'Ticked, a run uses one thread in every stage, in a batch '
                'run and in the session, and the run record says '
                '"reproducible": true. Needs the open window; the stored '
                'field gmsh.global_sizing.reproducible is also reachable '
                'with configuration.patch.'),
)


async def _reproducible(session, command) -> object:
    parameters = command.parameters or {}
    api = _need_panel(session, 'global_sizing', 'the Global sizing page')
    enabled = parameters.get('enabled')
    try:
        done = await asyncio.to_thread(
            api.reproducible, None if enabled is None else bool(enabled))
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, dict(done))


def handlers() -> dict:
    return {'gmsh.global_sizing.reproducible': _reproducible}
