"""The distance ramp of a volume group, in its one store.

Plan 43 LY13 D1 (DP-2210). The ramp is ``castellation/volumeBands``: one row
per ``(distance level)`` band, named by the group it belongs to. That is the
store the guided editor shows and edits (DP-586), the store the legacy volume
dialog now writes, and the store the case builder reads first. The nested
``refinementVolumes/<id>/bands`` list is only what an old project carries; it
is moved here when the project loads (``migrateDocument``).
"""
from __future__ import annotations

PATH = 'castellation/volumeBands'


def group_rows(db, group: str) -> list[tuple[float, int]]:
    """The ``(distance, level)`` rows of ``group``, nearest first."""
    rows = []
    for band in db.getElements(PATH).values():
        if str(band.value('groupName')) == group:
            rows.append((float(band.value('distance')),
                         int(band.value('level'))))
    return sorted(rows)


def write_group_rows(db, replace: tuple[str, ...], group: str,
                     rows: list[tuple[float, int]]) -> None:
    """Replace the rows of every group in ``replace`` with ``rows`` for ``group``.

    ``replace`` carries the group's old name as well as its new one, so a
    rename moves the ramp instead of leaving it behind.
    """
    names = {str(name) for name in replace} | {str(group)}
    db.removeElementsByFilter(
        PATH, lambda _key, element: str(element['groupName']) in names)
    for distance, level in rows:
        _, band = db.addNewElement(PATH)
        band.setValue('groupName', group)
        band.setValue('distance', str(distance))
        band.setValue('level', str(level))


def migrate_legacy_bands(castellation: dict) -> None:
    """Move every ``refinementVolumes/<id>/bands`` ramp into ``volumeBands``.

    A document-level pass on the plain saved dict. A group that already has
    live rows keeps them (the live editor wins) and its legacy rows are
    dropped; a volume with no group name has nowhere to be named, so its rows
    stay for the case builder's fallback.
    """
    volumes = castellation.get('refinementVolumes')
    if not isinstance(volumes, dict):
        return
    live = castellation.get('volumeBands')
    if not isinstance(live, dict):
        live = {}
    named = {str(row.get('groupName', '')) for row in live.values()
             if isinstance(row, dict)}
    moved = False
    for volume in volumes.values():
        if not isinstance(volume, dict):
            continue
        bands = volume.get('bands')
        group = str(volume.get('groupName') or '')
        if not isinstance(bands, dict) or not bands or not group.strip():
            continue
        if group not in named:
            def order(key):
                try:
                    return (0, int(str(key)))
                except ValueError:
                    return (1, 0)
            for key in sorted(bands, key=order):
                band = bands[key]
                if not isinstance(band, dict):
                    continue
                next_key = str(max((int(k) for k in live if str(k).isdigit()),
                                   default=0) + 1)
                live[next_key] = {'groupName': group,
                                  'distance': band.get('distance', 1.0),
                                  'level': band.get('level', 1)}
                moved = True
            named.add(group)
        volume.pop('bands', None)
    if moved or 'volumeBands' in castellation:
        castellation['volumeBands'] = live
