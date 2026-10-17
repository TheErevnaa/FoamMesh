"""The shared surface names as Gmsh physical groups (Plan 42 SG13, D7).

The prepared group manifest owns the names of the surfaces; the Gmsh session
used to make its own groups that nothing seeded and the runner never read.
The open model is now seeded with one dimension-2 physical group per shared
surface group, and a rename goes back through the shared naming step.

A link file ``<case>/foammesh/gmsh/shared_groups.json`` records which session
group came from which ``patch_uuid``. Only a linked group is ever renamed to
follow the manifest; a group the user made is never touched, and a shared name
that a user group already holds is skipped rather than taken.

Curve, point and volume groups stay Gmsh-only (D7).

No Qt. The session editor is passed in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

__all__ = ['shared_surface_groups', 'links_path', 'load_links', 'save_links',
           'sync', 'origin_of']

SURFACE = 2


def links_path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / 'shared_groups.json'


def load_links(case_path) -> dict:
    """``{patch_uuid: {'dim': 2, 'tag': n}}``; unreadable is no links."""
    try:
        document = json.loads(links_path(case_path).read_text(
            encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    items = document.get('links') if isinstance(document, dict) else None
    return {str(k): v for k, v in (items or {}).items()
            if isinstance(v, dict) and isinstance(v.get('tag'), int)}


def save_links(case_path, links: dict) -> None:
    target = links_path(case_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix('.json.tmp')
    scratch.write_text(json.dumps({'schema_version': 1, 'links': links},
                                  indent=2), encoding='utf-8')
    os.replace(scratch, target)


def shared_surface_groups(prepared_geometry, surface_tags=None,
                          names=None) -> list:
    """``[{'patch_uuid', 'name', 'tags'}]`` for every placeable surface group.

    The name is the one the group publishes under (``solver_name``). A group
    the manifest cannot place on Gmsh tags is absent, as in the runner.

    WALK-2 (SG13). ``scope_surface_map`` gives *positions* among the imported
    surfaces, from 0 -- the runner reads them as indices into
    ``getEntities(2)``. *surface_tags* is that list from the open model, and
    each position becomes the tag it names; without it the positions are
    returned as they are. MEASURED on the kept Gmsh pipe: three groups at
    positions 0, 1, 2 were seeded on surfaces 1 and 2 (the first refused),
    each one surface off. *names* (``{patch_uuid: name}``, the patch records
    the shared Rename writes) wins over the prepared name, which is read only
    at the next preparation, so a rename reaches the session group now.
    """
    from .execution import _group_manifest, scope_surface_map

    if prepared_geometry is None:
        return []
    placed = scope_surface_map(prepared_geometry)
    names = names or {}
    rows = []
    for group in _group_manifest(prepared_geometry).get('groups') or ():
        if not isinstance(group, dict):
            continue
        token = str(group.get('patch_uuid') or '').strip()
        name = str(names.get(token) or group.get('solver_name')
                   or group.get('display_name') or '').strip()
        positions = [int(t) for t in placed.get(token, ())]
        if surface_tags is not None:
            positions = [int(surface_tags[i]) for i in positions
                         if 0 <= i < len(surface_tags)]
        tags = sorted(set(positions))
        if token and name and tags:
            rows.append({'patch_uuid': token, 'name': name, 'tags': tags})
    return rows


def origin_of(links: dict, dim: int, tag: int):
    """The ``patch_uuid`` a session group came from, or None (a Gmsh group)."""
    if int(dim) != SURFACE:
        return None
    for token, link in links.items():
        if link.get('tag') == int(tag):
            return token
    return None


def sync(editor, shared: list, links: dict) -> tuple:
    """Seed missing shared groups and follow renames; ``(links, report)``.

    *report* is a list of ``(action, name)`` with action one of ``added``,
    ``renamed``, ``kept`` (a user group holds the name), ``skipped`` (Gmsh
    refused the entities) and ``dropped`` (a link whose group is gone).
    """
    report: list = []
    groups = [g for g in editor.groups() if g.dim == SURFACE]
    by_tag = {g.tag: g for g in groups}
    known = {s['patch_uuid']: s for s in shared}
    links = {k: dict(v) for k, v in links.items()}
    for token in list(links):
        group = by_tag.get(links[token]['tag'])
        wanted = known.get(token)
        if (group is None or wanted is None
                or set(group.entities) != set(wanted['tags'])):
            report.append(('dropped', token))
            del links[token]
    for item in shared:
        token, name, tags = item['patch_uuid'], item['name'], item['tags']
        link = links.get(token)
        if link is not None:
            group = by_tag[link['tag']]
            if group.name != name:
                taken = any(g.name == name and g.tag != group.tag
                            for g in groups)
                if taken:
                    report.append(('kept', name))
                    continue
                editor.rename(SURFACE, group.tag, name)
                report.append(('renamed', name))
            continue
        if any(g.name == name for g in groups):
            report.append(('kept', name))
            continue
        try:
            made = editor.add(name, [(SURFACE, t) for t in tags])
        except Exception:                            # noqa: BLE001
            report.append(('skipped', name))
            continue
        links[token] = {'dim': SURFACE, 'tag': int(made.tag)}
        report.append(('added', name))
    return links, report
