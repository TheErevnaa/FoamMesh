"""The background cell and RAM estimate, as a label a page can carry.

Plan 37 #7. The guided base-grid and castellation pages and the legacy
castellation page say the same thing, from the same source:
``core.mesh.background_estimate`` over the case as it is being edited and the
surfaces' extent from the viewport -- the extent a run hands the case builder.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.core.mesh import background_estimate


def geometry_bounds():
    """The surfaces' extent in the viewport as six numbers, or ``None``."""
    try:
        from foammesh.app import app

        window = app.window
        manager = getattr(window, 'geometryManager', None) if window else None
        surfaces = getattr(manager, 'getSurfaceBounds', None)
        extent = surfaces() if surfaces is not None else None
        return extent.toTuple() if extent is not None else None
    except Exception:                                       # noqa: BLE001
        return None


def _storable(value):
    value = getattr(value, 'value', value)
    if isinstance(value, bool) or value is None:
        return value
    return str(value)


def overlay(db, pending) -> None:
    """Write the page's unsaved edits into the detached copy ``db``, so the
    estimate follows what is typed before Update."""
    if not pending:
        return
    from foammesh.core.facade import FIELD_REGISTRY

    for field_id, value in dict(pending).items():
        try:
            path = FIELD_REGISTRY.get(field_id).storage_path
        except Exception:                                   # noqa: BLE001
            continue
        if not path or value in (None, ''):
            continue
        try:
            db.setValue(path, _storable(value))
        except Exception:                                   # noqa: BLE001
            continue


def current(client=None, pending=None, db=None):
    """The estimate for the open case with ``pending`` edits laid over it."""
    if db is None:
        try:
            if client is None:
                from foammesh.app import app

                client = app.facadeClient
            db = client.checkout()
        except Exception:                                   # noqa: BLE001
            return None
        overlay(db, pending)
    return background_estimate.estimate(db, geometry_bounds())


def prepare(label: QLabel, object_name: str) -> QLabel:
    """Name and wrap a page's estimate label; it stays hidden until there is
    an estimate to show."""
    label.setVisible(False)
    label.setObjectName(object_name)
    label.setWordWrap(True)
    label.setAccessibleName('Background cell and memory estimate')
    return label


def show(label, client=None, pending=None, db=None):
    """Fill ``label`` and return the estimate it shows."""
    if label is None:
        return None
    found = current(client, pending, db)
    # Plan 33 check 1: the settings column holds no standing prose. With no
    # case or no geometry there is nothing to count, so nothing is said;
    # once there is, the count is what the sizing above it costs.
    label.setText(background_estimate.describe(found) if found else '')
    label.setVisible(found is not None)
    label.setProperty('warning', bool(found is not None and found.over_ram))
    return found
