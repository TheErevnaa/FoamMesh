"""Curve controls and volume controls (Plan 48 RR, DP-4009).

Moved out of ``runner_v1.py``; the batch run and the resident session put
node counts, local sizes, structured faces and per-volume sizes on the model
with these functions."""
from __future__ import annotations


import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from quantities import count_text


class ControlsStage:
    def apply_curve_controls(self):
        """Put node counts on curves, then make the asked-for surfaces structured.

        Two passes, and the order is a prerequisite rather than tidiness.
        MEASURED on Gmsh 4.15.2: a transfinite surface is only honoured when
        every one of its bounding curves already carries a count and the
        opposing sides agree. Applying each row's surface request inside that
        row's own iteration meant a later row could change a shared curve and
        turn an already-accepted surface unstructured -- silently, because
        Gmsh does not complain (see :meth:`structured_surface_refusal`).
        """
        rows = self.intent.get('curveControls') or []
        if not rows:
            return
        applied = structured = 0
        pending: list[tuple] = []
        # DP-611 (field audit 0924 gmsh-sizing D2). Rows arrive highest
        # priority first, and each setTransfiniteCurve / setSize overwrote the
        # one before, so on a curve two rows share -- every edge between two
        # adjacent faces -- the LOWEST priority won. MEASURED (audit probe,
        # Gmsh 4.15.2): 21 nodes at priority 100 then 6 at priority 0 left 6.
        # A curve (and, in size mode, an end point) now belongs to the first
        # row that reaches it in priority order; a lower row skips it and
        # says so. Sorted here too, so an older job's row order cannot matter.
        rows = sorted(rows, key=lambda item: -int(item.get('order') or 0))
        claimed_curves: dict[int, str] = {}
        claimed_points: dict[int, str] = {}
        # DP-612 (field audit 0924 gmsh-sizing D4). A local size is a size on
        # the curve's end points, and Gmsh reads point sizes only while
        # Mesh.MeshSizeFromPoints is on. With "From points" off the setSize
        # call changed nothing and the row was still logged as applied.
        from_points = bool((self.intent.get('sizing') or {}).get(
            'fromPoints', True))
        for row in rows:
            if row['mode'] == 'size' and not from_points:
                self.warnings.append(
                    f'curve control {row["name"]!r} sets a local size, which '
                    'Gmsh reads only while "From points" is ticked on the '
                    'Global sizing page; it is off, so the row was skipped')
                self.ledger.record(f'curveControl:{row["name"]}', 'size',
                                   None, applied=False,
                                   note='"From points" is off, so Gmsh '
                                        'ignores point sizes')
                continue
            surfaces = self.scope_surfaces.get(row['scopeToken'])
            if not surfaces:
                self.warnings.append(
                    f'curve control {row["name"]!r} has no resolvable scope '
                    'and was skipped')
                self.ledger.record(f'curveControl:{row["name"]}',
                                   row['scopeToken'], None, applied=False,
                                   note='scope did not resolve')
                continue
            curves: set[int] = set()
            for tag in surfaces:
                _up, down = self.gmsh.model.getAdjacencies(2, tag)
                curves.update(int(item) for item in down)
            # F-26: `setTransfiniteCurve` counts *nodes*, the control counts
            # elements, and the two used to be the same number -- so a curve
            # asked for ten segments came back with nine. The derivation adds
            # the one; older jobs without the key are corrected here.
            nodes = int(row.get('nodes') or int(row['segments']) + 1)
            coefficient = self.grading_coefficient(row)
            kept_by: dict[int, str] = {curve: claimed_curves[curve]
                                       for curve in curves
                                       if curve in claimed_curves}
            mine = sorted(curves - set(kept_by))
            for curve in mine:
                claimed_curves[curve] = row['name']
                if row['mode'] == 'transfinite':
                    self.gmsh.model.mesh.setTransfiniteCurve(
                        curve, nodes, row['law'], coefficient)
                    self.transfinite_curves[curve] = {
                        'control': row['name'], 'nodes': nodes,
                        'law': row['law'], 'coefficient': coefficient}
                else:
                    points = []
                    for _d, point in self.gmsh.model.getBoundary(
                            [(1, curve)], oriented=False):
                        point = int(point)
                        owner = claimed_points.setdefault(point, row['name'])
                        if owner == row['name']:
                            points.append((0, point))
                    if points:
                        self.gmsh.model.mesh.setSize(
                            points, float(row['localSize']))
                applied += 1
            note = (f'{count_text(len(mine), "curve")}, '
                    f'{count_text(nodes, "node")} each'
                    if row['mode'] == 'transfinite'
                    else count_text(len(mine), 'curve'))
            if kept_by:
                owners = ', '.join(repr(name)
                                   for name in sorted(set(kept_by.values())))
                skipped = count_text(len(kept_by), 'shared curve')
                note += (f'; {skipped} left to higher-priority {owners}')
                self.warnings.append(
                    f'curve control {row["name"]!r} left {skipped} to '
                    f'{owners}, which ranks higher (priority, then name)')
            self.ledger.record(
                f'curveControl:{row["name"]}', row['mode'],
                row['mode'] if mine else None, applied=bool(mine),
                note=note)
            if row.get('transfiniteSurface'):
                pending.append((row, surfaces))
        for row, surfaces in pending:
            structured += self.apply_transfinite_surfaces(row, surfaces)
        self.statistics['curveControls'] = {
            'curvesTouched': applied, 'surfacesStructured': structured}

    def grading_coefficient(self, row) -> float:
        """The coefficient Gmsh wants, sign and all.

        MEASURED: ``setTransfiniteCurve`` carries the grading *direction* in
        the sign of the coefficient. On an 0.8 m edge given nine nodes,
        Progression 1.4 spaced them 0.023 .. 0.245 m and Progression -1.4
        spaced them 0.245 .. 0.023 -- the exact reverse. Beta behaves the
        same way. Bump is symmetric about the middle and came back identical
        for both signs, so a reversal there is reported rather than obeyed.

        The schema keeps the coefficient positive, because a negative growth
        rate is not something a user means; the direction is its own flag and
        the sign convention lives here, where it meets Gmsh.
        """
        coefficient = float(row['coefficient'])
        if not row.get('reverseGrading'):
            return coefficient
        law = str(row.get('law') or 'Progression')
        if law == 'Bump':
            self.warnings.append(
                f'curve control {row["name"]!r} asks for the grading to be '
                'reversed, but a Bump law is symmetric about the middle of '
                'the curve and comes back the same either way')
            self.ledger.record(f'curveControl:{row["name"]}.reverseGrading',
                               True, False, applied=False,
                               note='a Bump distribution is symmetric')
            return coefficient
        self.ledger.record(f'curveControl:{row["name"]}.reverseGrading',
                           True, True,
                           note='Gmsh takes the direction as the sign of the '
                                'coefficient')
        return -coefficient

    def side_nodes(self, tag, corners):
        """The node counts of surface *tag*'s sides, corners already checked.

        Kept apart from :meth:`transfinite_sides` so the walk can be given a
        boundary rather than having to fetch one, which is what makes it
        testable without a model.
        """
        gmsh = self.gmsh
        _up, down = gmsh.model.getAdjacencies(2, int(tag))
        curves = [int(item) for item in down]
        ends = {}
        for curve in curves:
            _u, points = gmsh.model.getAdjacencies(1, curve)
            ends[curve] = {int(point) for point in points}
        sides = self.transfinite_sides(curves, ends, corners)
        if sides is None:
            # The refusal already walked this boundary and accepted it, so
            # reaching here means the model changed underneath. Report the
            # curves instead of guessing at sides.
            return sorted(self.transfinite_curves[curve]['nodes']
                          for curve in curves)
        return sides

    def transfinite_sides(self, curves, ends, corners):
        """Node counts along each side of a face, corner to corner.

        Plan 31 FC-C. A side is the run of bounding curves between two named
        corners, so a five-curve face with four corners has one side made of
        two curves. Two curves of 3 nodes meeting at a corner that is not a
        corner make a side of 5 nodes, not 6: the shared node is one node.

        Returns the counts in the order the boundary walks, so opposing sides
        are two apart, or ``None`` if the boundary does not close -- which is
        not a judgement, only a refusal to guess.
        """
        wanted = set(int(item) for item in corners)
        remaining = set(curves)
        start = None
        for curve in curves:
            for point in ends[curve]:
                if point in wanted:
                    start = point
                    break
            if start is not None:
                break
        if start is None:
            return None
        sides, point, side_nodes, side_curves = [], start, 0, 0
        while remaining:
            step = None
            for curve in sorted(remaining):
                if point in ends[curve]:
                    step = curve
                    break
            if step is None:
                return None
            remaining.discard(step)
            nodes = self.transfinite_curves[step]['nodes']
            # Shared nodes are counted once: two curves of n nodes meeting
            # end to end run 2n-1 nodes, not 2n.
            side_nodes = nodes if not side_curves else side_nodes + nodes - 1
            side_curves += 1
            far = [item for item in ends[step] if item != point]
            point = far[0] if far else point
            if point in wanted:
                sides.append(side_nodes)
                side_nodes, side_curves = 0, 0
        if side_curves or point != start or len(sides) != len(wanted):
            return None
        return sides

    def structured_surface_refusal(self, tag, named_corners=()) -> str:
        """Why surface *tag* cannot be structured as asked, or ``''``.

        MEASURED against Gmsh 4.15.2 on duct.step, and this is the reason the
        check exists at all: Gmsh does not refuse a transfinite surface whose
        prerequisites are unmet. Neither ``setTransfiniteSurface`` nor
        ``generate`` raised when a face's two opposing sides were given 5 and
        9 nodes -- it came back with 46 triangles where a structured 4x8 grid
        is 64 -- nor when only two of the four bounding curves carried a count
        at all, which came back with 179. The run then recorded the control
        applied, on an unstructured mesh.

        Plan 31 FC-C: *named_corners* are the corners the control asked Gmsh
        to interpolate between. Naming them is what makes a face with more
        than four corners meshable at all, so a face is only refused for
        having five corners when nobody said which four they meant.
        """
        gmsh = self.gmsh
        _up, down = gmsh.model.getAdjacencies(2, int(tag))
        curves = [int(item) for item in down]
        ends = {}
        corners = set()
        for curve in curves:
            _u, points = gmsh.model.getAdjacencies(1, curve)
            ends[curve] = {int(point) for point in points}
            corners |= ends[curve]
        named = [int(item) for item in named_corners or ()]
        if named:
            # Plan 31 FC-C. MEASURED on Gmsh 4.15.2: given a corner that
            # belongs to another face, `setTransfiniteSurface` takes it and
            # `generate` does not raise -- the face came back as 44 triangles
            # where the node counts asked for a 4x4 grid, which is 32. So the
            # membership Gmsh does not check is checked here.
            stray = [item for item in named if item not in corners]
            if stray:
                return ('corner point '
                        + ', '.join(str(item) for item in stray)
                        + ' is not on this surface, whose corners are '
                        + ', '.join(str(item) for item in sorted(corners))
                        + '; Gmsh takes a corner from elsewhere without '
                          'complaint and interpolates a different grid')
        elif len(corners) not in (3, 4):
            return (f'a transfinite surface interpolates between three or '
                    f'four corners and this one has {len(corners)}; name '
                    f'three or four of '
                    + ', '.join(str(item) for item in sorted(corners))
                    + ' as the corners to say which')
        loose = [curve for curve in curves
                 if curve not in self.transfinite_curves]
        if loose:
            return ('every bounding curve needs a node count before the '
                    'surface can be structured, and '
                    + ', '.join(str(curve) for curve in sorted(loose))
                    + ' has none')
        if named:
            sides = self.transfinite_sides(curves, ends, named)
            if sides is None:
                return ('the corners named do not divide the boundary of '
                        'this surface into sides that close; a side runs '
                        'from one named corner to the next along the '
                        'bounding curves')
            if len(sides) == 4 and (sides[0] != sides[2]
                                    or sides[1] != sides[3]):
                return (f'opposing sides must carry the same node count, and '
                        f'the sides between the corners named run '
                        + ', '.join(str(item) for item in sides)
                        + ' nodes')
            return ''
        if len(curves) == 4:
            # Two curves of a four-sided face that share no corner are the
            # opposing pair. Read from the corners rather than from the order
            # the boundary comes back in, so a differently wound face is
            # judged the same way.
            for curve in curves:
                facing = [other for other in curves
                          if other != curve and not (ends[curve] & ends[other])]
                for other in facing:
                    mine = self.transfinite_curves[curve]['nodes']
                    theirs = self.transfinite_curves[other]['nodes']
                    if mine != theirs:
                        return (f'opposing sides must carry the same node '
                                f'count; curve {curve} has {mine} and the '
                                f'curve facing it, {other}, has {theirs}')
        return ''

    def apply_transfinite_surfaces(self, row, surfaces):
        """Make the scoped surfaces structured, or say why they cannot be.

        Plan 30 WP12, F-26. A transfinite surface interpolates between the
        corner points of a three- or four-sided CAD face. A tessellated import
        has no such faces -- Gmsh meshes it as a discrete surface with no
        parametrisation -- so the whole control is refused up front with a
        reason, rather than applied face by face and failing on each.
        """
        name = row['name']
        if self.tessellated:
            reason = ('a transfinite surface interpolates between the corner '
                      'points of a CAD face, and this geometry was imported '
                      'as triangles; re-import it as STEP/IGES/BREP, or leave '
                      'the surface unstructured')
            self.warnings.append(f'curve control {name!r}: {reason}')
            self.ledger.record(f'curveControl:{name}.transfiniteSurface',
                               True, False, applied=False, note=reason)
            return 0
        corners = [int(item) for item in row.get('cornerPoints') or ()]
        accepted = 0
        for tag in surfaces:
            refusal = self.structured_surface_refusal(int(tag), corners)
            if refusal:
                # Gmsh would take the request and quietly mesh the face
                # unstructured, so the run says which face and why instead of
                # certifying a structure that is not there.
                self.warnings.append(
                    f'curve control {name!r}: surface {tag} was left '
                    f'unstructured because {refusal}')
                continue
            try:
                if corners:
                    # MEASURED: the order of the tags makes no difference --
                    # the same four corners scrambled produced the same 32
                    # triangles -- so they go in as the user wrote them.
                    self.gmsh.model.mesh.setTransfiniteSurface(
                        int(tag), 'Left', list(corners))
                    sides = self.side_nodes(int(tag), corners)
                else:
                    self.gmsh.model.mesh.setTransfiniteSurface(int(tag))
                    sides = sorted(
                        self.transfinite_curves[abs(int(curve))]['nodes']
                        for _d, curve in self.gmsh.model.getBoundary(
                            [(2, int(tag))], oriented=False))
                self.transfinite_surfaces[int(tag)] = {
                    'control': name, 'nodes': sides, 'corners': list(corners),
                }
                accepted += 1
            except Exception as error:                       # noqa: BLE001
                # Gmsh takes three- and four-cornered faces only. Refusing the
                # whole run over one face is worse than naming the face.
                self.warnings.append(
                    f'curve control {name!r}: Gmsh would not make surface '
                    f'{tag} transfinite ({error})')
        self.ledger.record(
            f'curveControl:{name}.transfiniteSurface', len(surfaces), accepted,
            applied=bool(accepted),
            note='a transfinite surface needs three or four corners, a node '
                 'count on every bounding curve, and equal counts on '
                 'opposing sides')
        if corners:
            # Requested and produced, separately: the corners asked for
            # against the faces they were actually applied to.
            self.ledger.record(
                f'curveControl:{name}.cornerPoints',
                ', '.join(str(item) for item in corners), accepted,
                applied=bool(accepted),
                note='the corners Gmsh was told to interpolate between, and '
                     'the number of surfaces that took them')
        return accepted

    def unscoped_volume_warning(self, name):
        """Why a volume control sized nothing, and what to do about it.

        Plan 37 UF13. This said "has no resolvable volume scope and was
        skipped", which names the runner's bookkeeping rather than the case.
        With a farfield on, the usual cause is the cut itself: every imported
        volume is consumed and the fluid around the bodies is a new volume no
        control was scoped to.
        """
        farfield = self.statistics.get('farfield') or {}
        shape = farfield.get('shape')
        if shape:
            return (f'volume control {name!r} was skipped: the farfield '
                    f'{shape} cut the volume it is scoped to out of the '
                    'domain, so there is nothing left for it to act on. '
                    'Remove the control, or turn the farfield off to mesh '
                    'that volume.')
        return (f'volume control {name!r} was skipped: the volume it is '
                'scoped to is not in the imported geometry. Choose its volume '
                'again on the Volume controls page, or remove the control.')

    def apply_volume_controls(self):
        """Per-volume sizing, structure and exclusion.

        A volume control is scoped to a region rather than a surface, so it
        resolves against the imported volumes. Sizing goes through a Restrict
        field rather than Mesh.setSize: setSize acts on points, and a point
        shared with the neighbouring volume would carry the size across the
        boundary.
        """
        rows = self.intent.get('volumeControls') or []
        if not rows:
            return
        gmsh = self.gmsh
        field = gmsh.model.mesh.field
        applied = {'sized': 0, 'transfinite': 0, 'excluded': 0, 'skipped': 0}

        for row in rows:
            name = row['name']
            volumes = self.scope_volumes.get(row['scopeToken'])
            if not volumes:
                self.warnings.append(self.unscoped_volume_warning(name))
                self.ledger.record(f'volumeControl:{name}', row['scopeToken'],
                                   None, applied=False,
                                   note='scope did not resolve')
                applied['skipped'] += 1
                continue

            if not row.get('included', True):
                gmsh.model.occ.remove([(3, tag) for tag in volumes],
                                      recursive=False)
                gmsh.model.occ.synchronize()
                # C31-04. Removing a volume is a topology change like any
                # other: the identities it carried are gone and the scope maps
                # every later control reads have to say so.
                self.revalidate_scopes(f'volume-excluded:{name}',
                                       dimensions=(3,))
                applied['excluded'] += len(volumes)
                self.ledger.record(f'volumeControl:{name}.included', False,
                                   False,
                                   note=f'{count_text(len(volumes), "volume")}'
                                        ' removed')
                continue

            size = row.get('targetSize')
            if size:
                constant = field.add('Constant')
                field.setNumber(constant, 'VIn', float(size))
                field.setNumbers(constant, 'VolumesList',
                                 [float(tag) for tag in volumes])
                self.background_fields.append(constant)
                self.background_sizes.append((str(name), float(size)))
                applied['sized'] += 1
                self.ledger.record(f'volumeControl:{name}.targetSize', size,
                                   size,
                                   note=count_text(len(volumes), 'volume'))

            if row.get('transfinite'):
                accepted = 0
                if self.tessellated:
                    # Plan 30 WP12. A discrete surface has no corners to
                    # interpolate between, so the volume cannot be structured
                    # either. Say so once instead of failing per volume.
                    reason = ('a transfinite volume is built from transfinite '
                              'faces, and a tessellated import has no CAD '
                              'faces to make structured')
                    self.warnings.append(f'volume control {name!r}: {reason}')
                    self.ledger.record(f'volumeControl:{name}.transfinite',
                                       len(volumes), 0, applied=False,
                                       note=reason)
                    continue
                for tag in volumes:
                    try:
                        # Plan 30 WP12, section 4.4: setTransfiniteVolume on
                        # its own was ineffective, because Gmsh builds a
                        # structured volume out of structured *faces* and the
                        # faces were still unstructured. Every bounding face
                        # is made transfinite first.
                        for _dim, face in gmsh.model.getBoundary(
                                [(3, tag)], oriented=False):
                            gmsh.model.mesh.setTransfiniteSurface(int(abs(face)))
                        gmsh.model.mesh.setTransfiniteVolume(tag)
                        self.transfinite_volumes[int(tag)] = {'control': name}
                        accepted += 1
                    except Exception as error:
                        # Only 5- and 6-faced volumes can be transfinite.
                        # Refusing the whole run over one is worse than saying
                        # which one Gmsh would not take.
                        self.warnings.append(
                            f'volume control {name!r}: Gmsh would not make '
                            f'volume {tag} transfinite ({error})')
                applied['transfinite'] += accepted
                self.ledger.record(
                    f'volumeControl:{name}.transfinite', len(volumes), accepted,
                    applied=bool(accepted),
                    note='transfinite needs a 5- or 6-faced volume whose '
                         'faces are transfinite too')

        self.statistics['volumeControls'] = {**applied, 'requested': len(rows)}
