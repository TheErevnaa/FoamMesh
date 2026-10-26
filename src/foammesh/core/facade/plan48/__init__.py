"""Plan 48 local-API routes, one file per package (seam).

A package adds its operations in ``<package>_routes.py`` in this folder and
edits neither ``operations.py`` nor ``domain_operations.py``. The module
defines either or both of:

``OPERATIONS``
    a tuple of ``OperationDescriptor`` (what the local API publishes).
``handlers()``
    a function returning ``{operation id: handler}``; a handler is called
    exactly as the ones in ``DomainOperations.handlers()`` are, as
    ``handler(session, command)``.

Discovery imports every module named ``*_routes`` here, in name order. A
module without those names contributes nothing. ``build_operation_registry``
registers the descriptors and ``DomainOperations.handlers()`` adds the
handlers, so the operations reach the same registry, scheduler and OpenAPI
paths as every other operation, and the facade coverage file picks them up
when it is regenerated.
"""
from __future__ import annotations

import importlib
import pkgutil


def route_modules() -> list[str]:
    """Names of the ``*_routes`` modules in this package, sorted."""
    return sorted(info.name for info in pkgutil.iter_modules(__path__)
                  if info.name.endswith('_routes')
                  and not info.name.startswith('_'))


def _modules():
    for name in route_modules():
        yield name, importlib.import_module(f'{__name__}.{name}')


def discover_descriptors() -> tuple:
    """Every ``OperationDescriptor`` the route modules declare."""
    found = []
    for _name, module in _modules():
        found.extend(getattr(module, 'OPERATIONS', ()) or ())
    return tuple(found)


def discover_handlers() -> dict:
    """Every ``{operation: handler}`` the route modules declare."""
    table: dict = {}
    for name, module in _modules():
        factory = getattr(module, 'handlers', None)
        if not callable(factory):
            continue
        for operation, handler in factory().items():
            if operation in table:
                raise RuntimeError(
                    f'{operation} is declared twice in plan48 routes '
                    f'(again in {name})')
            table[operation] = handler
    return table
