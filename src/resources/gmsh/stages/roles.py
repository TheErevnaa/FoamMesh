"""Plan 49 SR5. Internal faces and blocking baffles on the Gmsh route.

The job intent's ``surfaceIntent`` (``core/gmsh/surface_roles``) names each
prepared surface whose role is not a plain external boundary. Two roles need
the runner itself:

* ``internalFace`` -- a surface inside one region. A free sheet is embedded
  in the volume holding it (``occ.fragment``), so the volume mesh conforms to
  it; it is given its own physical group, and the publisher turns its faces
  into a face zone (no patch).
* ``blockingBaffle`` -- an impermeable sheet. Embedded the same way, and
  after the volume mesh the Crack plugin duplicates its nodes, so the two
  sides become two boundary patches (MEASURED in SR0 probe 3: 176 nodes
  duplicated, Crack's new group unnamed, so it is named here). The publisher
  types both sides ``wall``.

Region interfaces are the fusion's (``geometry.fuse_contacts``) and periodic
relations the periodic stage's; neither is handled here.
"""
from __future__ import annotations

import math

from .common import MeshFailure

from sheet_imprint import ImprintError, imprint_model
from shell_topology import bounding_box, point_in_shell

INTERNAL = 'internalFace'
BAFFLE = 'blockingBaffle'
#: The suffix of the second side Crack makes of a baffle.
BAFFLE_SLAVE_SUFFIX = '_slave'
#: DP-6064. How near a sheet's rim must be to a wall, as a fraction of the
#: model's diagonal, to be imprinted on it (unless the geometry tolerance is
#: wider). A single-precision STL coordinate is good to about 1e-7 of itself.
IMPRINT_TOLERANCE = 1e-6


class RolesStage:
    NETGEN_EMBEDDED_NOTE = (
        'the Netgen optimiser was not run: this model holds an internal face '
        'or a blocking baffle embedded in a volume, and Gmsh 4.15.2 crashes '
        "optimising such a volume with Netgen. Gmsh's own optimiser ran.")

    def embeds_role_sheets(self) -> bool:
        """Whether the job embeds an internal face or a baffle."""
        return bool(self.role_surfaces(INTERNAL, BAFFLE))

    def role_surfaces(self, *roles) -> list[dict]:
        """The valid surfaces of the job's ``surfaceIntent`` with a role in
        *roles*."""
        intent = getattr(self, 'intent', None)
        block = (intent.get('surfaceIntent') or {}) \
            if isinstance(intent, dict) else {}
        return [surface for surface in block.get('surfaces') or ()
                if isinstance(surface, dict)
                and surface.get('role') in roles
                and surface.get('state', 'valid') == 'valid']

    def _volume_holding(self, tag):
        """The one volume whose box holds surface *tag*, or ``None``."""
        occ = self.gmsh.model.occ
        try:
            box = occ.getBoundingBox(2, int(tag))
        except Exception:  # noqa: BLE001 - OCC raises many ways
            return None
        tolerance = 1e-9 * max(1.0, max(abs(value) for value in box))
        holding = []
        for _dim, volume in occ.getEntities(3):
            try:
                outer = occ.getBoundingBox(3, int(volume))
            except Exception:  # noqa: BLE001
                continue
            if all(outer[axis] - tolerance <= box[axis]
                   and box[axis + 3] <= outer[axis + 3] + tolerance
                   for axis in range(3)):
                holding.append(int(volume))
        return holding[0] if len(holding) == 1 else None

    def embed_role_sheets(self):
        """Embed each free internal-face or baffle sheet in its volume.

        A sheet imported beside a solid bounds no volume; meshed as it is,
        the volume mesh ignores it. ``occ.fragment`` of the one volume that
        holds it with the sheet makes the sheet part of that volume's
        topology (SR0 probe 3). A sheet that lies already between two
        volumes, or on one (fragmented by the fusion), is left alone.
        """
        surfaces = self.role_surfaces(INTERNAL, BAFFLE)
        if not surfaces:
            return
        gmsh = self.gmsh
        scopes = self.import_time_scopes()
        embedded = []
        for surface in surfaces:
            for tag in list(scopes.get(str(surface.get('scope') or '')) or ()):
                try:
                    upward, _down = gmsh.model.getAdjacencies(2, int(tag))
                except Exception:  # noqa: BLE001
                    continue
                if len(upward):
                    continue
                volume = self._volume_holding(tag)
                if volume is None:
                    raise MeshFailure(
                        f'{surface.get("name")!r} is an '
                        + ('internal face' if surface.get('role') == INTERNAL
                           else 'blocking baffle')
                        + ' and lies inside no single volume of this model, '
                          'so there is nothing to embed it in. Move it into '
                          'the region it belongs to, or make it an external '
                          'boundary.')
                # The fragment renumbers the volume and every face of it
                # (MEASURED live: a box's faces 1-6 came back as 8-13), so
                # each is followed by its signature, as the fusion's are;
                # the sheet itself by the fragment's own map, since it may
                # be cut into pieces no signature matches.
                signatures = {key: value for key, value
                              in self._entity_signatures((2, 3)).items()
                              if key != (2, int(tag))}
                _out, out_map = gmsh.model.occ.fragment(
                    [(3, volume)], [(2, int(tag))])
                gmsh.model.occ.synchronize()
                self._follow_duplicate_fusion(signatures)
                pieces = [int(child) for d, child in
                          (out_map[1] if len(out_map) > 1 else ())
                          if int(d) == 2]
                if pieces and pieces != [int(tag)]:
                    self.retarget_entities('embed', {int(tag): pieces},
                                           dimensions=(2,))
                embedded.append(str(surface.get('name') or tag))
        if embedded:
            self.ledger.record(
                'surfaceRoles/embedded', sorted(embedded), sorted(embedded),
                note='embedded in the volume holding '
                     + ('it' if len(embedded) == 1 else 'each'))

    # -- DP-6064: the same sheets on the tessellated (STL) route ----------- #

    def _role_sheet_name(self, surface) -> str:
        return ('internal face' if surface.get('role') == INTERNAL
                else 'blocking baffle')

    def _open_rim(self, tag) -> int:
        """How many edges of surface *tag* are used by one of its triangles."""
        uses: dict = {}
        types, _elements, nodes = self.gmsh.model.mesh.getElements(2, int(tag))
        for element_type, node_tags in zip(types, nodes):
            if int(element_type) != 2:
                continue
            flat = [int(value) for value in node_tags]
            for start in range(0, len(flat) - 2, 3):
                triple = flat[start:start + 3]
                for corner in range(3):
                    edge = tuple(sorted((triple[corner],
                                         triple[(corner + 1) % 3])))
                    uses[edge] = uses.get(edge, 0) + 1
        return sum(1 for used in uses.values() if used == 1)

    def set_aside_role_sheets(self, exclude=()) -> set:
        """Take the internal-face and baffle sheets of an STL import aside.

        DP-6064. ``embed_role_sheets`` does this for CAD, with an OCC
        fragment; a tessellated import has no OCC model, and its open sheet
        was read as part of a shell, which the shell topology then refused
        as not closed (MEASURED, run sr9h: "shell ... is not closed: 32 edges
        belong to one triangle instead of two"). Here each such sheet -- an
        imported surface with a role of either kind and an open rim -- is
        kept out of the shells and its rim imprinted on the walls it lies on
        (:mod:`sheet_imprint`), so that once classified it shares its edge
        curves with them and can be embedded in the volume conformally
        (:meth:`embed_tessellated_sheets`).

        Returns the import tags of the sheets. *exclude* are surfaces already
        read as faces between two bodies, which close both and stay as they
        are.
        """
        self.role_sheet_imports = set()
        self.role_sheet_tolerance = 0.0
        surfaces = self.role_surfaces(INTERNAL, BAFFLE)
        if not surfaces:
            return set()
        exclude = {int(tag) for tag in exclude}
        scopes = self.import_time_scopes()
        sheets = {}
        for surface in surfaces:
            for tag in scopes.get(str(surface.get('scope') or '')) or ():
                if int(tag) in exclude or self._open_rim(tag) == 0:
                    continue
                sheets[int(tag)] = surface
        if not sheets:
            return set()
        gmsh = self.gmsh
        box = gmsh.model.getBoundingBox(-1, -1)
        diagonal = math.sqrt(sum((box[axis + 3] - box[axis]) ** 2
                                 for axis in range(3)))
        tolerance = max(float(gmsh.option.getNumber('Geometry.Tolerance')),
                        IMPRINT_TOLERANCE * diagonal)
        try:
            summary = imprint_model(gmsh, sorted(sheets), tolerance)
        except ImprintError as error:
            names = sorted({str(surface.get('name')) for surface
                            in sheets.values()})
            raise MeshFailure(
                ', '.join(repr(name) for name in names)
                + ' could not be joined to the walls it touches: '
                + f'{error}. Check that its edge lies on the wall, or move it '
                  'clear of the wall.') from error
        self.role_sheet_imports = set(sheets)
        self.role_sheet_tolerance = tolerance
        self.statistics['surfaceRoles'] = {
            **dict(self.statistics.get('surfaceRoles') or {}),
            'imprinted': {
                'sheets': sorted({str(surface.get('name'))
                                  for surface in sheets.values()}),
                'rimEdgesOnWalls': int(summary['rimOnWall']),
                'wallSplits': int(summary['wallSplits']),
                'rimSplits': int(summary['rimSplits']),
                'tolerance': tolerance}}
        return set(sheets)

    def embed_tessellated_sheets(self, topology, triangles, coordinates,
                                 pieces):
        """Embed the classified pieces of each set-aside sheet in its volume.

        DP-6064. The volume holding a piece is the one whose outer shell
        holds a point of it and none of whose holes do, read off the same
        triangles the shells were built from. A piece in no volume, or in
        more than one, is refused, as on the CAD route.
        """
        if not pieces:
            return
        gmsh = self.gmsh
        boxes = {}
        scale = 1.0
        for plan in topology.volumes:
            for group in (plan.outer, *plan.voids):
                group = tuple(group)
                if group not in boxes:
                    boxes[group] = bounding_box(
                        [triangle for tag in group
                         for triangle in triangles[tag]], coordinates)
                    scale = max(scale, max(boxes[group][axis + 3]
                                           - boxes[group][axis]
                                           for axis in range(3)))

        def inside(point, group):
            group = tuple(group)
            return point_in_shell(
                point, [triangle for tag in group
                        for triangle in triangles[tag]],
                coordinates, box=boxes[group], length_scale=scale)

        by_volume: dict = {}
        embedded = []
        for piece in sorted(pieces):
            owners = None
            for first, second, third in triangles[piece]:
                point = tuple(sum(coordinates[node][axis] for node
                                  in (first, second, third)) / 3.0
                              for axis in range(3))
                answers = []
                for plan in topology.volumes:
                    outer = inside(point, plan.outer)
                    voids = [inside(point, void) for void in plan.voids]
                    if outer is None or None in voids:
                        answers = None
                        break
                    if outer and not any(voids):
                        answers.append(plan)
                if answers is not None:
                    owners = answers
                    break
            surface = self._piece_surface(piece)
            if not owners or len(owners) != 1:
                raise MeshFailure(
                    f'{surface.get("name")!r} is an '
                    f'{self._role_sheet_name(surface)} and lies inside no '
                    'single volume of this model, so there is nothing to '
                    'embed it in. Move it into the region it belongs to, or '
                    'make it an external boundary.')
            volume = int(topology.shell(owners[0].name).volume_tag)
            by_volume.setdefault(volume, []).append(int(piece))
            embedded.append(str(surface.get('name') or piece))
        for volume, tags in sorted(by_volume.items()):
            gmsh.model.mesh.embed(2, tags, 3, volume)
        self.embedded_role_sheets = dict(by_volume)
        names = sorted(set(embedded))
        imprinted = (self.statistics.get('surfaceRoles') or {}).get(
            'imprinted') or {}
        self.ledger.record(
            'surfaceRoles/embedded', names, names,
            note='embedded in the volume holding '
                 + ('it' if len(names) == 1 else 'each')
                 + (', its edge imprinted on the walls it touches'
                    if imprinted.get('rimEdgesOnWalls') else ''))

    def embedded_sheet_pieces(self) -> set:
        """Every sheet piece :meth:`embed_tessellated_sheets` embedded."""
        return {int(tag) for tags in self.embedded_role_sheets.values()
                for tag in tags}

    def _points_of(self, tag) -> set:
        return {abs(int(point)) for _dim, point in self.gmsh.model.getBoundary(
            [(2, int(tag))], combined=False, oriented=False, recursive=True)}

    def sheets_to_carry(self, carved, surfaces) -> list:
        """The embedded sheet pieces of the *carved* volumes, to re-embed.

        DP-6064. The layer carve removes the volume a sheet is embedded in
        and builds the core from the layer tops and the rebuilt patches. A
        sheet clear of every wall is embedded in that core as it was (the
        CAD route keeps such a sheet whole the same way). A sheet that
        stands on a wall cannot be: its edge is on a surface the carve
        replaces, so in the core it would end in mid-air on one side and
        cut through the prism stack on the other. MEASURED on the CAD route
        with the same geometry (sr9h duct, three layers on every wall): a
        plate on the walls fails "the boundary layer left no rim to rebuild
        the un-layered patches from", a full section "boundary layers are
        not supported on a 2-volume assembly". Refused here by name instead.
        """
        carved = {int(tag) for tag in carved}
        pieces = sorted(int(tag) for volume, tags
                        in self.embedded_role_sheets.items()
                        if int(volume) in carved for tag in tags)
        if not pieces:
            return []
        walls = {}
        for tag in surfaces:
            for point in self._points_of(tag):
                walls.setdefault(point, set()).add(int(tag))
        touching: dict = {}
        for piece in pieces:
            for point in self._points_of(piece):
                for wall in walls.get(point, ()):
                    touching.setdefault(piece, set()).add(wall)
        if touching:
            sheets = sorted({str(self._piece_surface(piece).get('name'))
                             for piece in touching})
            met = sorted({self.surface_patch_name(wall)[0]
                          for found in touching.values() for wall in found})
            raise MeshFailure(
                'boundary layers cannot be grown with '
                + ', '.join(repr(name) for name in sheets)
                + (' in the volume: it is' if len(sheets) == 1
                   else ' in the volume: they are')
                + ' an internal face or blocking baffle standing on '
                + ', '.join(met) + '. The layer is carved out of the volume '
                  'and the core rebuilt from its inner surfaces, and a sheet '
                  'whose edge is on the wall has nothing to stand on in that '
                  'core. Mesh this geometry without boundary layers, or move '
                  'the sheet clear of the walls.')
        return pieces

    def _piece_surface(self, piece) -> dict:
        """The role surface a classified sheet piece came from."""
        origin = getattr(self, 'surface_origin', {}).get(int(piece))
        scopes = self.import_time_scopes()
        for surface in self.role_surfaces(INTERNAL, BAFFLE):
            tags = {int(tag) for tag in scopes.get(
                str(surface.get('scope') or '')) or ()}
            if int(piece) in tags or (origin is not None
                                      and int(origin) in tags):
                return surface
        return {'name': str(piece), 'role': INTERNAL}

    def tag_role_groups(self, grouped) -> list[str]:
        """Give each internal-face or baffle surface a physical group.

        A sheet embedded in one volume is a boundary of it by adjacency and
        is grouped with the boundary already; a face between two volumes is
        not, and without a group neither the face zone nor Crack can find
        it. Returns the names grouped here.
        """
        named = []
        for surface in self.role_surfaces(INTERNAL, BAFFLE):
            tags = sorted({int(tag) for tag in self.scope_surfaces.get(
                str(surface.get('scope') or '')) or ()})
            if not tags:
                continue
            name = self.surface_patch_name(tags[0])[0]
            if name in grouped:
                continue
            self.gmsh.model.addPhysicalGroup(2, tags, name=name)
            grouped[name] = tags
            named.append(name)
        return named

    def crack_baffles(self):
        """Split each baffle's mesh into two sides with the Crack plugin.

        Runs after the volume mesh (and after the duplicate-node weld, which
        would undo it). Each side becomes a boundary patch: the original
        group keeps the name, Crack's new group -- which it leaves unnamed --
        is named ``<name>_slave``. A baffle Crack duplicated no node of is
        refused: its two sides would be one face and nothing would block.
        """
        surfaces = self.role_surfaces(BAFFLE)
        if not surfaces:
            return
        gmsh = self.gmsh
        groups = {gmsh.model.getPhysicalName(dim, tag): tag
                  for dim, tag in gmsh.model.getPhysicalGroups(2)}
        if not groups:
            # A session meshing before its groups stage: nothing is named
            # yet, and the groups stage's mesh is the one that is cracked.
            return
        cracked = []
        for surface in surfaces:
            tags = sorted({int(tag) for tag in self.scope_surfaces.get(
                str(surface.get('scope') or '')) or ()})
            if not tags:
                raise MeshFailure(
                    f'the blocking baffle {surface.get("name")!r} reached no '
                    'surface of the meshed model')
            name = self.surface_patch_name(tags[0])[0]
            if f'{name}{BAFFLE_SLAVE_SUFFIX}' in groups:
                # The session meshes again on the same model: cracked once.
                continue
            physical = groups.get(name)
            if physical is None:
                raise MeshFailure(
                    f'the blocking baffle {name!r} has no physical group to '
                    'crack')
            before = len(gmsh.model.mesh.getNodes()[0])
            new_tag = max(tag for _dim, tag
                          in gmsh.model.getPhysicalGroups()) + 1
            gmsh.plugin.setNumber('Crack', 'Dimension', 2)
            gmsh.plugin.setNumber('Crack', 'PhysicalGroup', int(physical))
            gmsh.plugin.setNumber('Crack', 'NewPhysicalGroup', int(new_tag))
            gmsh.plugin.run('Crack')
            after = len(gmsh.model.mesh.getNodes()[0])
            if after <= before:
                raise MeshFailure(
                    f'the blocking baffle {name!r} was cracked and no node '
                    'was duplicated, so its two sides are still one face. '
                    'Check that the surface lies inside the volume.')
            slave = f'{name}{BAFFLE_SLAVE_SUFFIX}'
            gmsh.model.setPhysicalName(2, int(new_tag), slave)
            cracked.append({'name': name, 'slave': slave,
                            'duplicatedNodes': after - before})
        self.statistics['surfaceRoles'] = {
            **dict(self.statistics.get('surfaceRoles') or {}),
            'baffles': cracked}
        self.ledger.record('surfaceRoles/baffles',
                           [item['name'] for item in cracked],
                           [item['slave'] for item in cracked],
                           note='each baffle cracked into two wall sides')

    def record_internal_faces(self):
        """Say which internal faces the mesh holds, for the publisher."""
        internal = []
        for surface in self.role_surfaces(INTERNAL):
            tags = sorted({int(tag) for tag in self.scope_surfaces.get(
                str(surface.get('scope') or '')) or ()})
            if tags:
                internal.append(self.surface_patch_name(tags[0])[0])
        if internal:
            self.statistics['surfaceRoles'] = {
                **dict(self.statistics.get('surfaceRoles') or {}),
                'internalFaces': sorted(set(internal))}


__all__ = ('BAFFLE', 'BAFFLE_SLAVE_SUFFIX', 'INTERNAL', 'RolesStage')
