"""AF7 transports for the vendor-neutral reference agent client.

The same :class:`AgentClient` drives an in-process :class:`FoamMeshFacade` or a
running ``/api/v1`` server. Neither transport imports an LLM SDK or a model
vendor — they expose only the facade's discovery, planning, confirmation,
execution, streaming, and cancellation surface (§7, §10).
"""
from __future__ import annotations

from typing import Protocol


class FacadeTransport(Protocol):
    """The minimal surface an agent needs to drive FoamMesh deterministically."""

    async def capabilities(self) -> dict: ...
    async def fields(self) -> dict: ...
    async def operations(self) -> dict: ...
    async def snapshot(self, case_id: str) -> dict: ...
    async def plan(self, case_id: str, commands: list) -> dict: ...
    async def confirm(self, case_id: str, plan_id: str, digest: str, *, confirmed_by: str) -> dict: ...
    async def execute(self, case_id: str, plan_id: str, token: str) -> dict: ...
    async def events(self, case_id: str, after_sequence: int) -> list: ...
    async def cancel(self, case_id: str, plan_id: str) -> dict: ...
    async def close(self, case_id: str) -> dict: ...
    async def create(self, path: str) -> dict: ...
    async def open(self, path: str) -> dict: ...


class InProcessTransport:
    """Drive a facade directly — the desktop loopback / embedded-test path."""

    def __init__(self, facade, *, actor_id: str = 'reference-agent'):
        self._facade = facade
        self._actor_id = actor_id

    async def capabilities(self) -> dict:
        return {'api_version': 'v1',
                'operations': [d['operation'] for d in self._facade.describe_operations()['operations']],
                'field_count': len(self._facade.fields)}

    async def fields(self) -> dict:
        return self._facade.describe_fields()

    async def operations(self) -> dict:
        return self._facade.describe_operations()

    async def snapshot(self, case_id: str) -> dict:
        return self._facade.snapshot(case_id)

    async def plan(self, case_id: str, commands: list) -> dict:
        return self._facade.validate_plan(case_id, commands)

    async def confirm(self, case_id: str, plan_id: str, digest: str, *, confirmed_by: str) -> dict:
        return self._facade.confirm_plan(case_id, plan_id, digest, confirmed_by=confirmed_by)

    async def execute(self, case_id: str, plan_id: str, token: str) -> dict:
        return await self._facade.execute_plan(case_id, plan_id, token, actor_id=self._actor_id)

    async def events(self, case_id: str, after_sequence: int) -> list:
        return self._facade.case(case_id).events_after(after_sequence)

    async def cancel(self, case_id: str, plan_id: str) -> dict:
        return self._facade.cancel_plan(case_id, plan_id)

    async def close(self, case_id: str) -> dict:
        self._facade.close_case(case_id)
        return {'closed': True, 'case_id': case_id}

    async def create(self, path: str) -> dict:
        session = self._facade.create_case(path)
        return self._facade.snapshot(session.case_id)

    async def open(self, path: str) -> dict:
        session = self._facade.open_case(path)
        return self._facade.snapshot(session.case_id)


class RestTransport:
    """Drive a running ``/api/v1`` server over HTTP (any host, any framework)."""

    def __init__(self, client, *, base: str = ''):
        # ``client`` is an httpx.AsyncClient (or compatible) already carrying the
        # bearer token; ``base`` is prepended to every route.
        self._client = client
        self._base = base.rstrip('/')

    def _url(self, path: str) -> str:
        return f'{self._base}{path}'

    async def _get(self, path: str) -> dict:
        response = await self._client.get(self._url(path))
        response.raise_for_status()
        return response.json()

    async def _post(self, path: str, payload: dict | None = None) -> dict:
        response = await self._client.post(self._url(path), json=payload or {})
        response.raise_for_status()
        return response.json()

    async def capabilities(self) -> dict:
        return await self._get('/api/v1/system/capabilities')

    async def fields(self) -> dict:
        return await self._get('/api/v1/fields')

    async def operations(self) -> dict:
        return await self._get('/api/v1/operations')

    async def snapshot(self, case_id: str) -> dict:
        return await self._get(f'/api/v1/cases/{case_id}/snapshot')

    async def plan(self, case_id: str, commands: list) -> dict:
        return await self._post(f'/api/v1/cases/{case_id}/plans:validate', {'commands': commands})

    async def confirm(self, case_id: str, plan_id: str, digest: str, *, confirmed_by: str) -> dict:
        return await self._post(f'/api/v1/cases/{case_id}/plans/{plan_id}:confirm',
                                {'digest': digest, 'confirmed_by': confirmed_by})

    async def execute(self, case_id: str, plan_id: str, token: str) -> dict:
        return await self._post(f'/api/v1/cases/{case_id}/plans/{plan_id}:execute', {'token': token})

    async def events(self, case_id: str, after_sequence: int) -> list:
        payload = await self._get(
            f'/api/v1/cases/{case_id}/events?after_sequence={after_sequence}')
        return payload['events']

    async def cancel(self, case_id: str, plan_id: str) -> dict:
        return await self._post(f'/api/v1/cases/{case_id}/plans/{plan_id}:cancel')

    async def close(self, case_id: str) -> dict:
        return await self._post(f'/api/v1/cases/{case_id}:close')

    async def create(self, path: str) -> dict:
        return await self._post('/api/v1/cases:create', {'path': path})

    async def open(self, path: str) -> dict:
        return await self._post('/api/v1/cases:open', {'path': path})
