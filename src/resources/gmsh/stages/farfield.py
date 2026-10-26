"""The generated farfield (Plan 48 RR, DP-4009).

Moved out of ``runner_v1.py``; the batch run and the resident session wrap
the bodies in a box, sphere or cylinder with these functions."""
from __future__ import annotations

import math

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from quantities import agreeing, count_text
import farfield_primitives

from .common import (MeshFailure, _successors_of, _cavities)


class FarfieldStage:
    def build_farfield(self):
        """Wrap the imported solids in a box, sphere or cylinder; cut them out.

        Plan 30 WP12, section 4.1. The assembly fixtures ship a
        ``_farfield.stl`` beside every model because nothing in the product
        could make one, so an external-flow case needed a file authored
        elsewhere. This builds the same thing from the geometry that is
        already loaded: a box ``padding`` bounding-box diagonals larger on
        every side, with the bodies subtracted, leaving one fluid volume whose
        inner boundary is the bodies.

        Returns True when the model changed, because every tag the caller is
        holding is then stale.

        Two things have to survive the cut. The body faces come back with new
        tags, so each is matched to the face it replaced by centre of mass and
        area and recorded in ``surface_origin`` -- the same mechanism a
        classified STL import uses, which is why the scopes and the patch
        names still resolve afterwards. The six new faces are named here,
        because nothing prepared them: ``far_field_xMin`` and its five
        siblings, spelled so the publisher's category rule reads them as a far
        field rather than defaulting them to walls.

        Plan 37 UF13. The primitive may also be a sphere or a cylinder, from
        the shared farfield specification (``farfield_primitives``). It is
        resolved against the bounds the CAD kernel reports and refused --
        before anything is added to the model -- when it does not contain
        every body with clearance. Its faces are named by where they are: a
        boolean renumbers every face, so the sphere's one face is
        ``far_field`` and the cylinder's are ``far_field_side``,
        ``far_field_inlet`` and ``far_field_outlet``, the caps ordered along
        the axis. The box is built, classified and named exactly as before.
        """
        farfield = self.intent.get('farfield') or {}
        if not farfield.get('enabled'):
            return False
        gmsh = self.gmsh
        padding = float(farfield.get('padding', 2.0) or 0.0)
        shape = str(farfield.get('shape') or farfield_primitives.BOX
                    ).strip().lower()
        if self.tessellated:
            reason = (f'a farfield {shape} is cut out of the imported solids '
                      'by the CAD kernel, and a tessellated import has no '
                      'solid to cut; supply the geometry as STEP, or supply '
                      'the farfield as a surface of its own')
            self.warnings.append(f'no farfield {shape} was built: {reason}')
            self.ledger.record('gmsh/farfield/enabled', True, False,
                               applied=False, note=reason)
            return False

        bodies = gmsh.model.getEntities(3)
        before = gmsh.model.getBoundingBox(-1, -1)
        diagonal = math.dist(before[0:3], before[3:6])
        try:
            primitive = farfield_primitives.resolve(farfield, before)
        except farfield_primitives.FarfieldError as error:
            # Refused before the model is touched: nothing was added, and
            # the imported solids are exactly as they were read.
            raise MeshFailure(f'no farfield {shape} was built: {error}'
                              ) from error
        signatures = {tag: self.surface_signature(tag)
                      for _dim, tag in gmsh.model.getEntities(2)}

        tool = self.add_farfield_primitive(primitive)
        try:
            gmsh.model.occ.cut([(3, tool)], list(bodies),
                               removeObject=True, removeTool=True)
            gmsh.model.occ.synchronize()
        except Exception as error:
            raise MeshFailure(
                f'the farfield {shape} could not be cut against the '
                f'geometry: {error}') from error
        volumes = gmsh.model.getEntities(3)
        if not volumes:
            raise MeshFailure(
                f'cutting the farfield {shape} against '
                f'{count_text(len(bodies), "solid")} left no volume at all; '
                f'the {shape} was consumed by the cut, '
                'which happens when a solid is larger than the farfield '
                'allows for')
        if shape == farfield_primitives.BOX:
            origin, span = primitive['origin'], primitive['span']
            domain, sealed = self.classify_cut_volumes(volumes, origin, span,
                                                       diagonal)
        else:
            domain, sealed = self.classify_cut_volumes_by(
                volumes,
                lambda face: self.primitive_face_name(primitive, face)
                is not None)
        if not domain:
            side, remedy = (('a side', 'increase the padding')
                            if shape == farfield_primitives.BOX
                            else ('a face', f'enlarge the {shape}'))
            raise MeshFailure(
                f'cutting the farfield {shape} against '
                f'{count_text(len(bodies), "solid")} left '
                f'{count_text(len(volumes), "volume")}, none of them bounded '
                f'by {side} of the generated {shape}, so none of them is the '
                f'external domain. A solid reaching outside the farfield is '
                f'the usual cause; {remedy} or supply the domain as its own '
                'surface.')
        if len(domain) > 1:
            raise MeshFailure(
                f'cutting the farfield {shape} against '
                f'{count_text(len(bodies), "solid")} '
                f'split the external domain into {len(domain)} separate '
                f'volumes, each touching the generated {shape}. Meshing one '
                'of them would silently drop the rest of the flow region, so '
                'the run stops here: the usual cause is a solid that spans '
                f'the {shape}, or overlapping and open solids.')
        sealed_policy = str((self.intent.get('farfield') or {}).get(
            'sealedCavities', 'discard') or 'discard').strip().lower()
        volumes = self.apply_cavity_policy(domain, sealed, sealed_policy)

        # The body faces, matched back to the tags their names are keyed on.
        self.import_surface_tags = list(signatures)
        inherited, box_faces = 0, []
        for _dim, tag in gmsh.model.getEntities(2):
            centre, area = self.surface_signature(tag)
            source = self.matching_surface(centre, area, signatures, diagonal)
            if source is None:
                box_faces.append((tag, centre))
            else:
                self.surface_origin[tag] = source
                inherited += 1
        # C31-04. The cut is a topology change of the strongest kind: the body
        # faces survive under new tags and every imported *volume* is consumed
        # -- the one volume that remains is the fluid around the bodies, which
        # no imported source owns. Following the surfaces and emptying the
        # volumes is what makes a region-scoped control on a cut-away body a
        # refusal instead of a size silently applied to the fluid domain.
        self.retarget_entities('farfield-cut',
                               _successors_of(self.surface_origin))
        self.retarget_entities('farfield-cut', {}, dimensions=(3,))
        if shape == farfield_primitives.BOX:
            self.name_farfield_faces(box_faces, origin, span, diagonal)
        else:
            self.name_primitive_faces(box_faces, primitive)

        self.record_farfield(primitive, bodies, padding)
        self.statistics['farfield'] = {
            'shape': shape,
            # Only the box reads a padding; a sphere or a cylinder is sized by
            # its radius and length, which ``primitive`` carries.
            'padding': padding if shape == farfield_primitives.BOX else None,
            'standoff': primitive.get('standoff'),
            'primitive': {key: value for key, value in primitive.items()
                          if key not in ('shape',)},
            'primitiveVolume': primitive['volume'],
            'clearance': primitive['clearance'],
            'boundingBox': list(gmsh.model.getBoundingBox(-1, -1)),
            'bodies': len(bodies),
            'domainVolumes': len(domain),
            'domainIdentifiedBy': (
                'faces lying on a side of the generated box'
                if shape == farfield_primitives.BOX
                else f'faces lying on the generated {shape}'),
            'sealedCavities': len(sealed),
            'sealedCavityPolicy': sealed_policy,
            'sealedCavityVolumes': [round(mass, 12) for mass, _tag in sealed],
            'volumes': len(volumes),
            'bodyFaces': inherited,
            'boxFaces': len(box_faces),
            'primitiveFaces': len(box_faces),
            'patches': sorted(self.generated_names.values()),
        }
        if shape != farfield_primitives.BOX:
            # ``boxFaces`` is the box's own count, kept for the readers of the
            # box statistics; a sphere or a cylinder has no box faces, so it
            # reports ``primitiveFaces`` alone. The box keeps both keys, in
            # the same order, so its statistics are unchanged.
            del self.statistics['farfield']['boxFaces']
        detail = ({'standoff_m': round(primitive['standoff'], 6)}
                  if shape == farfield_primitives.BOX
                  else {'clearance_m': round(primitive['clearance'], 6)})
        self.reporter.emit(
            'progress', 'import', 0.12,
            f'built a farfield {shape} around '
            f'{count_text(len(bodies), "solid")}', detail)
        return True

    def add_farfield_primitive(self, primitive):
        """Add the resolved primitive to the OCC model; return its tag."""
        occ = self.gmsh.model.occ
        shape = primitive['shape']
        if shape == farfield_primitives.BOX:
            return occ.addBox(*primitive['origin'], *primitive['span'])
        if shape == farfield_primitives.SPHERE:
            return occ.addSphere(*primitive['centre'], primitive['radius'])
        length = primitive['length']
        return occ.addCylinder(*primitive['base'],
                               *(item * length for item in primitive['axis']),
                               primitive['radius'])

    def record_farfield(self, primitive, bodies, padding):
        """The ledger lines: what was asked for and what was built."""
        shape = primitive['shape']
        solids = count_text(len(bodies), 'solid')
        if shape == farfield_primitives.BOX:
            standoff = primitive['standoff']
            self.ledger.record('gmsh/farfield/enabled', True, True,
                               note=f'{solids} cut out '
                                    f'of a box {padding:g} '
                                    f'{agreeing(padding, "diagonal")} larger '
                                    'on each side')
            self.ledger.record('gmsh/farfield/padding', padding, padding,
                               note=f'{standoff:.6g} m on each side')
        else:
            self.ledger.record('gmsh/farfield/enabled', True, True,
                               note=f'{solids} cut out of a {shape} of '
                                    f'{primitive["volume"]:.6g} m^3, '
                                    f'{primitive["clearance"]:.6g} m clear '
                                    'of the geometry')
            self.ledger.record('gmsh/farfield/radius', primitive['radius'],
                               primitive['radius'], note='m')
        self.ledger.record('gmsh/farfield/shape', shape, shape)
        farfield = self.intent.get('farfield') or {}
        mode = str(farfield.get('centreMode') or 'auto')
        self.ledger.record('gmsh/farfield/centreMode', mode, mode,
                           note=('the bounding-box centre' if mode == 'auto'
                                 else 'as authored'))
        if shape == farfield_primitives.CYLINDER:
            self.ledger.record('gmsh/farfield/length', primitive['length'],
                               primitive['length'], note='m')
            self.ledger.record('gmsh/farfield/axis', farfield.get('axis'),
                               [round(item, 12) for item in primitive['axis']],
                               note='normalised to a unit direction')

    def face_point(self, tag):
        """A point on surface ``tag`` itself.

        The centre of mass of a curved face lies off it -- a sphere's is its
        centre -- so the face is sampled at the middle of its own
        parametrisation instead. Falls back to the centre of mass where the
        kernel cannot say.
        """
        gmsh = self.gmsh
        try:
            low, high = gmsh.model.getParametrizationBounds(2, int(tag))
            middle = [(float(low[index]) + float(high[index])) / 2.0
                      for index in range(2)]
            point = gmsh.model.getValue(2, int(tag), middle)
            if len(point) >= 3:
                return tuple(float(item) for item in point[:3])
        except Exception:                                    # noqa: BLE001
            pass
        return tuple(gmsh.model.occ.getCenterOfMass(2, int(tag)))

    def primitive_face_name(self, primitive, tag):
        """The outer patch face ``tag`` is on, or ``None`` for a body face."""
        return farfield_primitives.classify_point(primitive,
                                                  self.face_point(tag))

    def classify_cut_volumes_by(self, volumes, on_primitive):
        """``classify_cut_volumes`` for a sphere or a cylinder.

        The same rule: the external domain is the volume the primitive
        bounds, and a volume bounded only by body faces is sealed inside the
        geometry. Returns ``(domain, sealed)``, each a list of ``(mass, tag)``.
        """
        domain, sealed = [], []
        for _dim, tag in volumes:
            faces = [int(face) for _face_dim, face
                     in self.gmsh.model.getBoundary([(3, int(tag))],
                                                    oriented=False)]
            entry = (float(self.gmsh.model.occ.getMass(3, int(tag))), int(tag))
            (domain if any(on_primitive(abs(face)) for face in faces)
             else sealed).append(entry)
        return domain, sealed

    def name_primitive_faces(self, faces, primitive):
        """Name the sphere's or cylinder's faces by where each one lies."""
        for tag, _centre in faces:
            name = self.primitive_face_name(primitive, tag)
            if name is not None:
                self.generated_names[int(tag)] = name
                continue
            self.warnings.append(
                f'surface {tag} appeared during the farfield cut and lies '
                f'on no face of the {primitive["shape"]}; it publishes '
                'unnamed')

    def classify_cut_volumes(self, volumes, origin, span, diagonal):
        """Split what the cut left into the external domain and the cavities.

        Plan 31 CP-08 item 7: "never silently assume the largest remaining
        volume is the user's intended domain". This used to sort the volumes
        by mass and call the biggest one the domain, which is right on every
        fixture here and right for the wrong reason -- it is a guess that
        happens to hold while the box contains everything. Touching the
        generated box is not a guess: the external domain is the volume the
        box bounds, and a volume bounded only by body faces is enclosed by
        the geometry.

        MEASURED on all six assembly fixtures at padding 1.5. Every one is a
        solid with internal voids, and every cut left exactly two volumes:
        the domain, with 12 faces of which 6 lie on the box and 6 are
        inherited body faces, and one sealed pocket whose faces are all
        inherited -- 126 on the finned heat sink, 147 on the lattice bracket,
        79 on the tube bundle, 111 on the turbine cascade, 34 on the
        centrifugal impeller. The drone quadcopter left six volumes: the
        domain, four three-faced pockets of 1.85e-06 m^3 and one of
        4.02e-04 m^3. The mass order and this classification agree on all six
        -- which is the point of measuring rather than assuming, because the
        case they disagree on is a domain the cut has split in two, and there
        the mass rule discards half the flow region without a word.

        Returns ``(domain, sealed)``, each a list of ``(mass, tag)``.
        """
        tolerance = max(diagonal, 1e-9) * 1e-6
        planes = [(axis, coordinate)
                  for axis in range(3)
                  for coordinate in (origin[axis], origin[axis] + span[axis])]

        def on_the_box(face):
            centre, _area = self.surface_signature(face)
            return any(abs(centre[axis] - coordinate) <= tolerance
                       for axis, coordinate in planes)

        domain, sealed = [], []
        for _dim, tag in volumes:
            faces = [int(face) for _face_dim, face
                     in self.gmsh.model.getBoundary([(3, int(tag))],
                                                    oriented=False)]
            entry = (float(self.gmsh.model.occ.getMass(3, int(tag))), int(tag))
            (domain if any(on_the_box(abs(face)) for face in faces)
             else sealed).append(entry)
        return domain, sealed

    def apply_cavity_policy(self, domain, sealed, policy):
        """Do what the case says about sealed cavities, and say it was done.

        Three answers, because a sealed pocket inside the geometry is a real
        region and which of the three is right is the user's question, not
        this runner's: ``discard`` meshes the external domain alone,
        ``keep`` meshes the pockets too as separate regions, and ``refuse``
        stops so the geometry can be looked at. Discarding stays the default
        -- an external-flow case that meshes an unreachable pocket hands the
        solver a disconnected region -- but it is now a recorded decision
        with an alternative, not the only thing the product can do.
        """
        self.ledger.record('gmsh/farfield/sealedCavities', policy, policy,
                           note=f'{_cavities(len(sealed))} found')
        if not sealed:
            return self.gmsh.model.getEntities(3)
        sizes = ', '.join(f'{mass:.3g} m^3' for mass, _tag in sealed)
        if policy == 'refuse':
            raise MeshFailure(
                f'{_cavities(len(sealed))} inside the geometry '
                f'{agreeing(len(sealed), "was", "were")} '
                f'left by the farfield cut ({sizes}), and this case asks to '
                'be told rather than have them decided. External flow does '
                'not reach them. Choose to discard them to mesh the outside '
                'only, or to keep them to mesh them as separate regions.')
        if policy == 'keep':
            self.warnings.append(
                f'{_cavities(len(sealed))} inside the geometry '
                f'{agreeing(len(sealed), "was", "were")} '
                'left by the farfield cut and kept, as this case asks: they '
                'mesh as regions disconnected from the external domain, so '
                f'the mesh has {len(domain) + len(sealed)} volumes. Their '
                f'volumes are {sizes}')
            self.ledger.record('gmsh/farfield/enabled.cavities',
                               len(sealed), len(sealed),
                               note='sealed internal cavities kept')
            return self.gmsh.model.getEntities(3)
        for _mass, tag in sealed:
            self.gmsh.model.occ.remove([(3, tag)], recursive=True)
        self.gmsh.model.occ.synchronize()
        self.warnings.append(
            f'{_cavities(len(sealed))} inside the geometry '
            f'{agreeing(len(sealed), "was", "were")} '
            'left by the farfield cut and discarded: external flow does not '
            'reach them, and meshing them would produce a second, '
            f'disconnected region. Their volumes were {sizes}')
        self.ledger.record('gmsh/farfield/enabled.cavities',
                           len(sealed), len(sealed),
                           note='sealed internal cavities discarded')
        return self.gmsh.model.getEntities(3)

    def surface_signature(self, tag):
        """``(centre of mass, area)``: what a face still is after a cut."""
        return (tuple(self.gmsh.model.occ.getCenterOfMass(2, int(tag))),
                float(self.gmsh.model.occ.getMass(2, int(tag))))

    @staticmethod
    def matching_surface(centre, area, signatures, diagonal):
        """The pre-cut face this one came from, or ``None`` if it is new.

        A cut does not move the faces it keeps, so the centre of mass and the
        area are the same to within the kernel's own tolerance. The tolerance
        is scaled by the model so it means the same thing on a 40 mm bracket
        and a 4 m wing.
        """
        tolerance = max(diagonal, 1e-9) * 1e-6
        for source, (source_centre, source_area) in signatures.items():
            if (math.dist(centre, source_centre) <= tolerance
                    and abs(area - source_area)
                    <= max(source_area, 1e-12) * 1e-6):
                return source
        return None

    def name_farfield_faces(self, box_faces, origin, span, diagonal):
        """Name the six sides of the generated box by the plane each lies on.

        ``far_field_xMin`` rather than ``farfield_xMin``: the publisher reads
        a patch's category out of its name, and ``far_field`` is the category
        it knows. Spelled the other way every side of the domain would publish
        as a wall.
        """
        tolerance = max(diagonal, 1e-9) * 1e-6
        planes = []
        for axis, name in enumerate('xyz'):
            planes.append((axis, origin[axis], f'far_field_{name}Min'))
            planes.append((axis, origin[axis] + span[axis],
                           f'far_field_{name}Max'))
        for tag, centre in box_faces:
            for axis, coordinate, name in planes:
                if abs(centre[axis] - coordinate) <= tolerance:
                    self.generated_names[int(tag)] = name
                    break
            else:
                # Not on a side of the box: an inner face the match missed.
                # Naming it far field would put a wall on the boundary list.
                self.warnings.append(
                    f'surface {tag} appeared during the farfield cut and lies '
                    'on no side of the box; it publishes unnamed')
