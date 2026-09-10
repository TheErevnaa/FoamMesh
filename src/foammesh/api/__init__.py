#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""FoamMesh API (FastAPI) — versioned ``/api/v1`` over the facade.

REST + WebSocket drive the same :class:`FoamMeshFacade` the GUI and CLI use, so
GUI/API/CLI/agent all execute one set of typed facade commands against one
persistent case session (§8). ``create_app`` builds the versioned app with
persistent session hosting (no in-memory shadow projects).

Optional ``[api]`` extra (FastAPI is imported lazily).
"""

__all__ = ['create_app', 'create_facade_app']


def create_facade_app(facade=None, **kwargs):
    """Build the versioned ``/api/v1`` app over a facade (fresh one if omitted)."""
    from foammesh.core.facade import FoamMeshFacade
    from .v1 import create_facade_app as _build
    return _build(facade or FoamMeshFacade(), **kwargs)


def create_app(facade=None, **kwargs):
    """Default API app: the versioned facade transport with persistent hosting."""
    return create_facade_app(facade, **kwargs)
