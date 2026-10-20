"""Plan 46: the local API -- a program on this PC works on the open case.

Off unless asked for, in one of two ways:

* ``--api-attach`` on the command line, for this run only. A program that
  starts FoamMesh itself passes it, and nothing is stored.
* Settings > Preferences > Connections, stored, off by default.

When it is on, the window serves the ``/api/v1`` routes over the case that is
open in it (``DesktopHost``): 127.0.0.1 only, a random port, a fresh random
token for every run, and case create/open/close turned off, so a client can
never put another case in the person's window. The endpoint and the token go
into a discovery file only this user can read. The status bar shows while it
is on, and a click there turns it off.

Command line::

    FoamMesh.exe [case] --api-attach [--api-discovery FILE] [--api-allow-root DIR]...

``--api-discovery`` and ``--api-allow-root`` mean nothing without
``--api-attach``. With no ``--api-discovery`` the file is
``~/.foammesh/local-api.json``. A file left behind by a process that died
names that process's pid and a token nobody serves any more, and the next
start overwrites it.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ATTACH_FLAG = '--api-attach'
DISCOVERY_FLAG = '--api-discovery'
ALLOW_ROOT_FLAG = '--api-allow-root'
DISCOVERY_NAME = 'local-api.json'


@dataclass(frozen=True)
class Request:
    """What the command line asked for."""

    attach: bool = False
    discovery_path: Path | None = None
    allowed_roots: tuple[Path, ...] = ()


def parse_arguments(arguments: Sequence[str]) -> tuple[Request, list[str]]:
    """Take the ``--api-*`` options out of ``arguments``.

    Returns the request and the arguments that are left, in order, so the
    case path is found whether it comes before or after the options. Raises
    ``ValueError`` for an option that is missing its path.
    """
    rest: list[str] = []
    attach = False
    discovery: Path | None = None
    roots: list[Path] = []
    items = list(arguments)
    index = 0
    while index < len(items):
        item = items[index]
        name, equals, inline = item.partition('=')
        if item == ATTACH_FLAG:
            attach = True
        elif name in (DISCOVERY_FLAG, ALLOW_ROOT_FLAG):
            if equals:
                value = inline
            elif index + 1 < len(items):
                index += 1
                value = items[index]
            else:
                value = ''
            if not value:
                raise ValueError(f'{name} needs a path')
            path = Path(value).expanduser()
            if name == DISCOVERY_FLAG:
                discovery = path
            else:
                roots.append(path)
        else:
            rest.append(item)
        index += 1
    return Request(attach, discovery, tuple(roots)), rest


def default_discovery_path(settings) -> Path:
    return Path(settings.settingsPath()) / DISCOVERY_NAME


class LocalApi:
    """Starts and stops the loopback host over the window's facade."""

    def __init__(self, facade, *, discovery_path: Path,
                 allowed_roots: Sequence[Path] = (), client_trust=None):
        self._facade = facade
        #: Who may drive the API; None means the Erevnaa key a stock build trusts.
        self._client_trust = client_trust
        self.discovery_path = Path(discovery_path)
        self.allowed_roots = tuple(Path(root) for root in allowed_roots)
        self._host = None
        self._listeners: list[Callable[[], None]] = []
        #: Why the last start failed, for the person (empty when it did not).
        self.problem = ''

    @property
    def active(self) -> bool:
        return self._host is not None

    @property
    def endpoint(self) -> str | None:
        return None if self._host is None else self._host.endpoint

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener) if listener in self._listeners else None

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:                               # noqa: BLE001
                logger.debug('local API listener failed', exc_info=True)

    async def start(self) -> bool:
        """Serve; False (and ``problem`` says why) when it cannot."""
        if self._host is not None:
            return True
        from foammesh.core.facade.desktop_host import DesktopHost
        from foammesh.support.client_trust import ClientTrust
        # The discovery token only buys the signed handshake (client_trust).
        host = DesktopHost(self._facade, enabled=True,
                           allowed_case_roots=self.allowed_roots,
                           discovery_path=self.discovery_path,
                           client_trust=self._client_trust or ClientTrust.erevnaa())
        try:
            await host.start()
        except Exception as error:                          # noqa: BLE001
            self.problem = str(error) or type(error).__name__
            logger.warning('The local API could not start: %s', self.problem)
            self._changed()
            return False
        self.problem = ''
        self._host = host
        logger.info('Local API on %s (discovery file %s)', host.endpoint,
                    self.discovery_path)
        self._changed()
        return True

    async def stop(self) -> None:
        host, self._host = self._host, None
        if host is None:
            return
        try:
            await host.stop()
        finally:
            self.remove_discovery_file()
            logger.info('Local API stopped')
            self._changed()

    def remove_discovery_file(self) -> None:
        """At exit, when there is no event loop left to stop the host on."""
        try:
            self.discovery_path.unlink(missing_ok=True)
        except OSError:
            logger.debug('could not remove %s', self.discovery_path, exc_info=True)


#: The window's local API, once main() has made one.
_current: LocalApi | None = None


def current() -> LocalApi | None:
    return _current


def install(facade, settings, request: Request) -> LocalApi:
    """Make the window's controller. Starting it is ``should_start``'s call."""
    global _current
    discovery = request.discovery_path or default_discovery_path(settings)
    _current = LocalApi(facade, discovery_path=discovery,
                        allowed_roots=request.allowed_roots)
    return _current


def should_start(settings, request: Request) -> bool:
    if request.attach:
        return True
    getter = getattr(settings, 'getLocalApiEnabled', None)
    return bool(getter()) if callable(getter) else False
