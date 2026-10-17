#!/usr/bin/env python3
"""The automatic size field as a background mesh in a Gmsh session (Plan 41
GN25, slice S5).

``foammesh-sizefield`` 1.x writes the octree as arrays (``nodes_xyz`` M x 3,
``nodes_size`` M, ``hexes`` N x 8 indices into the nodes, in Gmsh's own
hexahedron order).  The session turns them into:

* a separate model ``foammesh_sizefield`` holding one discrete volume made of
  those hexahedra (the model being meshed is never touched),
* a view on that model with the size at every node
  (``view.addHomogeneousModelData``, one value per node),
* a ``PostView`` field reading the view, optionally wrapped in ``Octree``
  (Gmsh builds a search tree over the view; faster lookups on large fields),

and the caller makes that field part of the background and switches back to
the model it was meshing.  While the field is active the three native sizing
options that would fight it are held at 0 and put back when it is turned off.
"""
from __future__ import annotations

import os

MODEL = 'foammesh_sizefield'
VIEW_NAME = 'foammesh size field'
#: Gmsh element type of an 8-node hexahedron.
HEXAHEDRON = 5
#: Options Gmsh would combine with the field; held at 0 while it is active.
HELD = ('Mesh.MeshSizeExtendFromBoundary', 'Mesh.MeshSizeFromPoints',
        'Mesh.MeshSizeFromCurvature')
FILES = ('nodes_xyz.npy', 'nodes_size.npy', 'hexes.npy')


class FieldError(ValueError):
    """The field files are missing or inconsistent."""


def load(directory: str) -> dict:
    """Read the three arrays; check they agree before Gmsh sees them."""
    import numpy as np
    missing = [name for name in FILES
               if not os.path.isfile(os.path.join(directory, name))]
    if missing:
        raise FieldError('the size field in %s lacks %s' % (
            directory, ', '.join(missing)))
    nodes = np.load(os.path.join(directory, 'nodes_xyz.npy'))
    sizes = np.load(os.path.join(directory, 'nodes_size.npy'))
    hexes = np.load(os.path.join(directory, 'hexes.npy'))
    if nodes.ndim != 2 or nodes.shape[1] != 3:
        raise FieldError('nodes_xyz.npy must be M x 3, it is %s'
                         % (tuple(nodes.shape),))
    if sizes.shape != (nodes.shape[0],):
        raise FieldError('nodes_size.npy has %d values for %d nodes'
                         % (sizes.size, nodes.shape[0]))
    if hexes.ndim != 2 or hexes.shape[1] != 8 or hexes.shape[0] == 0:
        raise FieldError('hexes.npy must be N x 8 with N > 0, it is %s'
                         % (tuple(hexes.shape),))
    if int(hexes.min()) < 0 or int(hexes.max()) >= nodes.shape[0]:
        raise FieldError('hexes.npy points outside the %d nodes'
                         % nodes.shape[0])
    return {'nodes': nodes, 'sizes': sizes, 'hexes': hexes}


def hold(gmsh, held: dict | None = None) -> dict:
    """Set the three options to 0.  *held* keeps what was there first."""
    held = dict(held or {})
    for name in HELD:
        value = gmsh.option.getNumber(name)
        if name not in held:
            held[name] = value
        gmsh.option.setNumber(name, 0.0)
    return held


def release(gmsh, held: dict) -> dict:
    """Put the held options back; returns what they are now."""
    for name, value in held.items():
        gmsh.option.setNumber(name, value)
    return {name: gmsh.option.getNumber(name) for name in held}


def build(gmsh, directory: str, *, octree: bool = False) -> dict:
    """Create the model, the view and the field.  Returns tags and counts.

    The caller's current model is current again on return.
    """
    import numpy as np
    arrays = load(directory)
    nodes, sizes, hexes = arrays['nodes'], arrays['sizes'], arrays['hexes']
    main = gmsh.model.getCurrent()
    try:
        gmsh.model.add(MODEL)
        gmsh.model.addDiscreteEntity(3, 1)
        tags = np.arange(1, nodes.shape[0] + 1, dtype=np.uint64)
        gmsh.model.mesh.addNodes(
            3, 1, tags,
            np.ascontiguousarray(nodes, dtype=np.float64).ravel())
        element_tags = np.arange(1, hexes.shape[0] + 1, dtype=np.uint64)
        gmsh.model.mesh.addElementsByType(
            1, HEXAHEDRON, element_tags,
            (hexes.astype(np.uint64) + 1).ravel())
        view = gmsh.view.add(VIEW_NAME)
        gmsh.view.addHomogeneousModelData(
            view, 0, MODEL, 'NodeData', tags,
            np.ascontiguousarray(sizes, dtype=np.float64))
    finally:
        gmsh.model.setCurrent(main)
    field = gmsh.model.mesh.field
    post = field.add('PostView')
    field.setNumber(post, 'ViewTag', float(view))
    chosen = post
    wrapper = None
    if octree:
        wrapper = field.add('Octree')
        field.setNumber(wrapper, 'InField', float(post))
        chosen = wrapper
    return {'view': view, 'postView': post, 'octree': wrapper,
            'field': chosen, 'model': MODEL, 'current': main,
            'nodes': int(nodes.shape[0]), 'hexes': int(hexes.shape[0]),
            'sizeMin': float(sizes.min()), 'sizeMax': float(sizes.max()),
            'bounds': [nodes.min(axis=0).tolist(), nodes.max(axis=0).tolist()]}


def remove(gmsh, built: dict) -> None:
    """Drop the fields, the view and the model of a field built before."""
    field = gmsh.model.mesh.field
    for key in ('octree', 'postView'):
        if built.get(key) is not None:
            try:
                field.remove(built[key])
            except Exception:                             # noqa: BLE001
                pass
    try:
        gmsh.view.remove(built['view'])
    except Exception:                                     # noqa: BLE001
        pass
    main = gmsh.model.getCurrent()
    try:
        gmsh.model.setCurrent(MODEL)
        gmsh.model.remove()
    except Exception:                                     # noqa: BLE001
        pass
    finally:
        try:
            gmsh.model.setCurrent(built.get('current') or main)
        except Exception:                                 # noqa: BLE001
            pass
