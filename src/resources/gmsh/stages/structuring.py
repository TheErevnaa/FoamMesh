"""Automatic structuring and the structuring option (Plan 48 RR, DP-4009)."""
from __future__ import annotations


import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from quantities import agreeing, count_text


class StructuringStage:
    def apply_structuring_options(self):
        """The one structuring setting that is a Gmsh option.

        Plan 31 FC-C. ``Mesh.TransfiniteTri`` decides how a three-sided
        transfinite face is filled, and it is read at meshing time, so it is
        set here rather than beside ``setTransfiniteAutomatic`` -- a face made
        transfinite by a curve control obeys it too, with automatic
        structuring switched off.

        MEASURED on Gmsh 4.15.2: a triangle carrying nine nodes on each side
        came back as 120 triangles with this off and 64 with it on, and 64 is
        8x8 -- the count a structured side of eight elements owes. At five
        nodes a side the pair reads 28 and 16, and 16 is 4x4. What the option
        produced is counted out of the mesh either way; nothing here reads it
        back.
        """
        structuring = self.intent.get('structuring') or {}
        if not structuring.get('transfiniteTri'):
            return
        self.set_number('Mesh.TransfiniteTri', 1,
                        control='gmsh/structuring/transfiniteTri')

    def apply_automatic_structuring(self):
        """Ask Gmsh to structure every volume it can, and remember which.

        Plan 31 FC-C. ``setTransfiniteAutomatic`` walks the model, finds the
        volumes whose faces it can interpolate between, constrains them, and
        recombines them. It returns nothing and raises nothing when it takes
        some volumes and leaves others.

        MEASURED on Gmsh 4.15.2. A unit box came back as 125 hexahedra behind
        150 quadrilateral boundary faces, where the same box is 415 tetrahedra
        behind 260 triangles without it. A model holding that box beside a
        sphere came back as 125 hexahedra *and* 207 tetrahedra -- one volume
        of two structured, in one mesh, silently. So the volumes are recorded
        here and counted out of the mesh in :meth:`measure_structuring`; this
        method never claims the model was structured.
        """
        structuring = self.intent.get('structuring') or {}
        if not structuring.get('automatic'):
            return
        volumes = [int(tag) for _dim, tag in self.gmsh.model.getEntities(3)]
        if not volumes:
            reason = ('automatic structuring works volume by volume and this '
                      'model has no volumes yet')
            self.warnings.append(reason)
            self.ledger.record('gmsh/structuring/automatic', True, False,
                               applied=False, note=reason)
            return
        try:
            self.gmsh.model.mesh.setTransfiniteAutomatic()
        except Exception as error:                           # noqa: BLE001
            reason = f'Gmsh would not structure this model automatically ({error})'
            self.warnings.append(reason)
            self.ledger.record('gmsh/structuring/automatic', True, False,
                               applied=False, note=reason)
            return
        self.automatic_structuring = {'volumes': volumes}

    def measure_structuring(self):
        """How many volumes automatic structuring actually took.

        The request is one flag; the result is one answer per volume, and the
        two are recorded separately. MEASURED: a volume Gmsh structured comes
        back with no tetrahedra in it -- 125 hexahedra for a unit box -- and
        one it declined comes back as tetrahedra, 209 of them for a sphere in
        the same run. That difference is the reading; the option cannot be
        asked, because there is no option.
        """
        if not self.automatic_structuring:
            return
        gmsh = self.gmsh
        families, structured, unstructured = {}, [], []
        for tag in self.automatic_structuring['volumes']:
            try:
                types, groups, _n = gmsh.model.mesh.getElements(3, int(tag))
            except Exception:                                # noqa: BLE001
                # The volume is gone -- excluded, or consumed by the farfield
                # cut. It was not left unstructured; it was not there.
                continue
            found: dict = {}
            for etype, group in zip(types, groups):
                name = gmsh.model.mesh.getElementProperties(etype)[0]
                found[name] = found.get(name, 0) + len(group)
            families[str(tag)] = found
            went = bool(found) and not any(
                'Tetrahedron' in name for name in found)
            (structured if went else unstructured).append(int(tag))
        total = len(families)
        whole = bool(total) and not unstructured
        self.statistics['automaticStructuring'] = {
            'requestedVolumes': total,
            'structuredVolumes': sorted(structured),
            'unstructuredVolumes': sorted(unstructured),
            'structured': whole,
            'families': families,
        }
        if unstructured:
            # Reporting this mesh as structured is the failure the control was
            # written to avoid.
            self.warnings.append(
                f'automatic structuring took {len(structured):,} of '
                f'{count_text(total, "volume")}; '
                f'{agreeing(len(unstructured), "volume")} '
                + ', '.join(str(tag) for tag in sorted(unstructured))
                + ' came back as tetrahedra, so the mesh is partly structured '
                  'and is not reported as a structured mesh')
        self.ledger.record(
            'gmsh/structuring/automatic', total, len(structured),
            applied=bool(structured),
            note='volumes asked for against volumes that came back with no '
                 'tetrahedra in them')
