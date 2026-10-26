"""DP-6071/DP-6072. A sheet standing on a wall cannot be carried into layers.

The Gmsh runner grows a boundary layer by carving it out of the volume and
rebuilding the core from the layer's inner surfaces. An internal face or a
blocking baffle embedded in that volume is carried into the core only when it
is clear of every wall: a sheet whose edge is on a wall has nothing to stand
on there. The runner refuses that inside the job
(``resources/gmsh/stages/roles.py`` ``RolesStage.sheets_to_carry``), after
WSL has booted and the geometry was imported, and the stage report used to
call such a case buildable.

This predicts the same refusal from the project and the prepared revision,
for the pre-flight (``surface_capabilities.gate``) and the stage report. It
is conservative: it says nothing unless every fact is on hand and points the
same way --

* boundary layers are switched on for a volume mesh and grow on something;
* the prepared revision counted one volume (the runner then carves it);
* the sheet is a valid internal face or blocking baffle of a file that holds
  no surface of another role (a free sheet, as the runner sets aside; a face
  of a solid is not one);
* its own triangles have an open rim (a closed surface bounds a volume of
  its own and is not embedded); and
* a rim point lies on another prepared face, within the tolerance the
  runner imprints rims with (``IMPRINT_TOLERANCE`` of the model diagonal) or
  the tessellation's deflection.

A floating sheet, a file that cannot be read or a face whose triangles
cannot be told apart is never refused here; the runner keeps its own check.
Nothing here imports Gmsh.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from foammesh.db import surface_intent as model

#: ``resources/gmsh/stages/roles.py`` ``IMPRINT_TOLERANCE``: how near a rim
#: must be to a wall, as a fraction of the model diagonal, to stand on it.
IMPRINT_TOLERANCE = 1e-6

_SHEET_ROLES = (model.INTERNAL, model.BAFFLE)


def refusal_text(sheet: str, walls) -> tuple[str, str]:
    """``(reason, remedy)`` of the runner's refusal for one *sheet*.

    The runner's sentence (``RolesStage.sheets_to_carry``), split where the
    remedy starts; ``test_dp6071_*`` holds the two to the same words.
    """
    reason = ('boundary layers cannot be grown with ' + repr(str(sheet))
              + ' in the volume: it is an internal face or blocking baffle '
              'standing on ' + ', '.join(walls) + '. The layer is carved out '
              'of the volume and the core rebuilt from its inner surfaces, '
              'and a sheet whose edge is on the wall has nothing to stand on '
              'in that core')
    remedy = ('Mesh this geometry without boundary layers, or move the sheet '
              'clear of the walls.')
    return reason, remedy


def layers_grown(document, dimensionality: str | None = None) -> bool:
    """Whether the project asks the Gmsh route for a boundary layer."""
    gmsh = (document or {}).get('gmsh')
    block = gmsh.get('boundaryLayers') if isinstance(gmsh, dict) else None
    if not isinstance(block, dict) or not model._flag(block.get('enabled')):
        return False
    mode = model._text(block.get('mode')).split('.')[-1]
    if mode == 'none':
        return False
    if dimensionality is None and isinstance(gmsh, dict):
        section = gmsh.get('dimensionality')
        dimensionality = model._text((section or {}).get('mode')) if \
            isinstance(section, dict) else ''
    dimensionality = str(getattr(dimensionality, 'value', dimensionality)
                         or '').split('.')[-1]
    if dimensionality in ('two_d', 'axisymmetric'):
        return False      # plan_derivation drops the layer on a section
    from .layers import _patch_names
    patch_mode = model._text(block.get('patchMode')).split('.')[-1]
    if patch_mode == 'selected' and not _patch_names(block.get('patches')):
        return False      # refused before the job, by its own sentence
    return True


def _load(path) -> dict:
    try:
        value = json.loads(Path(str(path)).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def full_prepared(prepared) -> dict:
    """``{reference: {root}, manifest, group_manifest}`` of *prepared*.

    The Gmsh run hands the gate a ``PreparedGeometryRef``'s mapping, which
    names the manifests' paths; the facade a revision's ``to_dict``, which
    holds them. Either is read into the shape ``interface_contact`` reads.
    """
    if prepared is None:
        return {}
    if not isinstance(prepared, Mapping):
        to_dict = getattr(prepared, 'to_dict', None)
        try:
            prepared = to_dict() if callable(to_dict) else {}
        except (OSError, ValueError, TypeError):
            return {}
    prepared = dict(prepared or {})
    manifest = prepared.get('manifest')
    groups = prepared.get('group_manifest')
    reference = prepared.get('reference')
    if not isinstance(reference, Mapping):
        reference = prepared if 'root' in prepared else {}
    if not isinstance(manifest, Mapping) or not manifest:
        manifest = _load(prepared.get('manifest_path')
                         or (reference or {}).get('manifest_path') or '')
    if not isinstance(groups, Mapping) or not groups:
        groups = _load(prepared.get('group_manifest_path')
                       or (reference or {}).get('group_manifest_path') or '')
    if not manifest or not groups:
        return {}
    return {'reference': {'root': (reference or {}).get('root')},
            'manifest': dict(manifest), 'group_manifest': dict(groups)}


def _volume_count(prepared: dict, sheet_files=()) -> int | None:
    """The volumes the runner carves, or ``None`` when that is not known.

    The prepared topology's count when it measured one; otherwise the
    included regions, less those of the sheets' own files and those bounded
    by nothing (a free sheet the runner sets aside before it counts).
    """
    topology = ((prepared.get('manifest') or {}).get('preparation')
                or {}).get('topology') or {}
    if str(topology.get('volumes_source') or 'unknown') == 'unknown':
        regions = [region for region in
                   (prepared.get('group_manifest') or {}).get('regions') or ()
                   if isinstance(region, Mapping)
                   and region.get('included', True)
                   and str(region.get('geometry_id') or '') not in sheet_files
                   and region.get('boundary_patch_uuids')]
        return len(regions) if regions else None
    try:
        return int(topology.get('volumes') or 0)
    except (TypeError, ValueError):
        return None


def _triangles_by_group(prepared: dict, groups: list) -> dict:
    """patch uuid -> ``(n, 3, 3)`` triangles, for the groups that can be
    read apart. Each prepared surface file is read once."""
    import numpy as np

    from foammesh.core.geometry import interface_contact as contact

    by_file: dict = {}
    for group in groups:
        by_file.setdefault(str(group.get('geometry_id') or ''), []).append(
            group)
    found = {}
    for geometry_id, members in by_file.items():
        path = contact._surface_path(prepared, geometry_id)
        if path is None:
            continue
        if contact._is_ascii(path):
            solids = contact._read_solids(path, None)
            if len(members) == 1:
                every = [tri for solid in solids.values() for tri in solid]
                if every:
                    found[str(members[0]['patch_uuid'])] = np.asarray(
                        every, dtype=float)
                continue
            for group in members:
                triangles = solids.get(contact._solid_name(group))
                if triangles:
                    found[str(group['patch_uuid'])] = np.asarray(
                        triangles, dtype=float)
        elif len(members) == 1:
            triangles = contact._read_binary(path)
            if triangles is not None and len(triangles):
                found[str(members[0]['patch_uuid'])] = triangles
    return found


def _rim_points(triangles, scale: float):
    """The points on edges used by one triangle of *triangles*."""
    import numpy as np

    step = max(scale * 1e-9, 1e-15)
    keys = np.round(triangles.reshape(-1, 3) / step).astype(np.int64)
    index: dict = {}
    nodes = []
    for key in map(tuple, keys):
        nodes.append(index.setdefault(key, len(index)))
    uses: dict = {}
    for start in range(0, len(nodes), 3):
        triple = nodes[start:start + 3]
        for corner in range(3):
            edge = tuple(sorted((triple[corner], triple[(corner + 1) % 3])))
            uses[edge] = uses.get(edge, 0) + 1
    rim = {node for edge, used in uses.items() if used == 1 for node in edge}
    if not rim:
        return np.zeros((0, 3))
    points = triangles.reshape(-1, 3)
    first = {}
    for position, node in enumerate(nodes):
        first.setdefault(node, position)
    return points[[first[node] for node in sorted(rim)]]


def standing_sheets(document, prepared, *,
                    dimensionality: str | None = None) -> list[dict]:
    """``[{key, name, walls}]``: each sheet the layer carve cannot carry.

    ``[]`` whenever the answer is not certain (see the module notes).
    """
    if not layers_grown(document, dimensionality):
        return []
    prepared = full_prepared(prepared)
    if not prepared:
        return []
    groups = [group for group in
              (prepared.get('group_manifest') or {}).get('groups') or ()
              if isinstance(group, Mapping) and group.get('patch_uuid')]
    by_uuid = {str(group['patch_uuid']): group for group in groups}
    rows = (document or {}).get('geometry') or {}
    sheets = {}
    for key, row in model._sorted_items(rows if isinstance(rows, dict)
                                        else {}):
        if not model._is_surface(row):
            continue
        if model._text(row.get('surfaceRole')) not in _SHEET_ROLES:
            continue
        if model._text(row.get('intentState')) != model.VALID:
            continue
        uuid = model._text(row.get('patchUuid'))
        if uuid in by_uuid:
            sheets[uuid] = (str(key), model._text(row.get('name')) or uuid)
    if not sheets:
        return []
    # A sheet the runner sets aside is a free one: its file holds nothing
    # of another role. A face of a solid stays in its shell.
    sheet_files = {str(by_uuid[uuid].get('geometry_id') or '')
                   for uuid in sheets}
    for group in groups:
        if (str(group.get('geometry_id') or '') in sheet_files
                and str(group['patch_uuid']) not in sheets):
            sheet_files.discard(str(group.get('geometry_id') or ''))
    sheets = {uuid: value for uuid, value in sheets.items()
              if str(by_uuid[uuid].get('geometry_id') or '') in sheet_files}
    walls = [group for group in groups
             if str(group['patch_uuid']) not in sheets]
    if not sheets or not walls:
        return []
    count = _volume_count(prepared, sheet_files)
    if count is None or count > 1:
        return []
    try:
        import numpy as np

        from foammesh.core.geometry import interface_contact as contact

        triangles = _triangles_by_group(
            prepared, [by_uuid[uuid] for uuid in sheets] + walls)
        if not triangles:
            return []
        every = np.concatenate([tri.reshape(-1, 3)
                                for tri in triangles.values()])
        diagonal = float(np.linalg.norm(every.max(axis=0)
                                        - every.min(axis=0)))
        tolerance = IMPRINT_TOLERANCE * diagonal
        found = []
        for uuid, (key, name) in sorted(sheets.items(),
                                        key=lambda item: item[1][0]):
            sheet = triangles.get(uuid)
            if sheet is None:
                continue
            rim = _rim_points(sheet, diagonal or 1.0)
            if not len(rim):
                continue
            allowed_sheet = max(tolerance, contact._deflection(
                prepared, str(by_uuid[uuid].get('geometry_id') or '')))
            met = []
            for wall in walls:
                faces = triangles.get(str(wall['patch_uuid']))
                if faces is None:
                    continue
                allowed = max(allowed_sheet, contact._deflection(
                    prepared, str(wall.get('geometry_id') or '')))
                if float(contact._distances(rim, faces).min()) <= allowed:
                    met.append(str(wall.get('solver_name')
                                   or wall.get('display_name')
                                   or wall['patch_uuid']))
            if met:
                found.append({'key': key, 'name': name,
                              'walls': sorted(set(met))})
        return found
    except Exception:  # noqa: BLE001 - a prediction never refuses on doubt
        return []


__all__ = ('IMPRINT_TOLERANCE', 'full_prepared', 'layers_grown',
           'refusal_text', 'standing_sheets')
