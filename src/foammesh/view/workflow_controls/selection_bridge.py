"""Stable-ID bridge between engine scope tables and canonical VTK datasets."""
from __future__ import annotations

from foammesh.rendering.canonical_mesh_actor import isolate_stable_ids
from foammesh.rendering.selection_capabilities import capability_document


class CanonicalSelectionBridge:
    def __init__(self, *, pick_round_trip_qualified=False, service=None):
        self.capabilities = capability_document(
            pick_round_trip_qualified=pick_round_trip_qualified)
        self.service = service

    def isolate(self, dataset, stable_ids, *, entity_kind='volume_cell'):
        return isolate_stable_ids(dataset, stable_ids, entity_kind=entity_kind)

    def picking_available(self):
        return self.capabilities['capabilities'][3]['available']

    def select(self, stable_ids):
        if self.service is None:
            return tuple(stable_ids)
        self.service.select(stable_ids)
        return self.service.snapshot().selected_ids

    def picked(self, stable_ids):
        if self.service is None:
            return tuple(stable_ids)
        return self.service.viewport_picked(stable_ids)
