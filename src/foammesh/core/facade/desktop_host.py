"""Optional loopback transport attached to the in-process desktop facade.

The host is deliberately small in AF1: it exposes snapshots, typed commands,
and retained events.  It never creates or opens a second project session.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SecurityPolicyError
from .facade import FoamMeshFacade


def _is_loopback(host: str) -> bool:
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass
class DesktopHost:
    facade: FoamMeshFacade
    host: str = '127.0.0.1'
    port: int = 0
    enabled: bool = False
    allowed_case_roots: tuple[Path, ...] = field(default_factory=tuple)
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    actor_id: str = field(default_factory=lambda: f'desktop-api-{secrets.token_hex(6)}')
    discovery_path: Path | str | None = None

    def __post_init__(self) -> None:
        if not _is_loopback(self.host):
            raise SecurityPolicyError('AF1 desktop transport must bind to loopback', details={
                'host': self.host,
            })
        self.allowed_case_roots = tuple(Path(root).resolve() for root in self.allowed_case_roots)
        self._server = None
        self._serve_task: asyncio.Task | None = None

    def case_allowed(self, path: str | Path) -> bool:
        candidate = Path(path).resolve()
        return not self.allowed_case_roots or any(
            candidate == root or root in candidate.parents for root in self.allowed_case_roots)

    def build_app(self):
        """The desktop loopback transport is the versioned facade app with case
        lifecycle disabled — a client attaches to the foreground session and can
        never create, open, or close the desktop's case (§9)."""
        try:
            from foammesh.api.v1 import create_facade_app
        except ImportError as error:  # pragma: no cover - depends on optional api extra
            raise RuntimeError('desktop API requires the FoamMesh api dependencies') from error
        return create_facade_app(
            self.facade, token=self.token, allowed_case_roots=self.allowed_case_roots,
            actor_id=self.actor_id, lifecycle=False, title='FoamMesh desktop facade')

    @property
    def bound_port(self) -> int | None:
        """Actual serving port once started (differs from `port` when 0)."""
        if self._server is None or not self._server.started:
            return None
        servers = getattr(self._server, 'servers', None) or []
        for server in servers:
            for socket_object in server.sockets or []:
                return socket_object.getsockname()[1]
        return None

    @property
    def endpoint(self) -> str | None:
        port = self.bound_port
        return None if port is None else f'http://{self.host}:{port}'

    async def start(self) -> None:
        if not self.enabled:
            raise RuntimeError('desktop transport is disabled')
        if self._serve_task is not None:
            return
        try:
            import uvicorn
        except ImportError as error:  # pragma: no cover
            raise RuntimeError('desktop API requires the FoamMesh api dependencies') from error
        self._server = uvicorn.Server(uvicorn.Config(
            self.build_app(), host=self.host, port=self.port, loop='asyncio', log_level='warning'))
        self._serve_task = asyncio.create_task(self._server.serve(), name='foammesh-desktop-api')
        while not self._server.started:
            if self._serve_task.done():
                self._serve_task.result()
                raise RuntimeError('desktop API server exited during startup')
            await asyncio.sleep(0.01)
        if self.discovery_path is not None:
            self._write_discovery_file()

    async def stop(self) -> None:
        if self._serve_task is None:
            return
        self._remove_discovery_file()
        self._server.should_exit = True
        await self._serve_task
        self._serve_task = None
        self._server = None

    def _write_discovery_file(self) -> None:
        """Publish the endpoint through a user-restricted discovery file (§9)."""
        path = Path(self.discovery_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'endpoint': self.endpoint, 'token': self.token, 'pid': os.getpid()}
        temporary = path.with_suffix('.tmp')
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + '\n')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_discovery_file(self) -> None:
        if self.discovery_path is not None:
            Path(self.discovery_path).unlink(missing_ok=True)
