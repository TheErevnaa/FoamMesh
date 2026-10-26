"""Boundary layers (Plan 48 RR, DP-4009).

Moved out of ``runner_v1.py``; the batch run and the resident session grow
the layer with one function, in one order, from one set of inputs."""
from __future__ import annotations

import math

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from layer_targets import (
    MODE_ALL_WALLS, eligible_wall_names, normalise_mode)
from quantities import agreeing, count_text

from .common import (MeshFailure)


class LayersStage:
    def apply_boundary_layers(self):
        """Grow prism layers, carving them out of the imported volume.

        MEASURED: skipping the carve leaves the original volume intact and
        stacks the prisms on top of it. The tet mesh comes back bit-identical
        to the unlayered one, no cell is inverted, and the total volume is
        8.75% too large.
        """
        layers = self.intent.get('layers') or {}
        if not layers.get('enabled'):
            return
        gmsh = self.gmsh
        heights = [float(item) for item in layers['cumulativeHeights']]
        count = int(layers['layerCount'])
        elements = int(layers.get('elementsPerLayer') or 1)

        # DP-6064. A sheet embedded in a volume (an STL internal face or
        # baffle) bounds nothing: no layer grows on it and nothing is rebuilt
        # for it. It is embedded again in the core the carve leaves.
        sheets = self.embedded_sheet_pieces()
        surfaces = [tag for _dim, tag in gmsh.model.getEntities(2)
                    if int(tag) not in sheets]
        volumes = [tag for _dim, tag in gmsh.model.getEntities(3)]
        self.layer_patches_replaced = []
        selected, skipped = self.layer_surfaces(surfaces, layers)
        # Plan 41 GN10 (M-34). A patch with a stack row of its own grows that
        # stack and nothing else: its row selects it, whatever the list above
        # says, and the list's stack is kept for every other patch.
        stack_rows = [dict(row) for row in (layers.get('stacks') or ())]
        stack_of = {}
        for number, row in enumerate(stack_rows, start=1):
            chosen, _left = self.layer_surfaces(
                surfaces, {**layers, 'patchMode': 'selected',
                           'patches': [row['patch']]})
            for tag in chosen:
                stack_of[int(tag)] = number
        if stack_of:
            held = {int(tag) for tag in selected} | set(stack_of)
            selected = [tag for tag in surfaces if int(tag) in held]
            skipped = [tag for tag in surfaces if int(tag) not in held]
        selected, skipped = self.periodic_sides_left_bare(
            selected, skipped, layers, stack_of)
        if not selected and getattr(self, 'layer_patches_replaced', None):
            # DP-5716. The walls the layers were asked for are gone: the
            # geometry build replaced them. Said, recorded, and not grown.
            note = self.layers_replaced_note()
            self.warnings.append(note)
            self.ledger.record('gmsh/boundaryLayers/layerCount', count, 0,
                               applied=False, note=note)
            self.statistics['layersDropped'] = {
                'reason': 'geometry_build_replaced_patches',
                'patches': list(self.layer_patches_replaced),
                'note': note}
            return
        thin = self.one_cell_thick_build()
        if thin and self.grow_in_plane_layers(selected, thin, layers,
                                              stack_of, stack_rows):
            return
        if thin:
            # DP-5717. A 2D-case or wedge build is one cell thick: a layer
            # there rebuilt the slab as tetrahedra, MEASURED 520 inverted
            # (pipe -> case_2d, layers on face_2..5). Said, not grown --
            # now only where no ticked wall meets the front or back face
            # (DP-5718 grows the rest in the plane).
            note = self.one_cell_layers_note(thin)
            self.warnings.append(note)
            self.ledger.record('gmsh/boundaryLayers/layerCount', count, 0,
                               applied=False, note=note)
            self.statistics['layersDropped'] = {
                'reason': 'geometry_build_one_cell_thick',
                'patches': list(thin), 'note': note}
            return
        if selected and getattr(self, 'layer_patches_replaced', None):
            self.warnings.append(
                'the geometry build replaced '
                + ', '.join(self.layer_patches_replaced) + ', so the '
                'boundary layers are grown on the other ticked walls only')
        if not selected:
            # Growing on nothing is not the same as growing on everything, and
            # quietly doing the latter is how a user gets prisms on an inlet.
            #
            # DP-57. This warned and returned, and the run then published as
            # `succeeded` with `statistics.layers: null`. MEASURED across the
            # 66 Gmsh runs this repository has published: 23 asked for layers
            # and grew none, and 20 of those reported success. The page
            # pre-ticks the wall patches, so a selection is nearly always
            # present; where the import carries no patch names of its own,
            # every offered name misses and the whole selection empties. A
            # mesh with no near-wall resolution is not the mesh that was asked
            # for, and a warning on a run marked successful did not stop one
            # of those twenty from being used.
            self.ledger.record('gmsh/boundaryLayers/layerCount', count, 0,
                               applied=False,
                               note='no selected patch matched a surface')
            asked = ', '.join(
                str(item).strip() for item in (layers.get('patches') or ())
                if str(item).strip())
            offer = ', '.join(sorted({self.surface_patch_name(tag)[0]
                                      for tag in surfaces})) or 'none'
            if not asked:
                # Plan 33 section 1.1. This case used to mesh, growing layers
                # on every boundary surface including the inlet and the
                # outlet. Silence is not consent to that.
                raise MeshFailure(
                    'boundary layers are switched on and no surface was '
                    'chosen to grow them, so no layer could be grown and the '
                    'mesh would have had no near-wall resolution at all. The '
                    f'surfaces this run imported are: {offer}. Choose one of '
                    'those, ask for every eligible wall, or turn boundary '
                    'layers off.')
            raise MeshFailure(
                f'boundary layers were asked for on {asked}, which matched no '
                'imported surface, so no layer could be grown and the mesh '
                'would have had no near-wall resolution at all. The surfaces '
                f'this run imported are: {offer}. Name one of those, or turn '
                'boundary layers off.')
        # R118. The carve removes the volume the layer grows into and rebuilds
        # it from the layer's inner surfaces. That can be done on an assembly
        # as long as every base bounds the same volume: the others are left
        # exactly as they were imported, and the interface is the very surface
        # the prisms grew from, so the two sides stay conformal. MEASURED on
        # `annulus_shell.step`, three layers, the two cylindrical walls only
        # and the end caps rebuilt -- solid 21769 tets, interface layer 10716
        # prisms, outer layer 17754 prisms, fluid core 34065 tets, 0 inverted,
        # and 12550829 mm3 against an analytic 12566371, 0.12% low, which is
        # the faceting of the two cylinders. What cannot be done is a
        # selection that spans volumes: only one core is rebuilt.
        carved = list(volumes)
        if len(volumes) > 1:
            core, candidates = self.volume_the_layer_grows_into(volumes,
                                                                selected)
            if core is None and candidates:
                raise MeshFailure(
                    'the boundary layer was asked for on surfaces that bound '
                    f'{len(candidates)} volumes and on nothing else, so there '
                    'is no way to tell which side of the interface it belongs '
                    'on. Name a patch that bounds only the volume the layer '
                    'is for as well.')
            if core is None:
                raise MeshFailure(
                    f'boundary layers are not supported on a {len(volumes)}'
                    '-volume assembly unless every patch they grow on bounds '
                    'one and the same volume: the layer is carved out of a '
                    'single core, and a selection spanning more than one '
                    'leaves neither side closed. Name the patches of one '
                    'volume, mesh this geometry without layers, or split it '
                    'into one job per volume.')
            carved = [core]
            surfaces = self.volume_surfaces(core)
            chosen = set(int(tag) for tag in selected)
            skipped = [tag for tag in surfaces if int(tag) not in chosen]
            selected, skipped = self.keep_shared_surfaces(volumes, selected,
                                                          skipped)
        # The bounding curves have to be read while the surface still exists;
        # they are what ties the faces that replace it back to its patch name.
        # The boxes are read here for the same reason, and check that what the
        # rebuild closes really is where the un-layered patch was.
        skipped_curves = {tag: self.surface_curves(tag) for tag in skipped}
        # DP-6054. Kept for `apply_periodic`: the edges a periodic side shares
        # with the walls around it, which outlive the side itself.
        self.layer_side_curves = {int(tag): [abs(int(curve)) for curve in curves]
                                  for tag, curves in skipped_curves.items()}
        skipped_boxes = {tag: gmsh.model.getBoundingBox(2, tag)
                         for tag in skipped}
        # DP-55. The sign of `heights` is a direction along the surface
        # normal, and one sign cannot serve two shells. Each shell's normals
        # face away from its own interior, so on the outermost one the fluid
        # lies at -n and on an obstacle inside it the fluid lies at +n. Grown
        # with a single sign the obstacle's layer goes into the solid: MEASURED
        # on `box_with_obstacle_two_files`, 2082 open cells, 2082 wrongly
        # oriented face pyramids, 2082 concave cells, four failed checkMesh
        # checks -- and a volume of 63.156 where the fluid is 63.000, because
        # the shell around the obstacle was covered twice.
        #
        # `void_surfaces` and `surface_winding` answer this where the shell
        # topology ran. It does not run on the CAD route, which arrives here
        # with nothing meshed at all, so the boundary is split into shells and
        # the enclosed ones handed to `layer_height_groups` as well.
        shells = self.boundary_shells(surfaces)
        internal = self.internal_shells(shells)
        # DP-124. A shell of the core the layer never touches needs no
        # rebuild at all. MEASURED on `two_solid_block.stl`, a box inside a
        # box: the farfield is one volume bounded by two disjoint shells, the
        # layer covers the outer one entirely, and so there is no rim
        # anywhere -- every curve of every layer top is shared with another
        # top. The inner shell was handed to the rebuild regardless, which
        # found no rim to stand in for it and refused the run. It is already
        # closed, already meshed and already named; it just has to be handed
        # to `addVolume` as the hole it is.
        kept_shells = self.shells_kept_whole(shells, selected, internal)
        # DP-127. The mirror of the same reading. On `turbine_cascade` the
        # layer is on the blades and the farfield around them carries none,
        # so the shell the layer never reached is the enclosing one. It needs
        # no rim either; it is the exterior the fluid is bounded by, and the
        # layer tops are the holes in it.
        outer_shell = self.shell_kept_as_exterior(shells, selected, internal)
        retained = {tag for group in kept_shells for tag in group}
        retained.update(int(tag) for tag in outer_shell)
        if retained:
            skipped = [tag for tag in skipped if int(tag) not in retained]
            skipped_curves = {tag: curves for tag, curves
                              in skipped_curves.items()
                              if int(tag) not in retained}
            skipped_boxes = {tag: box for tag, box in skipped_boxes.items()
                             if int(tag) not in retained}
        # DP-71. Nesting says a surface bounds a hole; it does not say which
        # way that surface faces, and the flip needs both. `outward_normals`
        # asks the solid directly, while it is still in the model.
        outward = ({} if self.surface_winding
                   else self.outward_normals(selected, carved))
        carried = self.sheets_to_carry(carved, surfaces)
        self.remove_entities([(3, tag) for tag in carved])

        # Plan 41 GN10 (M-34). Every stack is its own set of calls: the
        # heights, the elements in each layer and the cell shape are
        # arguments of `extrudeBoundaryLayer`, so patches with different
        # stacks cannot share one. The list's stack is stack 0.
        stacks = {0: {'heights': heights, 'count': count,
                      'elements': elements, 'name': '',
                      'quads': bool(layers.get('quads', True))}}
        for number, row in enumerate(stack_rows, start=1):
            stacks[number] = {
                'heights': [float(item) for item in row['cumulativeHeights']],
                'count': int(row['layerCount']),
                'elements': int(row.get('elementsPerLayer') or 1),
                'quads': bool(row.get('quads', True)), 'name': row['patch']}
        calls, inward, refusal = [], [], ''
        for number in sorted({stack_of.get(int(tag), 0) for tag in selected}):
            members = [tag for tag in selected
                       if stack_of.get(int(tag), 0) == number]
            stack = stacks[number]
            groups = self.layer_height_groups(
                members, stack['heights'], internal=internal,
                outward=outward)
            # DP-91. Two directions that meet on a shared edge extrude that
            # edge twice, and Gmsh says so only part-way through the 3D pass,
            # in a sentence with no patch name in it. Asked here, while the
            # patches still have names and before anything has been built.
            meetings = self.directions_that_meet(groups)
            inward.extend(tag for _signed, tags, flipped in groups if flipped
                          for tag in tags)
            # DP-404. The refusal below used to stand here, in front of the
            # extrusion. It stands behind it now, because the premise it was
            # written on is only half true: the *height* is signed once per
            # call, the *call* is not limited to one direction. A dim-tag
            # carries a sign of its own and `extrudeBoundaryLayer` reads it.
            if meetings:
                refusal = refusal or self.opposed_directions_refusal(
                    members, groups, meetings, skipped)
                groups = self.one_call_for_both_directions(groups)
            calls.extend((stack, signed, tags) for signed, tags, _flipped
                         in groups)
        # MEASURED on Gmsh 4.15.2 (duct.step, wall_y0 five layers of two
        # cells beside three walls of three layers): the run fails in the
        # volume pass with "Could not find extruded node ... in surface N",
        # a sentence that names a coordinate and no patch. The same row equal
        # to the list's stack meshes, so the cause is the unlike stacks on one
        # edge, and it is said here, before anything is built.
        contacts = self.stacks_that_meet(selected, stack_of, stacks)
        if contacts:
            raise MeshFailure(self.stack_contact_refusal(contacts))
        try:
            extruded = []
            columns = []
            for stack, signed, tags in calls:
                produced = list(gmsh.model.geo.extrudeBoundaryLayer(
                    [(2, tag) for tag in tags],
                    [stack['elements']] * stack['count'], signed,
                    stack['quads']))
                extruded.extend(produced)
                # DP-74. The inner face of each stack, paired with the wall it
                # grew from. The pairing only holds within one call: the two
                # directions are extruded separately and the returned list
                # runs in the order of the surfaces handed to that call.
                # DP-404. A tag in this list may be negative, which is how
                # the direction is carried when both senses go in one call.
                # What is paired with the stack is the surface, not the sense.
                columns.extend(zip([abs(int(tag)) for tag in tags], [
                    produced[index - 1][1]
                    for index in range(1, len(produced))
                    if produced[index][0] == 3
                    and produced[index - 1][0] == 2]))
            gmsh.model.geo.synchronize()
        except Exception as error:
            # DP-404. Where the two senses were put in one call and the call
            # still would not run, the contact is the thing to say: it is the
            # reading that names patches and offers a selection, and `error`
            # names neither.
            raise MeshFailure(
                refusal or f'the boundary layer could not be extruded: {error}'
            ) from error

        tops = [extruded[index - 1] for index in range(1, len(extruded))
                if extruded[index][0] == 3 and extruded[index - 1][0] == 2]
        if not tops:
            raise MeshFailure(
                'the boundary-layer extrusion produced no inner surfaces, so '
                'the core volume cannot be rebuilt')
        top_tags = [tag for _dim, tag in tops]
        laterals = list(dict.fromkeys(
            tag for dim, tag in extruded
            if dim == 2 and tag not in set(top_tags)))
        rebuilt_loops = (self.rebuild_unextruded(skipped, top_tags,
                                                 skipped_boxes, columns)
                         if skipped else {})
        rebuilt = list(rebuilt_loops)
        if outer_shell:
            # DP-127. The layer is inside, on bodies the kept shell encloses.
            # The exterior is that shell, whole, and each body's layer tops
            # close around it to make one hole -- so they are grouped by the
            # shell their bases stood on rather than poured into one loop.
            loops = [gmsh.model.geo.addSurfaceLoop(list(outer_shell))]
            loops.extend(gmsh.model.geo.addSurfaceLoop(group)
                         for group in self.layer_tops_by_shell(shells,
                                                               columns))
        else:
            loops = [gmsh.model.geo.addSurfaceLoop(top_tags + rebuilt)]
        # DP-124. Every kept shell is enclosed by the one the layer grew on --
        # `shells_kept_whole` will not keep one that is not -- so it is a hole
        # in the core, and the geo kernel spells a hole as a surface loop
        # after the first. Same shape the tessellated import builds its voids
        # with, above.
        loops.extend(gmsh.model.geo.addSurfaceLoop(list(group))
                     for group in kept_shells)
        core_volume = gmsh.model.geo.addVolume(loops)
        gmsh.model.geo.synchronize()
        if carried:
            gmsh.model.mesh.embed(2, carried, 3, int(core_volume))
            self.embedded_role_sheets = {
                **{volume: tags for volume, tags
                   in self.embedded_role_sheets.items()
                   if int(volume) not in {int(tag) for tag in carved}},
                int(core_volume): list(carried)}
        if skipped:
            self.name_layer_replacements(skipped_curves, rebuilt_loops,
                                         laterals)

        self.layer_bases = [int(tag) for tag in selected]
        self.layer_columns = [(int(base), int(top)) for base, top in columns]
        self.layer_volumes = [int(tag) for dim, tag in extruded if dim == 3]
        # R118. The carved volume is gone; these are what it became. A
        # region is not renamed by having a layer grown in it -- MEASURED on
        # `annulus_shell` before this: the fluid's own name landed on one of
        # its two layer volumes by tag reuse and its core published as
        # `volume_4`, and on the wrapped single-solid route 17 of the 18
        # volumes published as `volume_N`.
        self.volume_replacements = {
            int(tag): [int(core_volume)] + list(self.layer_volumes)
            for tag in carved}
        self.layer_laterals = [int(tag) for tag in laterals]
        self.statistics['layers'] = {
            'requestedLayers': count,
            'requestedFirstHeight': abs(heights[0]),
            'requestedTotalThickness': abs(heights[-1]),
            'extrudedSurfaces': len(selected),
            'skippedSurfaces': len(skipped),
            'rebuiltSurfaces': len(rebuilt),
            # DP-55. How the boundary divided, and how many bases had to be
            # grown the other way because they bound a hole rather than the
            # outside of the fluid, so a finished mesh can be read back
            # against the decision.
            'shells': len(shells),
            'reversedSurfaces': len(inward),
            # DP-71. How many of the bases had their direction measured
            # against the solid rather than inferred from nesting.
            'measuredSurfaces': len(outward),
            'patches': sorted({self.surface_patch_name(tag)[0]
                               for tag in selected}),
            # Plan 41 GN10 (M-34). What each patch was asked for, so the
            # measured stack can be read against its own request.
            'elementsPerLayer': elements,
            'requestedByPatch': {
                self.surface_patch_name(tag)[0]: {
                    'stack': stack_of.get(int(tag), 0),
                    'layers': stacks[stack_of.get(int(tag), 0)]['count'],
                    'elementsPerLayer':
                        stacks[stack_of.get(int(tag), 0)]['elements'],
                    'firstHeight': abs(
                        stacks[stack_of.get(int(tag), 0)]['heights'][0]),
                    'totalThickness': abs(
                        stacks[stack_of.get(int(tag), 0)]['heights'][-1]),
                    'quads': stacks[stack_of.get(int(tag), 0)]['quads']}
                for tag in selected},
            'stackCount': len(stacks) if stack_of else 1,
            'scope': layers.get('scope', 'all_boundary_surfaces'),
            # R118. Which volume was carved and rebuilt, and which ones were
            # left as they were imported. On a single-solid case this is the
            # only volume there was; on an assembly it says which side of the
            # interface the layer was grown into.
            'coreVolume': int(carved[0]) if len(carved) == 1 else None,
            'volumesLeftInPlace': sorted(
                int(tag) for tag in volumes if int(tag) not in
                {int(item) for item in carved}),
        }
        note = f'extruded off {count_text(len(selected), "surface")}'
        if inward:
            note += (f', {len(inward)} of them bounding a hole and grown the '
                     'other way, into the fluid')
        self.ledger.record('gmsh/boundaryLayers/layerCount', count, count,
                           note=note)
        self.ledger.record(
            'gmsh/boundaryLayers/elementsPerLayer', elements, elements,
            note='the cells each layer is divided into, in the first stack')
        if stack_rows:
            grown = sorted({stack_of[tag] for tag in stack_of
                            if int(tag) in {int(item) for item in selected}})
            self.ledger.record(
                'gmsh/boundaryLayers/stacks', len(stack_rows), len(grown),
                applied=len(grown) == len(stack_rows),
                note=f'{count_text(len(grown), "stack")} of its own grown '
                     'on ' + ', '.join(sorted(
                         stacks[number]['name'] for number in grown)))

    def periodic_sides_left_bare(self, selected, skipped, layers, stack_of):
        """*selected* and *skipped* with every periodic side moved to skipped.

        DP-6054. A periodic side is no wall: the flow leaves through it and
        comes back through its partner, whose mesh is copied onto it. A layer
        grown on it lands on top of that copy. MEASURED (MCP pass, duct.step,
        every eligible wall, a translation pair on the two ends): 2,408
        interior faces with both cells on one side, and the publish refused.
        Left bare, the side is rebuilt from the layer's rim like any patch
        without a layer, and ``apply_periodic`` pairs what it became.
        """
        periodic = self.periodic_side_tags()
        dropped = [tag for tag in selected if int(tag) in periodic]
        if not dropped:
            return selected, skipped
        names = sorted({self.surface_patch_name(tag)[0] for tag in dropped})
        note = ('no boundary layer on ' + ', '.join(names) + ': a side of a '
                'periodic pair is not a wall, its mesh is its partner\'s')
        # The sides left bare, which is what was meant: not a mismatch.
        self.ledger.record('gmsh/boundaryLayers/periodicSidesBare',
                           len(dropped), len(dropped), note=note)
        if (normalise_mode(layers.get('patchMode')) != MODE_ALL_WALLS
                or any(int(tag) in stack_of for tag in dropped)):
            # Asked for by name, so said; "every eligible wall" never meant
            # a periodic side.
            self.warnings.append(note)
        return ([tag for tag in selected if int(tag) not in periodic],
                list(skipped) + dropped)

    def layer_height_groups(self, selected, heights, internal=(),
                            outward=None):
        """Split the layer bases by the direction that grows into the fluid.

        DP-55. MEASURED on ``box_with_obstacle.stl``, a 4x4x4 box holed by a
        1x1x1 cube, three layers, every boundary surface selected. All 12
        facets of the inner shell are wound out of the cube, so their normals
        point into the fluid; the outer box's 12 are wound out of the fluid.
        One sign for both put 6258 of 500430 cells inside the obstacle - three
        layers off its 2082 wall faces, plus the twelve tetrahedra that closed
        the corner - 1040 of them at negative volume, and checkMesh named
        every cell that held the inner cap non-closed at an openness of
        exactly 1. A prism's two triangular caps carry all the area in the cap
        direction and cancel; reverse one and the sum equals the sum of the
        magnitudes, which is what openness 1 means. Measured again on the
        two-file form of the same geometry in leg t6-r2 of the Plan 31
        campaign: 2082 open cells, 2082 wrongly oriented face pyramids, 2082
        concave cells, four failed checkMesh checks, and 63.156 m3 of fluid
        where the analytic answer is 63.000.

        *internal* is the CAD route's answer to the same question. The shell
        topology runs on the tessellated route only, so an OCC import arrives
        here with no voids and no windings recorded; the surfaces of every
        shell the outermost one encloses are passed in instead. A nested shell
        can only be a hole by this point, because a multi-volume assembly was
        already refused above.

        DP-71. *internal* alone was never that answer, and reading it as one
        reversed the surfaces it should have left alone. `hole != inverted` is
        two readings and the tessellated route supplies both: a void shell is
        wound out of the solid it cuts, so its normals already point into the
        fluid, and the two readings cancel. `surface_winding` is written by
        the tessellated import and by nothing else, so on the CAD route
        `inverted` was False for every surface in the run and every enclosed
        shell was reversed on the strength of half a test. MEASURED on
        `box_with_cavity.step`: three layers of prisms grown into a spherical
        cavity, 5974 open cells, and 0.0712 m3 of fluid where the analytic
        answer is 0.0568.

        *outward* is the reading that was missing, and it is a measurement
        rather than an inference: for each base, whether the surface's own
        normal -- the direction ``extrudeBoundaryLayer`` follows -- leaves the
        fluid. Where it answers it decides, because it answers the question
        the other two only approach. Probed across the 22 CAD files of the
        catalogue, one of them holed: every surface of every single-solid file
        answers, and every one of them faces out of the fluid.

        The sign the intent carries is kept as the outer shell's direction, so
        a geometry with no holes extrudes exactly as it did before.
        """
        base = list(heights)
        opposite = [-value for value in heights]
        enclosed = {int(tag) for tag in internal}
        measured = dict(outward or {})
        keep, flip = [], []
        for tag in selected:
            if int(tag) in measured:
                (keep if measured[int(tag)] else flip).append(tag)
                continue
            inverted = self.surface_winding.get(int(tag), 1) < 0
            hole = int(tag) in self.void_surfaces or int(tag) in enclosed
            # A void shell and an inverted shell each reverse the normal; both
            # together cancel back to the outer shell's direction.
            (flip if hole != inverted else keep).append(tag)
        groups = []
        if keep:
            groups.append((base, keep, False))
        if flip:
            groups.append((opposite, flip, True))
            self.warnings.append(
                f'{count_text(len(flip), "boundary layer base")} '
                f'{agreeing(len(flip), "bounds", "bound")} a hole in the '
                f'volume and {agreeing(len(flip), "was", "were")} grown '
                'inwards from it, away from the void')
        return groups

    def stacks_that_meet(self, selected, stack_of, stacks):
        """Which patches with different stacks share an edge.

        Plan 41 GN10. One edge is extruded by one call, and a call carries one
        stack. Patches with unlike stacks that meet on an edge are returned
        as ``(tag, other tag)`` so the failure can name them.
        """
        if len({stack_of.get(int(tag), 0) for tag in selected}) < 2:
            return []
        curves = {int(tag): set(self.surface_curves(tag)) for tag in selected}
        meetings = []
        ordered = [int(tag) for tag in selected]
        for index, tag in enumerate(ordered):
            for mate in ordered[index + 1:]:
                if (stack_of.get(tag, 0) != stack_of.get(mate, 0)
                        and curves[tag] & curves[mate]):
                    meetings.append((tag, mate))
        return meetings

    def stack_contact_refusal(self, meetings):
        names = sorted({self.surface_patch_name(tag)[0]
                        for pair in meetings for tag in pair})
        return ('layer stacks that differ were asked for on patches that '
                f'share an edge ({", ".join(names)}): an edge is extruded '
                'once, by one stack, so the stacks cannot differ across it. '
                'Give the neighbouring patches the same stack, or put a '
                'patch without layers between them.')

    def directions_that_meet(self, groups):
        """Which layer bases grow opposite ways and share an edge.

        DP-91. ``extrudeBoundaryLayer`` takes one signed height per call, so
        bases that grow towards each other are extruded in two calls -- and a
        curve on the boundary between them is then extruded twice, once in
        each direction. Gmsh does not notice while the geometry is built; it
        notices during the 3D pass, when the second call looks for a node the
        first one moved somewhere else.

        MEASURED on `annulus_shell.step` with layers on every wall of the
        fluid annulus. The interface with the bore is wound out of the bore,
        so its normal points into the fluid and it is grown the other way;
        the outer cylinder and the two end caps are grown along -n. The
        interface shares curve 3 with one cap and curve 2 with the other, and
        the run failed 12 s in with `Could not find extruded node
        (0.06016981, 1.1304e-08, -0.00017653) in surface 63` -- a point on the
        bore radius, one layer height off the cap. Nothing in that names a
        patch, and the same selection is what the Boundary Layers page
        proposes for this geometry.

        Returns the pairs that touch, as ``(tag, other tag)``.
        """
        if len(groups) < 2:
            return []
        curves = {}
        for _signed, tags, _flipped in groups:
            for tag in tags:
                curves[int(tag)] = set(self.surface_curves(tag))
        meetings = set()
        for index in range(len(groups)):
            for other in range(index + 1, len(groups)):
                for tag in groups[index][1]:
                    for mate in groups[other][1]:
                        if curves[int(tag)] & curves[int(mate)]:
                            meetings.add((int(tag), int(mate)))
        return sorted(meetings)

    def one_call_for_both_directions(self, groups):
        """Both senses in one group, the sense carried on each tag.

        DP-404. `directions_that_meet` finds bases that grow opposite ways
        along their own normals and share a curve, and DP-91 refused them,
        because two senses meant two calls to `extrudeBoundaryLayer` and a
        curve handed to both is extruded twice. The height is indeed signed
        once per call. The call is not: a dim-tag carries a sign of its own.

        MEASURED on Gmsh 4.15.2, one unit square, one layer, reading back
        where the nodes landed:

            tag +1  height +0.1  ->  nodes span z 0.0 .. 0.1
            tag +1  height -0.1  ->  nodes span z -0.1 .. 0.0
            tag -1  height +0.1  ->  nodes span z -0.1 .. 0.0
            tag -1  height -0.1  ->  nodes span z 0.0 .. 0.1

        Negating the tag is exactly negating the height, so `(2, -tag)` at the
        base heights grows where `(2, tag)` at the opposite heights grows, and
        one call can hold both.

        MEASURED again on the contact itself -- two unit boxes fragmented into
        a shared face, the core taken as the volume that face_s normal enters,
        layers of 3 on its five outer walls and on its half of the partition,
        which share the partition_s whole rim:

            two calls, one signed height each
                FAILED  Could not find extruded node
                        (1.051477373670381, 0.2, 1.051477373670381)
                        in surface 152
            one call, the sense carried on each tag
                MESHED  1,200 prisms

        The first line is DP-91_s failure, in DP-91_s words. The second is the
        same geometry, the same heights and the same selection.

        This is reached only where the two-call arrangement is measured not to
        work -- a contact was found -- so every selection that extrudes today
        extrudes exactly as it did.
        """
        base = next((list(signed) for signed, _tags, flipped in groups
                     if not flipped), None)
        if base is None:
            # Every group flipped: there is no contact to resolve, but the
            # sense the tags will carry has to be read against something.
            base = [-value for value in groups[0][0]] if groups else []
        tags = []
        for _signed, group, flipped in groups:
            tags.extend((-abs(int(tag)) if flipped else abs(int(tag)))
                        for tag in group)
        return [(base, tags, False)]

    def opposed_directions_refusal(self, selected, groups, meetings, skipped):
        """What to say when the contact cannot be extruded at all.

        DP-91 wrote this, and DP-404 moved it: it is the sentence the run
        makes when a contact was found *and* the one call that holds both
        senses would not run either. Every fact in it is read before anything
        is built, which is why it can still name patches -- the Gmsh failure
        it stands in for arrives part-way through the 3D pass and names a
        coordinate.
        """
        gmsh = self.gmsh
        boxes = {int(tag): gmsh.model.getBoundingBox(2, int(tag))
                 for tag in list(selected) + list(skipped)}
        touching = sorted({self.surface_patch_name(tag)[0]
                           for pair in meetings for tag in pair})
        workable = self.selection_that_would_mesh(
            selected, groups, meetings, skipped, boxes)
        advice = ('Grow layers on a set of patches that all face the same '
                  'way, mesh one volume per job, or turn boundary layers '
                  'off.')
        if workable:
            names = sorted({self.surface_patch_name(tag)[0]
                            for tag in workable})
            advice = ('Layers on ' + ', '.join(names) + ' would mesh: '
                      'what is left over is flat, and a flat opening is '
                      'one the rebuild can close. Or mesh one volume per '
                      'job, or turn boundary layers off.')
        return ('boundary layers were asked for on both sides of an edge '
                f'shared by {", ".join(touching)}, and those patches are '
                'wound opposite ways: one layer grows along its surface '
                'normal and the other against it. A shared edge cannot be '
                'extruded in two directions at once. ' + advice)

    def selection_that_would_mesh(self, selected, groups, meetings,
                                  skipped, boxes):
        """A subset of this selection whose bases no longer meet head-on.

        DP-91. The refusal above is a dead end unless it says what to do
        instead, and the run holds every fact the answer needs. Dropping the
        patches on one side of the contact removes it; which side to drop is
        decided by :meth:`patch_lies_in_a_plane`, because whatever is dropped
        joins what :meth:`rebuild_unextruded` has to close with a flat face,
        and DP-90 refuses a curved opening.

        MEASURED on `annulus_shell.step` with layers on the whole fluid
        annulus: dropping the interface leaves the outer cylinder uncovered
        and curved, which DP-90 refuses; dropping the two end caps leaves the
        interface and the outer cylinder, which meshes -- 302474 cells and
        0.01256056 m3 against an analytic 0.01256637. Returns the surviving
        tags, the largest surviving selection first, or an empty list where
        neither side can be dropped.
        """
        chosen = {int(tag) for tag in selected}
        touching = {int(tag) for pair in meetings for tag in pair}
        left_out = [int(tag) for tag in skipped]
        curves = {tag: set(self.surface_curves(tag)) for tag in chosen}
        candidates = []
        for index, (_signed, tags, _flipped) in enumerate(groups):
            dropped = sorted(touching & {int(tag) for tag in tags})
            kept = sorted(chosen - set(dropped))
            if not dropped or not kept:
                continue
            # Dropping one side has to leave the other side clear of every
            # contact, not only of the ones this side was in.
            if any(curves[one] & curves[other] for one in kept
                   for other in kept if one < other
                   and self.grow_the_same_way(groups, one, other) is False):
                continue
            if all(self.patch_lies_in_a_plane(tag, boxes[tag])
                   for tag in dropped + left_out if tag in boxes):
                candidates.append((len(kept), index, kept))
        if not candidates:
            return []
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][2]

    @staticmethod
    def grow_the_same_way(groups, one, other):
        """Are these two bases extruded in the same call, and so the same way?"""
        homes = {}
        for index, (_signed, tags, _flipped) in enumerate(groups):
            for tag in tags:
                homes[int(tag)] = index
        if one not in homes or other not in homes:
            return None
        return homes[one] == homes[other]

    #: Where on a surface to sample it, as fractions of its parametric range.
    NORMAL_SAMPLES = (0.25, 0.5, 0.75)

    def outward_normals(self, selected, volumes):
        """Which of these surfaces have their own normal leaving the fluid.

        DP-71. ``extrudeBoundaryLayer`` walks each stack along the surface's
        own normal, so the only thing the direction decision needs to know is
        where that normal points -- and on the CAD route the solid it points
        out of, or into, is still in the model when the layers are grown.
        Nothing is meshed yet, which is why the nesting reading was reached
        for; a solid can be asked about a point without meshing it.

        Returns the surfaces it could answer for and leaves the rest to the
        readings that were here before.
        """
        gmsh = self.gmsh
        if not volumes:
            return {}
        try:
            box = gmsh.model.getBoundingBox(-1, -1)
            span = max(box[axis + 3] - box[axis] for axis in range(3))
        except Exception:
            return {}
        if not span:
            return {}
        answers = {}
        for tag in selected:
            verdict = self.normal_leaves_the_fluid(
                int(tag), volumes, span * 1e-4, span * 1e-6)
            if verdict is not None:
                answers[int(tag)] = verdict
        return answers

    def normal_leaves_the_fluid(self, tag, volumes, step, slack):
        """True, False, or None where the samples do not agree on one.

        DP-71. Sample the surface, step off the point along its normal by
        *step*, and ask the solid which side each step landed on. Leaving the
        fluid forwards and entering it backwards is a normal that faces out;
        the reverse is a normal that faces in; anything else is not an answer.

        Parametric bounds are a rectangle and a trimmed face is not, so a
        sample is dropped on its coordinates rather than trusted: a point
        outside the surface's own bounding box is not on the surface. MEASURED
        across the CAD catalogue -- every surface of every single-solid file
        answers, and the two that do not belong to the one file holding two
        solids, which cannot grow layers at all.

        ``getBoundary(..., oriented=True)`` was tried for this first and
        cannot serve: probed live on Gmsh 4.15.2 its signs are mixed within a
        single shell, and on ``box_with_cavity.step`` three of the box's six
        faces carry the sign opposite to the other three while all six face
        the same way.
        """
        gmsh = self.gmsh
        try:
            low, high = gmsh.model.getParametrizationBounds(2, tag)
            corners = gmsh.model.getBoundingBox(2, tag)
        except Exception:
            return None
        votes = []
        for first in self.NORMAL_SAMPLES:
            for second in self.NORMAL_SAMPLES:
                params = [low[0] + (high[0] - low[0]) * first,
                          low[1] + (high[1] - low[1]) * second]
                try:
                    point = gmsh.model.getValue(2, tag, params)
                    normal = gmsh.model.getNormal(tag, params)
                    if any(not corners[axis] - slack <= point[axis]
                           <= corners[axis + 3] + slack for axis in range(3)):
                        continue
                    ahead = [point[axis] + normal[axis] * step
                             for axis in range(3)]
                    behind = [point[axis] - normal[axis] * step
                              for axis in range(3)]
                    forward = any(gmsh.model.isInside(3, volume, ahead)
                                  for volume in volumes)
                    backward = any(gmsh.model.isInside(3, volume, behind)
                                   for volume in volumes)
                except Exception:
                    return None
                if bool(forward) == bool(backward):
                    continue
                votes.append(not forward)
        if not votes or len(set(votes)) != 1:
            return None
        return votes[0]

    def volume_surfaces(self, volume):
        """The surfaces bounding one volume, unsigned.

        The sign `getBoundary` returns is not read anywhere: probed live on
        Gmsh 4.15.2 it is mixed within a single shell, which is why
        `normal_leaves_the_fluid` exists.
        """
        return [abs(int(tag)) for _dim, tag in
                self.gmsh.model.getBoundary([(3, int(volume))],
                                            oriented=False)]

    def surface_owners(self, volumes):
        """Which volumes each surface bounds, as ``{surface: {volume, ...}}``.

        One notion of adjacency for the two questions that need it: which
        single volume every layer base bounds, and which surfaces bound more
        than one. `getBoundary` is the source for both, so the second cannot
        answer differently from the first.
        """
        owners: dict[int, set] = {}
        for volume in volumes:
            for tag in self.volume_surfaces(volume):
                owners.setdefault(int(tag), set()).add(int(volume))
        return owners

    def volume_the_layer_grows_into(self, volumes, selected):
        """Which volume every selected base bounds, if there is just one.

        R118. A face shared by two volumes bounds both, so the answer is the
        intersection and not the union: on `annulus_shell.step` the fluid
        annulus is named by its outer wall alone, while the interface it
        shares with the solid bore names both and settles nothing.

        Returns ``(volume, None)`` when one volume carries every base,
        ``(None, candidates)`` when more than one still could -- a selection
        made entirely of shared faces -- and ``(None, [])`` when the bases
        span volumes and no single core can be rebuilt.
        """
        owners = self.surface_owners(volumes)
        common = {int(volume) for volume in volumes}
        for tag in selected:
            common &= owners.get(int(tag), set())
        if len(common) == 1:
            return next(iter(common)), None
        return None, sorted(common)

    def keep_shared_surfaces(self, volumes, selected, skipped):
        """Move a surface two volumes share out of ``skipped`` and into it.

        DP-398. `rebuild_unextruded` deletes every skipped surface and builds
        a plane across the rim in its place, because the layer's laterals lie
        in the neighbouring patch's plane and reusing the patch would cover it
        twice. On the outside of a volume that is right. On a surface two
        volumes share it is not: only the volume the layer is carved out of is
        rebuilt, so the carved side gets the new plane and the other side
        keeps the original, and an interface that was one face becomes two
        triangulations that meet nowhere.

        MEASURED on the four multiregion STEP fixtures. `shell_and_tube` and
        `coaxial_ducts` name their interface as a layer base -- `wall1` in
        both -- and publish, with the interface internal and every boundary
        face named. `tee_with_plug` names `wall4`..`wall7` and not its
        interface `wall3`; `baffled_chamber` names `wall7`..`wall12` and
        `wall14`, the copy the fusion had already deleted. Both refuse, with
        884 and 4,200 unnamed boundary faces covering 0.0020289 m2 and
        0.0159999 m2 -- twice 0.0010179 and twice 0.008, their two interface
        areas, present once from each side.

        A base survives the carve: the prisms stand on it and it goes on
        bounding both volumes. So a shared surface is made a base. The cost is
        a layer grown on the interface, which is what the two fixtures that
        already work already do, and what a conjugate interface wants.

        This is called once the core volume is known and `skipped` has been
        recomputed from that core's surfaces, so every tag it can reach bounds
        the core already; what it tests is whether the tag bounds something
        else as well.
        """
        # DP-57's refusal stands: growing on nothing is an answer, and this
        # is not the place to turn it into something. The caller has returned
        # on an empty selection long before here; the guard is kept so that
        # staying true of this method does not depend on the caller.
        if not selected:
            return selected, skipped
        owners = self.surface_owners(volumes)
        shared = [tag for tag in skipped if len(owners.get(int(tag), ())) > 1]
        if not shared:
            return selected, skipped
        names = sorted({self.surface_patch_name(tag)[0] for tag in shared})
        self.warnings.append(
            'boundary layers were not asked for on ' + ', '.join(names)
            + ', and each of those is shared by two regions. A patch left out '
            'of the layer is rebuilt for the region the layer is carved out '
            'of alone, which would leave the two regions meeting across two '
            'separate surfaces instead of one, so the layer was grown on '
            'them as well to keep the interface conformal.')
        for tag in shared:
            self.ledger.record(
                f'layerPatch:{self.surface_patch_name(tag)[0]}',
                False, True,
                note='shared by two regions: not asked for, grown anyway to '
                     'keep the interface one surface')
        keep = {int(tag) for tag in shared}
        return (list(selected) + list(shared),
                [tag for tag in skipped if int(tag) not in keep])

    def layer_surfaces(self, surfaces, layers):
        """Split the boundary into the surfaces that grow layers and the rest.

        R118. MEASURED on venturi.stl: with no selection every boundary surface
        is extruded, and the worst elements of the finished mesh sat on the
        inlet plane (z=0.004999) and the outlet plane (z=0.5937 / 0.5967) --
        the very stack that failed the quality gate of R113. Prism layers on an
        inlet and an outlet are wrong for every flow case, so the plan may name
        the patches that get them.

        Plan 33 section 1.1 retired the rest of that rule. An empty selection
        used to mean every boundary surface, which is the defect above with
        the user's own silence for a cause, and no reader of the page could
        tell the two apart. The plan now carries the choice as well as the
        list: ``selected`` is the list and nothing else, empty included, and
        ``all_eligible_walls`` reads the walls off the surfaces this run
        actually imported, through the same rule the page ticks its rows
        with.
        """
        mode = normalise_mode(layers.get('patchMode'))
        wanted = {str(item).strip() for item in (layers.get('patches') or ())
                  if str(item).strip()}
        if not mode:
            # A job written before the choice existed. Read it the way the
            # saved case it came from is read, so one plan cannot mean two
            # things depending on which side of the seam is looking.
            mode = MODE_ALL_WALLS if not wanted else 'selected'
        if mode == MODE_ALL_WALLS:
            # DP-867. Judged by the category each patch publishes with,
            # which the job carries; a name alone (`face0`) says nothing.
            categories = self.job.get('surfaceCategories') or {}
            names = [self.surface_patch_name(tag)[0] for tag in surfaces]
            wanted = set(eligible_wall_names(
                (name, categories.get(name, '')) for name in names))
            if not wanted and not getattr(self, 'geometry_built', False):
                self.warnings.append(
                    'every eligible wall was asked for and this import names '
                    'no boundary as a wall, so no layer could be grown. Name '
                    'the surfaces that are walls, or choose them here by '
                    'hand.')
                return [], list(surfaces)
        elif not wanted:
            # Growing on nothing is an answer. It is refused above this, in
            # `apply_boundary_layers`, rather than turned into everything.
            return [], list(surfaces)
        if getattr(self, 'geometry_built', False):
            # DP-5716. A geometry build replaced surfaces: a ticked wall it
            # took away is dropped, never re-bound to a new surface that has
            # its tag, and "every eligible wall" is the walls that were
            # ticked, not the unnamed faces of what the build made.
            asked = {str(item).strip() for item in (layers.get('patches')
                                                    or ()) if str(item).strip()}
            present = {self.surface_patch_name(tag)[0] for tag in surfaces}
            wanted = self.replaced_layer_patches(
                wanted, asked, mode == MODE_ALL_WALLS, present)
            if not wanted:
                return [], list(surfaces)
        # Plan 30 WP12, F-26. The comparison was exact, and on a tessellated
        # import the surfaces are called `face_3` while the page offers the
        # prepared patch names, so a selection matched nothing and the run
        # said only "matched no imported surface" -- with no way to learn what
        # the surfaces were called instead. Matching is now case- and
        # separator-insensitive, the generated `face_N` spelling is accepted
        # as well as the prepared name, and a miss says what was on offer.
        folded = {self._fold_patch_name(item): item for item in wanted}
        # DP-500. The generated spelling is a Gmsh tag, numbered from 1, and
        # a prepared CAD import names its faces `face0`, `face1`, ... from 0.
        # Folded, `face_2` (the tag of prepared `face1`) *is* `face2`, so a
        # selection of face2-face5 also grew prisms off the unselected outlet
        # face1. MEASURED on G4 `duct.step`: five surfaces extruded for four
        # selected, 37 prism columns standing on the outlet plane. A generated
        # alias that spells a name the prepared geometry declared belongs to
        # that name's surface, never to this one.
        declared = {self._fold_patch_name(item)
                    for item in self.declared_surface_names()}
        selected, skipped, present, matched = [], [], {}, set()
        for tag in surfaces:
            name, known = self.surface_patch_name(tag)
            present[name] = present.get(name, 0) + 1
            own = self._fold_patch_name(name)
            alias = self._fold_patch_name(
                f'face_{self.surface_origin.get(tag, tag)}')
            keys = {own}
            if alias == own or alias not in declared:
                keys.add(alias)
            # DP-410. Ditto: the face the fusion kept answers to both names.
            keys.update(self._fold_patch_name(alias) for alias
                        in self.merged_surface_names.get(int(tag), ()))
            hit = keys & set(folded)
            if hit:
                matched.update(folded[key] for key in hit)
                selected.append(tag)
            else:
                skipped.append(tag)
            if not known and hit:
                # The name matched the fallback, not a prepared patch. Worth
                # recording: the same job on renamed geometry would not match.
                self.ledger.record(
                    f'layerPatch:{name}', name, name,
                    note='matched a generated surface name, not a prepared '
                         'patch name')
        missing = sorted(wanted - matched)
        if missing:
            # DP-399. Two unlike things arrive here. A name the prepared
            # geometry never declared is a selection pointing at nothing, and
            # the list of what was imported answers it. A name the prepared
            # geometry *did* declare, whose surface this import no longer
            # has, is the run losing work the user did: the wall publishes
            # bare, and the layer summary -- which counts the patches it grew
            # on against the patches it found -- reports full coverage over
            # the ones that remain. Nothing in the finished case records that
            # a wall was ever asked for, which is why this one is a refusal.
            lost = [name for name in missing
                    if name in self.names_without_surface]
            offer = ', '.join(sorted(present)) or 'none'
            if lost:
                fused = (
                    f' Import fused '
                    f'{count_text(self.duplicate_faces_fused, "face")} as '
                    'duplicates, which is what takes a prepared name away; '
                    'turn off "remove duplicate faces" if these surfaces are '
                    'meant to stay separate.'
                    if self.duplicate_faces_fused else '')
                raise MeshFailure(
                    'boundary layers were asked for on ' + ', '.join(lost)
                    + ', which the prepared geometry names but no surface in '
                    'this model carries, so the layers cannot be grown and '
                    f'the patch would publish bare.{fused} The surfaces this '
                    f'run imported are: {offer}.')
            reason = (
                'boundary layers were asked for on ' + ', '.join(missing)
                + f', which matched no imported surface. The surfaces this '
                f'run imported are: {offer}.')
            if self.tessellated and not any(
                    self.surface_patch_name(tag)[1] for tag in surfaces):
                reason += (' A tessellated import carries no patch names of '
                           'its own, so the surfaces are numbered rather than '
                           'named; prepare the geometry with named groups, or '
                           'name the numbered faces above.')
            self.warnings.append(reason)
        return selected, skipped

    @staticmethod
    def _fold_patch_name(name) -> str:
        """``Inlet-Top`` and ``inlet_top`` are the same patch to a user."""
        return ''.join(character for character in str(name).strip().lower()
                       if character.isalnum())

    def remove_entities(self, dim_tags):
        """Delete entities, whichever kernel is holding them.

        MEASURED (R118) on a discrete import: ``model.removeEntities`` alone
        leaves a classified surface in place while the geo kernel still names
        it in the surface loop of a volume, and the next ``geo.synchronize()``
        puts the surface back complete with its triangles. The z=0 plane then
        carried 0.02 m2 of facets for a 0.01 m2 face and the mesher refused the
        domain with "PLC Error: a segment and a facet intersect". The owning
        kernel has to forget the entity too.
        """
        dim_tags = list(dim_tags)
        if not dim_tags:
            return
        for kernel in (self.gmsh.model.geo, self.gmsh.model.occ):
            try:
                kernel.remove(dim_tags, recursive=False)
                kernel.synchronize()
            except Exception:                    # noqa: BLE001 - other kernel
                pass
        try:
            self.gmsh.model.removeEntities(dim_tags, False)
        except Exception:                        # noqa: BLE001 - already gone
            pass

    # -- DP-5718: layers in the plane of a one-cell-thick build ------------ #

    def grow_in_plane_layers(self, selected, thin, layers, stack_of,
                             stack_rows) -> bool:
        """Grow the layer of a 2D-case or wedge build in its own plane.

        DP-5718. The 3D layer extrudes off a wall into the volume, and a
        volume one cell thick has no room for that (DP-5717 measured 520
        inverted cells). The layer such a case needs lies in the plane: rows
        of quadrilaterals along the wall's edges on the front face, which the
        build's one-cell extrusion carries to the back face as hexahedra.
        That is a 2D ``BoundaryLayer`` field on the edges each ticked wall
        shares with the front or back face, with every other face excluded
        (Gmsh 4.15.2 refuses a 2D layer on an edge two meshed faces share:
        "Only 2D Boundary Layers are supported"). MEASURED on a 0.4 x 0.1 m
        slab, 5 layers on the two long walls, the field on the front edges or
        on the front and back edges alike: 400 hexahedra and 754 prisms,
        front and back identical, checkMesh "Mesh OK".

        True when the layer was set up; False when no ticked wall meets the
        front or back face, which leaves the caller to say so.
        """
        gmsh = self.gmsh
        names = set(thin)
        faces = [int(tag) for _dim, tag in gmsh.model.getEntities(2)
                 if self.surface_patch_name(int(tag))[0] in names]
        if not faces:
            return False
        face_curves = set()
        for tag in faces:
            face_curves |= self.surface_curves(tag)
        walls, flat = {}, []
        for tag in selected:
            tag = int(tag)
            if tag in faces:
                flat.append(self.surface_patch_name(tag)[0])
                continue
            shared = sorted(self.surface_curves(tag) & face_curves)
            if shared:
                walls[tag] = shared
        if not walls:
            return False
        stacks = self.in_plane_stacks(layers, stack_rows)
        excluded = [int(tag) for _dim, tag in gmsh.model.getEntities(2)
                    if int(tag) not in faces]
        ends = self.in_plane_layer_ends(
            face_curves, {c for shared in walls.values() for c in shared})
        fields = []
        for number in sorted({stack_of.get(tag, 0) for tag in walls}):
            stack = stacks[number]
            curves = sorted({curve for tag, shared in walls.items()
                             if stack_of.get(tag, 0) == number
                             for curve in shared})
            field = gmsh.model.mesh.field.add('BoundaryLayer')
            gmsh.model.mesh.field.setNumbers(field, 'CurvesList', curves)
            gmsh.model.mesh.field.setNumbers(field, 'ExcludedSurfacesList',
                                             excluded)
            gmsh.model.mesh.field.setNumber(field, 'Size', stack['first'])
            gmsh.model.mesh.field.setNumber(field, 'Ratio', stack['ratio'])
            gmsh.model.mesh.field.setNumber(field, 'NbLayers', stack['cells'])
            gmsh.model.mesh.field.setNumber(field, 'Thickness',
                                            stack['cap'])
            gmsh.model.mesh.field.setNumber(field, 'Quads',
                                            1 if stack['quads'] else 0)
            mine = sorted(point for point, curve in ends.items()
                          if curve in curves)
            if mine:                                        # DP-5726
                gmsh.model.mesh.field.setNumbers(field, 'PointsList', mine)
            gmsh.model.mesh.field.setAsBoundaryLayer(field)
            fields.append(field)
        if flat:
            self.warnings.append(
                'no boundary layer was grown on ' + ', '.join(sorted(set(
                    flat))) + ': on a one-cell-thick case that is the front '
                'or back face the layer lies in, not a wall it grows off')
        self.layers_in_plane = {
            'faces': faces, 'walls': walls, 'fields': fields,
            'stack_of': {tag: stack_of.get(tag, 0) for tag in walls}}
        self.layer_bases = sorted(walls)
        main = stacks[0]

        def asked(tag):
            stack = stacks[stack_of.get(tag, 0)]
            return {'stack': stack_of.get(tag, 0), 'layers': stack['count'],
                    'elementsPerLayer': stack['elements'],
                    'firstHeight': stack['height'],
                    'totalThickness': stack['thickness'],
                    'quads': stack['quads']}

        self.statistics['layers'] = {
            'requestedLayers': main['count'],
            'requestedFirstHeight': main['height'],
            'requestedTotalThickness': main['thickness'],
            'inPlane': True,
            'planeFaces': sorted(names),
            'extrudedSurfaces': len(walls),
            'skippedSurfaces': len(selected) - len(walls),
            'patches': sorted({self.surface_patch_name(tag)[0]
                               for tag in walls}),
            'elementsPerLayer': main['elements'],
            'requestedByPatch': {self.surface_patch_name(tag)[0]: asked(tag)
                                 for tag in walls},
            'stackCount': len(fields),
            'scope': layers.get('scope', 'all_boundary_surfaces'),
        }
        self.ledger.record(
            'gmsh/boundaryLayers/layerCount', main['count'], main['count'],
            note='grown in the plane of the one-cell-thick case off '
                 + count_text(len(walls), 'wall') + ', on '
                 + ', '.join(sorted(names)))
        self.ledger.record(
            'gmsh/boundaryLayers/elementsPerLayer', main['elements'],
            main['elements'],
            note='the cells each layer is divided into, in the first stack')
        return True

    def in_plane_layer_ends(self, plane_curves, layer_curves) -> dict:
        """``{point: layer curve}`` for each end of the in-plane layer: a
        point a layer curve shares with a plane curve that grows no layer
        (an outlet, a symmetry side, the axis of a wedge).

        DP-5726. Without the point in the field's ``PointsList`` the 2D
        ``BoundaryLayer`` field stops short of it: the last column has no
        rows and the side it meets keeps its bulk spacing. MEASURED on a
        0.4 x 0.1 m section, walls on three sides, 3 layers from 0.308 mm at
        1.3, the fourth side not a wall: the side's first gaps were 7.7 mm,
        the worst triangle gamma 0.042 and the end columns' quadrilaterals
        minSICN 0.015 (the wedge walk refused 11 prisms there); with the two
        end points listed the side is graded 0.31, 0.40, 0.52 mm, the worst
        triangle gamma 0.61, and every quadrilateral is a row cell."""
        layer_curves = {int(c) for c in layer_curves}
        quiet = set()
        for curve in plane_curves:
            if int(curve) not in layer_curves:
                quiet |= self.curve_points(curve)
        ends = {}
        for curve in sorted(layer_curves):
            for point in self.curve_points(curve):
                if point in quiet:
                    ends.setdefault(point, curve)
        return ends

    def curve_points(self, curve) -> set:
        try:
            return {int(tag) for _dim, tag in self.gmsh.model.getBoundary(
                [(1, int(curve))], oriented=False)}
        except Exception:                                # noqa: BLE001
            return set()

    def in_plane_stacks(self, layers, stack_rows) -> dict:
        """``{number: {...}}`` for the 2D layer field of each stack: the
        first cell, the growth ratio and the cell count, from the cumulative
        heights the page writes (a layer split into *elements* cells grows by
        the *elements*-th root of its ratio)."""
        rows = {0: layers}
        rows.update({number: row for number, row in
                     enumerate(stack_rows, start=1)})
        stacks = {}
        for number, row in rows.items():
            heights = [abs(float(item)) for item in row['cumulativeHeights']]
            count = int(row['layerCount'])
            elements = max(1, int(row.get('elementsPerLayer') or 1))
            ratio = ((heights[1] - heights[0]) / heights[0]
                     if len(heights) > 1 and heights[0] > 0 else 1.0)
            ratio = ratio if ratio > 0 else 1.0
            cell = ratio ** (1.0 / elements)
            first = (heights[0] * (cell - 1.0) / (cell ** elements - 1.0)
                     if abs(cell - 1.0) > 1e-12 else heights[0] / elements)
            stacks[number] = {
                'count': count, 'elements': elements, 'height': heights[0],
                'thickness': heights[-1], 'first': first, 'ratio': cell,
                'cells': count * elements,
                # Gmsh's 2D layer stops at Thickness, not at NbLayers, and
                # one set at the stack's own top loses the last layer to
                # rounding: MEASURED 3 asked, 162 quadrilaterals (2 rows)
                # with the exact top, 240 (3 rows) with the cap half-way into
                # the layer after it; no cap grew 704 (9 rows).
                'cap': heights[-1] + 0.5 * first * cell ** (count * elements),
                'quads': bool(row.get('quads', True))}
        return stacks

    def in_plane_quads(self):
        """The quadrilaterals on the front and back faces of a DP-5718
        layer, as corner-node lists, and ``{edge: [quad index]}``."""
        gmsh = self.gmsh
        quads, by_edge = [], {}
        for face in self.layers_in_plane['faces']:
            types, _tags, nodes = gmsh.model.mesh.getElements(2, face)
            for etype, group in zip(types, nodes):
                shape, _d, _o, per, *_rest = \
                    gmsh.model.mesh.getElementProperties(etype)
                if not shape.startswith('Quadrilateral'):
                    continue
                flat = [int(item) for item in group]
                for index in range(0, len(flat), per):
                    quad = flat[index:index + 4]
                    number = len(quads)
                    quads.append(quad)
                    for k in range(4):
                        edge = frozenset((quad[k], quad[(k + 1) % 4]))
                        by_edge.setdefault(edge, []).append(number)
        return quads, by_edge

    def in_plane_layer_cells(self) -> frozenset:
        """DP-5718. The volume cells of an in-plane layer, by element tag:
        the cells the one-cell extrusion made of the quadrilateral rows that
        stand on the ticked walls' edges. Provenance, as for the 3D layer
        (DP-76): the rows are walked off the wall, so a quadrilateral the
        mesher made anywhere else is not counted."""
        gmsh = self.gmsh
        quads, by_edge = self.in_plane_quads()
        rows = set()
        for curves in self.layers_in_plane['walls'].values():
            for curve in curves:
                types, _tags, nodes = gmsh.model.mesh.getElements(1, curve)
                for etype, group in zip(types, nodes):
                    per = gmsh.model.mesh.getElementProperties(etype)[3]
                    flat = [int(item) for item in group]
                    for index in range(0, len(flat), per):
                        edge = frozenset(flat[index:index + 2])
                        while True:
                            ahead = [q for q in by_edge.get(edge, ())
                                     if q not in rows]
                            if not ahead:
                                break
                            rows.add(ahead[0])
                            quad = quads[ahead[0]]
                            k = next(i for i in range(4) if frozenset(
                                (quad[i], quad[(i + 1) % 4])) == edge)
                            edge = frozenset((quad[(k + 2) % 4],
                                              quad[(k + 3) % 4]))
        if not rows:
            return frozenset()
        feet = {frozenset(quads[number]) for number in rows}
        found = set()
        types, tags, nodes = gmsh.model.mesh.getElements(3)
        for etype, group, flat in zip(types, tags, nodes):
            shape, _d, _o, per, *_rest = \
                gmsh.model.mesh.getElementProperties(etype)
            if not shape.startswith('Hexahedron'):
                continue
            flat = [int(item) for item in flat]
            for number, tag in enumerate(group):
                corners = flat[number * per:number * per + 8]
                if (frozenset(corners[:4]) in feet
                        or frozenset(corners[4:8]) in feet):
                    found.add(int(tag))
        return frozenset(found)

    def measure_in_plane_layers(self) -> None:
        """Read a DP-5718 layer back off the front and back faces.

        Each wall edge segment stands at the foot of a row of quadrilaterals
        running into the face; the row is walked from the wall until the next
        cell is not a quadrilateral. Recorded in the shape the 3D reading
        uses (``achieved.byPatch``): one column per wall segment and face.
        """
        gmsh = self.gmsh
        plane = self.layers_in_plane
        record = self.statistics['layers']
        cache = {}

        def point(tag):
            if tag not in cache:
                cache[tag] = gmsh.model.mesh.getNode(tag)[0]
            return cache[tag]

        quads, by_edge = self.in_plane_quads()

        def row(base):
            """The steps of the row standing on *base*, and its last edge."""
            steps, edge, seen = [], base, set()
            while True:
                ahead = [q for q in by_edge.get(edge, ()) if q not in seen]
                if not ahead:
                    return steps, edge
                quad = quads[ahead[0]]
                seen.add(ahead[0])
                k = next(i for i in range(4) if frozenset(
                    (quad[i], quad[(i + 1) % 4])) == edge)
                steps.append((math.dist(point(quad[(k + 1) % 4]),
                                        point(quad[(k + 2) % 4]))
                              + math.dist(point(quad[k]),
                                          point(quad[(k + 3) % 4]))) / 2.0)
                edge = frozenset((quad[(k + 2) % 4], quad[(k + 3) % 4]))

        def reach(foot, head):
            """How far the row's last edge lies from the wall, across it."""
            a, b = (point(tag) for tag in foot)
            axis = [q - p for p, q in zip(a, b)]
            length = math.sqrt(sum(c * c for c in axis)) or 1.0
            axis = [c / length for c in axis]
            c, d = (point(tag) for tag in head)
            away = [(c[i] + d[i] - a[i] - b[i]) / 2.0 for i in range(3)]
            along = sum(x * y for x, y in zip(away, axis))
            return math.sqrt(max(0.0, sum(x * x for x in away)
                                 - along * along))

        by_patch = {}
        for wall, curves in plane['walls'].items():
            name = self.surface_patch_name(wall)[0]
            entry = by_patch.setdefault(name, {
                'columns': 0, 'segments': 0, 'layers': set(), 'first': [],
                'reach': []})
            for curve in curves:
                types, _tags, nodes = gmsh.model.mesh.getElements(1, curve)
                for etype, group in zip(types, nodes):
                    per = gmsh.model.mesh.getElementProperties(etype)[3]
                    flat = [int(item) for item in group]
                    for index in range(0, len(flat), per):
                        # the two end nodes; a quadrilateral's corners
                        base = frozenset(flat[index:index + 2])
                        entry['segments'] += 1
                        steps, head = row(base)
                        if not steps:
                            continue
                        entry['columns'] += 1
                        entry['layers'].add(len(steps))
                        entry['first'].append(steps[0])
                        entry['reach'].append(reach(sorted(base), sorted(head)))
        wanted = set(record.get('patches') or ())
        result, first_all, reach_all, depths = {}, [], [], set()
        for name, entry in sorted(by_patch.items()):
            asked = (record.get('requestedByPatch') or {}).get(name) or {}
            split = int(asked.get('elementsPerLayer') or 1)
            total = entry['segments']
            result[name] = {
                'selected': name in wanted, 'faces': total,
                'columns': entry['columns'],
                'coverage': entry['columns'] / total if total else None,
                'layersPerColumn': sorted(
                    {n // split if n % split == 0 else n
                     for n in entry['layers']}),
                'cellsPerColumn': sorted(entry['layers']),
                'requested': asked or None,
                'firstHeight': self.spread(entry['first']),
                'travelled': self.spread(entry['reach']),
                'reach': self.spread(entry['reach'])}
            first_all += entry['first']
            reach_all += entry['reach']
            depths |= entry['layers']
        record['achieved'] = {
            'inPlane': True, 'prisms': 0,
            'columns': sum(item['columns'] for item in result.values()),
            'layersPerColumn': sorted(depths),
            'firstHeight': self.spread(first_all),
            'travelled': self.spread(reach_all),
            'reach': self.spread(reach_all),
            'unattributed': 0, 'byPatch': result}
        short = [f'{name} ({item["columns"]} of {item["faces"]} edges)'
                 for name, item in result.items()
                 if item['coverage'] is not None
                 and item['coverage'] < 1 - 1e-9]
        if short:
            self.warnings.append(
                'the boundary layer covers only part of ' + ', '.join(short)
                + ': the near-wall spacing jumps where the rows stop')
        self.ledger.record(
            'gmsh/boundaryLayers/quads', True,
            'Quadrilateral rows' if first_all else None,
            applied=bool(first_all), matched=bool(first_all),
            note='rows of quadrilaterals in the plane of the case, carried '
                 'through the one cell as hexahedra')

    def surface_curves(self, tag):
        """The curves bounding one surface, unsigned."""
        return {abs(curve) for _dim, curve in self.gmsh.model.getBoundary(
            [(2, tag)], oriented=False, recursive=False)}

    def boundary_shells(self, surfaces):
        """The boundary split into connected shells, by shared curves.

        DP-55. Two surfaces that share a bounding curve are two faces of the
        same closed surface, so the transitive closure of that relation is a
        shell. A box with an obstacle inside it is two of them, and they want
        their layers grown in opposite directions.
        """
        parent = {tag: tag for tag in surfaces}

        def find(tag):
            while parent[tag] != tag:
                parent[tag] = parent[parent[tag]]
                tag = parent[tag]
            return tag

        owners: dict[int, int] = {}
        for tag in surfaces:
            for curve in self.surface_curves(tag):
                other = owners.setdefault(curve, tag)
                left, right = find(tag), find(other)
                if left != right:
                    parent[left] = right
        shells: dict[int, list[int]] = {}
        for tag in surfaces:
            shells.setdefault(find(tag), []).append(tag)
        return [sorted(group) for group in shells.values()]

    def internal_shells(self, shells):
        """The surfaces of every shell the outermost one encloses.

        DP-55. Bounding-box containment, because it answers from geometry the
        model already has. Where the tessellated route ran, the shell topology
        has already said which shells are voids and which way each is wound,
        and that reading is the better one; this is the CAD route's answer,
        where nothing is meshed until after the layers are grown. The sign of
        each shell's own volume read off its surface mesh would also serve
        (+64 for a 4 m box, +1.0 for the 1 m cube cut out of it, measured
        live), and there is no surface mesh here to read it from.

        A nested shell can only be a hole, because a multi-volume assembly is
        refused before this point.

        Empty where there is one shell, or where no single shell encloses all
        the others: then every surface keeps the direction the plan asked for,
        which is what this did before it could tell them apart.
        """
        if len(shells) < 2:
            return set()
        boxes = []
        for group in shells:
            corners = [self.gmsh.model.getBoundingBox(2, tag) for tag in group]
            boxes.append(tuple(
                [min(corner[axis] for corner in corners) for axis in range(3)]
                + [max(corner[axis] for corner in corners)
                   for axis in range(3, 6)]))
        slack = max(box[axis + 3] - box[axis]
                    for box in boxes for axis in range(3)) * 1e-9

        def encloses(outer, inner):
            return (all(outer[axis] <= inner[axis] + slack
                        for axis in range(3))
                    and all(outer[axis] >= inner[axis] - slack
                            for axis in range(3, 6)))

        enclosing = [index for index, box in enumerate(boxes)
                     if all(encloses(box, other)
                            for position, other in enumerate(boxes)
                            if position != index)]
        if len(enclosing) != 1:
            return set()
        return {tag for index, group in enumerate(shells)
                if index != enclosing[0] for tag in group}

    def shells_kept_whole(self, shells, selected, internal):
        """The core's shells that close it without being rebuilt.

        DP-124. :meth:`rebuild_unextruded` replaces every un-layered surface
        with a flat face stitched into the rim the layer stopped at, because
        the layer's own lateral faces lie in the plane of the surface it grew
        beside and the two would overlap. That reasoning is about a surface
        the layer *reaches*. A shell it never touches is not in the way of
        anything: it is still closed, still meshed and still named, and
        rebuilding it is neither possible nor wanted.

        MEASURED on `two_solid_block.stl`, one wrapped file holding a box
        inside a box. The farfield is a single volume bounded by two disjoint
        shells; with layers on the outer one every curve of every layer top is
        shared with another top, so `rim_loops` returns nothing at all and the
        rebuild refused with "the boundary layer left no rim to rebuild the
        un-layered patches from". Kept whole instead, the inner shell goes to
        `addVolume` as the hole it always was.

        Only a shell the layered one encloses is kept. The geo kernel reads
        the first surface loop of a volume as its exterior and the rest as
        holes, so an un-layered shell *outside* the layer -- an obstacle given
        layers inside a box that was not -- cannot be a loop after the first.
        That one is left in `skipped` and refused by the rebuild as before,
        rather than built into a volume turned inside out.
        """
        chosen = {int(tag) for tag in selected}
        inside = {int(tag) for tag in internal}
        kept = []
        for group in shells:
            tags = sorted(int(tag) for tag in group)
            if any(tag in chosen for tag in tags):
                continue
            if tags and all(tag in inside for tag in tags):
                kept.append(tags)
        return kept

    def shell_kept_as_exterior(self, shells, selected, internal):
        """The un-layered shell the core is bounded by from outside.

        DP-127. :meth:`shells_kept_whole` reads the same fact -- a shell the
        layer never reached needs no rebuild -- and only acts on it where the
        shell is a hole. MEASURED on `turbine_cascade`, two files and one
        fluid volume: the layer is on the blades (`99 boundary layer base(s)
        bound a hole in the volume and were grown inwards from it`) and the
        farfield around them carries none. So the untouched shell is the
        enclosing one, DP-124 declined it, the rebuild was handed it, and a
        layer that wraps a blade leaves no rim for a flat face to stitch into:
        the run died on "the boundary layer left no rim to rebuild the
        un-layered patches from".

        That volume is not ill-defined. It is bounded outside by the farfield
        and inside by the layer tops, and `addVolume` spells exactly that --
        the kept shell first, one loop of tops per body after it.

        Kept only where the split is clean: one shell that encloses all the
        others and carries no layer, and every enclosed shell layered whole.
        A shell the layer reached in part has its own faces in the way of the
        layer's laterals, which is what the rebuild exists for, so a mixture
        goes there as before rather than into a volume that covers a plane
        twice.
        """
        if len(shells) < 2 or not internal:
            return []
        chosen = {int(tag) for tag in selected}
        inside = {int(tag) for tag in internal}
        outside, enclosed = [], []
        for group in shells:
            tags = sorted(int(tag) for tag in group)
            (enclosed if tags and all(tag in inside for tag in tags)
             else outside).append(tags)
        if len(outside) != 1 or not enclosed:
            return []
        if any(tag in chosen for tag in outside[0]):
            return []
        if not all(all(tag in chosen for tag in group) for group in enclosed):
            return []
        return outside[0]

    def layer_tops_by_shell(self, shells, columns):
        """Each shell's layer tops, grouped so they can close one hole.

        DP-127. `addVolume` takes one surface loop per hole, and a cascade
        has a hole per blade. The grouping is read off the columns rather
        than the model: the top of a column stands on its base, so the tops
        of a shell's bases close around that shell and nothing else.
        """
        top_of = {int(base): int(top) for base, top in columns}
        groups = []
        for group in shells:
            tops = [top_of[int(tag)] for tag in group if int(tag) in top_of]
            if tops:
                groups.append(tops)
        return groups

    def rim_owners(self, tops):
        """Which layer top each rim curve belongs to.

        Where two extruded surfaces meet, their tops share a curve. A curve
        owned by a single top is on the rim where the layer stops.
        """
        seen: dict[int, list] = {}
        for tag in tops:
            for curve in self.surface_curves(tag):
                seen.setdefault(curve, []).append(int(tag))
        return {curve: tags[0] for curve, tags in seen.items()
                if len(tags) == 1}

    def rim_loops(self, tops):
        """Closed loops of the curves that bound exactly one layer top.

        Where two extruded surfaces meet, their tops share a curve. A curve
        owned by a single top is therefore on the rim where the layer stops --
        the outline of the hole the un-extruded surface has to fill.
        """
        rim = list(self.rim_owners(tops))
        parent = {curve: curve for curve in rim}

        def find(curve):
            while parent[curve] != curve:
                parent[curve] = parent[parent[curve]]
                curve = parent[curve]
            return curve

        by_point: dict[int, list[int]] = {}
        for curve in rim:
            for _dim, point in self.gmsh.model.getBoundary(
                    [(1, curve)], oriented=False, recursive=False):
                by_point.setdefault(abs(point), []).append(curve)
        for shared in by_point.values():
            for other in shared[1:]:
                parent[find(other)] = find(shared[0])
        loops: dict[int, list[int]] = {}
        for curve in rim:
            loops.setdefault(find(curve), []).append(curve)
        return list(loops.values())

    def patch_lies_in_a_plane(self, tag, box):
        """Is this patch flat enough to be stood in for by a plane face?

        DP-90. :meth:`rebuild_unextruded` closes the opening a layer leaves
        with ``addPlaneSurface``, and every guard it had reads the *rim*
        rather than the patch. MEASURED on `annulus_shell.step`, layers on
        `wall4`, `wall5` and `wall6` -- the three faces of the fluid annulus
        that are not its interface with the bore. Each rim of that interface
        is a single closed circle, so it bounds exactly one point:
        `_box_holding` attributes it freely and `_out_of_plane` reports 0.0
        over fewer than four points. Both guards passed vacuously and the two
        rims, 0.4 m apart, were welded into one plane surface as outline and
        hole. The core came out open on 124 edges and the 3D pass spun on it
        for over 45 minutes emitting nothing -- `generate` has no heartbeat,
        so the GUI showed a run at 35% that never moved and had to be killed.

        The same blindness is worse where it finishes. With layers on `wall4`
        alone the interface is left with no rim at all, dropped, and the core
        closed as though the bore were not there: 0.01707852 m3 of cells
        against an analytic 0.01256637, the bore's 0.00452 m3 meshed twice
        and the two regions overlapping -- and the run published `succeeded`.

        The normal of a plane is the same everywhere on it. Sampled the way
        :meth:`normal_leaves_the_fluid` samples, that answers for a slanted
        face as well as an axis-aligned one, which a bounding box does not.
        A patch that cannot be asked is called flat: an unanswerable one
        keeps the shipped behaviour rather than refusing a run that worked.
        """
        gmsh = self.gmsh
        try:
            low, high = gmsh.model.getParametrizationBounds(2, tag)
        except Exception:                    # noqa: BLE001 - discrete surface
            return True
        span = max(box[axis + 3] - box[axis] for axis in range(3))
        slack = max(span * 1e-6, 1e-9)
        normals = []
        for first in self.NORMAL_SAMPLES:
            for second in self.NORMAL_SAMPLES:
                params = [low[0] + (high[0] - low[0]) * first,
                          low[1] + (high[1] - low[1]) * second]
                try:
                    point = gmsh.model.getValue(2, tag, params)
                    normal = gmsh.model.getNormal(tag, params)
                except Exception:            # noqa: BLE001 - not parametrised
                    return True
                # Parametric bounds are a rectangle and a trimmed face is
                # not, so a sample off the patch says nothing about it.
                if any(not box[axis] - slack <= point[axis]
                       <= box[axis + 3] + slack for axis in range(3)):
                    continue
                length = math.sqrt(sum(value * value for value in normal))
                if length <= 0.0:
                    continue
                normals.append([value / length for value in normal])
        if len(normals) < 2:
            return True
        first = normals[0]
        # Unsigned: a parametrisation may flip the normal across the patch
        # without the patch bending at all.
        return all(abs(sum(first[axis] * other[axis] for axis in range(3)))
                   >= 1.0 - 1e-4 for other in normals[1:])

    def rebuild_unextruded(self, skipped, tops, boxes, columns):
        """Close the core volume across the surfaces that grew no layer.

        R118. The core is rebuilt from the extrusion's *top* surfaces, and with
        only some surfaces extruded that loop is open. The un-extruded surface
        cannot simply join it: the layer's own lateral faces lie in the same
        plane, so the two overlap. MEASURED on a 0.1 x 0.1 x 0.6 m duct, layers
        on the four walls only -- reusing the caps gave "PLC Error: a segment
        and a facet intersect", adding the laterals gave "Invalid boundary mesh
        (overlapping facets)", and the z=0 plane was covered twice over,
        0.02 m2 of facets for a 0.01 m2 face. Rebuilding each un-extruded
        surface from the rim the extrusion left, and deleting the original,
        closes it: 14,199 tets + 16,712 prisms, 0.005999988 m3 against an
        analytic 0.006, and not one prism rooted on either cap (against 388 on
        the same duct with every surface extruded).
        """
        gmsh = self.gmsh
        rebuilt = {}
        # R118. Every geometric read below -- the rim's points,
        # its plane, its span -- goes through `gmsh.model`, and
        # the extrusion built its rims in the `geo` kernel.
        # MEASURED on `annulus_shell` without this call: all four
        # rim curves answer `Empty bounding box`, parametrise as
        # (0, 1) with `getValue` returning the origin, and bound
        # no points at all -- so every guard here passed
        # vacuously, all four rims were attributed to one cap and
        # the plane surface built across them held two concentric
        # circles in one loop. Gmsh then spun on `8 intersections
        # in the 1D mesh (curves 10 10 12 12 33 33 35 35)` for
        # over thirty minutes without growing by a byte.
        gmsh.model.geo.synchronize()
        owners = self.rim_owners(tops)
        bases = {int(top): int(base) for base, top in columns}
        names = sorted({self.surface_patch_name(tag)[0] for tag in skipped})
        # DP-90. The closure built below is a plane, so a patch that is not
        # one cannot be stood in for. Refused here, where the patch still has
        # a name, rather than left to a 3D pass that does not return.
        curved = [tag for tag in skipped
                  if not self.patch_lies_in_a_plane(tag, boxes[tag])]
        if curved:
            bent = sorted({self.surface_patch_name(tag)[0] for tag in curved})
            raise MeshFailure(
                'the patches left without a boundary layer — '
                + ', '.join(bent) + ' — are not flat, and the opening a '
                'layer leaves can only be closed with a flat face. A curved '
                'patch cannot be left uncovered: the core would be rebuilt as '
                'though the curve were not there. Name a set of patches that '
                'leaves only flat ones uncovered, or turn layers off.')
        rings = []
        for curves in self.rim_loops(tops):
            # MEASURED: the rim only outlines the un-layered patch when the
            # layer surrounds it. Asking for layers on the inlet of the duct
            # and *not* on its walls leaves a rim that loops round the tube
            # instead, and the plane surface built across it cuts through the
            # domain -- the run died four stages later on "Invalid boundary
            # mesh (overlapping facets) on surface 63 surface 64". A rim that
            # does not sit inside the patch it is standing in for, or that is
            # not flat, is refused here where the cause can still be named.
            points = self._loop_points(curves)
            owner = self._box_holding(boxes, points)
            if owner is None:
                raise MeshFailure(
                    'the boundary layer leaves an opening that does not lie '
                    'on any of the patches without one (' + ', '.join(names)
                    + '), so it cannot be closed: the layer has to surround '
                    'them. Grow the layer on the patches around them too, or '
                    'turn layers off.')
            deviation, extent = self._out_of_plane(points)
            if deviation > max(extent * 1e-4, 1e-12):
                raise MeshFailure(
                    'the patches left without a boundary layer — '
                    + ', '.join(names) + f' — span {deviation:.4g} m out of '
                    'any one plane, and the opening the layer leaves can only '
                    'be closed with a flat face. Grow the layer on them too, '
                    'or turn layers off.')
            rings.append((owner, curves))
        grouped: dict[int, list[list[int]]] = {}
        for owner, curves in rings:
            grouped.setdefault(owner, []).append(curves)
        for owner, members in grouped.items():
            # An annular opening leaves two rims in one plane, and one plane
            # surface per rim lays a disc across the bore. MEASURED on
            # `annulus_shell`: the fluid is the space between two cylinders,
            # so each end cap is bounded by the inner layer's rim and the
            # outer layer's rim together. The widest rim is the outline and
            # every other one is a hole in it.
            flat = self._flat_axis(boxes[owner])
            members.sort(reverse=True, key=lambda ring: self._rim_rank(
                ring, owners, bases, flat))
            try:
                loops = [gmsh.model.geo.addCurveLoop(ring, reorient=True)
                         for ring in members]
                surface = gmsh.model.geo.addPlaneSurface(loops)
            except Exception as error:
                raise MeshFailure(
                    'the patches left without a boundary layer could not be '
                    f'rebuilt to close the core volume: {error}. Grow the '
                    'layer on every patch, or turn layers off.') from error
            rebuilt[surface] = [curve for ring in members for curve in ring]
        if not rebuilt:
            raise MeshFailure(
                'the boundary layer left no rim to rebuild the un-layered '
                'patches from, so the core volume cannot be closed')
        gmsh.model.geo.synchronize()
        # The originals still carry their triangles; clearing the mesh first is
        # what keeps them out of the z=0 plane when the kernel is discrete.
        gmsh.model.mesh.clear([(2, tag) for tag in skipped])
        self.remove_entities([(2, tag) for tag in skipped])
        for tag in skipped:
            self.layer_replacements.setdefault(tag, [])
        return rebuilt

    def _loop_points(self, curves):
        """Coordinates of the points bounding a set of curves.

        R118. `combined` defaults to true, and a closed curve begins and
        ends at one point: combining cancels the two occurrences against
        each other and the curve is reported as bounding nothing at all.
        MEASURED on `annulus_shell`, whose four rims are full circles --
        every one of them returned no points, `_box_holding` then held
        them all to be inside the first cap it looked at, and the plane
        surface built across them carried two concentric circles in one
        loop. Gmsh spun on `8 intersections in the 1D mesh` for over
        thirty minutes.
        """
        seen, points = set(), []
        for curve in curves:
            for _dim, point in self.gmsh.model.getBoundary(
                    [(1, curve)], oriented=False, combined=False,
                    recursive=False):
                tag = abs(point)
                if tag not in seen:
                    seen.add(tag)
                    points.append(list(self.gmsh.model.getValue(0, tag, [])))
        return points

    def _flat_axis(self, box):
        """The axis an un-layered patch has no thickness in."""
        extents = [box[axis + 3] - box[axis] for axis in range(3)]
        return extents.index(min(extents))

    def _rim_rank(self, curves, owners, bases, flat):
        """How wide the layer this rim belongs to started out, in the cap.

        R118. The rim itself cannot be measured: it is a `geo` curve the
        extrusion made, and MEASURED on `annulus_shell` those answer `Empty
        bounding box`, parametrise as (0, 1) and evaluate to the origin. The
        surface the layer grew from is the imported one, and that has real
        coordinates -- so the rims of one cap are ranked by the bases behind
        them, measured across the cap rather than along it.

        This is exact for the shape that needs it, nested shells, where the
        rim is its base offset by the layer thickness. It would not be for a
        base that narrows away from the cap, and no such case is meshed here:
        a cap with a single rim never asks the question.
        """
        spans = []
        for curve in curves:
            base = bases.get(owners.get(int(curve)))
            if base is None:
                continue
            try:
                box = self.gmsh.model.getBoundingBox(2, int(base))
            except Exception:                                # noqa: BLE001
                continue
            spans.append(max(box[axis + 3] - box[axis]
                             for axis in range(3) if axis != flat))
        return max(spans) if spans else 0.0

    @staticmethod
    def _box_holding(boxes, points):
        """Which un-layered surface's box holds all of these points.

        `_within_any` answered whether any of them did. The rebuild now has
        to group the rims by the patch each one stands in, because a patch
        can be left with more than one -- so the answer has to say which.
        """
        # An empty set of points is held by every box: `all` over nothing
        # is true. MEASURED, that is not a hypothetical -- a rim made of
        # full circles bounds no points at all, and the first box in the
        # dictionary then claimed all four of them.
        if not points:
            return None
        for tag, box in boxes.items():
            span = max(box[index + 3] - box[index] for index in range(3))
            slack = max(span * 1e-6, 1e-9)
            if all(box[index] - slack <= point[index] <= box[index + 3] + slack
                   for point in points for index in range(3)):
                return tag
        return None

    @staticmethod
    def _within_any(boxes, points):
        """Do all these points sit inside one un-layered surface's box?

        The rebuilt face stands in for a patch, so it has to be where that
        patch was. Its own bounding box cannot be asked for -- a geo surface
        that has not been meshed yet answers +-DBL_MAX -- but the points of
        its rim have real coordinates.
        """
        # An empty set of points is held by every box: `all` over nothing
        # is true. MEASURED, that is not a hypothetical -- a rim made of
        # full circles bounds no points at all, and the first box in the
        # dictionary then claimed all four of them.
        if not points:
            return False
        for box in boxes.values():
            span = max(box[index + 3] - box[index] for index in range(3))
            slack = max(span * 1e-6, 1e-9)
            if all(box[index] - slack <= point[index] <= box[index + 3] + slack
                   for point in points for index in range(3)):
                return True
        return False

    @staticmethod
    def _out_of_plane(points):
        """``(worst distance from the best plane, size of the rim)``, metres.

        Three points always lie in a plane, so a rim of three or fewer is flat
        by definition and reports zero.
        """
        if len(points) < 4:
            return 0.0, 0.0
        origin = points[0]

        def offset(point):
            return [point[index] - origin[index] for index in range(3)]

        def norm(vector):
            return math.sqrt(sum(value * value for value in vector))

        extent = max(norm(offset(point)) for point in points)
        first = max(points, key=lambda point: norm(offset(point)))
        base = offset(first)

        def cross(left, right):
            return [left[1] * right[2] - left[2] * right[1],
                    left[2] * right[0] - left[0] * right[2],
                    left[0] * right[1] - left[1] * right[0]]

        normal = max((cross(base, offset(point)) for point in points),
                     key=norm)
        length = norm(normal)
        if length <= 0.0:
            return 0.0, extent           # every point on one line: still flat
        unit = [value / length for value in normal]
        deviation = max(abs(sum(unit[index] * offset(point)[index]
                                for index in range(3)))
                        for point in points)
        return deviation, extent

    def name_layer_replacements(self, skipped_curves, rebuilt_loops, laterals):
        """Hand each new face the patch name of the surface it replaced.

        An un-layered inlet ends up covered by the extrusion's own lateral
        faces -- the frame the layer leaves in that plane -- plus the rebuilt
        inner face. Both are real boundary faces and both must carry the patch
        name, or publication refuses the mesh for boundary faces no physical
        group names.

        MEASURED (R118): geometry cannot do this attribution. Asked right after
        the rebuild, ``getBoundingBox`` returns +-DBL_MAX for a freshly added
        plane surface and raises "Empty bounding box" for every extruded
        lateral, so the two caps of the live duct came back unnamed and the
        mesh published with no inlet and no outlet at all. Topology answers:
        a lateral was extruded along a curve that bounded the surface it now
        stands in for, and the rebuilt face is stitched to the far edge of
        those same laterals.
        """
        gmsh = self.gmsh
        owner_of_curve: dict[int, list[int]] = {}
        for tag, curves in skipped_curves.items():
            for curve in curves:
                owner_of_curve.setdefault(curve, []).append(tag)
        lateral_owner: dict[int, int] = {}
        for tag in laterals:
            owners = {owner for curve in self.surface_curves(tag)
                      for owner in owner_of_curve.get(curve, ())}
            if len(owners) == 1:
                owner = owners.pop()
                lateral_owner[tag] = owner
                self.layer_replacements[owner].append(tag)
        for surface, curves in rebuilt_loops.items():
            votes: dict[int, int] = {}
            for curve in curves:
                upward, _down = gmsh.model.getAdjacencies(1, curve)
                for neighbour in upward:
                    owner = lateral_owner.get(int(neighbour))
                    if owner is not None:
                        votes[owner] = votes.get(owner, 0) + 1
            if not votes:
                continue
            owner = max(votes, key=lambda tag: (votes[tag], -tag))
            names = sorted({self.surface_patch_name(tag)[0] for tag in votes})
            if len(names) > 1:
                # Two un-layered patches meeting edge to edge share one hole,
                # and one rebuilt face cannot belong to both.
                self.warnings.append(
                    'patches ' + ', '.join(names) + ' grew no boundary layer '
                    'and meet across the same opening; the face rebuilt to '
                    f'close it publishes as {self.surface_patch_name(owner)[0]}')
            self.layer_replacements[owner].append(surface)
        for tag, members in self.layer_replacements.items():
            if not members:
                name, _known = self.surface_patch_name(tag)
                self.warnings.append(
                    f'patch {name!r} grew no boundary layer and no rebuilt '
                    'face could be traced back to it, so it may publish with '
                    'fewer faces than it had')
