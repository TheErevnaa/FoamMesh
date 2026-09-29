"""AF5 CLI as a thin facade client.

These subcommands open a persistent :class:`CaseSession` through the shared
:class:`FoamMeshFacade` and execute the identical facade commands that REST and
the GUI use, so ``foammesh field-set`` / ``op`` / ``snapshot`` / ``history`` are
contract-equivalent to the REST and in-process Python paths (§8 parity). No
command touches ``ProjectState`` or an OpenFOAM utility directly.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from foammesh.core.facade import (
    Actor, ActorKind, CaseSession, Command, CommandSource, FoamMeshFacade)


def _coerce(raw: str):
    """Interpret a CLI value: JSON if it parses, else the literal string."""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _open(path):
    """Open the case at ``path``, creating a configuration if it has none.

    The question asked here has to be the one :meth:`CaseSession.open` asks,
    which is whether the configuration *file* is there. It used to ask whether
    the `foammesh` *directory* was there, and a case that carries the sidecar
    directory without a configuration in it -- a case whose history was
    written before its first save, which is what the shipped `single_hex`
    fixture is -- answered "yes, it exists" here and "no, it does not" one
    call later, so every CLI verb on such a case died with a FileNotFoundError
    naming a file the CLI had just decided not to create.
    """
    facade = FoamMeshFacade()
    case_path = Path(path)
    from foammesh.db.configurations import FILE_NAME
    configured = ((case_path / 'foammesh' / FILE_NAME).is_file()
                  or (case_path / FILE_NAME).is_file())
    session = CaseSession.open(case_path, create=not configured)
    facade.attach(session)
    return facade, session


def _cli_command(session, operation, parameters, *, scope='case'):
    return Command(operation, session.case_id if scope != 'application' else '',
                   parameters, Actor('cli-user', ActorKind.CLI), CommandSource.CLI, scope=scope)


def _emit(payload) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def execute_case_operation(case, operation: str, parameters: dict | None = None):
    """Execute one semantic operation through a short-lived CLI session."""
    facade, session = _open(case)
    try:
        return asyncio.run(facade.execute(
            _cli_command(session, operation, parameters or {})))
    finally:
        facade.detach(session.case_id)


def cmd_field_get(args) -> int:
    facade, session = _open(args.case)
    try:
        _emit(facade.field(session.case_id, args.field))
    finally:
        facade.detach(session.case_id)
    return 0


def cmd_field_set(args) -> int:
    facade, session = _open(args.case)
    try:
        patch = {args.field: _coerce(args.value)}
        result = asyncio.run(facade.execute(
            _cli_command(session, 'configuration.patch', {'patch': patch})))
        _emit(result.to_dict())
    finally:
        facade.detach(session.case_id)
    return 0


def cmd_op(args) -> int:
    parameters = _coerce(args.parameters) if args.parameters else {}
    result = execute_case_operation(args.case, args.operation, parameters)
    _emit(result.to_dict())
    return 0


def cmd_fluid_regions(args) -> int:
    """Plan 36 RP7: detect the fluid spaces, then write the chosen ones.

    ``fluid-regions detect CASE --count 2 [--external]`` prints what
    ``geometry.fluid_regions.detect`` returns -- the JSON the facade hands the
    GUI, including its ``detection_id`` -- and ``fluid-regions apply CASE
    --ids 2,3 --detection-id ID`` writes those spaces' seeds as regions in one
    undoable change. The id binds the ids to that detection: an apply after
    the geometry or the box changed is refused as ``detection_stale``
    (Plan 36 RP13 #3).
    """
    parameters = {}
    if args.external:
        parameters['external'] = True
    if args.resolution is not None:
        parameters['resolution'] = args.resolution
    if args.action == 'detect':
        parameters['count'] = args.count
        operation = 'geometry.fluid_regions.detect'
    else:
        if not args.ids:
            raise SystemExit('fluid-regions apply needs --ids, e.g. --ids 2,3')
        parameters['ids'] = [int(value) for value in args.ids.split(',')
                             if value.strip()]
        if args.detection_id:
            parameters['detection_id'] = args.detection_id
        if args.type:
            parameters['type'] = args.type
        if args.replace:
            parameters['replace'] = True
        operation = 'geometry.fluid_regions.apply'
    from foammesh.core.facade import FacadeError
    try:
        result = execute_case_operation(args.case, operation, parameters)
    except FacadeError as error:
        # RP13 #3: a refusal says what it is -- ``error: detection_stale``
        # -- then why, and exits non-zero.
        details = dict(getattr(error, 'details', None) or {})
        print(f'error: {details.get("error") or error.code}: {error}',
              file=sys.stderr)
        if details:
            print(json.dumps(details, sort_keys=True), file=sys.stderr)
        return 2
    _emit(result.to_dict())
    return 0


def cmd_snapshot(args) -> int:
    facade, session = _open(args.case)
    try:
        _emit(facade.snapshot(session.case_id))
    finally:
        facade.detach(session.case_id)
    return 0


def cmd_history(args) -> int:
    facade, session = _open(args.case)
    try:
        _emit(asyncio.run(facade.history(session.case_id, limit=args.limit)))
    finally:
        facade.detach(session.case_id)
    return 0


def cmd_operations(args) -> int:
    _emit(FoamMeshFacade().describe_operations())
    return 0


def cmd_journey(args) -> int:
    from foammesh.core.automation import (
        Guardrails, Orchestrator, authored_mesh_recipe, converter_import_recipe,
        export_archive_recipe, geometry_import_recipe, quality_adjustment_recipe,
        quality_assessment_recipe, transform_recovery_recipe)
    options = _coerce(args.parameters) if args.parameters else {}
    factories = {
        'geometry': lambda: geometry_import_recipe(options['source']),
        'authored-mesh': lambda: authored_mesh_recipe(options.get('patch', {})),
        'qa-assess': quality_assessment_recipe,
        'qa-adjust': lambda: quality_adjustment_recipe(options['patch']),
        'transform': lambda: transform_recovery_recipe(options['kind'], options.get('parameters', {})),
        'converter-import': lambda: converter_import_recipe(options['source'], options['format']),
        'export-archive': lambda: export_archive_recipe(
            options['export_destination'], options['archive_destination']),
    }
    facade, session = _open(args.case)
    try:
        guardrails = Guardrails(
            max_cells=args.max_cells, max_attempts=args.max_attempts,
            wall_clock_seconds=args.max_seconds,
            allowed_roots=(Path(args.allowed_root).resolve(),) if args.allowed_root else ())
        report = asyncio.run(Orchestrator(facade, guardrails, actor_id='cli-journey').run(
            session.case_id, factories[args.journey](), estimated_cells=args.estimated_cells))
        _emit(report.to_dict())
        return 0 if report.status == 'succeeded' else 2
    finally:
        facade.detach(session.case_id)


def add_facade_commands(sub: argparse._SubParsersAction) -> None:
    """Register the facade-client subcommands on the CLI parser."""
    fg = sub.add_parser('field-get', help='read a semantic field value from a case')
    fg.add_argument('case'); fg.add_argument('field')
    fg.set_defaults(func=cmd_field_get)

    fs = sub.add_parser('field-set', help='set a semantic field via configuration.patch')
    fs.add_argument('case'); fs.add_argument('field'); fs.add_argument('value')
    fs.set_defaults(func=cmd_field_set)

    op = sub.add_parser('op', help='execute any registered facade operation')
    op.add_argument('case'); op.add_argument('operation')
    op.add_argument('--parameters', help='JSON parameters object', default=None)
    op.set_defaults(func=cmd_op)

    fr = sub.add_parser(
        'fluid-regions',
        help='detect the fluid spaces of a case and create regions in them')
    fr.add_argument('action', choices=('detect', 'apply'))
    fr.add_argument('case')
    fr.add_argument('--count', type=int, default=1,
                    help='how many fluid regions are wanted (detect)')
    fr.add_argument('--external', action='store_true',
                    help='offer the space outside the geometry first')
    fr.add_argument('--resolution', type=float, default=None,
                    help='voxel size in metres (default: half a base cell)')
    fr.add_argument('--ids', default=None,
                    help='comma-separated space ids to create regions in (apply)')
    fr.add_argument('--detection-id', default=None,
                    help='the detection_id detect printed; binds --ids to '
                         'that detection (apply)')
    fr.add_argument('--type', choices=('fluid', 'solid'), default=None,
                    help='region type for apply (default fluid)')
    fr.add_argument('--replace', action='store_true',
                    help='replace the existing regions of the type applied '
                         '(apply; default adds to them)')
    fr.set_defaults(func=cmd_fluid_regions)

    sn = sub.add_parser('snapshot', help='print the facade snapshot for a case')
    sn.add_argument('case'); sn.set_defaults(func=cmd_snapshot)

    hi = sub.add_parser('facade-history', help='transaction history via the facade')
    hi.add_argument('case'); hi.add_argument('--limit', type=int, default=200)
    hi.set_defaults(func=cmd_history)

    ops = sub.add_parser('operations', help='list registered facade operations')
    ops.set_defaults(func=cmd_operations)

    journey = sub.add_parser('journey', help='run a confirmed deterministic PC6 recipe')
    journey.add_argument('case')
    journey.add_argument('journey', choices=(
        'geometry', 'authored-mesh', 'qa-assess', 'qa-adjust', 'transform',
        'converter-import', 'export-archive'))
    journey.add_argument('--parameters', default='{}', help='journey-specific JSON object')
    journey.add_argument('--max-cells', type=int)
    journey.add_argument('--estimated-cells', type=int)
    journey.add_argument('--max-attempts', type=int, default=3)
    journey.add_argument('--max-seconds', type=float)
    journey.add_argument('--allowed-root')
    journey.set_defaults(func=cmd_journey)
