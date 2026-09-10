"""AF5 versioned transport: the complete ``/api/v1`` surface over the facade.

Every route drives the shared :class:`FoamMeshFacade` (§8). The same app backs
the desktop loopback transport (attached to the foreground session) and the
headless ``foammesh serve`` host, so REST/CLI/agent and the GUI all execute the
identical facade commands against one persistent case session — a REST mutation
updates the open desktop case without reopening it.

There is no raw ``path/value`` endpoint, no in-memory project session, and no
generic utility runner; public clients use semantic field IDs and the typed
operation registry only (§4.3, §4.4, §11).
"""
from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from uuid import uuid4

from foammesh.core.facade import (
    Actor, ActorKind, Command, CommandSource, FacadeError, FoamMeshFacade,
    SecurityPolicyError, ValidationFailedError)

_CONFLICT_CODES = {
    'revision_conflict', 'case_locked', 'interaction_busy',
    'idempotency_conflict', 'event_replay_gap'}
_AUTH_CODES = {'authorization_required'}


def create_facade_app(facade: FoamMeshFacade, *, token: str | None = None,
                      allowed_case_roots=(), actor_id: str | None = None,
                      lifecycle: bool = True, title: str = 'FoamMesh API v1'):
    """Build the FastAPI app exposing the full ``/api/v1`` facade surface.

    ``lifecycle`` enables case create/open/close (headless serve). The desktop
    loopback disables it so a client cannot displace the foreground case.
    """
    from fastapi import Depends, FastAPI, Header, HTTPException, Query
    from fastapi.responses import JSONResponse, Response
    from starlette.websockets import WebSocketDisconnect

    actor_id = actor_id or f'api-{secrets.token_hex(6)}'
    bearer_token = token  # server bearer; kept distinct from any query param
    allowed_roots = tuple(Path(root).resolve() for root in allowed_case_roots)
    app = FastAPI(title=title, docs_url=None, redoc_url=None)

    def path_allowed(path) -> bool:
        candidate = Path(path).resolve()
        return not allowed_roots or any(
            candidate == root or root in candidate.parents for root in allowed_roots)

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        if bearer_token is None:
            return
        if authorization is None or not secrets.compare_digest(authorization, f'Bearer {bearer_token}'):
            raise HTTPException(status_code=401, detail={'code': 'unauthorized'})

    def guard(case_id: str):
        session = facade.case(case_id)
        if not path_allowed(session.case_path):
            raise SecurityPolicyError('case is outside the configured roots')
        return session

    def rest_command(operation, case_id, parameters, *, scope='case', body=None):
        body = body or {}
        return Command(
            operation=operation, case_id=case_id, parameters=parameters or {}, scope=scope,
            actor=Actor(actor_id, ActorKind.API_CLIENT), source=CommandSource.REST,
            expected_revision=body.get('expected_revision'),
            idempotency_key=body.get('idempotency_key'),
            command_id=body.get('command_id') or str(uuid4()),
            correlation_id=body.get('correlation_id'),
            authorization=body.get('authorization'))

    @app.exception_handler(FacadeError)
    async def facade_error(_request, error: FacadeError):
        status = 409 if error.code in _CONFLICT_CODES else (
            403 if error.code in _AUTH_CODES else 400)
        return JSONResponse(status_code=status, content={'error': error.to_dict()})

    # ---- events WebSocket (registered first so nothing shadows it) -------- #

    async def _event_stream(websocket):
        case_id = websocket.path_params['case_id']
        params = websocket.query_params
        query_token = params.get('token')
        try:
            cursor = int(params.get('after_sequence', 0))
        except (TypeError, ValueError):
            cursor = 0
        if bearer_token is not None:
            header = websocket.headers.get('authorization')
            ok = ((header is not None and secrets.compare_digest(header, f'Bearer {bearer_token}'))
                  or (query_token is not None and secrets.compare_digest(query_token, bearer_token)))
            if not ok:
                await websocket.close(code=4401)
                return
        await websocket.accept()
        try:
            session = guard(case_id)
            while True:
                for item in session.events_after(cursor, limit=1000):
                    cursor = item['sequence']
                    await websocket.send_json(item)
                await asyncio.sleep(0.05)
        except WebSocketDisconnect:
            return
        except FacadeError as error:
            await websocket.send_json({'error': error.to_dict()})
            await websocket.close(code=4409)

    app.add_websocket_route('/api/v1/cases/{case_id}/events/stream', _event_stream)

    # ---- discovery ------------------------------------------------------- #

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/api/v1/system/capabilities')
    async def capabilities(_auth=Depends(authenticate)):
        return {'api_version': 'v1', 'lifecycle': lifecycle,
                'operations': [d['operation'] for d in facade.describe_operations()['operations']],
                'field_count': len(facade.fields)}

    @app.get('/api/v1/fields')
    async def fields(_auth=Depends(authenticate)):
        return facade.describe_fields()

    @app.get('/api/v1/operations')
    async def operations(_auth=Depends(authenticate)):
        return facade.describe_operations()

    @app.get('/api/v1/openapi.json')
    async def openapi(_auth=Depends(authenticate)):
        return facade.openapi()

    @app.get('/api/v1/sessions/current')
    async def current_sessions(_auth=Depends(authenticate)):
        return {'sessions': [
            {'case_id': cid, 'session_id': s.session_id, 'case_path': str(s.case_path),
             'read_only': s.read_only, 'revisions': s.revisions.to_dict()}
            for cid, s in facade._cases.items() if path_allowed(s.case_path)]}

    # ---- application scope ----------------------------------------------- #

    @app.get('/api/v1/application/snapshot')
    async def application_snapshot(_auth=Depends(authenticate)):
        return facade.application.snapshot()

    @app.post('/api/v1/application/operations/{operation_id}:execute')
    async def application_execute(operation_id: str, body: dict, _auth=Depends(authenticate)):
        return (await facade.execute(rest_command(
            operation_id, '', body.get('parameters'), scope='application', body=body))).to_dict()

    # ---- case lifecycle -------------------------------------------------- #

    def _require_lifecycle():
        if not lifecycle:
            raise SecurityPolicyError('case lifecycle is disabled on this transport')

    @app.post('/api/v1/cases:create')
    async def create_case(body: dict, _auth=Depends(authenticate)):
        _require_lifecycle()
        path = body.get('path')
        if not path or not path_allowed(path):
            raise SecurityPolicyError('path is missing or outside the configured roots')
        session = facade.create_case(path)
        return facade.snapshot(session.case_id)

    @app.post('/api/v1/cases:open')
    async def open_case(body: dict, _auth=Depends(authenticate)):
        _require_lifecycle()
        path = body.get('path')
        if not path or not path_allowed(path):
            raise SecurityPolicyError('path is missing or outside the configured roots')
        session = facade.open_case(path)
        return facade.snapshot(session.case_id)

    @app.post('/api/v1/cases/{case_id}:save')
    async def save_case(case_id: str, _auth=Depends(authenticate)):
        guard(case_id)
        return (await facade.execute(rest_command('case.save', case_id, {}))).to_dict()

    @app.post('/api/v1/cases/{case_id}:close')
    async def close_case(case_id: str, _auth=Depends(authenticate)):
        _require_lifecycle()
        guard(case_id)
        facade.close_case(case_id)
        return {'closed': case_id}

    # ---- case queries + commands ----------------------------------------- #

    @app.get('/api/v1/cases/{case_id}/snapshot')
    async def snapshot(case_id: str, _auth=Depends(authenticate)):
        session = guard(case_id)
        return Response(content=await session.snapshot_json(), media_type='application/json')

    @app.get('/api/v1/cases/{case_id}/fields/{field_id}')
    async def field(case_id: str, field_id: str, _auth=Depends(authenticate)):
        guard(case_id)
        return facade.field(case_id, field_id)

    @app.post('/api/v1/cases/{case_id}/operations/{operation_id}:execute')
    async def execute_operation(case_id: str, operation_id: str, body: dict,
                                _auth=Depends(authenticate)):
        guard(case_id)
        return (await facade.execute(rest_command(
            operation_id, case_id, body.get('parameters'), body=body))).to_dict()

    @app.post('/api/v1/cases/{case_id}/commands')
    async def command(case_id: str, body: dict, _auth=Depends(authenticate)):
        guard(case_id)
        operation = body.get('operation')
        if not isinstance(operation, str):
            raise ValidationFailedError('operation must be a string')
        return (await facade.execute(rest_command(
            operation, case_id, body.get('parameters'), body=body))).to_dict()

    @app.get('/api/v1/cases/{case_id}/history')
    async def history(case_id: str, limit: int = Query(200, ge=1, le=5000),
                      _auth=Depends(authenticate)):
        guard(case_id)
        return await facade.history(case_id, limit=limit)

    @app.get('/api/v1/cases/{case_id}/artifacts')
    async def artifacts(case_id: str, limit: int = Query(200, ge=1, le=5000),
                        _auth=Depends(authenticate)):
        guard(case_id)
        return facade.artifacts(case_id, limit=limit)

    # ---- events (replayable + WS) ---------------------------------------- #

    @app.get('/api/v1/cases/{case_id}/events')
    async def events(case_id: str, after_sequence: int = Query(0, ge=0),
                     limit: int = Query(1000, ge=1, le=1000), _auth=Depends(authenticate)):
        guard(case_id)
        return {'events': facade.case(case_id).events_after(after_sequence, limit=limit)}

    # ---- plans ----------------------------------------------------------- #

    @app.post('/api/v1/cases/{case_id}/operations/{operation_id}:plan')
    async def plan_operation(case_id: str, operation_id: str, body: dict,
                             _auth=Depends(authenticate)):
        guard(case_id)
        commands = body.get('commands') or [
            {'operation': operation_id, 'parameters': body.get('parameters') or {}}]
        return facade.validate_plan(case_id, commands)

    @app.post('/api/v1/cases/{case_id}/plans:validate')
    async def validate_plan(case_id: str, body: dict, _auth=Depends(authenticate)):
        guard(case_id)
        return facade.validate_plan(case_id, body.get('commands') or [])

    @app.get('/api/v1/cases/{case_id}/plans/{plan_id}')
    async def get_plan(case_id: str, plan_id: str, _auth=Depends(authenticate)):
        guard(case_id)
        return facade.plan(plan_id)

    @app.post('/api/v1/cases/{case_id}/plans/{plan_id}:confirm')
    async def confirm_plan(case_id: str, plan_id: str, body: dict, _auth=Depends(authenticate)):
        guard(case_id)
        return facade.confirm_plan(case_id, plan_id, body.get('digest', ''),
                                   confirmed_by=body.get('confirmed_by') or actor_id,
                                   ttl_seconds=body.get('ttl_seconds', 300))

    @app.post('/api/v1/cases/{case_id}/plans/{plan_id}:execute')
    async def execute_plan(case_id: str, plan_id: str, body: dict, _auth=Depends(authenticate)):
        guard(case_id)
        return await facade.execute_plan(case_id, plan_id, body.get('token', ''), actor_id=actor_id)

    @app.post('/api/v1/cases/{case_id}/plans/{plan_id}:cancel')
    async def cancel_plan(case_id: str, plan_id: str, _auth=Depends(authenticate)):
        guard(case_id)
        return facade.cancel_plan(case_id, plan_id)

    # ---- jobs ------------------------------------------------------------ #

    @app.get('/api/v1/cases/{case_id}/jobs/{job_id}')
    async def job(case_id: str, job_id: str, _auth=Depends(authenticate)):
        guard(case_id)
        return facade.slice_operations.job(job_id)

    @app.post('/api/v1/cases/{case_id}/jobs/{job_id}:cancel')
    async def cancel_job(case_id: str, job_id: str, _auth=Depends(authenticate)):
        guard(case_id)
        return (await facade.execute(rest_command(
            'job.cancel', case_id, {'job_id': job_id}))).to_dict()

    # ---- presentation ---------------------------------------------------- #

    @app.get('/api/v1/cases/{case_id}/presentation/snapshot')
    async def presentation_snapshot(case_id: str, _auth=Depends(authenticate)):
        session = guard(case_id)
        if session.presentation is None:
            return {'attached': False}
        return session.presentation.to_dict()

    @app.post('/api/v1/cases/{case_id}/presentation/operations/{operation_id}:execute')
    async def presentation_execute(case_id: str, operation_id: str, body: dict,
                                   _auth=Depends(authenticate)):
        guard(case_id)
        return (await facade.execute(rest_command(
            operation_id, case_id, body.get('parameters'), scope='presentation', body=body))).to_dict()

    @app.get('/api/v1/cases/{case_id}/metrics')
    async def metrics(case_id: str, _auth=Depends(authenticate)):
        return guard(case_id).monitor.snapshot()

    return app


async def serve(facade: FoamMeshFacade, *, host: str = '127.0.0.1', port: int = 8000,
                token: str | None = None, allowed_case_roots=()):  # pragma: no cover
    """Host the versioned facade app headlessly (``foammesh serve``)."""
    import uvicorn
    app = create_facade_app(facade, token=token, allowed_case_roots=allowed_case_roots,
                            lifecycle=True, title='FoamMesh headless API v1')
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level='warning'))
    await server.serve()
