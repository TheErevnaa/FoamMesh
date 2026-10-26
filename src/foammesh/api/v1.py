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
import logging
import secrets
from pathlib import Path
from uuid import uuid4

from foammesh.core.facade import (
    Actor, ActorKind, Command, CommandSource, FacadeError, FoamMeshFacade,
    SecurityPolicyError, ValidationFailedError)

logger = logging.getLogger(__name__)

_CONFLICT_CODES = {
    'revision_conflict', 'case_locked', 'interaction_busy',
    'idempotency_conflict', 'event_replay_gap'}
_AUTH_CODES = {'authorization_required'}


def create_facade_app(facade: FoamMeshFacade, *, token: str | None = None,
                      allowed_case_roots=(), actor_id: str | None = None,
                      lifecycle: bool = True, title: str = 'FoamMesh API v1',
                      client_trust=None):
    """Build the FastAPI app exposing the full ``/api/v1`` facade surface.

    ``lifecycle`` enables case create/open/close (headless serve). The desktop
    loopback disables it so a client cannot displace the foreground case.

    With ``client_trust`` (``foammesh.support.client_trust.ClientTrust``) the
    bearer ``token`` buys the signed handshake and nothing else; every other
    route needs the session token a trusted key's signature wins.
    """
    from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
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

    def _bearer(authorization):
        if authorization is None or not authorization.startswith('Bearer '):
            return None
        return authorization[len('Bearer '):]

    def _is_token(value):
        return value is not None and secrets.compare_digest(value, bearer_token)

    def _granted(value) -> bool:
        if client_trust is not None:
            return client_trust.session_valid(value)
        return bearer_token is None or _is_token(value)

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        value = _bearer(authorization)
        if _granted(value):
            return
        if client_trust is not None and bearer_token is not None and _is_token(value):
            raise HTTPException(status_code=403, detail={
                'code': 'client_not_trusted',
                'message': 'this FoamMesh answers only signed clients: '
                           'GET /api/v1/handshake, sign the challenge, '
                           'POST it back and use the session token'})
        raise HTTPException(status_code=401, detail={'code': 'unauthorized'})

    async def authenticate_pairing(authorization: str | None = Header(default=None)) -> None:
        if bearer_token is not None and not _is_token(_bearer(authorization)):
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

    # DP-6080. An operation handed a parameter it cannot read (a missing key, a
    # value of the wrong type) answered a bare 500 with no code. It is the
    # caller's input, so it is a coded 400 that names what was wrong; the
    # traceback stays in the log for the cases that are a defect after all.
    def _bad_parameters(_request, error: Exception):
        logger.warning('operation refused its parameters', exc_info=error)
        what = (f'missing parameter {error.args[0]!r}' if isinstance(error, KeyError) and error.args
                else (str(error) or type(error).__name__))
        return JSONResponse(status_code=400, content={'error': {
            'code': 'validation_failed', 'message': what,
            'details': {'exception': type(error).__name__}}})

    for _kind in (KeyError, TypeError, ValueError):
        app.add_exception_handler(_kind, _bad_parameters)

    # ---- events WebSocket (registered first so nothing shadows it) -------- #

    async def _event_stream(websocket):
        case_id = websocket.path_params['case_id']
        params = websocket.query_params
        query_token = params.get('token')
        try:
            cursor = int(params.get('after_sequence', 0))
        except (TypeError, ValueError):
            cursor = 0
        if client_trust is not None or bearer_token is not None:
            ok = (_granted(_bearer(websocket.headers.get('authorization')))
                  or (query_token is not None and _granted(query_token)))
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
        return {'status': 'ok', 'signed_clients_only': client_trust is not None}

    # ---- the signed handshake ---------------------------------------------- #

    @app.get('/api/v1/handshake')
    async def handshake_challenge(_auth=Depends(authenticate_pairing)):
        if client_trust is None:
            return {'required': False}
        return {'required': True, **client_trust.challenge()}

    @app.post('/api/v1/handshake')
    async def handshake_answer(body: dict, _auth=Depends(authenticate_pairing)):
        if client_trust is None:
            return {'required': False, 'session_token': bearer_token}
        session = client_trust.answer(body.get('challenge'), body.get('key_id'),
                                      body.get('signature'))
        if session is None:
            raise HTTPException(status_code=403, detail={
                'code': 'client_not_trusted',
                'message': "the signature is not a trusted key's, or the challenge "
                           'expired or was spent; ask for a new one'})
        return {'required': True, 'session_token': session}

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
    async def close_case(case_id: str, body: dict | None = Body(default=None),
                         _auth=Depends(authenticate)):
        if lifecycle:
            guard(case_id)
            facade.close_case(case_id)
            return {'closed': case_id}
        # DP-2764 (user, 2026-10-20). On the window's API a signed client may
        # close the case the window has open, as the window's own close does:
        # unsaved changes ask the person (*Save changed*), unless ``save``
        # answers it as the dialog's Ok (true) or Discard (false). Nothing
        # here opens a case or reaches any case but the window's.
        session = guard(case_id)
        save = (body or {}).get('save')
        if save is not None and not isinstance(save, bool):
            raise ValidationFailedError('save is true, false or left out',
                                        details={'save': save})
        closer = getattr(session.presentation_renderer, 'close_case', None)
        if client_trust is None or session.presentation is None or not callable(closer):
            _require_lifecycle()
        return await closer(save=save)

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
        session = guard(case_id)
        found = facade.slice_operations.job(job_id)
        if found.get('status') != 'not_found':
            return found
        # DP-2855/2856. A job the case's job manager ran, or a mesh run by
        # its run id (the id a snappy run answers as its job id).
        from foammesh.core.facade import window_text
        record = await asyncio.to_thread(window_text.job_record, session, job_id)
        return record if record is not None else found

    @app.post('/api/v1/cases/{case_id}/jobs/{job_id}:cancel')
    async def cancel_job(case_id: str, job_id: str, _auth=Depends(authenticate)):
        guard(case_id)
        return (await facade.execute(rest_command(
            'job.cancel', case_id, {'job_id': job_id}))).to_dict()

    @app.get('/api/v1/cases/{case_id}/jobs/{job_id}/log')
    async def job_log(case_id: str, job_id: str, tail_lines: int = 200,
                      offset: int | None = None, _auth=Depends(authenticate)):
        """DP-2763. A job's log: its last lines, or the bytes after ``offset``."""
        from foammesh.core.facade import window_text
        return window_text.job_log(guard(case_id), job_id,
                                   tail_lines=tail_lines, offset=offset)

    # ---- what the window said -------------------------------------------- #

    @app.get('/api/v1/cases/{case_id}/console')
    async def console(case_id: str, after_line: int = 0, limit: int = 500,
                      min_level: str = 'info', _auth=Depends(authenticate)):
        """DP-2762. The Console panel's lines after ``after_line``."""
        from foammesh.core.facade import window_text
        return window_text.console(guard(case_id), after_line=after_line,
                                   limit=limit, min_level=min_level)

    @app.get('/api/v1/cases/{case_id}/status')
    async def status_text(case_id: str, after: int = 0, _auth=Depends(authenticate)):
        """DP-2762. The status line, its recent messages, the viewport's
        notices and the run status strip."""
        from foammesh.core.facade import window_text
        return window_text.status(guard(case_id), after=after)

    # ---- presentation ---------------------------------------------------- #

    @app.get('/api/v1/cases/{case_id}/presentation/snapshot')
    async def presentation_snapshot(case_id: str, _auth=Depends(authenticate)):
        return guard(case_id).presentation_snapshot()

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
    from foammesh.support.client_trust import ClientTrust
    app = create_facade_app(facade, token=token, allowed_case_roots=allowed_case_roots,
                            lifecycle=True, title='FoamMesh headless API v1',
                            client_trust=ClientTrust.erevnaa())
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level='warning'))
    await server.serve()
