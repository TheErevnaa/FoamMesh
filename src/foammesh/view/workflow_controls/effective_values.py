"""What an *Auto* field comes to, in the words the field shows.

Plan 40 UXB2 (#38). The size fields that are left unset are resolved by the one
place that proposes sizes (``core/sizing/derive.py``); this only reads the
proposal and spells it. A page with a better answer overrides
``EngineTaskPage.effective_values``; a field nobody resolves says whose default
applies (``field_widgets.auto_text``).
"""
from __future__ import annotations

#: Registry field -> the attribute of the size proposal it comes to.
SIZE_FIELDS = {
    'meshing.base_grid.target_cell_size': 'background_cell',
    'gmsh.global_sizing.target_size': 'gmsh_target_size',
    'gmsh.global_sizing.minimum_size': 'gmsh_minimum_size',
}

_TYPED = {
    'meshing.base_grid.target_cell_size': 'background_cell',
    'gmsh.global_sizing.target_size': 'gmsh_target_size',
    'gmsh.global_sizing.minimum_size': 'gmsh_minimum_size',
}


def model_bounds():
    """The model's extent as ``(xmin, xmax, ymin, ymax, zmin, zmax)``, or None."""
    try:
        from foammesh.app import app
        manager = getattr(getattr(app, 'window', None), 'geometryManager', None)
        surfaces = getattr(manager, 'getSurfaceBounds', None)
        extent = surfaces() if surfaces is not None else None
        return None if extent is None else tuple(extent.toTuple())
    except Exception:                                       # noqa: BLE001
        return None


def length_words(metres: float) -> str:
    """A length with 3 significant figures in a unit a person would say."""
    if metres >= 1.0:
        return f'{metres:.3g} m'
    if metres >= 1.0e-3:
        return f'{metres * 1.0e3:.3g} mm'
    return f'{metres * 1.0e6:.3g} µm'


def size_effective_texts(client, field_ids) -> dict:
    """``{field id: '15.4 mm'}`` for the size fields among *field_ids*."""
    wanted = [item for item in field_ids if item in SIZE_FIELDS]
    bounds = model_bounds()
    if not wanted or bounds is None:
        return {}
    try:
        from foammesh.core.sizing import derive_sizes
        values = client.field_values(tuple(_TYPED) + ('mesh.size_preset',))
        typed = {name: values.get(field) for field, name in _TYPED.items()
                 if values.get(field) not in (None, '')}
        proposal = derive_sizes(values.get('mesh.size_preset'), bounds,
                                typed=typed)
    except Exception:                                       # noqa: BLE001
        return {}
    if proposal is None:
        return {}
    return {field: length_words(getattr(proposal, SIZE_FIELDS[field]).value)
            for field in wanted}


def refresh_scene(client=None) -> None:
    """Tell the quantity evaluator what ``diag``, ``bbox_x/y/z`` and ``cell`` mean.

    Plan 40 UXD6. Best effort: with no geometry loaded the names stay unknown
    and an expression that uses one says so in place.
    """
    from foammesh.core import quantity_expr
    bounds = model_bounds()
    if bounds is None:
        return
    xmin, xmax, ymin, ymax, zmin, zmax = (float(v) for v in bounds)
    cell = None
    try:
        from foammesh.core.sizing import derive_sizes
        if client is None:
            from foammesh.app import app
            client = app.facadeClient
        values = client.field_values(('mesh.size_preset',))
        proposal = derive_sizes(values.get('mesh.size_preset'), bounds, typed={})
        cell = None if proposal is None else float(proposal.background_cell.value)
    except Exception:                                       # noqa: BLE001
        cell = None
    quantity_expr.set_scene(
        bbox=(xmax - xmin, ymax - ymin, zmax - zmin), cell=cell)
