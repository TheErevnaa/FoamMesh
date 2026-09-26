"""Which regions and cell zones the viewport shows: the Region picker's rules.

DP-711 (viewport audit 0925 F1). A conjugate case is a fluid and a solid, a
Gmsh tee is two bodies, and the question a user asks of both is "show me just
this one" -- which, with only per-patch eyes, meant hunting the right dozen
rows. The picker lists All, each region (when there is more than one) and
each volume part (cell zone, or a region recovered from its seed point), and
turns ticks into per-actor visibility here, free of any widget, so the rule is
testable on its own.

The rule, for each region:

* a region that is unticked hides everything it owns;
* a region whose volume parts are all ticked (or which has none) shows what
  it showed before the picker was first used;
* a region with only some parts ticked shows exactly those parts' volumes --
  its whole-volume actor, patches and face zones are hidden, because each of
  them is drawn across the unticked parts too.
"""
from __future__ import annotations

ALL = 'all'
VOLUME_CATEGORIES = ('cellZones', 'regions')


def region_key(region: str) -> str:
    return f'region:{region}'


def part_key(actor_id: str) -> str:
    return f'part:{actor_id}'


def _category(actor_id: str) -> str:
    parts = str(actor_id).split(':')
    return parts[-2] if len(parts) >= 2 else ''


def volume_parts(ids, zone_ids) -> list[str]:
    zones = set(zone_ids)
    return [key for key in ids
            if key in zones and _category(key) in VOLUME_CATEGORIES]


def picker_entries(region_ids: dict, zone_ids) -> list[tuple[str, str, int]]:
    """``(key, label, depth)`` rows below All; empty when there is no choice."""
    multi = len(region_ids) > 1
    entries = []
    for region, ids in region_ids.items():
        if multi:
            entries.append((region_key(region), region or '(default)', 0))
        for actor_id in volume_parts(ids, zone_ids):
            name = str(actor_id).split(':')[-1]
            entries.append((part_key(actor_id), name, 1 if multi else 0))
    return entries if len(entries) > 1 else []


def picker_visibility(region_ids: dict, zone_ids, checked,
                      baseline: dict) -> dict[str, bool]:
    """Per-actor visibility for the ticked *checked* keys."""
    checked = set(checked)
    multi = len(region_ids) > 1
    result: dict[str, bool] = {}
    for region, ids in region_ids.items():
        if multi and region_key(region) not in checked:
            result.update({key: False for key in ids})
            continue
        parts = volume_parts(ids, zone_ids)
        chosen = {key for key in parts if part_key(key) in checked}
        if not parts or len(chosen) == len(parts):
            result.update({key: bool(baseline.get(key, True)) for key in ids})
            continue
        for key in ids:
            result[key] = key in chosen
    return result


class RegionFilter:
    """The picker's state: what is ticked and what to go back to.

    The first narrowing remembers every actor's visibility, so ticking All
    again returns the scene the user had -- including patches they had hidden
    by hand -- rather than switching every part on.
    """

    def __init__(self):
        self._baseline: dict[str, bool] | None = None
        self._checked: set[str] | None = None

    def reset(self):
        self._baseline = None
        self._checked = None

    def isFiltering(self) -> bool:
        return self._baseline is not None

    def checked(self, entries) -> set[str]:
        keys = {key for key, _label, _depth in entries}
        if self._checked is None:
            return keys
        return self._checked & keys

    def apply(self, region_ids: dict, zone_ids, entries, checked,
              current: dict) -> dict[str, bool]:
        keys = {key for key, _label, _depth in entries}
        checked = set(checked) & keys
        if self._baseline is None:
            self._baseline = dict(current)
        mapping = picker_visibility(region_ids, zone_ids, checked,
                                    self._baseline)
        if checked == keys:
            # Everything ticked is the scene as it was; stop remembering.
            self.reset()
        else:
            self._checked = checked
        return mapping
