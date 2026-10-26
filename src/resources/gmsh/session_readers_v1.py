"""Session operations for the readers area (Plan 48 seam, owner GM10).

A mixin of ``session_v1.Session``. ``dispatch()`` finds a public method by
name (``session.<name>``), so a method here is reachable as the operation
``session.<name>``. The mixin must not import ``session_v1``: it is mixed
into ``Session``, and uses ``self.gmsh`` (the recording wrapper) from it.

What it adds (all measured on Gmsh 4.15.2, ``tests/plan48/gm10``):

* ``read_file``: File > Open / Merge for the readers ``session.open`` does not
  list (``.nas``, ``.dat``, ``.neu``, ``.p3d``, ``.diff``, ``.wrl`` /
  ``.vrml`` / ``.iv``, ``.geom``, ``.mmed``, ``.rmed``, ``.vis``) and for a
  ``.gz`` of any format, which is decompressed first, as Gmsh does.
* ``clear_model``: File > Clear.
* ``save_mesh_statistics``: File > Save as ``.pos`` of the mesh quality.
* ``save_visibility``: File > Save as ``.vis``.

Nothing here imports FoamMesh: this file runs inside the Gmsh process.
"""
from __future__ import annotations

import gzip
import os
import shutil
import tempfile

#: Extensions ``read_file`` takes, each with the reader Gmsh runs.
READERS = {
    '.nas': 'Nastran bulk data', '.bdf': 'Nastran bulk data',
    '.dat': 'Nastran, Actran or Samcef data',
    '.neu': 'Gambit neutral', '.p3d': 'Plot3D', '.diff': 'Diffpack',
    '.wrl': 'VRML', '.vrml': 'VRML', '.iv': 'Open Inventor',
    '.geom': 'Geom triangulation', '.mmed': 'MED', '.rmed': 'MED',
    '.med': 'MED', '.msh': 'Gmsh mesh', '.unv': 'I-DEAS UNV',
    '.mesh': 'Medit', '.vtk': 'VTK (legacy)', '.stl': 'STL',
    '.obj': 'Wavefront OBJ', '.ply': 'PLY', '.ply2': 'PLY2',
    '.off': 'OFF', '.cgns': 'CGNS', '.vis': 'Gmsh visibility',
    '.pos': 'Gmsh post-processing',
}
#: ``.dat``: the first bytes choose the reader (``OpenFile.cpp``).
DAT_HEADERS = (('BEGIN ACTRAN', 'actran'), ('!', 'samcef'),
               (';ECHO', 'samcef'), ('.NOE', 'samcef'))

#: ``Print.Post*`` options of the mesh-statistics ``.pos`` writer, by the
#: argument name ``save_mesh_statistics`` takes.
POS_OPTIONS = {'elementary': 'Print.PostElementary',
               'element': 'Print.PostElement',
               'sicn': 'Print.PostSICN', 'sige': 'Print.PostSIGE',
               'gamma': 'Print.PostGamma', 'disto': 'Print.PostDisto'}


def dat_flavour(header: str) -> str:
    for prefix, flavour in DAT_HEADERS:
        if header.startswith(prefix):
            return flavour
    return 'nastran'


def _inner_name(path: str) -> str:
    name = os.path.basename(path)
    return name[:-3] if name.lower().endswith('.gz') else name


class SessionReadersMixin:
    """Operations of the readers area."""

    __slots__ = ()

    def _read_header(self, path, size=128):
        opener = gzip.open if str(path).lower().endswith('.gz') else open
        with opener(path, 'rb') as handle:
            return handle.read(size).decode('latin-1', 'replace')

    def read_file(self, path, mode='open', **_):
        """File > Open (replace the model) or Merge, for the readers
        ``session.open`` does not list, and for ``.gz`` files."""
        gmsh = self.gmsh
        if mode not in ('open', 'merge'):
            raise ValueError('mode must be open or merge')
        if not os.path.isfile(path):
            raise ValueError(f'{path} does not exist')
        inner = _inner_name(path)
        extension = os.path.splitext(inner)[1].lower()
        if extension not in READERS:
            raise ValueError(
                f'{extension or "this file"} is not a format Gmsh reads: '
                + ', '.join(sorted(READERS)))
        info = {'format': READERS[extension], 'extension': extension,
                'compressed': inner != os.path.basename(path)}
        scratch = None
        source = path
        try:
            if info['compressed']:
                # Gmsh runs the system gunzip; reading it here needs no tool
                scratch = tempfile.mkdtemp(prefix='foammesh-gz-')
                source = os.path.join(scratch, inner)
                with gzip.open(path, 'rb') as packed, \
                        open(source, 'wb') as plain:
                    shutil.copyfileobj(packed, plain)
            if extension == '.dat':
                info['flavour'] = dat_flavour(self._read_header(source))
                info['format'] = {
                    'nastran': 'Nastran bulk data', 'actran': 'Actran data',
                    'samcef': 'Samcef data'}[info['flavour']]
            if mode == 'open':
                gmsh.clear()
                gmsh.model.add(os.path.splitext(inner)[0] or 'foammesh')
                self.model_unit = None
                self._forget_fields()
            before = self.state()
            gmsh.merge(source)
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)
        after = self.state()
        added = {d: after['entities'][d] - before['entities'][d]
                 for d in after['entities']}
        result = {'kind': 'visibility' if extension == '.vis' else 'mesh',
                  'mode': mode, 'path': path, 'entities': after['entities'],
                  'added': added, 'nodes': after['nodes'],
                  'elements': sum(after['elements'].values()),
                  'has_mesh': any(after['elements'].values()),
                  'groups': [[d, t, gmsh.model.getPhysicalName(d, t).strip()]
                             for d, t in gmsh.model.getPhysicalGroups()],
                  'read': info}
        if extension == '.vis':
            result['hidden'] = self._hidden_entities()
        return result

    def _hidden_entities(self):
        gmsh = self.gmsh
        hidden = []
        for dim in range(4):
            for _d, tag in gmsh.model.getEntities(dim):
                if not gmsh.model.getVisibility(dim, tag):
                    hidden.append([dim, tag])
        return hidden

    def _forget_fields(self):
        """Fields belong to the model that was cleared."""
        self._native_tags = []
        self._user_background = []
        self._user_aniso = False
        self._combiner_tag = None
        self._auto = None

    def clear_model(self, **_):
        """File > Clear: an empty model, as a fresh session has."""
        gmsh = self.gmsh
        gmsh.clear()
        gmsh.model.add('foammesh')
        self.model_unit = None
        self._forget_fields()
        return self.state()

    def save_mesh_statistics(self, path, elementary=False, element=False,
                             sicn=False, sige=False, gamma=True,
                             disto=False, save_all=False, **_):
        """File > Save as, ``.pos``: the mesh as a post-processing view with
        the chosen quality measure per element (``Print.Post*``).

        MEASURED (4.15.2): SICN and Disto fail with ``Unknown element type
        for ordered monomials: 1`` when the file also holds the 1D elements
        (``Mesh.SaveAll`` on a 3D mesh); only the physical groups are written
        unless *save_all*, which is the default Gmsh's own Save uses.
        """
        gmsh = self.gmsh
        path = str(path)
        if not path.lower().endswith('.pos'):
            raise ValueError('mesh statistics are saved as a .pos file')
        chosen = {'elementary': elementary, 'element': element,
                  'sicn': sicn, 'sige': sige, 'gamma': gamma,
                  'disto': disto}
        options = dict(POS_OPTIONS, save_all='Mesh.SaveAll')
        previous = {name: gmsh.option.getNumber(option)
                    for name, option in options.items()}
        try:
            for name, option in POS_OPTIONS.items():
                gmsh.option.setNumber(option, 1 if chosen[name] else 0)
            gmsh.option.setNumber('Mesh.SaveAll', 1 if save_all else 0)
            gmsh.write(path)
        except Exception as error:
            raise ValueError(
                f'Gmsh could not write the statistics: {error}. SICN and '
                'Disto cannot be written together with the 1D elements: '
                'leave "save all elements" off') from error
        finally:
            for name, option in options.items():
                gmsh.option.setNumber(option, previous[name])
        return {'path': path, 'bytes': os.path.getsize(path),
                'measures': [name for name, on in chosen.items() if on]}

    def save_visibility(self, path, **_):
        """File > Save as, ``.vis``: which entities are hidden."""
        path = str(path)
        if not path.lower().endswith('.vis'):
            raise ValueError('visibility is saved as a .vis file')
        self.gmsh.write(path)
        return {'path': path, 'bytes': os.path.getsize(path),
                'hidden': self._hidden_entities()}
