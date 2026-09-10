"""Truthful interaction capability registry for canonical mixed meshes.

The registry deliberately separates always-available data interactions from
viewer-dependent picking.  Callers can therefore keep coordinate/table/export
fallbacks reachable when a richer VTK interaction has not been qualified.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class SelectionLevel(IntEnum):
    VIEW = 0
    COLOUR_AND_FILTER = 1
    TABLE_ISOLATION = 2
    PICK = 3
    DRAW_SCOPE = 4
    TOPOLOGY_EDIT = 5


@dataclass(frozen=True)
class SelectionCapability:
    level: SelectionLevel
    capability_id: str
    display_name: str
    available: bool
    reason: str
    fallbacks: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            'level': int(self.level),
            'capability_id': self.capability_id,
            'display_name': self.display_name,
            'available': self.available,
            'reason': self.reason,
            'fallbacks': list(self.fallbacks),
        }


def canonical_selection_capabilities(*, pick_round_trip_qualified: bool = False,
                                     drawing_qualified: bool = False,
                                     topology_edit_qualified: bool = False
                                     ) -> tuple[SelectionCapability, ...]:
    """Return V0-V5 in stable order, with unqualified features disabled."""
    table_fallbacks = ('coordinate_entry', 'stable_id_table', 'csv', 'json', 'vtu')
    return (
        SelectionCapability(SelectionLevel.VIEW, 'canonical.view',
                            'Canonical mesh view', True,
                            'Canonical triangle/quad boundaries and mixed volume cells render.'),
        SelectionCapability(SelectionLevel.COLOUR_AND_FILTER, 'canonical.filter',
                            'Colour and threshold', True,
                            'Patch, region, cell type, and numeric arrays are stable.'),
        SelectionCapability(SelectionLevel.TABLE_ISOLATION, 'canonical.table_isolate',
                            'Stable-ID table isolation', True,
                            'Table rows isolate deterministic canonical IDs.'),
        SelectionCapability(SelectionLevel.PICK, 'canonical.pick', 'Viewer picking',
                            pick_round_trip_qualified,
                            ('Pick-to-stable-ID round trip is qualified.'
                             if pick_round_trip_qualified else
                             'Pick-to-stable-ID round trip is not qualified.'),
                            table_fallbacks),
        SelectionCapability(SelectionLevel.DRAW_SCOPE, 'canonical.draw_scope',
                            'Draw selection scope', drawing_qualified,
                            ('Draw-to-stable-ID round trip is qualified.'
                             if drawing_qualified else
                             'Drawing selection is not qualified.'), table_fallbacks),
        SelectionCapability(SelectionLevel.TOPOLOGY_EDIT, 'canonical.topology_edit',
                            'Topology editing', topology_edit_qualified,
                            ('Topology edits preserve canonical identity.'
                             if topology_edit_qualified else
                             'Canonical topology editing is not supported.'), table_fallbacks),
    )


def capability_document(**qualification) -> dict:
    capabilities = canonical_selection_capabilities(**qualification)
    return {
        'schema_version': 1,
        'capabilities': [item.to_dict() for item in capabilities],
        'mandatory_levels': [0, 1, 2],
        'fallbacks': ['coordinate_entry', 'stable_id_table', 'csv', 'json', 'vtu'],
    }

