"""Plan 49 SR8 local-API routes: the surface tab and the role model.

A plain remote control for the window's own controls (AGENTS.md):

* ``geometry.surface_intent.read`` -- what the surface tab and the pair
  tables show: each surface's role, purpose, mesh type, origin, state, issue,
  partner and regions, the relations, the choices the combo boxes offer and
  the conversion report of an older case;
* ``geometry.surface_intent.preview`` -- the issues the tab shows under its
  controls while editing, before OK;
* ``geometry.surface_intent.edit`` -- OK on the surface tab: role, purpose,
  mesh type, partner and the region on each side, planned and written by
  ``db/surface_authoring.py`` exactly as the dialog does, in one undoable
  transaction (``edit surface``);
* ``geometry.surface_intent.stage_report`` -- requested / supported /
  validated / built / exported per surface and relation, the shared
  validation the run and export apply, and each region interface's coupling
  intent (``will_couple``, ``coupled``, ``state``);
* ``geometry.surface_intent.capability_table`` -- the one capability table
  both routes and the export read;
* ``gmsh.periodic_pair.check`` -- the measurement the Gmsh Periodic page's
  pair editor makes on OK (``GmshPeriodicPairPanel.mapCheck``), through the
  window (``pa5_window``), like ``geometry.interface_pair.check``.
"""
from __future__ import annotations

from foammesh.core.facade import surface_intent_api as api
from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

_STRING = {'type': 'string'}
_NUMBER = {'type': 'number'}
_NULLABLE = {'type': ['string', 'null']}
_IDS = {'type': 'array', 'items': {'type': ['string', 'integer']},
        'minItems': 1}


def _schema(required=(), **properties) -> dict:
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': properties}
    if required:
        schema['required'] = list(required)
    return schema


_EDIT_SCHEMA = _schema(
    ('geometry_ids',), geometry_ids=_IDS,
    surface_role={'type': 'string', 'enum': [
        'externalBoundary', 'internalFace', 'regionInterfaceConformal',
        'regionInterfaceNonConformal', 'blockingBaffle']},
    boundary_category={'type': ['string', 'null'], 'enum': [
        'wall', 'inlet', 'outlet', 'far_field', 'symmetry', 'wedge',
        'unclassified', None]},
    mesh_patch_type={'type': ['string', 'null'], 'enum': [
        *api.MESH_PATCH_TYPES, None]},
    partner=_NULLABLE, own_region=_NULLABLE, partner_region=_NULLABLE)

_EDIT_HELP = (
    'Absent parameters are kept as they are (a mixed selection the tab '
    'leaves untouched). surface_role is one of externalBoundary, '
    'internalFace, regionInterfaceConformal, regionInterfaceNonConformal, '
    'blockingBaffle. boundary_category (purpose) and mesh_patch_type apply '
    'to an external boundary; a purpose change carries its normal mesh type '
    'unless the type was chosen apart from it. partner (a geometry id, '
    '"shared" for one surface between two regions, or null), own_region and '
    'partner_region (a region ref from geometry.surface_intent.read '
    'choices.regions) apply to a region interface of one surface. Refused '
    'as the tab refuses: surface_edit_refused with code (not_a_surface, '
    'unknown_role, no_role, partner_for_many, own_partner, '
    'partner_not_surface, partner_periodic, partner_taken, sides_differ, '
    'needs_external_boundary, needs_interface, unknown_purpose, '
    'unknown_patch_type, unknown_region), or periodic_side_conflict '
    '{surface, pair, reason, remedy} for a side of a periodic pair.')

OPERATIONS = (
    OperationDescriptor(
        'geometry.surface_intent.read', 'Surface roles and relations', 'case',
        ImpactClass.READ,
        summary='What the surface tab and the pair tables show: per surface '
                'its role, purpose, mesh type, origin, state, issue (with '
                'remedy and the capability verdict), partner and regions; '
                'the relations with their state and periodic measurement; '
                'the choices the tab offers; the model version and the '
                'conversion report of an older case. geometry_ids limits it '
                'to those surfaces.',
        parameters_schema=_schema(geometry_ids=_IDS)),
    OperationDescriptor(
        'geometry.surface_intent.preview', 'Preview a surface edit', 'case',
        ImpactClass.READ,
        summary='The issues the surface tab shows under its controls for '
                'an edit, before OK; nothing is written. ' + _EDIT_HELP,
        parameters_schema=_EDIT_SCHEMA),
    OperationDescriptor(
        'geometry.surface_intent.edit', 'Edit surface role', 'case',
        ImpactClass.REVERSIBLE_EDIT, reversible=True, recovery='undo',
        summary='OK on the surface tab: write the role, purpose, mesh type, '
                'partner and regions of the surfaces as the dialog does, in '
                'one undoable edit (history.undo). Returns surface_issues, '
                'what is still unresolved or not buildable on the '
                'configured route. ' + _EDIT_HELP,
        parameters_schema=_EDIT_SCHEMA),
    OperationDescriptor(
        'geometry.surface_intent.stage_report', 'Surface intent stages',
        'case', ImpactClass.READ,
        summary='For the configured route and target (or route and target '
                'given): one row per surface and relation with requested, '
                'supported, validated, built, exported and stale kept '
                'apart; issues, the shared validation the run and the '
                'export apply ({subject, reason, remedy, route, target, '
                'code, kind, blocking, text}); interfaces, each region '
                'interface\'s coupling intent (honoured, will_couple, '
                'coupled, state planned / coupled / not coupled / '
                'unmatched / refused); and the built and exported records.',
        parameters_schema=_schema(
            route={'type': 'string', 'enum': ['snappy', 'gmsh']},
            target={'type': 'string', 'enum': ['openfoam', 'su2',
                                               'native']})),
    OperationDescriptor(
        'geometry.surface_intent.capability_table',
        'Surface capability table', 'case', ImpactClass.READ,
        summary='The one table both routes and the export read: per '
                'subject (role, mesh type or construction), route and '
                'target, whether it is supported, the condition, and the '
                'reason and remedy when it is not; with the issue codes.'),
    OperationDescriptor(
        'gmsh.periodic_pair.check', 'Periodic pair transform check', 'case',
        ImpactClass.READ,
        summary='The measurement the Gmsh Periodic page makes on OK: '
                'whether the translation or rotation of a pair carries the '
                'source surface onto the target (checked, verified, reason, '
                'measures) and the question OK would ask if it does not. '
                'Values are the pair editor\'s fields '
                '(gmsh.periodic_pairs.controls).',
        parameters_schema={'type': 'object',
                           'required': ['master_scope_token',
                                        'slave_scope_token', 'transform'],
                           'properties': {
                               'master_scope_token': _STRING,
                               'slave_scope_token': _STRING,
                               'transform': {'type': 'string', 'enum': [
                                   'translation', 'rotation',
                                   'translational', 'rotational']}},
                           'additionalProperties': True}),
)


def _done(session, command, payload, **extra) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=dict(payload or {}), **extra)


def _document(session) -> dict:
    return session.state.checkout().data()


def handlers():
    def read(session, command):
        parameters = dict(command.parameters or {})
        keys = parameters.get('geometry_ids')
        if isinstance(keys, (str, int)):
            keys = [keys]
        return _done(session, command, api.read(_document(session), keys))

    def preview(session, command):
        from foammesh.db import surface_authoring as authoring
        keys, edit = api.edit_from_parameters(command.parameters)
        data = session.state.checkout()
        found = api.plan(data, keys, edit)
        document = data.data()
        authoring.apply_plan(document, found)
        return _done(session, command, {
            'geometry_ids': keys, 'affected': list(found.affected),
            'surface_issues': api.issue_dicts(
                authoring.all_issues(document, found.affected))})

    def edit(session, command):
        from foammesh.core.facade.facade import (
            _source, _with_surface_issues, refuse_locked_edit)
        session.require_writable()
        keys, surface_edit = api.edit_from_parameters(command.parameters)
        data = session.state.checkout()
        found = api.write(data, keys, surface_edit)
        refuse_locked_edit(session, data)
        transaction = session.state.commit(
            data, action='edit surface', source=_source(command.source),
            target='geometry.items/' + ','.join(keys),
            reason=f'actor={command.actor.id}')
        latest = session.latest_change_set()
        result = _done(
            session, command,
            {'geometry_ids': keys, 'affected': list(found.affected),
             'transaction_id': transaction.tx_id,
             'change_set_id': latest['change_set_id'] if latest else None},
            changed_fields=tuple(f'geometry.items/{key}'
                                 for key in found.affected),
            invalidated_outputs=('mesh', 'quality'))
        return _with_surface_issues(session, result, list(found.affected))

    def stage_report(session, command):
        from foammesh.core.engine import surface_capabilities as capabilities
        parameters = dict(command.parameters or {})
        document = _document(session)
        configured_route, configured_target = capabilities.route_target_of(
            document)
        route = capabilities.normalise_route(
            parameters.get('route') or configured_route)
        target = capabilities.normalise_target(
            parameters.get('target') or configured_target, route)
        if route not in capabilities.ROUTES:
            raise PreconditionFailedError(
                'no meshing route is chosen: choose one on Mesh setup, or '
                'name the route', details={
                    'error': 'no_route', 'routes': list(capabilities.ROUTES)})
        assessed = capabilities.assessed_document(document)
        case_path = getattr(session, 'case_path', None)
        # DP-6071. The run's own pre-flight, builders included, so a case
        # the run refuses is never reported buildable.
        issues = capabilities.preflight(
            session.state.db, case_path, route, target, stage='run')
        built = exported = None
        if case_path is not None:
            built = capabilities.read_record(case_path)
            exported = capabilities.read_record(
                case_path, capabilities.EXPORTED_RECORD)
        return _done(session, command, {
            'route': route, 'target': target,
            'rows': capabilities.stage_report(document, route, target,
                                              case_path=case_path,
                                              issues=issues),
            'issues': [issue.to_dict() for issue in issues],
            'blocking': any(issue.blocking for issue in issues),
            'interfaces': api.interface_rows(assessed, route, target),
            'built_record': built, 'exported_record': exported})

    def capability_table(session, command):
        from foammesh.core.engine import surface_capabilities as capabilities
        return _done(session, command, {
            'rows': capabilities.capability_table(),
            'routes': list(capabilities.ROUTES),
            'targets': list(capabilities.TARGETS),
            'issue_codes': list(capabilities.ISSUE_CODES)})

    async def periodic_check(session, command):
        from .pa5_routes import _api, _guard
        values = dict(command.parameters or {})
        window = _api(session)
        return _done(session, command, _guard(
            lambda: window.periodic_pair_check(values)))

    return {
        'geometry.surface_intent.read': read,
        'geometry.surface_intent.preview': preview,
        'geometry.surface_intent.edit': edit,
        'geometry.surface_intent.stage_report': stage_report,
        'geometry.surface_intent.capability_table': capability_table,
        'gmsh.periodic_pair.check': periodic_check,
    }


__all__ = ('OPERATIONS', 'handlers', 'ValidationFailedError')
