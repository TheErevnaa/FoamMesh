"""Reconcile the AF2 field registry against the AF0 source inventory.

The AF0 coverage manifest (``plans/facade_coverage.json``) enumerates every
configuration-schema path found in source. AF2 must prove the field registry
covers all of them, so no editable schema field is silently left without a
semantic descriptor. Full 100% GUI/action coverage is an AF8 gate; this module
provides the schema-field slice of that gate now.
"""
from __future__ import annotations

import json
from pathlib import Path

from .fields import REGISTRY, build_field_registry
from .field_adapters import build_entity_adapters


def schema_storage_paths() -> set[str]:
    """Every schema storage path the registry knows (scalars + entity fields).

    Collection element paths carry the ``{id}`` template used by the AF0
    inventory, e.g. ``addLayers/layers/{id}/expansionRatio``.
    """
    registry = build_field_registry()
    paths = {descriptor.storage_path for descriptor in registry.descriptors()}
    collections = dict(registry.collections)
    # C31-08. Rows repeated inside one element -- a volume refinement's
    # ``(distance level)`` bands -- carry descriptors like any other field;
    # they are simply not addressable as entities of their own, so they are
    # held apart from the operation-bearing collections. Their storage paths
    # are covered here, or the gate would read a field with a descriptor as a
    # field with none.
    collections.update(registry.nested_collections)
    for adapter in build_entity_adapters(collections).values():
        for element in adapter.fields.values():
            paths.add(f'{adapter.storage_path}/{{id}}/{element.relative_path}')
    return paths


def inventory_configuration_paths(coverage_path: str | Path) -> set[str]:
    document = json.loads(Path(coverage_path).read_text(encoding='utf-8'))
    return {item['name']
            for item in document.get('items', [])
            if item.get('kind') == 'configuration_schema_path'}


def missing_from_registry(coverage_path: str | Path) -> list[str]:
    """Inventory schema paths that have no registered descriptor (should be empty).

    A vector composite in the inventory (``.../point``) is covered when the
    registry exposes its scalar components (``.../point/x`` etc.), which is a
    finer-grained descriptor, not a gap.
    """
    inventory = inventory_configuration_paths(coverage_path)
    covered = schema_storage_paths()
    return sorted(path for path in inventory
                  if path not in covered
                  and not any(item.startswith(path + '/') for item in covered))


def coverage_summary(coverage_path: str | Path) -> dict:
    inventory = inventory_configuration_paths(coverage_path)
    return {
        'inventory_paths': len(inventory),
        'registry_scalar_fields': len(REGISTRY),
        'registry_collections': len(REGISTRY.collections),
        'missing': missing_from_registry(coverage_path),
    }
