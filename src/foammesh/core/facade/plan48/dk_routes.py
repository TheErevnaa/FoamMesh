"""Plan 48 DK local-API route: the sections of the Gmsh tool dock.

The Entities, Visibility and display and Plugin views panels are sections of
the dock (the rail's Gmsh tools popover).  ``gmsh.tools.open`` (PA4) brings a
panel forward and opens its section; ``gmsh.tools.section`` is the section's
own title bar: open or close it, and read which sections hold a panel.  A
plain remote control; it answers ``precondition_failed`` when no window shows
the case.
"""
from __future__ import annotations

from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

SECTIONS = ('entities', 'visibility', 'plugins')

OPERATIONS = (
    OperationDescriptor(
        'gmsh.tools.section', 'Open or close a section of the Gmsh tool dock',
        'case', ImpactClass.READ,
        summary='section entities, visibility or plugins: open (default) or '
                'close that section of the dock, as its title bar does; '
                'without section, read which sections hold a panel and '
                'which are open.',
        parameters_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {
                'section': {'type': 'string', 'enum': list(SECTIONS)},
                'open': {'type': 'boolean'}}}),
)


def handlers() -> dict:
    from foammesh.core.facade.plan48 import pa4_routes as pa4

    async def tools_section(session, command):
        p = dict(command.parameters or {})
        if p.get('section') is None:
            payload = pa4._call(session, pa4.TOOLS_ATTRIBUTE, 'sections')
        else:
            payload = pa4._call(
                session, pa4.TOOLS_ATTRIBUTE, 'set_section', p['section'],
                bool(p.get('open', True)))
        return pa4._result(session, command, payload)

    return {'gmsh.tools.section': tools_section}
