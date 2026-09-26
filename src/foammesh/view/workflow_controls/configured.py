"""Whether an OPTIONAL workflow row has anything in it (Plan 32 section 4.5).

An optional row asks a question the user is allowed not to answer. Proceed on
one of them therefore has two meanings and the button cannot tell them apart on
its own: run what is here, or record that there is nothing here and move on.
The answer is a fact about *storage*, not about the page -- a page that was
never opened has no dirty state, and "the user did not visit the tab" is not
the same statement as "the user has nothing to put in it".

Until Plan 32 the only predicate was ``StepManager._layersConfigured``, which
read one collection and was consulted from three places with
``task_id == 'snappy.layers'`` written out beside each call. Plan 32 sections
4.2 and 4.3 give six rows that shape, so the special case had to become a table
or it became six special cases in three methods.
"""
from __future__ import annotations


#: Optional task id -> the storage paths that say it was configured.
#:
#: Read out of the schema, not out of the semantic field ids: these are
#: ``configurations_schema`` paths, which is what the working copy indexes and
#: what ``FieldRegistry`` keys its collections and scalars by. A path that
#: names neither is a typo that would make its task permanently unconfigured
#: and therefore permanently skipped, so
#: `tests/unit/test_every_configured_path_names_a_field_that_exists.py`
#: resolves every one of them through the registry.
#:
#: Five of the six are collections, where "configured" means at least one row.
#: `gmsh.boundary_layers` is not: Gmsh's prism stack is eight scalars behind
#: one switch (`gmsh/boundaryLayers/enabled`, default off), so the switch is
#: the question, and the seven numbers below it have working defaults that mean
#: nothing until it is on.
CONFIGURED_PATHS: dict[str, tuple[str, ...]] = {
    'snappy.layers': ('addLayers/layers',),
    # Per-surface sizes are size fields with the geometry already chosen --
    # `AREA_BY_PREFIX` files both under `gmsh.size_fields` and the page draws
    # both tables -- so either one on its own configures the row.
    'gmsh.size_fields': ('gmsh/sizeFields', 'gmsh/surfaceSizes'),
    'gmsh.curve_controls': ('gmsh/curveControls',),
    'gmsh.volume_controls': ('gmsh/volumeControls',),
    'gmsh.boundary_layers': ('gmsh/boundaryLayers/enabled',),
    'gmsh.periodic': ('gmsh/periodicPairs',),
}

#: Strings a stored scalar uses for "no". SimpleDB keeps booleans as text, so
#: ``bool(db.getValue(path))`` is ``True`` for the string ``'false'``.
_FALSE_TEXT = frozenset({'', 'false', 'no', 'off', '0', 'none'})


def is_collection_path(storage_path: str) -> bool:
    """Whether ``storage_path`` names a registered repeated collection."""
    from foammesh.core.facade.fields import REGISTRY

    return any(collection.storage_path == storage_path
               for collection in REGISTRY.collections.values())


def resolves(storage_path: str) -> bool:
    """Whether the registry knows this path, as a collection or as a scalar.

    Both indexes are asked because ``FieldRegistry.by_storage_path`` covers
    scalars only: a collection is a ``SchemaList`` and is registered under
    ``REGISTRY.collections`` keyed by collection id, so a table that names one
    would look like a typo to the scalar index alone.
    """
    from foammesh.core.facade.fields import REGISTRY

    if REGISTRY.by_storage_path(storage_path) is not None:
        return True
    return is_collection_path(storage_path)


def path_is_configured(db, storage_path: str) -> bool:
    """Whether this one path holds something the user put there."""
    if db is None:
        return False
    if is_collection_path(storage_path):
        try:
            return bool(db.getElements(storage_path))
        except Exception:  # noqa: BLE001 - a case that never wrote the table
            return False
    try:
        value = db.getValue(storage_path)
    except Exception:  # noqa: BLE001 - a case that never wrote the field
        return False
    if isinstance(value, str):
        return value.strip().lower() not in _FALSE_TEXT
    return bool(value)


def task_is_configured(db, task_id: str) -> bool:
    """Whether the optional task ``task_id`` has anything in it.

    A task this table says nothing about is **not** optional and is always
    configured. That direction matters: a missing entry must never read as an
    empty task, or a required stage would be skipped by the same press that is
    supposed to run it.
    """
    paths = CONFIGURED_PATHS.get(task_id)
    if paths is None:
        return True
    return any(path_is_configured(db, path) for path in paths)
