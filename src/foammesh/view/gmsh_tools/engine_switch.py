"""The one place the Gmsh tool dock asks which engine is active (Plan 48 GM11b).

Every Gmsh-only entry the dock mounts -- the rail button, the menu items, the
shortcuts -- is shown while the case is on the Gmsh route and gone otherwise
(R6).  The dock reads the engine here, through the application's
:class:`~foammesh.view.engine_context.EngineContext`, and nowhere else, so
this is the single switch the dock adds to the view.
"""
from __future__ import annotations

from foammesh.view import engine_context as ec


def context(app=None):
    """The application's engine context."""
    return ec.engine_context(app)


def on_gmsh(engine_context) -> bool:
    """True while the engine in *engine_context* is Gmsh."""
    return engine_context.engine == ec.GMSH


def follow(engine_context, apply) -> None:
    """Call ``apply(on_gmsh)`` now and whenever the engine changes."""
    engine_context.engineChanged.connect(
        lambda _engine: apply(on_gmsh(engine_context)))
    apply(on_gmsh(engine_context))
