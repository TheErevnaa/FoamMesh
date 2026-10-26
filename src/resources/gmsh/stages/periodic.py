"""Periodic pairs (Plan 48 RR, DP-4009)."""
from __future__ import annotations

import math

from .common import (MeshFailure)


class PeriodicStage:
    def periodic_side_tags(self) -> set[int]:
        """The surfaces either side of a periodic pair, as imported (DP-6054)."""
        sides = set()
        for pair in (self.intent.get('periodic') or {}).get('pairs') or ():
            for key in ('masterScope', 'slaveScope'):
                sides.update(int(tag) for tag
                             in self.scope_surfaces.get(pair.get(key)) or ())
        return sides

    def entity_point_centre(self, dim, tag):
        """The mean of the points bounding an entity.

        Read off the points rather than the bounding box: right after the
        layer's rebuild a geo-kernel surface answers +-DBL_MAX or "Empty
        bounding box" (see ``name_layer_replacements``), and its points do
        not. A layer's lateral strip has each top point on its base point
        until it is meshed, which moves the mean of the two sides alike.
        """
        points = {abs(int(point)) for _dim, point in self.gmsh.model.getBoundary(
            [(int(dim), int(tag))], combined=False, oriented=False,
            recursive=True)}
        if not points:
            return None
        coords = [self.gmsh.model.getValue(0, point, []) for point in points]
        return [sum(c[axis] for c in coords) / len(coords) for axis in range(3)]

    @staticmethod
    def carried(affine, point):
        """*point* moved by the 4x4 row-major transform of a pair."""
        return [sum(affine[4 * row + col] * point[col] for col in range(3))
                + affine[4 * row + 3] for row in range(3)]

    def model_reach(self, scale=1e-6):
        box = self.gmsh.model.getBoundingBox(-1, -1)
        return max(math.dist(box[:3], box[3:]) * scale, 1e-12)

    def match_by_transform(self, dim, masters, slaves, affine, reach):
        """``[(master, slave)]``: the slave entity the transform lands each on.

        ``(master, None)`` for a master entity with no partner, and the slave
        entities left over, so the caller can say what did not correspond.
        """
        where = {tag: self.entity_point_centre(dim, tag) for tag in slaves}
        pairs, free = [], list(slaves)
        for tag in masters:
            centre = self.entity_point_centre(dim, tag)
            target = (self.carried(affine, centre) if centre is not None
                      else None)
            best = min((other for other in free
                        if where[other] is not None and target is not None),
                       key=lambda other: math.dist(where[other], target),
                       default=None)
            if best is None or math.dist(where[best], target) > reach:
                pairs.append((int(tag), None))
                continue
            free.remove(best)
            pairs.append((int(tag), int(best)))
        return pairs, free

    def rebuilt_sides(self, pair, master, slave):
        """The pieces of a pair the layer stage rebuilt, matched; or None.

        DP-6054. A periodic side grows no layer (``apply_boundary_layers``)
        and is rebuilt from the layer's rim like any patch without one: an
        inner face and the lateral strips the layer stands on that plane
        (``layer_replacements``). ``setPeriodic`` cannot be asked for those
        pieces. MEASURED on duct.step with a translation pair on its ends:
        the strips are refused, "could not find all point correspondences",
        because each top point sits on its base point until the strip is
        meshed; and the rebuilt faces are refused while meshing, "cannot find
        periodic counterpart of node 11 of curve 16 on curve 14", because
        their edges are the layer's top curves, which the extrusion meshes
        and a periodic copy cannot. Their wall edges can be paired, and are,
        so both layers are extruded off the same nodes; the rebuilt faces are
        made to correspond after the surface pass
        (:meth:`copy_periodic_surfaces`).
        """
        replaced = self.layer_replacements
        if not any(int(tag) in replaced for tag in list(master) + list(slave)):
            return None
        affine = [float(item) for item in pair['affine']]
        reach = self.model_reach()
        masters = [piece for tag in master
                   for piece in (replaced.get(int(tag)) or [int(tag)])]
        slaves = [piece for tag in slave
                  for piece in (replaced.get(int(tag)) or [int(tag)])]
        pieces, left = self.match_by_transform(2, masters, slaves, affine,
                                               reach)
        if left or any(other is None for _tag, other in pieces):
            raise MeshFailure(
                f'periodic pair {pair["name"]!r}: the boundary layer rebuilt '
                f'its sides into {len(masters)} piece(s) on the master side '
                f'and {len(slaves)} on the slave side, and they do not '
                'correspond under its transform. Grow the same layers on the '
                'walls around both sides, or turn layers off.')
        side = getattr(self, 'layer_side_curves', {}) or {}
        edges, _left = self.match_by_transform(
            1, [curve for tag in master for curve in side.get(int(tag), ())],
            [curve for tag in slave for curve in side.get(int(tag), ())],
            affine, reach)
        return pieces, [(one, other) for one, other in edges
                        if other is not None]

    def apply_periodic(self):
        pairs = (self.intent.get('periodic') or {}).get('pairs') or []
        if not pairs:
            return
        applied = 0
        for pair in pairs:
            master = self.scope_surfaces.get(pair['masterScope'])
            slave = self.scope_surfaces.get(pair['slaveScope'])
            if not master or not slave:
                self.warnings.append(
                    f'periodic pair {pair["name"]!r} could not resolve both '
                    'scopes and was skipped')
                self.ledger.record(f'periodic:{pair["name"]}',
                                   pair['transform'], None, applied=False,
                                   note='scope did not resolve')
                continue
            affine = [float(item) for item in pair['affine']]
            rebuilt = self.rebuilt_sides(pair, master, slave)
            try:
                if rebuilt is None:
                    self.gmsh.model.mesh.setPeriodic(2, slave, master, affine)
                else:
                    pieces, edges = rebuilt
                    for edge_master, edge_slave in edges:
                        self.gmsh.model.mesh.setPeriodic(
                            1, [edge_slave], [edge_master], affine)
                    master = [one for one, _other in pieces]
                    slave = [other for _one, other in pieces]
                    self.periodic_copies.append({
                        'name': pair['name'], 'affine': affine,
                        'pieces': pieces})
            except Exception as error:
                # The commonest cause by far is a transform pointing the wrong
                # way: it must map the master surface onto the slave.
                raise MeshFailure(
                    f'periodic pair {pair["name"]!r} was rejected by Gmsh: '
                    f'{error}. Check that the transform maps the master '
                    f'surface onto the slave and not the reverse.') from error
            applied += 1
            self.periodic_applied.append({
                'name': pair['name'], 'transform': pair['transform'],
                'master': [int(tag) for tag in master],
                'slave': [int(tag) for tag in slave],
                'affine': affine,
                # Plan 49 SR5 (DP-5805). The relation the pair was lowered from.
                **{key: str(pair[key]) for key in ('origin', 'relationId')
                   if pair.get(key)}})
            # Accepted, not achieved. What this call establishes is a request
            # Gmsh agreed to honour while meshing; whether the two surfaces
            # ended up corresponding is read out of the mesh afterwards, in
            # :meth:`measure_periodic`.
            self.ledger.record(
                f'periodic:{pair["name"]}', pair['transform'],
                pair['transform'],
                note='setPeriodic accepted' if rebuilt is None else
                'wall edges paired; the sides the layer rebuilt are copied '
                'after the surface pass')
        self.statistics['periodic'] = {'applied': applied,
                                       'requested': len(pairs)}

    def copy_periodic_surfaces(self):
        """Make each rebuilt slave piece the image of its master (DP-6054).

        Run between the surface and the volume pass. A piece whose nodes
        already land on the master's under the transform -- every lateral
        strip, extruded off paired wall edges by the same stack -- is only
        recorded. A rebuilt inner face is triangulated on its own, so its
        mesh is replaced with the master's, moved by the transform onto the
        nodes its edges already have. The node map is kept for
        :meth:`measure_periodic`, which reads it as it reads Gmsh's own.
        """
        reach = self.model_reach()
        laterals = {int(tag) for tag in getattr(self, 'layer_laterals', ())}
        for copy in self.periodic_copies:
            affine = copy['affine']
            for master, slave in copy['pieces']:
                mapping = self.land_nodes(master, slave, affine, reach)
                if mapping is None:
                    if int(slave) in laterals:
                        raise MeshFailure(
                            f'periodic pair {copy["name"]!r}: the boundary '
                            'layer was not extruded alike on its two sides, so '
                            'the strips it stands on them do not correspond. '
                            'Grow the same layers on the walls around both '
                            'sides, or turn layers off.')
                    self.copy_surface_mesh(master, slave, affine, reach,
                                           copy['name'])
                # The tags are renumbered after meshing, so the map is read
                # again from the finished mesh (`copied_periodic_nodes`).
                self.periodic_node_maps[int(slave)] = (int(master),
                                                       list(affine))
                if int(slave) in laterals:
                    # DP-6066. Not yet: see `declare_periodic_strips`.
                    self.periodic_strips_pending.append(
                        (int(master), int(slave), list(affine)))
                    continue
                self.declare_copied_periodic(master, slave, affine)

    def declare_periodic_strips(self):
        """Declare the pairs' lateral strips periodic, after the volume pass.

        DP-6066. A strip is the side of the layer stacks standing on a
        periodic plane, and the stacks are meshed by extrusion in the volume
        pass. Declared periodic before that pass, Gmsh extruded the stacks
        along the slave side's edge with no height: MEASURED on the sr9h
        periodic channel (STL, Gmsh 4.15.2), the slave strips kept their
        quadrilaterals while the cells beside them came out as pyramids and
        tetrahedra, so 282 slave faces bounded no cell, 512 cell faces had no
        name and the mesh could not be published. Declared after it, Gmsh
        takes each strip as its master's image as it is (104 and 296 nodes
        per strip on that job), so the native file's ``$Periodic`` section
        still names them, and every stack is whole.
        """
        pending, self.periodic_strips_pending = (
            list(self.periodic_strips_pending), [])
        for master, slave, affine in pending:
            self.declare_copied_periodic(master, slave, affine)

    def declare_copied_periodic(self, master, slave, affine):
        """Tell Gmsh the copied piece is its master's image (DP-6054).

        The pair is then written to the ``$Periodic`` section of the native
        file and read back by the export (``periodic_pairs``), as for a pair
        Gmsh meshed itself. Both surfaces are meshed already, so Gmsh copies
        nothing. A piece Gmsh will not take (a strip whose top points sit on
        its base points) is left out: the face of the same pair names it.
        """
        try:
            self.gmsh.model.mesh.setPeriodic(2, [int(slave)], [int(master)],
                                             list(affine))
        except Exception:                                   # noqa: BLE001
            return False
        return True

    def copied_periodic_nodes(self, slave):
        """``(master, slave nodes, master nodes, affine)`` for a copied side.

        Read off the finished mesh by where the transform carries each master
        node, as :meth:`measure_periodic` reads Gmsh's own map.
        """
        master, affine = self.periodic_node_maps[int(slave)]
        mapping = self.node_map(master, slave, affine, self.model_reach())
        return master, list(mapping), list(mapping.values()), affine

    def surface_nodes(self, tag, boundary=True):
        tags, coords, _param = self.gmsh.model.mesh.getNodes(
            2, int(tag), includeBoundary=boundary)
        return {int(node): (coords[3 * index], coords[3 * index + 1],
                            coords[3 * index + 2])
                for index, node in enumerate(tags)}

    def node_map(self, master, slave, affine, reach):
        """``{slave node: master node}`` for each node the transform lands."""
        grid = self.node_grid(self.surface_nodes(slave), reach)
        mapping = {}
        for node, point in self.surface_nodes(master).items():
            found = self.nearest_node(grid, self.carried(affine, point), reach)
            if found is not None and found not in mapping:
                mapping[found] = node
        return mapping

    def land_nodes(self, master, slave, affine, reach):
        """``{slave node: master node}`` if every node lands, else None."""
        theirs = self.surface_nodes(master)
        mine = self.surface_nodes(slave)
        if len(theirs) != len(mine):
            return None
        mapping = self.node_map(master, slave, affine, reach)
        if len(mapping) != len(theirs):
            return None
        moved = {frozenset(mapping[node] for node in face)
                 for face in self.surface_faces(int(slave))}
        if moved != self.surface_faces(int(master)):
            return None
        return mapping

    @staticmethod
    def node_grid(nodes, reach):
        cell = max(reach * 1e3, 1e-9)
        grid = {}
        for node, point in nodes.items():
            key = tuple(int(math.floor(value / cell)) for value in point)
            grid.setdefault(key, []).append((node, point))
        return cell, grid

    @staticmethod
    def nearest_node(index, point, reach):
        cell, grid = index
        base = tuple(int(math.floor(value / cell)) for value in point)
        best, distance = None, reach
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for node, other in grid.get(
                            (base[0] + dx, base[1] + dy, base[2] + dz), ()):
                        gap = math.dist(other, point)
                        if gap <= distance:
                            best, distance = node, gap
        return best

    def copy_surface_mesh(self, master, slave, affine, reach, name):
        """Replace *slave*'s surface mesh with *master*'s, moved onto it.

        Returns ``{slave node: master node}``. The copy is wound the way the
        slave's own mesh was, so the face keeps the orientation Gmsh gave it.
        """
        gmsh = self.gmsh
        own_inner = self.surface_nodes(slave, boundary=False)
        edge = self.node_grid({
            node: point for node, point in self.surface_nodes(slave).items()
            if node not in own_inner}, reach)
        inner = self.surface_nodes(master, boundary=False)
        everything = self.surface_nodes(master)
        before_types, _tags, before_nodes = gmsh.model.mesh.getElements(
            2, int(slave))
        facing = self.first_normal(before_types, before_nodes)
        mapping = {}
        for node, point in everything.items():
            if node in inner:
                continue
            found = self.nearest_node(edge, self.carried(affine, point), reach)
            if found is None:
                raise MeshFailure(
                    f'periodic pair {name!r}: an edge node of the master '
                    'side has no partner on the slave side after the surface '
                    'pass, so the two sides cannot be made to correspond. '
                    'Grow the same layers on the walls around both sides, or '
                    'turn layers off.')
            mapping[node] = found
        gmsh.model.mesh.clear([(2, int(slave))])
        start = int(gmsh.model.mesh.getMaxNodeTag()) + 1
        new, coords = [], []
        for offset, (node, point) in enumerate(sorted(inner.items())):
            mapping[node] = start + offset
            new.append(start + offset)
            coords.extend(self.carried(affine, point))
        if new:
            gmsh.model.mesh.addNodes(2, int(slave), new, coords)
        types, _tags, nodes = gmsh.model.mesh.getElements(2, int(master))
        moved_nodes = [[mapping[int(node)] for node in group]
                       for group in nodes]
        mine = self.first_normal(types, moved_nodes)
        flip = (facing is not None and mine is not None
                and sum(a * b for a, b in zip(mine, facing)) < 0)
        for etype, group in zip(types, moved_nodes):
            width = gmsh.model.mesh.getElementProperties(etype)[3]
            rows = [group[index:index + width]
                    for index in range(0, len(group), width)]
            if flip:
                rows = [[row[0]] + row[1:][::-1] for row in rows]
            gmsh.model.mesh.addElementsByType(
                int(slave), etype, [], [node for row in rows for node in row])
        return {slave_node: master_node for master_node, slave_node
                in mapping.items()}

    def first_normal(self, types, nodes):
        """The normal of the first element, or None for an empty surface."""
        for _etype, group in zip(types, nodes):
            if len(group) < 3:
                continue
            points = [self.gmsh.model.mesh.getNode(int(node))[0]
                      for node in list(group)[:3]]
            u = [points[1][k] - points[0][k] for k in range(3)]
            v = [points[2][k] - points[0][k] for k in range(3)]
            return [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
                    u[0] * v[1] - u[1] * v[0]]
        return None
