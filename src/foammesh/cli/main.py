#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""``foammesh`` CLI — headless entry point sharing the same core as GUI/API.

Subcommands:
    foammesh version
    foammesh checkmesh <log>        parse a checkMesh log -> summary + readiness
    foammesh info <case>            structured mesh info (text/json/csv)
    foammesh check <case>           run the target solver's mesh check -> dashboard data
    foammesh target-solver <case>   read or set the solver the mesh is for
    foammesh transform <case> ...   recovery-backed transformPoints (capability-gated)
    foammesh restore <case>         restore the previous mesh recovery point
    foammesh import-mesh <case> ... native/converter mesh import
    foammesh export <case> <fmt>    native/vtu/gmsh/cgns export via shared service
    foammesh formats                truthful import/export availability matrix
    foammesh history <case>         unified artifact history
    foammesh engines <case>         discover/probe/select meshing engines
    foammesh mesh-plan <case>       derive an engine execution plan
    foammesh canonical <case> ...   inspect/validate/export a canonical mesh
    foammesh serve [--host --port]  run the REST/WebSocket API (needs [api] extra)

Every subcommand calls the exact service the GUI uses, so headless results
match GUI results by construction (U7.1 headless workflow layer).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _execute(case, operation: str, parameters: dict | None = None):
    from foammesh.cli.facade_cli import execute_case_operation
    from foammesh.core.facade import FacadeError
    try:
        return execute_case_operation(case, operation, parameters)
    except FacadeError as error:
        print(f'error [{error.code}]: {error}', file=sys.stderr)
        if error.details:
            print(json.dumps(error.details, sort_keys=True), file=sys.stderr)
        return None


def _cmd_version(args) -> int:
    from app_properties import meshAppProperties
    print(f'{meshAppProperties.name} {meshAppProperties.version}')
    return 0


def _cmd_checkmesh(args) -> int:
    from foammesh.core.quality import parse_checkmesh, readiness_verdict
    log = Path(args.log).read_text(encoding='utf-8', errors='ignore')
    result = parse_checkmesh(log)
    print(f'cells={result.cells} faces={result.faces} points={result.points} '
          f'patches={result.patches}')
    print(f'maxNonOrtho={result.max_non_ortho} maxSkewness={result.max_skewness} '
          f'maxAspectRatio={result.max_aspect_ratio}')
    verdict = readiness_verdict(result)
    print('READY' if verdict.ok else 'NOT READY')
    for r in verdict.reasons:
        print(f'  - {r}')
    return 0 if verdict.ok else 1


def _cmd_generate(args) -> int:
    """Import a surface, set the base domain, and generate an OpenFOAM case dir."""
    selected = _execute(args.out, 'mesh.engine.select', {
        'engine_id': 'snappy',
        'confirmed': True,
        'reason': 'Snappy selected by the explicit generate command',
    })
    if selected is None:
        return 1
    imported = _execute(args.out, 'geometry.import', {'source': str(args.stl)})
    if imported is None:
        return 1
    bbox = imported.payload.get('bbox')
    if not bbox or len(bbox) != 6:
        print('error: imported geometry has no finite bounding box', file=sys.stderr)
        return 1
    # DP-576. The margin round the geometry is the project's standoff, saved
    # in the case, not a pad only this command knows about: the same case
    # then writes the same blockMeshDict from the desktop, the facade and
    # here. Half the diagonal on every face, as this command always padded,
    # expressed as the standoff's fraction of the largest span.
    spans = [bbox[i + 1] - bbox[i] for i in (0, 2, 4)]
    if max(spans) <= 0 or min(spans) < 0:
        print('error: imported geometry has no extent to build a domain round',
              file=sys.stderr)
        return 1
    diagonal = sum(span ** 2 for span in spans) ** 0.5
    standoff = 0.5 * diagonal / max(spans)
    configured = _execute(args.out, 'configuration.patch', {
        'patch': {'meshing.base_grid.standoff': standoff}})
    if configured is None:
        return 1
    generated = _execute(args.out, 'workflow.generate_dictionaries',
                         {'bbox': list(bbox)})
    if generated is None or generated.status != 'accepted':
        return 1
    print(json.dumps({'import': imported.payload, 'dictionaries': generated.payload},
                     indent=2, sort_keys=True))
    return 0


def _cmd_mesh(args) -> int:
    """Generate a case and require the complete OpenFOAM pipeline to succeed."""
    rc = _cmd_generate(args)
    if rc != 0:
        return rc
    cores = int(getattr(args, 'cores', 1))
    timeout = float(getattr(args, 'timeout', 3600))
    result = _execute(args.out, 'workflow.run_pipeline', {
        'mode': 'parallel' if cores > 1 else 'serial',
        'cores': cores,
        'timeout_seconds': timeout,
    })
    if result is None:
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status == 'accepted' else 1


def _cmd_info(args) -> int:
    result = _execute(args.case, 'mesh.info', {
        'display_unit': args.unit, 'report': args.report})
    if result is None:
        return 1
    payload = result.payload or {}
    if args.report:
        print(payload.get('report'))
    elif getattr(args, 'json', False):
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        # A summary a person can read; --json is the whole payload.
        for key, value in sorted(payload.items()):
            if isinstance(value, (dict, list)):
                value = json.dumps(value, sort_keys=True, default=str)
            print(f'{key}: {value}')
    return 0


def _cmd_check(args) -> int:
    # Plan 28 WP4. checkMesh is OpenFOAM's; an SU2 project is usually meshed
    # where OpenFOAM is not installed, and this command could only fail there.
    # The facade owns the rule, so `--check auto` asks it rather than guessing.
    operation = {'openfoam': 'mesh.check',
                 'su2': 'quality.su2_readiness'}.get(
                     getattr(args, 'check', 'auto'), '')
    if not operation:
        listing = _execute(args.case, 'mesh.target_solver.get', {})
        operation = ((listing.payload or {}).get('qa_operation')
                     if listing is not None else None) or 'mesh.check'
    result = _execute(args.case, operation, {})
    if result is None:
        return 2
    if getattr(args, 'quiet', False):
        # One line: what the check concluded, not everything it said.
        payload = result.payload or {}
        report = payload.get('report') or payload.get('readiness') or {}
        print(f"{result.status}: check={operation} "
              f"severity={report.get('severity', 'unknown')} "
              f"verdict={report.get('verdict', 'unknown')}")
    else:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status == 'accepted' else 1


def _cmd_transform(args) -> int:
    try:
        def vector(raw):
            if not isinstance(raw, str):
                raise TypeError('a vector must be text')
            values = tuple(float(value) for value in raw.replace(',', ' ').split())
            if len(values) != 3:
                raise ValueError('a vector needs exactly three components')
            return values
        if args.operation == 'rotate':
            axes = {'x': (1, 0, 0), 'y': (0, 1, 0), 'z': (0, 0, 1)}
            axis = axes[args.axis.lower()] if args.axis.lower() in axes else vector(args.axis)
            parameters = {'vector': axis, 'angle_degrees': args.angle,
                          'pivot': vector(args.pivot) if args.pivot else None}
        else:
            parameters = {'vector': vector(args.values)}
    except (TypeError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 1
    result = _execute(args.case, f'mesh.transform.{args.operation}', parameters)
    if result is None:
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status == 'accepted' else 1


def _cmd_restore(args) -> int:
    result = _execute(args.case, 'mesh.restore', {})
    if result is None:
        return 1
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0


def _cmd_import_mesh(args) -> int:
    operation = 'mesh.import.native' if args.from_case else 'mesh.import.converter'
    parameters = {'source': str(args.source)}
    if not args.from_case:
        parameters['format'] = args.format
    result = _execute(args.case, operation, parameters)
    if result is None:
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status == 'accepted' else 1


def _cmd_export(args) -> int:
    operations = {
        'openfoam': 'case.export.native', 'vtk': 'case.export.vtk',
        'gmsh': 'case.export.gmsh', 'cgns': 'case.export.cgns',
        'su2': 'case.export.su2', 'fluent': 'case.export.fluent',
        'med': 'case.export.med', 'unv': 'case.export.unv',
        'openfoam_format': 'case.export.format_convert',
    }
    operation = operations.get(args.format)
    if operation is None:
        print(f'error: unknown export format {args.format!r}', file=sys.stderr)
        return 1
    parameters = {'write_format': args.write_format, 'compression': args.compression,
                  'commit_settings': args.commit_settings}
    if operation in {'case.export.native', 'case.export.vtk',
                     'case.export.gmsh', 'case.export.cgns', 'case.export.su2',
                     'case.export.med', 'case.export.unv'}:
        if not args.dest:
            print(f'error: {args.format} export needs a destination', file=sys.stderr)
            return 1
        parameters = {'destination': str(args.dest)}
    result = _execute(args.case, operation, parameters)
    if result is None:
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status == 'accepted' else 1


def _cmd_formats(args) -> int:
    from foammesh.core.facade import FoamMeshFacade
    utilities = ('fluentMeshToFoam', 'gmshToFoam', 'ideasUnvToFoam',
                 'star4ToFoam', 'foamMeshToFluent', 'foamFormatConvert')
    facade = FoamMeshFacade()
    body = facade.describe_capabilities(utilities)
    body['operations'] = [item['operation'] for item in
                          facade.describe_operations()['operations']
                          if item['operation'].startswith(('mesh.import.', 'case.export.'))]
    print(json.dumps(body, indent=2, sort_keys=True))
    return 0


def _cmd_history(args) -> int:
    result = _execute(args.case, 'history.query', {'limit': 200})
    if result is None:
        return 1
    print(json.dumps(result.payload, indent=2, sort_keys=True, default=str))
    return 0


def _emit_operation(result) -> int:
    if result is None:
        return 2
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    return 0 if result.status == 'accepted' else 1


def _cmd_engines(args) -> int:
    operation = {
        'list': 'mesh.engine.list', 'probe': 'mesh.engine.probe',
        'self-test': 'mesh.engine.self_test', 'select': 'mesh.engine.select',
        'workflow': 'mesh.engine.workflow',
    }[args.engine_action]
    parameters = {}
    if getattr(args, 'engine_id', None):
        parameters['engine_id'] = args.engine_id
    if getattr(args, 'profile_id', None):
        parameters['profile_id'] = args.profile_id
    if getattr(args, 'timeout', None) is not None:
        parameters['timeout_seconds'] = args.timeout
    if args.engine_action == 'select':
        parameters.update({'dry_run': args.dry_run, 'confirmed': args.confirmed})
    return _emit_operation(_execute(args.case, operation, parameters))


def _cmd_target_solver(args) -> int:
    """Read or set the solver this mesh is being built for.

    Setting it is what makes snappyHexMesh unselectable for SU2, and what
    decides which check ``foammesh check`` runs.
    """
    solver = getattr(args, 'solver', None)
    if solver is None:
        return _emit_operation(_execute(args.case, 'mesh.target_solver.get', {}))
    return _emit_operation(_execute(args.case, 'mesh.target_solver.set',
                                    {'target_solver': solver}))


def _cmd_mesh_plan(args) -> int:
    return _emit_operation(_execute(args.case, 'mesh.plan.derive', {
        'engine_id': args.engine, 'prepared_revision_id': args.prepared_revision,
        'run_id': args.run_id,
    }))


def _cmd_workflow_task(args) -> int:
    parameters = {'engine_id': args.engine}
    if args.task_action == 'transition':
        parameters.update({
            'task_id': args.task_id,
            'transition': args.transition,
        })
    return _emit_operation(_execute(
        args.case,
        'mesh.workflow.task_state'
        if args.task_action == 'state'
        else 'mesh.workflow.task_transition',
        parameters))


def _cmd_canonical(args) -> int:
    operations = {
        'info': 'mesh.canonical.info',
        'validate': 'mesh.canonical.validate',
        'quality': 'quality.canonical',
        'export-openfoam': 'mesh.canonical.export.openfoam',
        'selection-capabilities': 'mesh.canonical.selection_capabilities',
        'export-failed-set': 'quality.canonical.failed_set.export',
    }
    parameters = {'artifact_id': args.artifact_id}
    if args.canonical_action == 'quality':
        parameters['policy'] = args.policy
    elif args.canonical_action == 'export-openfoam' and args.destination:
        parameters['destination'] = args.destination
    elif args.canonical_action == 'export-failed-set':
        parameters.update({'metric': args.metric, 'destination': args.destination})
    return _emit_operation(_execute(args.case, operations[args.canonical_action], parameters))


def _cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print('The API requires the [api] extra: pip install "foammesh[api]"',
              file=sys.stderr)
        return 2
    from foammesh.api import create_app
    uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


def _engine_ids() -> tuple[str, ...]:
    """Selectable engine ids, taken from the registry rather than restated.

    Registering an engine is then the only step needed to expose it on the
    command line.
    """
    from foammesh.core.engine.registry import ENGINE_REGISTRY
    return ENGINE_REGISTRY.ids()


def build_parser() -> argparse.ArgumentParser:
    engine_ids = _engine_ids()
    parser = argparse.ArgumentParser(prog='foammesh',
                                     description='FoamMesh meshing toolkit')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('version', help='print version').set_defaults(func=_cmd_version)

    p_cm = sub.add_parser('checkmesh', help='parse a checkMesh log')
    p_cm.add_argument('log')
    p_cm.set_defaults(func=_cmd_checkmesh)

    p_gen = sub.add_parser('generate', help='import surface -> generate OpenFOAM case')
    p_gen.add_argument('stl', help='STL/OBJ surface file')
    p_gen.add_argument('-o', '--out', required=True, help='output case directory')
    p_gen.set_defaults(func=_cmd_generate)

    p_mesh = sub.add_parser(
        'mesh', help='generate a case and require the OpenFOAM pipeline to succeed')
    p_mesh.add_argument('stl', help='STL/OBJ surface file')
    p_mesh.add_argument('-o', '--out', required=True, help='output case directory')
    p_mesh.add_argument('--cores', type=int, default=1,
                        help='OpenFOAM MPI ranks (default: serial)')
    p_mesh.add_argument('--timeout', type=float, default=3600,
                        help='per-node timeout in seconds')
    p_mesh.add_argument(
        '--require-runtime', action='store_true',
        help='compatibility flag; runtime success is always required')
    p_mesh.set_defaults(func=_cmd_mesh)

    p_info = sub.add_parser('info', help='structured mesh info for a case')
    p_info.add_argument('case')
    p_info.add_argument('--unit', default='m', choices=('m', 'cm', 'mm', 'um'))
    p_info.add_argument('--json', action='store_true', help='print full JSON')
    p_info.add_argument('--report', help='save a .json/.csv report to this path')
    p_info.set_defaults(func=_cmd_info)

    p_check = sub.add_parser(
        "check", help="run the target solver's mesh check")
    p_check.add_argument('case')
    p_check.add_argument('--quiet', action='store_true', help='do not stream output')
    p_check.add_argument(
        '--check', choices=('auto', 'openfoam', 'su2'), default='auto',
        help="which check to run; 'auto' follows the project's target solver")
    p_check.set_defaults(func=_cmd_check)

    p_target = sub.add_parser(
        'target-solver', help='read or set the solver this mesh is built for')
    p_target.add_argument('case')
    p_target.add_argument('solver', nargs='?',
                          choices=('unselected', 'openfoam', 'su2'),
                          help='omit to read the current value')
    p_target.set_defaults(func=_cmd_target_solver)

    p_tr = sub.add_parser('transform', help='recovery-backed transformPoints')
    tr_sub = p_tr.add_subparsers(dest='operation', required=True)
    for operation in ('scale', 'translate'):
        p_op = tr_sub.add_parser(operation)
        p_op.add_argument('case')
        p_op.add_argument('values', help='"X Y Z" components')
        p_op.set_defaults(func=_cmd_transform)
    p_rot = tr_sub.add_parser('rotate')
    p_rot.add_argument('case')
    p_rot.add_argument('axis', help='X, Y, Z, or "X Y Z" vector')
    p_rot.add_argument('angle', type=float, help='degrees')
    p_rot.add_argument('--pivot', help='"X Y Z" pivot point')
    p_rot.set_defaults(func=_cmd_transform)

    p_restore = sub.add_parser('restore', help='restore the previous mesh recovery point')
    p_restore.add_argument('case')
    p_restore.set_defaults(func=_cmd_restore)

    p_imp = sub.add_parser('import-mesh', help='import a mesh into a case')
    p_imp.add_argument('case')
    p_imp.add_argument('source', help='mesh file or source case directory')
    p_imp.add_argument('--format', default='fluent',
                       help='converter format id (ignored with --from-case)')
    p_imp.add_argument('--from-case', action='store_true',
                       help='treat source as an OpenFOAM case to copy polyMesh from')
    p_imp.set_defaults(func=_cmd_import_mesh)

    p_exp = sub.add_parser('export', help='export a case through the shared service')
    p_exp.add_argument('case')
    p_exp.add_argument('format',
                       help='openfoam | vtk | gmsh | cgns | su2 | med | unv | '
                            'fluent | openfoam_format')
    p_exp.add_argument('dest', nargs='?', help='destination directory/file')
    p_exp.add_argument('--write-format', default='ascii', choices=('ascii', 'binary'))
    p_exp.add_argument('--compression', action='store_true')
    p_exp.add_argument('--commit-settings', action='store_true',
                       help='keep the write settings in controlDict afterwards')
    p_exp.set_defaults(func=_cmd_export)

    sub.add_parser('formats', help='truthful import/export availability matrix') \
        .set_defaults(func=_cmd_formats)

    p_hist = sub.add_parser('history', help='unified artifact history for a case')
    p_hist.add_argument('case')
    p_hist.set_defaults(func=_cmd_history)

    p_engines = sub.add_parser('engines', help='discover, probe, and select meshing engines')
    engine_sub = p_engines.add_subparsers(dest='engine_action', required=True)
    p_engine_list = engine_sub.add_parser('list')
    p_engine_list.add_argument('case')
    p_engine_list.set_defaults(func=_cmd_engines)
    for action in ('probe', 'workflow'):
        item = engine_sub.add_parser(action)
        item.add_argument('case')
        item.add_argument('engine_id', choices=engine_ids)
        if action == 'probe':
            item.add_argument('--profile-id')
            item.add_argument(
                '--timeout', type=float, default=120,
                help='bounded engine runtime probe timeout in seconds')
        item.set_defaults(func=_cmd_engines)
    p_self_test = engine_sub.add_parser('self-test')
    p_self_test.add_argument('case')
    p_self_test.add_argument('--profile-id')
    p_self_test.add_argument('--timeout', type=float, default=120)
    p_self_test.set_defaults(func=_cmd_engines)
    p_select = engine_sub.add_parser('select')
    p_select.add_argument('case')
    p_select.add_argument('engine_id', choices=engine_ids)
    p_select.add_argument('--profile-id')
    p_select.add_argument(
        '--timeout', type=float, default=120,
        help='bounded engine availability probe timeout in seconds')
    p_select.add_argument('--dry-run', action='store_true')
    p_select.add_argument('--confirmed', action='store_true')
    p_select.set_defaults(func=_cmd_engines)

    p_plan = sub.add_parser('mesh-plan', help='derive the selected engine execution plan')
    p_plan.add_argument('case')
    p_plan.add_argument('--engine', choices=engine_ids)
    p_plan.add_argument('--prepared-revision')
    p_plan.add_argument('--run-id')
    p_plan.set_defaults(func=_cmd_mesh_plan)

    p_task = sub.add_parser(
        'workflow-task',
        help='inspect or transition persisted engine task lifecycle state')
    task_sub = p_task.add_subparsers(dest='task_action', required=True)
    for action in ('state', 'transition'):
        item = task_sub.add_parser(action)
        item.add_argument('case')
        item.add_argument(
            '--engine', choices=engine_ids, default=engine_ids[0])
        if action == 'transition':
            item.add_argument('task_id')
            item.add_argument(
                'transition',
                choices=('configure', 'accept', 'skip', 'revert', 'fail'))
        item.set_defaults(func=_cmd_workflow_task)

    p_canonical = sub.add_parser('canonical', help='inspect and export a canonical mixed mesh')
    canonical_sub = p_canonical.add_subparsers(dest='canonical_action', required=True)
    for action in ('info', 'validate', 'selection-capabilities'):
        item = canonical_sub.add_parser(action)
        item.add_argument('case')
        item.add_argument('artifact_id')
        item.set_defaults(func=_cmd_canonical)
    p_cq = canonical_sub.add_parser('quality')
    p_cq.add_argument('case')
    p_cq.add_argument('artifact_id')
    p_cq.add_argument('--policy', default='balanced', choices=('coarse', 'balanced', 'strict'))
    p_cq.set_defaults(func=_cmd_canonical)
    p_co = canonical_sub.add_parser('export-openfoam')
    p_co.add_argument('case')
    p_co.add_argument('artifact_id')
    p_co.add_argument('--destination')
    p_co.set_defaults(func=_cmd_canonical)
    p_cf = canonical_sub.add_parser('export-failed-set')
    p_cf.add_argument('case')
    p_cf.add_argument('artifact_id')
    p_cf.add_argument('metric')
    p_cf.add_argument('destination')
    p_cf.set_defaults(func=_cmd_canonical)

    p_serve = sub.add_parser('serve', help='run the REST/WebSocket API')
    p_serve.add_argument('--host', default='127.0.0.1')
    p_serve.add_argument('--port', type=int, default=8000)
    p_serve.set_defaults(func=_cmd_serve)

    # AF5: thin facade-client subcommands (field-get/set, op, snapshot, ...).
    from foammesh.cli.facade_cli import add_facade_commands
    add_facade_commands(sub)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


def _run_as_script(module_name=__name__):
    """Run the command-line entry point when executed as a module."""
    if module_name == '__main__':
        raise SystemExit(main())


_run_as_script()
