"""Plan 49 SR5. What the surface roles change in a Gmsh polyMesh publication.

The publisher (``publish.publish`` -> ``build_canonical`` ->
``FoamPolyMeshWriter``) is unchanged in shape; this module supplies what the
roles add to it:

* an external boundary's authored mesh patch type (``patch``, ``wall``,
  ``symmetry``, ``symmetryPlane``; ``empty``/``wedge`` as declared types the
  constrained-mesh check holds to their contract);
* a blocking baffle's two sides (the Crack plugin's) both typed ``wall`` and
  declared a ``baffle`` pair, so the validator lets their duplicated points
  stand (SR0 probe 3: the points must not be merged);
* an internal face's faces, which ``build_canonical`` drops from the boundary
  because they are interior, written as a face zone after the writer (no
  patch, no change to the mesh itself);
* each non-conformal region interface's two regions, read off the cells that
  own its two patches, for the coupling the export makes after the region
  split (DP-5762: the coupling has to be made then, in the dictionary form,
  naming both regions).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

#: meshPatchType -> the prepared category the writer types it from.
_CATEGORY_FOR_TYPE = {'wall': 'wall', 'patch': 'patch',
                      'symmetryPlane': 'symmetryPlane',
                      'symmetry': 'symmetry', 'empty': 'empty',
                      'wedge': 'wedge'}
#: Types the writer cannot reach from a category, set on the patch directly.
_EXPLICIT_TYPES = ('symmetry',)


class RolePublicationError(ValueError):
    """A role the published mesh cannot carry, named and explained."""


@dataclass
class RolePublication:
    categories: dict = field(default_factory=dict)
    declared: dict = field(default_factory=dict)
    patch_types: dict = field(default_factory=dict)
    baffles: list = field(default_factory=list)
    internal: list = field(default_factory=list)
    non_conformal: list = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.categories or self.declared or self.patch_types
                    or self.baffles or self.internal or self.non_conformal)


def role_publication(surface_roles) -> RolePublication:
    """Read the run's ``surfaceRoles`` block (job + runner record).

    ``surface_roles`` is ``{'surfaces': [job surfaceIntent surfaces],
    'baffles': [{'name', 'slave'}], 'internalFaces': [name],
    'nonConformal': [{'name', 'masterSolverName', 'slaveSolverName'}]}``.
    """
    plan = RolePublication()
    surface_roles = surface_roles or {}
    for surface in surface_roles.get('surfaces') or ():
        if not isinstance(surface, dict):
            continue
        if surface.get('role') != 'externalBoundary' \
                or surface.get('state', 'valid') != 'valid':
            continue
        name = str(surface.get('name') or '')
        mesh_type = str(surface.get('meshPatchType') or '')
        category = _CATEGORY_FOR_TYPE.get(mesh_type)
        if not name or category is None:
            continue
        plan.categories[name] = category
        if mesh_type in ('empty', 'wedge'):
            plan.declared[name] = mesh_type
        if mesh_type in _EXPLICIT_TYPES:
            plan.patch_types[name] = mesh_type
    for baffle in surface_roles.get('baffles') or ():
        if isinstance(baffle, dict) and baffle.get('name') \
                and baffle.get('slave'):
            plan.baffles.append((str(baffle['name']), str(baffle['slave'])))
            plan.categories[str(baffle['name'])] = 'wall'
            plan.categories[str(baffle['slave'])] = 'wall'
    plan.internal = sorted({str(name) for name in
                            surface_roles.get('internalFaces') or ()
                            if str(name)})
    plan.non_conformal = [dict(pair) for pair in
                          surface_roles.get('nonConformal') or ()
                          if isinstance(pair, dict)]
    return plan


def apply_role_metadata(metadata: dict, plan: RolePublication) -> dict:
    """Set the explicit patch types and the baffle pairs on *metadata*."""
    for name, patch_type in plan.patch_types.items():
        item = metadata.setdefault(name, {
            'name': name, 'solver_name': name, 'stable_id': name,
            'category': plan.categories.get(name, 'patch')})
        item['patch_type'] = patch_type
    for name, slave in plan.baffles:
        for side, role, neighbour in ((name, 'master', slave),
                                      (slave, 'slave', name)):
            item = metadata.setdefault(side, {
                'name': side, 'solver_name': side, 'stable_id': side})
            item['category'] = 'wall'
            item['interface'] = {
                'coupling': 'baffle', 'pair_id': name, 'role': role,
                'neighbour_stable_id': neighbour,
                'neighbour_solver_name': neighbour}
    return metadata


# --------------------------------------------------------------------------- #
# Internal faces -> face zones
# --------------------------------------------------------------------------- #

def _cell_regions(mesh) -> np.ndarray:
    return np.concatenate([block.region_ids for block in mesh.cell_blocks])


def _region_name(mesh, region_id) -> str:
    from foammesh.core.export.poly_mesh_writer import _foam_name

    item = mesh.regions.get(int(region_id), {})
    return _foam_name(item.get('solver_name') or item.get('name')
                      or f'region_{region_id}')


def internal_face_zones(mesh, document, names) -> list[dict]:
    """``[{'name', 'labels', 'flips'}]``: each internal face's mesh faces.

    The faces are found by their points in the interior faces of the
    writer's own topology (the one ``FoamPolyMeshWriter`` numbered the faces
    file with), so the labels are the written face labels.
    """
    from foammesh.core.export.poly_mesh_writer import _foam_topology
    from foammesh.core.gmsh.publish import SURFACE_TYPES

    wanted = {int(tag): str(name)
              for (dim, tag), name in document.physical_names.items()
              if dim == 2 and str(name) in set(names)}
    if not wanted:
        return []
    position = {tuple(point): index
                for index, point in enumerate(mesh.points.tolist())}
    topology = _foam_topology(mesh)
    internal = int(topology.internal_count)
    keys = {}
    for label in range(internal):
        size = int(topology.face_sizes[label])
        keys[tuple(sorted(int(value) for value in
                          topology.faces[label][:size]))] = label
    found: dict[str, dict] = {name: {'labels': [], 'flips': [], 'missed': 0}
                              for name in wanted.values()}
    points = mesh.points
    for element_type, physical, nodes in document.elements:
        if element_type not in SURFACE_TYPES or int(physical) not in wanted:
            continue
        _kind, size = SURFACE_TYPES[element_type]
        rows = [position.get(tuple(document.points[
            document.node_index[node]].tolist())) for node in nodes[:size]]
        entry = found[wanted[int(physical)]]
        label = None if None in rows else keys.get(tuple(sorted(rows)))
        if label is None:
            entry['missed'] += 1
            continue
        face = [int(value) for value in topology.faces[label][:size]]
        element_normal = np.cross(points[rows[1]] - points[rows[0]],
                                  points[rows[2]] - points[rows[0]])
        face_normal = np.cross(points[face[1]] - points[face[0]],
                               points[face[2]] - points[face[0]])
        entry['labels'].append(label)
        entry['flips'].append(bool(np.dot(element_normal, face_normal) < 0))
    zones = []
    for name, entry in sorted(found.items()):
        if not entry['labels']:
            raise RolePublicationError(
                f'{name} is an internal face and none of its '
                f'{entry["missed"]} mesh faces lies inside the mesh: it is '
                'a boundary of the volume rather than a face within it. '
                'Make it an external boundary, or place it inside the '
                'region.')
        order = np.argsort(entry['labels'])
        zones.append({'name': name,
                      'labels': [entry['labels'][i] for i in order],
                      'flips': [entry['flips'][i] for i in order],
                      'missed': entry['missed']})
    return zones


def _zone_text(zone) -> str:
    from foammesh.core.export.poly_mesh_writer import _foam_name

    lines = [f'{_foam_name(zone["name"])}\n{{\n    type faceZone;\n'
             f'    faceLabels List<label>\n    {len(zone["labels"])}\n    (\n']
    lines.extend(f'        {int(label)}\n' for label in zone['labels'])
    lines.append('    );\n    flipMap List<bool>\n'
                 f'    {len(zone["flips"])}\n    (\n')
    # DP-565: 0 and 1, which vtkOpenFOAMReader reads.
    lines.extend('        %d\n' % (1 if flip else 0) for flip in zone['flips'])
    lines.append('    );\n}\n')
    return ''.join(lines)


def write_face_zones(poly_mesh: Path, zones) -> None:
    """Add *zones* to ``faceZones``, creating it if the writer wrote none."""
    from foammesh.core.export.poly_mesh_writer import _header

    path = Path(poly_mesh) / 'faceZones'
    added = ''.join(_zone_text(zone) for zone in zones)
    if path.is_file():
        text = path.read_text(encoding='ascii')
        match = re.search(r'\n(\d+)\n\(\n', text)
        end = text.rstrip().rfind(')')
        if match is None or end < 0:
            raise RolePublicationError(f'{path} is not a faceZone list')
        count = int(match.group(1)) + len(zones)
        text = (text[:match.start(1)] + str(count) + text[match.end(1):end]
                + added + ')\n')
    else:
        text = (_header('faceZoneList', 'faceZones') + f'{len(zones)}\n(\n'
                + added + ')\n')
    with path.open('w', encoding='ascii', newline='\n') as stream:
        stream.write(text)


# --------------------------------------------------------------------------- #
# Non-conformal interfaces -> the regions they join
# --------------------------------------------------------------------------- #

def non_conformal_couplings(mesh, pairs) -> list[dict]:
    """The post-split couplings for *pairs*, each naming its two regions.

    ``pairs`` are ``{'name', 'masterSolverName', 'slaveSolverName'}``. A side
    whose faces are owned by cells of more than one region, or two sides in
    one region, are refused: the coupling joins two regions.
    """
    if not pairs:
        return []
    from foammesh.core.export.poly_mesh_writer import _foam_name

    regions = _cell_regions(mesh)
    names_by_id = {}
    for block in mesh.boundary_blocks:
        for patch_id in np.unique(block.patch_ids):
            item = mesh.patches.get(int(patch_id), {})
            names_by_id[int(patch_id)] = _foam_name(
                item.get('solver_name') or item.get('name')
                or f'patch_{int(patch_id)}')
    from foammesh.core.export.poly_mesh_writer import _foam_topology

    topology = _foam_topology(mesh)
    internal = int(topology.internal_count)
    owners = topology.owner[internal:]
    patch_ids = topology.patch_ids
    side_regions: dict[str, set] = {}
    for patch_id, name in names_by_id.items():
        selected = owners[patch_ids == patch_id]
        side_regions[name] = {int(value) for value in regions[selected]}
    if len({int(value) for value in regions}) < 2:
        names = ', '.join(repr(str(pair.get('name'))) for pair in pairs)
        raise RolePublicationError(
            f'the non-conformal region interface {names} joins two regions, '
            'and this mesh has one: both sides were meshed into one cell '
            'zone. Give the two bodies their own regions, or make the '
            'interface conformal.')
    couplings = []
    for pair in pairs:
        name = str(pair.get('name') or '')
        master = _foam_name(pair.get('masterSolverName') or '')
        slave = _foam_name(pair.get('slaveSolverName') or '')
        sides = []
        for patch in (master, slave):
            found = side_regions.get(patch) or set()
            if len(found) != 1:
                raise RolePublicationError(
                    f'the non-conformal region interface {name!r}: its side '
                    f'{patch} is owned by cells of '
                    + (f'{len(found)} regions' if found else 'no region')
                    + ', so it cannot be coupled as one side of one region.')
            sides.append(_region_name(mesh, next(iter(found))))
        if sides[0] == sides[1]:
            raise RolePublicationError(
                f'the non-conformal region interface {name!r}: both its '
                f'sides lie in region {sides[0]}, so it joins no two '
                'regions.')
        couplings.append({
            'kind': 'non_conformal', 'name': _foam_name(name),
            'owner_patch': master, 'neighbour_patch': slave,
            'owner_zone': sides[0], 'neighbour_zone': sides[1],
            'relationId': str(pair.get('relationId') or ''),
            'source': 'gmsh'})
    return couplings


def couplings_path(case_path) -> Path:
    """Where a Gmsh publication records its post-split couplings."""
    return Path(case_path) / 'foammesh' / 'gmsh' / 'couplings.json'


def record_couplings(case_path, couplings) -> Path | None:
    """Write (or clear) the Gmsh couplings record of *case_path*."""
    import json

    path = couplings_path(case_path)
    if not couplings:
        if path.is_file():
            path.unlink()
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'couplings': list(couplings)}, indent=2),
                    encoding='utf-8')
    return path


def recorded_couplings(case_path) -> list[dict]:
    """The couplings the last Gmsh publication of *case_path* recorded.

    The hook for the export (SR4's ``core/import_export/couplings``): a
    case whose mesh came from Gmsh carries its non-conformal region
    interfaces here, in the same shape the case builder records them for
    snappy, to be made after ``splitMeshRegions``.
    """
    import json

    path = couplings_path(case_path)
    if not path.is_file():
        return []
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [item for item in (document.get('couplings') or ())
            if isinstance(item, dict)]


__all__ = ('RolePublication', 'RolePublicationError', 'apply_role_metadata',
           'couplings_path', 'internal_face_zones', 'non_conformal_couplings',
           'record_couplings', 'recorded_couplings', 'role_publication',
           'write_face_zones')
