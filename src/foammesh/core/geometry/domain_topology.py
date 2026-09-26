#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""What the imported surfaces are: domains, bodies, voids -- or nothing at all.

Plan 31 CP-04 (closure C31-06). The shell classifier that Plan 30 WP-06a wrote
lives beside the Gmsh runner and only ever ran *inside* a Gmsh job, in WSL,
after the user had pressed Mesh. Three consequences were measured on this
worktree before this module existed:

* a preparation built from an open surface -- one that cannot bound a volume
  at all -- succeeded silently. ``_ensure_prepared_geometry`` materialises a
  revision with ``{'decision': 'as_is'}`` and never reads a readiness report,
  so the acknowledgement that ``geometry.preparation.decide`` demands for
  risky geometry was never asked for on the automatic route both engines take;
* the only thing the user was told about a multi-shell import was the
  ``small_fragments`` line, "N disconnected shell(s)", which does not say
  whether those shells become N domains, one domain with N-1 voids, or a
  refusal;
* the two engines then disagreed about it. Gmsh refuses an open shell by name
  in the runner; snappyHexMesh writes its dictionaries and meshes whatever the
  seed happens to reach.

This module runs the *same* classifier on the host, before either engine is
launched, so both get one answer and the user is given it in words. It is
deliberately a thin adapter: the geometry is decided in
``src/resources/gmsh/shell_topology.py`` and nothing is reimplemented here.
"""
from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

from foammesh.core.quantities import agreeing, count_text

#: Version of the report this module writes, independent of the classifier's
#: own ``calculation_version``.
SCHEMA_VERSION = 1

#: Suffixes this module can classify. CAD is not among them on purpose: a CAD
#: solid states its own closed shells and Gmsh imports it natively, so there
#: is nothing to infer from a triangulation.
TESSELLATED_SUFFIXES = ('.stl', '.obj')


class DomainTopologyError(ValueError):
    """The geometry cannot yield a domain, and the message names the shell."""


def _classifier():
    """The runner's shell classifier, loaded from the resource tree.

    One implementation for both engines: importing it rather than restating
    it is the whole point, since a second copy is how the host and the runner
    would come to disagree about the same file.
    """
    module = sys.modules.get('foammesh._shell_topology')
    if module is not None:
        return module
    from resources import resource

    path = Path(resource.file('gmsh/shell_topology.py')).resolve()
    spec = importlib.util.spec_from_file_location(
        'foammesh._shell_topology', path)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a dataclass resolves its own annotations
    # through ``sys.modules`` and fails on a module that is not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Reading triangles the way ``gmsh.merge`` + ``removeDuplicateNodes`` do
# --------------------------------------------------------------------------- #

class _Welder:
    """Vertex welding on rounded coordinates, as the runner's Gmsh does."""

    def __init__(self):
        self.nodes: dict = {}
        self.coordinates: dict = {}

    def __call__(self, point) -> int:
        key = tuple(round(float(value), 9) + 0.0 for value in point)
        if key not in self.nodes:
            self.nodes[key] = len(self.nodes) + 1
            self.coordinates[self.nodes[key]] = key
        return self.nodes[key]


def _read_ascii_stl(text, welder, surfaces, triangles, sources, path):
    tag = None
    corners: list = []
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0] == 'solid':
            tag = len(surfaces) + 1
            surfaces.append(tag)
            triangles[tag] = []
            sources[tag] = str(path)
        elif words[0] == 'vertex' and tag is not None:
            corners.append(welder(words[1:4]))
        elif words[0] == 'endfacet' and tag is not None:
            if len(corners) == 3:
                triangles[tag].append(tuple(corners))
            corners = []


def _read_binary_stl(data, welder, surfaces, triangles, sources, path):
    """A binary STL is one unnamed solid: its shells come from connectivity.

    CP-04 step 6 asks for binary STL to be verified rather than assumed. The
    format carries no solid names at all, so every triangle lands on one
    surface and ``group_by_connectivity`` does the splitting -- which is the
    same answer an ASCII file with one ``solid`` block gets.

    DP-421. An empty ASCII STL reaches here: a ``solid``/``endsolid`` pair
    with no facets between them does not put the word ``facet`` in the first
    2048 bytes, so the sniff above sends it down the binary branch, where it
    is too short to hold the 80-byte header and the facet count and used to
    raise ``struct.error`` out of the reader. An import holding no triangles
    is a measurement the callers already state in words, so it returns empty.

    Only for a file that says ``solid``, though. A short file that says
    nothing of the kind is junk, and a reader that swallowed it would report
    an empty tessellation where the callers need to hear `unreadable` -- the
    difference between "this model has no faces" and "this is not a model".
    """
    if len(data) < 84:
        if data[:5].lower() == b'solid':
            return
        raise ValueError(
            f'{path} is {len(data)} bytes, too short to be a binary STL and '
            f'not an ASCII one either')
    count = struct.unpack('<I', data[80:84])[0]
    tag = len(surfaces) + 1
    surfaces.append(tag)
    triangles[tag] = []
    sources[tag] = str(path)
    offset = 84
    for _index in range(count):
        if offset + 50 > len(data):
            break
        values = struct.unpack('<12fH', data[offset:offset + 50])
        corners = [welder(values[3:6]), welder(values[6:9]),
                   welder(values[9:12])]
        triangles[tag].append(tuple(corners))
        offset += 50


def _read_obj(text, welder, surfaces, triangles, sources, path):
    """``g``/``o`` groups are surfaces; a file with none is one surface."""
    local: list = []
    tag = None
    for line in text.splitlines():
        words = line.split()
        if not words:
            continue
        keyword = words[0]
        if keyword == 'v':
            local.append(welder(words[1:4]))
        elif keyword in ('g', 'o'):
            tag = len(surfaces) + 1
            surfaces.append(tag)
            triangles[tag] = []
            sources[tag] = str(path)
        elif keyword == 'f':
            if tag is None:
                tag = len(surfaces) + 1
                surfaces.append(tag)
                triangles[tag] = []
                sources[tag] = str(path)
            indices = []
            for token in words[1:]:
                first = token.split('/')[0]
                value = int(first)
                indices.append(local[value - 1] if value > 0
                               else local[len(local) + value])
            for corner in range(1, len(indices) - 1):
                triangles[tag].append(
                    (indices[0], indices[corner], indices[corner + 1]))
    for tag in list(triangles):
        if not triangles[tag]:
            surfaces.remove(tag)
            del triangles[tag]
            sources.pop(tag, None)


def _split_into_components(surfaces, triangles, sources):
    """One surface per connected patch of triangles.

    MEASURED, and the reason this exists: the classifier groups *surfaces*
    into shells and never splits one, because inside a Gmsh job
    ``classifySurfaces`` has already cut the merged triangulation up before it
    is asked. A file format that declares no surfaces at all -- a binary STL,
    which has nowhere to put a name -- therefore arrives as a single surface,
    and two disconnected cubes in one binary STL were classified as 1 shell /
    1 domain against the 2 shells / 2 domains the identical ASCII file gives.
    Splitting on shared edges here is what ``classifySurfaces`` plus the
    classifier's own regrouping amount to, so both readings agree.
    """
    out_surfaces: list = []
    out_triangles: dict = {}
    out_sources: dict = {}
    for tag in surfaces:
        owned = triangles.get(tag, ())
        if not owned:
            continue
        parent = {}

        def find(node):
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for triangle in owned:
            for node in triangle:
                parent.setdefault(node, node)
            first = find(triangle[0])
            for node in triangle[1:]:
                parent[find(node)] = first
        buckets: dict = {}
        for triangle in owned:
            buckets.setdefault(find(triangle[0]), []).append(triangle)
        # Sorted so the numbering does not depend on dictionary order.
        for _key, group in sorted(buckets.items()):
            fresh = len(out_surfaces) + 1
            out_surfaces.append(fresh)
            out_triangles[fresh] = group
            out_sources[fresh] = sources.get(tag, '')
    return out_surfaces, out_triangles, out_sources


def read_tessellation(paths, *, labels=None):
    """``(surfaces, triangles, coordinates, sources)`` for the classifier.

    ASCII and binary STL and OBJ, welded exactly as the runner welds them, so
    a one-file and a two-file form of the same geometry are asked the same
    question on the host as they are in the job.

    *labels* renames a path for the purpose of naming shells. Shell names are
    taken from the source file's stem, and the file the host classifies is the
    stored artifact -- ``...\\geometry\\<uuid>\\rev1.stl``. MEASURED without
    this: importing ``open_box.stl`` and asking why it will not mesh answered
    "shell 'rev1' ... is not closed", naming an internal revision file the
    user has never seen instead of the geometry they picked.
    """
    labels = {str(key): str(value) for key, value in (labels or {}).items()}
    welder = _Welder()
    surfaces: list = []
    triangles: dict = {}
    sources: dict = {}
    for item in paths:
        path = Path(item)
        data = path.read_bytes()
        suffix = path.suffix.lower()
        named = labels.get(str(path), str(path))
        if suffix == '.obj':
            _read_obj(data.decode('utf-8', 'replace'), welder, surfaces,
                      triangles, sources, named)
        elif data[:5].lower() == b'solid' and b'facet' in data[:2048].lower():
            _read_ascii_stl(data.decode('ascii', 'replace'), welder, surfaces,
                            triangles, sources, named)
        else:
            _read_binary_stl(data, welder, surfaces, triangles, sources, named)
    surfaces, triangles, sources = _split_into_components(
        surfaces, triangles, sources)
    return surfaces, triangles, welder.coordinates, sources


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #

def _role_label(role: str) -> str:
    return {'fluid': 'domain', 'solid': 'body', 'void': 'void'}.get(role, role)


def classify_files(paths, *, roles=None, seed=None, labels=None) -> dict:
    """What these tessellated files are, said in one record.

    Never raises for a geometry it can read: a refusal is data here
    (``bounds_domain`` false plus a ``refusal`` naming the shell), because the
    caller decides whether that stops a run or merely warns.
    """
    paths = [str(item) for item in paths]
    topology = _classifier()
    surfaces, triangles, coordinates, sources = read_tessellation(
        paths, labels=labels)
    report = {
        'schema_version': SCHEMA_VERSION,
        'sources': paths,
        'representation': 'tessellation',
        'surfaces': len(surfaces),
        'triangles': sum(len(item) for item in triangles.values()),
        'shells': [],
        'domains': [],
        'bodies': [],
        'voids': [],
        'volumes': 0,
        # DP-53. Where the count came from, so a reader can tell "none"
        # from "not measured". The seed is `unknown` because a report that
        # returns early -- no triangles, or a shell topology that refuses --
        # never counted anything.
        'volumes_source': 'unknown',
        'void_count': 0,
        'bounds_domain': False,
        'refusal': None,
        'warnings': [],
        'calculation_version': topology.CALCULATION_VERSION,
    }
    if not surfaces:
        report['refusal'] = (
            'the import produced no triangles, so there is no surface to '
            'bound a domain')
        return report
    try:
        resolved = topology.resolve_topology(
            surfaces, triangles, coordinates, sources=sources, roles=roles,
            seed=seed)
    except topology.ShellTopologyError as error:
        # The classifier refuses before it can name every shell, so measure
        # what can still be said: which shells are closed and which are not.
        shells, _owned = topology.build_shells(
            surfaces, triangles, coordinates, sources)
        report['shells'] = [
            {'name': shell.name, 'source': shell.source,
             'closed': shell.closed, 'freeEdges': shell.free_edge_count,
             'role': None, 'meaning': 'unresolved',
             'triangles': shell.triangle_count,
             'enclosedVolume': shell.volume,
             'orientation': shell.orientation,
             'boundingBox': list(shell.box)}
            for shell in shells]
        report['refusal'] = str(error)
        return report

    for shell in resolved.shells:
        record = shell.to_dict()
        record['meaning'] = _role_label(shell.role)
        record['freeEdges'] = shell.free_edge_count
        report['shells'].append(record)
        bucket = {'fluid': 'domains', 'solid': 'bodies',
                  'void': 'voids'}[shell.role]
        report[bucket].append(shell.name)
    report['volumes'] = len(resolved.volumes)
    report['volumes_source'] = 'shell_topology'
    report['void_count'] = sum(len(plan.voids) for plan in resolved.volumes)
    report['domain'] = resolved.domain or None
    report['warnings'] = list(resolved.warnings)
    report['bounds_domain'] = bool(resolved.volumes)
    return report


def summary_sentence(report: dict) -> str:
    """One line the user can read, whichever of the three cases they have."""
    if report.get('refusal'):
        return report['refusal']
    if report.get('representation') == 'cad':
        # DP-52. A CAD import has no surfaces and no shell roles, so the
        # tessellated sentence rendered it as "0 surface(s) resolve to 0
        # domain(s)" -- which reads as a measurement and is not one. What
        # OCCT did find is the solids, so that is what gets said.
        solids = [str(name) for name in (report.get('solids') or ())]
        if report.get('volumes_source') != 'cad_regions':
            return ('the CAD import has not been read for solids yet, so the '
                    'number of volumes in it is not known')
        named = ', '.join(solids[:4])
        tail = f': {named}' if named else ''
        return (count_text(report.get('volumes', 0), 'solid')
                + ' imported from CAD' + tail)
    domains = report.get('domains') or []
    bodies = report.get('bodies') or []
    voids = report.get('voids') or []
    parts = [count_text(len(domains), 'domain')]
    if bodies:
        parts.append(count_text(len(bodies), 'declared body', 'declared bodies'))
    if voids:
        parts.append(count_text(len(voids), 'void'))
    named = ', '.join(str(item) for item in domains[:4])
    tail = f': {named}' if named else ''
    surfaces = report.get('surfaces', 0)
    return (count_text(surfaces, 'surface')
            + agreeing(surfaces, ' resolves to ', ' resolve to ')
            + ', '.join(parts) + tail)


def entry_path(entry) -> str:
    """The file a geometry entry actually stands for.

    ``artifact`` is the stored copy the engines are handed -- the same file
    the runner merges -- so it, and not the path the user originally picked,
    is what gets classified and what a cache of that classification is keyed
    on. Reading a different key in the two places is how every case in one
    run came to share a single cached answer.
    """
    return str((entry.get('artifact') or entry.get('path')
                or entry.get('source_file') or '') if entry else '')


def _tessellated(paths) -> list:
    return [item for item in paths
            if Path(item).suffix.lower() in TESSELLATED_SUFFIXES]


def classify_entries(entries) -> dict:
    """Classify the tessellated artifacts of ``store.source.entries()``.

    A CAD entry contributes ``representation: cad`` and is *not* classified
    here: OCCT already knows how many closed solids the file holds, and a
    tessellation of it would answer a question about the tessellation.
    """
    paths, cad, labels, solids = [], [], {}, []
    counted = False
    for entry in entries or ():
        path = entry_path(entry)
        if not path:
            continue
        if Path(path).suffix.lower() not in TESSELLATED_SUFFIXES:
            # DP-52. The CAD import already explored the solids and wrote one
            # region per solid onto the entry; that is the answer, and reading
            # it costs nothing. An entry with no `regions` key is an import
            # that never recorded them, which is different from a file with
            # no solids -- so it is not counted at all rather than counted
            # as zero.
            regions = entry.get('regions')
            if isinstance(regions, list):
                counted = True
                for index, region in enumerate(regions):
                    name = ''
                    if isinstance(region, dict):
                        name = str(region.get('name') or '').strip()
                    solids.append(name or f'solid {index + 1}')
        if Path(path).suffix.lower() in TESSELLATED_SUFFIXES:
            paths.append(str(path))
            # Name the shell after the geometry the user imported, not after
            # the revision file the artifact store wrote it to.
            display = str((entry.get('name') or '')).strip()
            if display:
                labels[str(path)] = f'{display}{Path(path).suffix.lower()}'
        else:
            cad.append(str(path))
    if not paths:
        return {
            'schema_version': SCHEMA_VERSION,
            'sources': cad,
            'representation': 'cad' if cad else 'none',
            'shells': [], 'domains': [], 'bodies': [], 'voids': [],
            'volumes': len(solids), 'void_count': 0,
            'volumes_source': 'cad_regions' if counted else 'unknown',
            'solids': list(solids),
            # DP-73. A CAD import states its own solids, and this is that
            # statement read back rather than the fact of the import. The
            # line here was `bool(cad)` -- true whenever a CAD entry existed
            # at all -- so a STEP whose import walked the file and recorded
            # no solid passed the gate written to catch exactly that, and was
            # refused later by the mesher in the mesher's words.
            #
            # An entry with no `regions` key at all is a different fact: that
            # import never recorded them, which is not the same as a file
            # with no solids, and refusing there would refuse on a question
            # nobody asked. `counted` is what separates the two.
            'bounds_domain': bool(solids) if counted else bool(cad),
            'refusal': (None if solids or not counted else
                        'the CAD import resolved no solid, so the file holds '
                        'no volume to mesh'),
            'warnings': [],
        }
    report = classify_files(paths, labels=labels)
    report['sources'] = list(paths)
    if cad:
        report['representation'] = 'mixed'
        # A mixed case is counted by both routes, so neither number is the
        # whole answer; the solids are carried alongside rather than added.
        report['solids'] = list(solids)
        report['warnings'] = list(report['warnings']) + [
            'the case mixes CAD and tessellated sources; only the '
            'tessellated sources are classified here']
        report['sources'] = list(report['sources']) + cad
    return report


def require_domain(report: dict) -> None:
    """Raise unless these surfaces can bound a domain."""
    if report.get('bounds_domain'):
        return
    reason = report.get('refusal') or (
        'the imported surfaces cannot bound a volume, so there is no domain '
        'to mesh')
    raise DomainTopologyError(reason)
