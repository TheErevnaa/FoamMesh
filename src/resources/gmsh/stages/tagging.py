"""Physical groups: the boundary and the volumes named for publication (Plan 48 RR)."""
from __future__ import annotations


class TaggingStage:
    def record_boundary_surfaces(self):
        """Remember which surfaces bound the volume, while that is still true.

        R65. The docstring below always said the adjacency test had to happen
        "before any extrusion has confused it", but ``execute`` called it
        after ``apply_boundary_layers``, which removes the imported volume,
        extrudes every surface and rebuilds the core from the inner faces.
        MEASURED on a five-patch tee: no surface passed ``len(upward) == 1``
        any more, so the run recorded ``boundarySurfaces: 0``, wrote no 2-D
        physical group at all, and publication refused the finished
        90,724-cell mesh with "11118 boundary faces but only 0 are named by a
        physical group". The surface tags themselves survive the extrusion --
        they are its base -- so the list is taken here and used to name them
        later.
        """
        if self.planar:
            # FC-E. One dimension down: a curve bounding exactly one surface
            # is the section's edge, exactly as a surface bounding exactly one
            # volume is a solid's. A curve two surfaces share is interior and
            # stays unnamed, which is also what keeps its line elements out of
            # the .msh -- Gmsh writes only what a physical group claims.
            curves = []
            for _dim, tag in self.gmsh.model.getEntities(1):
                upward, _down = self.gmsh.model.getAdjacencies(1, tag)
                if len(upward) == 1:
                    curves.append(tag)
            self.boundary_curves = curves
            self.boundary_surfaces = []
            return []
        # DP-5716. The groups a build made (or the group table renamed) name
        # the surfaces they hold, read before the layers change the model.
        if getattr(self, 'geometry_built', False):
            self.take_build_groups()
        boundary = []
        for _dim, tag in self.gmsh.model.getEntities(2):
            upward, _down = self.gmsh.model.getAdjacencies(2, tag)
            if len(upward) == 1:
                boundary.append(tag)
        self.boundary_surfaces = boundary
        return boundary

    def tag_physical_groups(self):
        """Name the real boundary and the volumes so publication can type them.

        Boundary surfaces are those adjacent to exactly one volume, which is
        established by ``record_boundary_surfaces`` before any extrusion can
        confuse the adjacency; the publication step re-derives the boundary
        from cell ownership and checks the two agree.
        """
        if self.planar:
            return self.tag_planar_groups()
        gmsh = self.gmsh
        boundary = getattr(self, 'boundary_surfaces', None)
        if boundary is None:
            boundary = self.record_boundary_surfaces()
        # DP-5716. On a built model the build's groups have named the
        # surfaces; they are taken off and made again below, one per patch.
        if getattr(self, 'geometry_built', False):
            self.clear_build_groups()
        # Plan 28 WP7. Every piece classification cut out of one imported
        # solid goes into that solid's group, so a named STL surface is one
        # patch in the polyMesh however many pieces it became.
        grouped: dict[str, list[int]] = {}
        for tag in boundary:
            name, _known = self.surface_patch_name(tag)
            # R118. A patch left out of the boundary layer was deleted and
            # rebuilt from the extrusion's rim; the faces that replaced it are
            # the patch now, so the name follows them rather than a tag the
            # model no longer has.
            members = self.layer_replacements.get(tag)
            grouped.setdefault(name, []).extend(
                members if members is not None else [tag])
        named = []
        for name, tags in grouped.items():
            if not tags:
                continue
            gmsh.model.addPhysicalGroup(2, tags, name=name)
            named.append(name)
        # Plan 49 SR5. An internal face or a baffle between two volumes is
        # no boundary by adjacency, and needs a group all the same.
        named.extend(self.tag_role_groups(grouped))
        volumes = [tag for _dim, tag in gmsh.model.getEntities(3)]
        volume_names = self.job.get('volumeNames') or {}

        def volume_name(tag):
            # C31-04. As for the surfaces: the identity map names this tag,
            # the version-1 map names a position two sources both claim.
            if getattr(self, 'geometry_built', False):
                return self.region_name(tag) or f'volume_{tag}'
            return (self.entity_volume_names.get(int(tag))
                    or volume_names.get(str(tag)) or f'volume_{tag}')

        # R118. A volume a layer was carved out of no longer exists, and the
        # layer volumes and the rebuilt core that replaced it are all still
        # the region the user named -- so they publish as one physical group
        # under that name, and none of them is named after a tag the model
        # reused underneath it.
        present = {int(tag) for tag in volumes}
        claimed = set()
        for original, members in self.volume_replacements.items():
            members = [int(tag) for tag in members if int(tag) in present]
            if not members:
                continue
            gmsh.model.addPhysicalGroup(3, members, name=volume_name(original))
            claimed.update(members)
        for tag in volumes:
            if int(tag) in claimed:
                continue
            gmsh.model.addPhysicalGroup(3, [tag], name=volume_name(tag))
        # DP-63. Two different numbers, and a reader downstream needs the
        # second one. `boundarySurfaces` is how many surface entities the
        # model has; `boundaryGroups` is how many patches they were grouped
        # into three lines above, which is what becomes a physical group and
        # therefore what a `.msh` or a `.su2` carries as a marker. They are
        # equal only when every surface has its own name. The SU2 export held
        # its marker count against the first and refused every mesh made from
        # a single-solid STL.
        self.statistics['groups'] = {
            'boundarySurfaces': len(boundary),
            'boundaryGroups': len(named),
            'boundaryGroupNames': sorted(named),
            'volumes': len(volumes)}

    def tag_planar_groups(self):
        """Name a section's boundary curves and its faces.

        FC-E. Everything drops one dimension on this route: the curves are the
        patches and the faces are the regions, because the publisher extrudes
        the section and each of them gains a dimension in doing so. Naming
        them is not cosmetic -- Gmsh writes only elements a physical group
        claims, so a boundary curve left unnamed here is a patch missing from
        the published polyMesh rather than a patch with a dull name.
        """
        gmsh = self.gmsh
        if not self.boundary_curves:
            self.record_boundary_surfaces()
        curves = self.boundary_curves
        # Named by tag, in import order. The prepared-geometry store names
        # surfaces and volumes, because a group in it is a set of faces; it
        # has nothing to say about a curve. DP-675 (field audit 0924
        # gmsh-generate-export D11): the user names them instead, by the tag
        # a first run published as `edge_<tag>`; a curve not named keeps it.
        names = self.planar_curve_names(curves)
        grouped = {}
        for tag in curves:
            grouped.setdefault(names.get(int(tag), f'edge_{tag}'),
                               []).append(tag)
        for name, tags in grouped.items():
            gmsh.model.addPhysicalGroup(1, tags, name=name)
        sections = [tag for _dim, tag in gmsh.model.getEntities(2)]
        region_names = self.job.get('volumeNames') or {}
        for tag in sections:
            name = (self.entity_surface_names.get(int(tag))
                    or region_names.get(str(tag)) or f'region_{tag}')
            gmsh.model.addPhysicalGroup(2, [tag], name=name)
        # DP-63. One group per curve on this route, so the two counts agree
        # here -- they are recorded separately anyway, because a reader that
        # has to know which of them it is holding is the fault being fixed.
        self.statistics['groups'] = {
            'boundarySurfaces': 0, 'volumes': 0,
            'boundaryGroups': len(grouped),
            'boundaryGroupNames': sorted(grouped),
            'boundaryCurves': len(curves), 'sections': len(sections),
            'curves': self.describe_curves(curves, names)}

    def planar_curve_names(self, curves):
        """``{curve tag: patch name}`` from the job, checked against the model.

        DP-675. A tag that is not a boundary curve of this section names
        nothing; that is said in the ledger and the warnings rather than
        dropped, and every name that did land is recorded as applied.
        """
        wanted = self.dimensionality.get('edgeNames') or {}
        present = {int(tag) for tag in curves}
        names, missing = {}, []
        for name, tags in wanted.items():
            for tag in tags:
                if int(tag) in present:
                    names[int(tag)] = str(name)
                else:
                    missing.append(f'{name}: {tag}')
        if wanted:
            self.ledger.record(
                'gmsh/dimensionality/edgeNames',
                {str(name): list(tags) for name, tags in wanted.items()},
                {name: sorted(tag for tag, other in names.items()
                              if other == name)
                 for name in wanted},
                applied=bool(names), matched=not missing,
                note=('' if not missing else
                      'no boundary curve of this section has tag '
                      + ', '.join(missing) + '; the boundary curves are '
                      + ', '.join(str(tag) for tag in sorted(present))))
        if missing:
            self.warnings.append(
                'edge names: ' + ', '.join(missing) + ' named no boundary '
                'curve of this section; its boundary curves are '
                + ', '.join(f'edge_{tag}' for tag in sorted(present)))
        return names

    def describe_curves(self, curves, names):
        """Each boundary curve's tag, patch, length and midpoint.

        DP-675. The tag is what the edge-name field is keyed by, so the run
        says where each one is.
        """
        described = []
        for tag in curves:
            item = {'tag': int(tag),
                    'patch': names.get(int(tag), f'edge_{tag}')}
            try:
                low, high = self.gmsh.model.getParametrizationBounds(1, tag)
                mid = self.gmsh.model.getValue(
                    1, tag, [0.5 * (low[0] + high[0])])
                item['midpoint'] = [round(float(value), 9) for value in mid]
                item['length'] = round(float(self.gmsh.model.occ.getMass(
                    1, tag)), 9)
            except Exception:
                pass
            described.append(item)
        return described
