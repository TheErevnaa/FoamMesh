"""Session operations for the fields area (Plan 48 seam, owner GM8).

A mixin of ``session_v1.Session``. ``dispatch()`` finds a public method by
name (``session.<name>``), so a method added here is reachable as the
operation ``session.<name>`` with no other change. The mixin must not import
``session_v1``: it is mixed into ``Session``, and uses ``self.gmsh`` (the
recording wrapper), ``self.raw`` (the bare module), ``self._native()`` and
``self._compose_background()`` from it.

* ``field_visualize``: the sizes of one field alone, for the viewport
  (decision D6 / the single-field view); the background is put back after.
* ``size_data_scan``: what a data file holds, read by Gmsh itself (the host
  reads the ASCII forms; this one also reads binary ``.msh`` files).
"""
from __future__ import annotations

import os

_HIDDEN_SCAN = '__sizeDataScan__'


class SessionFieldsMixin:
    """Operations of the fields area."""

    __slots__ = ()

    def field_visualize(self, name=None, tag=None, dim=2, limit=20000,
                        seed=1, **_):
        """Sizes ONE field gives, sampled where Gmsh asks for a size.

        Refused while the model has a mesh (the preview meshes the model with
        the field alone and clears it again). The user's background, with the
        automatic field in it, is put back whatever happens.
        """
        native = self._native()
        if tag is None:
            tag = native.last_tags().get(str(name))
            if tag is None:
                raise ValueError(f'there is no size field {name!r} on the '
                                 'model; apply the fields first')
        if native.mesh_exists(self.gmsh):
            raise ValueError('mesh_exists: clear the mesh first; the preview '
                             'meshes the model with the field alone')
        try:
            result = native.preview_field(self.gmsh, int(tag), dim=int(dim),
                                          limit=int(limit), seed=int(seed))
        except native.BuildError as error:
            raise ValueError(str(error)) from error
        finally:
            chosen = self._compose_background()
            if chosen is None:
                try:
                    self.gmsh.model.mesh.field.setAsBackgroundMesh(-1)
                except Exception:                     # noqa: BLE001
                    pass
        return result

    def size_data_scan(self, path, **_):
        """The datasets a .pos or .msh holds: name, steps, components."""
        gmsh = self.raw
        suffix = os.path.splitext(str(path))[1].lower()
        if suffix not in ('.pos', '.msh'):
            raise ValueError(f'{os.path.basename(str(path))!r} is not a '
                             '.pos or .msh file')
        before = set(gmsh.view.getTags())
        current = gmsh.model.getCurrent()
        hidden = suffix == '.msh'
        if hidden:
            gmsh.model.add(_HIDDEN_SCAN)
        datasets = []
        try:
            gmsh.merge(str(path))
            for index, tag in enumerate(t for t in gmsh.view.getTags()
                                        if t not in before):
                steps = int(gmsh.view.option.getNumber(tag, 'NbTimeStep'))
                try:
                    _k, _e, _d, _t, comps = gmsh.view.getModelData(tag, 0)
                    components = int(comps)
                except Exception:                     # noqa: BLE001
                    types, _n, _d = gmsh.view.getListData(tag)
                    kinds = {t[:1] for t in types}
                    components = {'S': 1, 'V': 3, 'T': 9}.get(
                        sorted(kinds)[0], 1) if kinds else 1
                datasets.append({
                    'index': index,
                    'name': gmsh.view.option.getString(tag, 'Name'),
                    'steps': [{'index': s} for s in range(steps)],
                    'components': components, 'scalar': components == 1})
        finally:
            for tag in [t for t in gmsh.view.getTags() if t not in before]:
                gmsh.view.remove(tag)
            if hidden:
                try:
                    gmsh.model.remove()
                    gmsh.model.setCurrent(current)
                except Exception:                     # noqa: BLE001
                    pass
        return {'file': str(path), 'datasets': datasets}
