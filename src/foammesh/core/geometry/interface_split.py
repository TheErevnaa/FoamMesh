#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Where two bodies meet, cut away from the skin they are wrapped in.

DP-421 and DP-422. A conformal assembly arrives as one closed surface per
body, and each body's surface is two different things at once: the part of it
the body shares with its neighbour, which is an *interface*, and the part it
shares with nothing, which is the model's own outer *wall*. snappyHexMesh
cannot be told that. ``refinementSurfaces.C:143-204`` reads exactly four keys
inside a ``regions`` sub-dictionary -- ``level``, ``gapLevelIncrement``,
``perpendicularAngle``, ``patchInfo`` -- and ``surfaceZonesInfo.C:66`` calls
the zone keys "Global zone names per surface", so one surface entry cannot be
a cell zone on part of itself and a patch on the rest.

v13's own answer is to split the geometry instead, and
``multiRegion/CHT/heatedDuct`` writes it out: ``heatedDuct.stl`` is the
assembly's outer skin and carries no zone keys at all, while
``fluidToMetal.stl`` and ``metalToHeater.stl`` are the interfaces, one
surface each, each carrying ``faceZone``/``cellZone``/``mode insidePoint``.
This module performs that split on the triangulation the user imported, so
the writer has the three surfaces v13 wants instead of the two the fixture
holds.

The reading is the same one :mod:`shell_topology` uses for DP-456: a triangle
of an interface is drawn twice, once by each body, from the *same welded
nodes*, the two copies differing only in the direction they are walked. Two
bodies sharing a face is therefore the one case where a node triple carries
triangles from two different shells, and that is what is measured here --
geometry, not tolerance. Nothing is moved, nothing is re-welded, and a body
that touches nothing comes back whole.
"""
from __future__ import annotations

import math
from pathlib import Path

from foammesh.core.quantities import count_text

from .domain_topology import _classifier, read_tessellation

#: Version of the record :func:`split_assembly` writes.
SCHEMA_VERSION = 1

#: Joins two shell names into the name their interface takes. Snake case,
#: because every other name this product writes into a dictionary is, and an
#: OpenFOAM zone name is a word either way.
INTERFACE_JOIN = '_to_'


class InterfaceSplitError(ValueError):
    """The assembly cannot be split, and the message says which shell."""


def interface_name(first: str, second: str) -> str:
    """The name the interface between two shells takes.

    Sorted, so the same pair of bodies yields the same name whichever order
    the import happened to hand them over in -- a zone name that depends on
    file order is a zone name that changes under a re-import.
    """
    low, high = sorted((str(first), str(second)))
    return f'{low}{INTERFACE_JOIN}{high}'


def _unordered(triple) -> tuple:
    return tuple(sorted(int(node) for node in triple))


def shared_triangles(owned: dict) -> dict:
    """``{(shell, shell): {node triple: [triangle per shell]}}``.

    *owned* is :func:`shell_topology.build_shells`'s second return value,
    ``{shell name: [triangles]}``. A triple carried by two different shells is
    a face those two bodies share; a triple carried twice by one shell is not
    an interface at all but a body folded onto itself, and is left alone here
    so that the readiness check still gets to refuse it in its own words.
    """
    holders: dict = {}
    for name, triangles in (owned or {}).items():
        for triangle in triangles:
            holders.setdefault(_unordered(triangle), {})[name] = triangle
    pairs: dict = {}
    for key, by_shell in holders.items():
        if len(by_shell) < 2:
            continue
        names = sorted(by_shell)
        for index, first in enumerate(names):
            for second in names[index + 1:]:
                pairs.setdefault((first, second), {})[key] = (
                    by_shell[first], by_shell[second])
    return pairs


def split_owned(owned: dict) -> tuple:
    """``(externals, interfaces)`` for an assembly's shells.

    *externals* is ``{shell: [triangles]}`` with every shared face removed,
    and *interfaces* is ``{interface name: {'between': (a, b),
    'triangles': [...]}}`` holding one copy of each shared face -- the copy
    the alphabetically first body drew, so the winding that survives is a
    body's own and not an arbitrary one.
    """
    pairs = shared_triangles(owned)
    removed: dict = {}
    interfaces: dict = {}
    for (first, second), faces in sorted(pairs.items()):
        name = interface_name(first, second)
        interfaces[name] = {
            'between': (first, second),
            'triangles': [faces[key][0] for key in sorted(faces)],
        }
        for key in faces:
            removed.setdefault(first, set()).add(key)
            removed.setdefault(second, set()).add(key)
    externals = {}
    for name, triangles in (owned or {}).items():
        gone = removed.get(name, ())
        externals[name] = [triangle for triangle in triangles
                           if _unordered(triangle) not in gone]
    return externals, interfaces


# --------------------------------------------------------------------------- #
# A point strictly inside a shell, which is what ``mode insidePoint`` wants
# --------------------------------------------------------------------------- #

def _normal(triangle, coordinates):
    a, b, c = (coordinates[node] for node in triangle)
    u = tuple(b[axis] - a[axis] for axis in range(3))
    v = tuple(c[axis] - a[axis] for axis in range(3))
    normal = (u[1] * v[2] - u[2] * v[1],
              u[2] * v[0] - u[0] * v[2],
              u[0] * v[1] - u[1] * v[0])
    length = math.sqrt(sum(value * value for value in normal))
    if length <= 0.0:
        return (0.0, 0.0, 0.0), 0.0
    return tuple(value / length for value in normal), length / 2.0


#: Ray directions the parity test walks, in order. None of them is axis
#: aligned and no two are parallel, because the one way a parity count goes
#: wrong is a ray that grazes an edge or a vertex -- and a box's centre lies
#: on the diagonal its two front triangles share, which an axis-aligned ray
#: hits twice and reads as outside.
_RAYS = (
    (0.5628, 0.6390, 0.5245),
    (-0.7311, 0.4409, 0.5203),
    (0.3313, -0.5621, 0.7584),
)


def _cast(point, direction, triangles, coordinates):
    """``(crossings, grazed)`` for one ray, by Moller-Trumbore.

    *grazed* is set when a hit lands within a whisker of an edge or of the
    origin, which is when the count cannot be trusted; the caller then tries
    another direction rather than returning a number it knows is arbitrary.
    """
    count, grazed = 0, False
    for triangle in triangles:
        a, b, c = (coordinates[node] for node in triangle)
        edge1 = tuple(b[axis] - a[axis] for axis in range(3))
        edge2 = tuple(c[axis] - a[axis] for axis in range(3))
        pvec = (direction[1] * edge2[2] - direction[2] * edge2[1],
                direction[2] * edge2[0] - direction[0] * edge2[2],
                direction[0] * edge2[1] - direction[1] * edge2[0])
        det = sum(edge1[axis] * pvec[axis] for axis in range(3))
        if abs(det) < 1e-18:
            continue
        inv = 1.0 / det
        tvec = tuple(point[axis] - a[axis] for axis in range(3))
        u = inv * sum(tvec[axis] * pvec[axis] for axis in range(3))
        qvec = (tvec[1] * edge1[2] - tvec[2] * edge1[1],
                tvec[2] * edge1[0] - tvec[0] * edge1[2],
                tvec[0] * edge1[1] - tvec[1] * edge1[0])
        v = inv * sum(direction[axis] * qvec[axis] for axis in range(3))
        w = 1.0 - u - v
        if u < -1e-9 or v < -1e-9 or w < -1e-9:
            continue
        distance = inv * sum(edge2[axis] * qvec[axis] for axis in range(3))
        if distance <= 1e-12:
            continue
        if min(u, v, w) < 1e-7:
            grazed = True
        count += 1
    return count, grazed


def _crossings(point, triangles, coordinates) -> int:
    """How many faces a ray from *point* passes through. Odd means inside."""
    count = 0
    for direction in _RAYS:
        count, grazed = _cast(point, direction, triangles, coordinates)
        if not grazed:
            return count
    return count


def inside(point, triangles, coordinates) -> bool:
    """Is *point* inside the closed surface *triangles* bounds?"""
    return _crossings(point, triangles, coordinates) % 2 == 1


def interior_point(triangles, coordinates, *, volume=None):
    """A point strictly inside a closed shell, or ``None``.

    ``mode insidePoint`` names the side of an interface the cell zone is on
    (``surfaceZonesInfo.C:70-82``), so this has to be a point the *region*
    holds and not merely one near it. The bounding-box centre answers for a
    convex body and is tried first because it is the point a reader would
    expect to see written down; anything else steps inward from a face, by a
    quarter of that face's own size so the step scales with the model.

    *volume* is the shell's signed volume, whose sign says which way the
    normals point; without it both directions are tried, which costs a second
    parity test and never guesses.
    """
    triangles = list(triangles)
    if not triangles:
        return None
    candidates = []
    box_low = [min(coordinates[node][axis]
                   for triangle in triangles for node in triangle)
               for axis in range(3)]
    box_high = [max(coordinates[node][axis]
                    for triangle in triangles for node in triangle)
                for axis in range(3)]
    candidates.append(tuple((box_low[axis] + box_high[axis]) / 2.0
                            for axis in range(3)))
    signs = (-1.0, 1.0) if volume is None else (
        (-1.0,) if float(volume) >= 0.0 else (1.0,))
    # Largest faces first: the step inward from a big face is the one least
    # likely to land back outside through a neighbouring wall.
    ranked = sorted(triangles,
                    key=lambda item: -_normal(item, coordinates)[1])
    for triangle in ranked[:64]:
        normal, area = _normal(triangle, coordinates)
        if area <= 0.0:
            continue
        centre = tuple(sum(coordinates[node][axis] for node in triangle) / 3.0
                       for axis in range(3))
        step = 0.25 * math.sqrt(area)
        for sign in signs:
            candidates.append(tuple(centre[axis] + sign * step * normal[axis]
                                    for axis in range(3)))
    for candidate in candidates:
        if inside(candidate, triangles, coordinates):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Writing the pieces back out
# --------------------------------------------------------------------------- #

def write_stl(path, name, triangles, coordinates) -> str:
    """One ASCII STL holding one named solid, with computed facet normals.

    ASCII on purpose: these files are generated, small beside the import they
    came from, and a reader who wants to know what the split produced can open
    one. The solid name is the name the dictionary will use, so the file and
    the ``refinementSurfaces`` entry cannot drift apart.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'solid {name}']
    for triangle in triangles:
        normal, _area = _normal(triangle, coordinates)
        lines.append('  facet normal {0:.9g} {1:.9g} {2:.9g}'.format(*normal))
        lines.append('    outer loop')
        for node in triangle:
            lines.append('      vertex {0:.9g} {1:.9g} {2:.9g}'.format(
                *coordinates[node]))
        lines.append('    endloop')
        lines.append('  endfacet')
    lines.append(f'endsolid {name}')
    path.write_text('\n'.join(lines) + '\n', encoding='ascii')
    return str(path)


# --------------------------------------------------------------------------- #
# The record
# --------------------------------------------------------------------------- #

def split_assembly(paths, *, labels=None, directory=None) -> dict:
    """Read an assembly and say what its interfaces and its outer skin are.

    Never raises for a geometry it can read. An import with no shared face is
    not an error and not an assembly: it comes back with no interfaces and
    every shell whole, which is the answer for the single-region models that
    are most of the catalogue.

    With *directory*, each piece is also written out as an STL beside it and
    the record carries the paths; without it the record is a measurement and
    nothing is written, which is what a readiness check wants.
    """
    topology = _classifier()
    surfaces, triangles, coordinates, sources = read_tessellation(
        paths, labels=labels)
    record = {
        'schema_version': SCHEMA_VERSION,
        'sources': [str(item) for item in paths],
        'shells': [],
        'interfaces': [],
        'externals': [],
        'warnings': [],
        'is_assembly': False,
    }
    if not surfaces:
        record['warnings'].append(
            'the import produced no triangles, so there is nothing to split')
        return record

    shells, owned = topology.build_shells(
        surfaces, triangles, coordinates, sources)
    record['shells'] = [shell.name for shell in shells]
    volumes = {shell.name: shell.volume for shell in shells}
    externals, interfaces = split_owned(owned)
    record['is_assembly'] = bool(interfaces)

    for name in sorted(interfaces):
        item = interfaces[name]
        first, second = item['between']
        entry = {
            'name': name,
            'between': [first, second],
            'triangles': len(item['triangles']),
            'inside': None,
            'insideShell': None,
        }
        # The zone an interface carves is the smaller of the two bodies it
        # separates. v13 names one region per interface and leaves the
        # outermost as the remainder (`heatedDuct`'s Allrun does it with
        # `splitMeshRegions -defaultRegionName fluid`), so the region named
        # here is the one *enclosed*, never the one doing the enclosing.
        enclosed = min((first, second), key=lambda item: abs(volumes.get(item, 0.0)))
        point = interior_point(owned.get(enclosed, ()), coordinates,
                               volume=volumes.get(enclosed))
        entry['insideShell'] = enclosed
        entry['inside'] = list(point) if point else None
        if point is None:
            record['warnings'].append(
                f'no point strictly inside {enclosed!r} could be found, so '
                f'the cell zone for {name!r} has no seed and must be given '
                'one by hand')
        if directory is not None:
            entry['path'] = write_stl(
                Path(directory) / f'{name}.stl', name,
                item['triangles'], coordinates)
        record['interfaces'].append(entry)

    for name in sorted(externals):
        kept = externals[name]
        entry = {
            'name': name,
            'triangles': len(kept),
            'removed': len(owned.get(name, ())) - len(kept),
        }
        if not kept:
            record['warnings'].append(
                f'every face of {name!r} is shared with another body, so it '
                'has no outer wall of its own')
        elif directory is not None:
            entry['path'] = write_stl(
                Path(directory) / f'{name}.stl', name, kept, coordinates)
        record['externals'].append(entry)
    return record


def summary_sentence(record: dict) -> str:
    """One line for the user, whichever of the two cases they have."""
    interfaces = record.get('interfaces') or []
    if not interfaces:
        return (count_text(len(record.get('shells') or ()), 'body', 'bodies')
                + ' and no shared face, so there is no interface to split out')
    named = ', '.join(str(item['name']) for item in interfaces[:4])
    faces = sum(int(item.get('triangles') or 0) for item in interfaces)
    return (count_text(len(interfaces), 'conformal interface')
            + f' carrying {count_text(faces, "face")}, cut away from '
            + count_text(len(record.get('externals') or ()), 'outer wall')
            + f': {named}')
