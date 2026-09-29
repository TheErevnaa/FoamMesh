"""Persistent, headless geometry artifacts and diagnostics."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4

from foammesh.core.format_registry import geometry_suffix_formats

from .cad.formats import detect_format
from .diagnostics import assess
from .diagnostics import is_watertight
from .diagnostics.readiness import RULES_VERSION, explain_conjugate
from .diagnostics.report import score_for
from .importers import SUPPORTED_SUFFIXES, import_surface
from .transform import TransformOp, TransformStack

#: What a solid may be called inside a snappy `regions` dictionary or a
#: Gmsh physical group: a plain word. Anything else is rewritten.
_SOLID_WORD = re.compile(r'[^A-Za-z0-9_]+')


CAD_SUFFIXES = frozenset(geometry_suffix_formats())

#: The finest grid the wrap's acceptance preview is meshed on (DP-640).
PREVIEW_RESOLUTION = 128


def ordered_entries(entries) -> list[dict]:
    """The geometry entries in a stable order, for every consumer of the set.

    DP-379. This used to be ``sorted(entries, key=... ['geometry_id'])``, and
    `geometry_id` is a fresh ``uuid4().hex`` minted by the store on every
    import. So the same two files, imported the same way twice, were handed to
    the mesher in whichever order the coin came down: the prepared `sources`
    list, the `index` each source is addressed by, and therefore the order the
    runner calls ``gmsh.merge`` in, all followed a random number.

    That was not cosmetic. Growing a boundary layer on `cube_a` of
    `two_cubes_two_files` refused with a node collision in every one of the
    nine runs where `cube_a` was merged first, and meshed in every one of the
    three where `cube_b` was, at one commit, one settings digest and one
    resolved layer patch. Which is to say the product answered the same
    question two different ways depending on a uuid.

    It is public, and it lives here beside ``uuid4()``, because two
    readers need the same answer: the prepared set the mesher is built
    from, and the boundary list the user reads. If they use different
    keys the panel says one thing and the mesher does another -- which
    is what DP-380 was, once DP-379 re-keyed only the first of them.

    The key is what a reader would name the sources by -- the display name --
    with the content fingerprint and then the id behind it, so two sources
    sharing a name still order stably and identically on every run. Import
    order does not enter it: importing the same pair the other way round now
    prepares the same set.
    """
    def key(entry):
        return (str(entry.get('name') or ''),
                str(entry.get('fingerprint') or ''),
                str(entry.get('geometry_id') or ''))

    return sorted(entries or (), key=key)


def is_cad_entry(entry) -> bool:
    """True while the solid model is still the geometry this entry means.

    An entry keeps its ``cad_artifact`` for provenance after a wrap or a
    feature-angle split, but from then on the surface beside it is what gets
    meshed, so the question 'is this CAD?' and the question 'was this CAD?'
    stopped having the same answer.
    """
    return bool(entry.get('cad_artifact')) and not entry.get('cad_superseded_by')


def gmsh_mixed_sources_refusal(entries=(), new_sources=()) -> str | None:
    """Why Gmsh cannot mesh these sources together, or ``None``.

    DP-638 (field audit 0924 D-SH-04). The Gmsh job imports either solid
    models or triangulated surfaces, never both (``runner_v1.py``: "CAD and
    tessellated geometry cannot be mixed in one job"), and that was the first
    place a STEP beside an STL was refused -- after the whole case had been
    authored. ``entries`` are the store's records (a split or wrapped CAD
    entry counts as the surface it is meshed as, see :func:`is_cad_entry`);
    ``new_sources`` are files about to be imported.
    """
    cad, surfaces = [], []
    for entry in entries or ():
        name = str(entry.get('name') or entry.get('geometry_id') or '?')
        (cad if is_cad_entry(entry) else surfaces).append(name)
    for source in new_sources or ():
        path = Path(source)
        (cad if path.suffix.lower() in CAD_SUFFIXES else surfaces).append(
            path.name)
    if not cad or not surfaces:
        return None
    return ('Gmsh cannot mesh solid models (STEP/IGES/BREP) and surface '
            'meshes (STL/OBJ) in one case: {} {} CAD and {} {}. '
            'Import one kind only, split or wrap the CAD so it is meshed as '
            'a surface, or use snappyHexMesh, which meshes both.').format(
                ', '.join(cad), 'is' if len(cad) == 1 else 'are',
                ', '.join(surfaces),
                'is a surface' if len(surfaces) == 1 else 'are surfaces')


def read_back_unit(entry) -> str:
    """The unit a re-read of this entry's stored CAD artifact comes back in.

    DP-08. This used to be the whole of ``entry['unit']``, whose comment
    claimed it was what the artifact is written in -- two different facts
    under one name, and the wrong one for a metre-declaring STEP whose
    artifact is a byte-for-byte copy of the metre-declaring file. They are
    recorded apart now: ``unit`` is the artifact's own unit and
    ``reader_unit`` is this one. Entries written before the split carry only
    ``unit``, and it held *this* value, so that is what they fall back to.
    """
    return str(entry.get('reader_unit') or entry.get('unit') or '')


def tessellation_params(values=None):
    """The deflection a CAD source is faceted at, from a mapping or params.

    F-10. One place turns what the user set into
    :class:`~foammesh.core.geometry.cad.TessellationParams`, so the panel,
    the facade operation and the store cannot disagree about what a missing
    field means: the dataclass default, which is what the fixed constant
    used to be.
    """
    from .cad import TessellationParams

    if values is None:
        return TessellationParams()
    if isinstance(values, TessellationParams):
        params = values
    else:
        fields = {field.name for field in dataclasses.fields(TessellationParams)}
        unknown = set(values) - fields
        if unknown:
            raise ValueError(
                f'unknown tessellation setting: {", ".join(sorted(unknown))}')
        params = TessellationParams(**{key: values[key] for key in values})
    params.validate()
    return params


def stored_tessellation(entry) -> dict:
    """The deflection *entry* was faceted at, with the linear one in metres.

    DP-520. ``TessellationParams.linear_deflection`` is in metres now. An
    entry written before that carries no ``tessellation_unit``, and its
    number was applied raw to the shape the reader handed back -- which for
    a STEP or IGES is millimetres -- so it is converted from
    :func:`read_back_unit` here, once, for every reader of the record.
    """
    values = dict((entry or {}).get('tessellation') or {})
    if (not values or (entry or {}).get('tessellation_unit') == 'm'
            or values.get('relative') or 'linear_deflection' not in values):
        return values
    factor = GeometryArtifactStore._unit_factor(read_back_unit(entry))
    values['linear_deflection'] = float(values['linear_deflection']) * factor
    return values


def _scaled_shape(shape, factor: float):
    """*shape* scaled about the origin by *factor*. Requires OCCT."""
    from OCC.Core.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCC.Core.gp import gp_Trsf

    transform = gp_Trsf()
    transform.SetScaleFactor(float(factor))
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()




def _surface_changed(before, after) -> bool:
    """Whether two surfaces differ in points or triangles (DP-487)."""
    import numpy as np
    from vtkmodules.util.numpy_support import vtk_to_numpy

    if (before.GetNumberOfPoints() != after.GetNumberOfPoints() or
            before.GetNumberOfCells() != after.GetNumberOfCells()):
        return True
    if before.GetNumberOfPoints():
        if not np.array_equal(vtk_to_numpy(before.GetPoints().GetData()),
                              vtk_to_numpy(after.GetPoints().GetData())):
            return True
    return not np.array_equal(
        vtk_to_numpy(before.GetPolys().GetConnectivityArray()),
        vtk_to_numpy(after.GetPolys().GetConnectivityArray()))


def _triangle_normal(a, b, c) -> tuple[float, float, float]:
    """Unit normal of a triangle, or a zero normal for a degenerate one."""
    ab = tuple(b[index] - a[index] for index in range(3))
    ac = tuple(c[index] - a[index] for index in range(3))
    normal = (ab[1] * ac[2] - ab[2] * ac[1],
              ab[2] * ac[0] - ab[0] * ac[2],
              ab[0] * ac[1] - ab[1] * ac[0])
    length = math.sqrt(sum(value * value for value in normal))
    if length == 0.0:
        return (0.0, 0.0, 0.0)
    return tuple(value / length for value in normal)


def _split_patch_names(regions, region_solids: dict, base: str) -> list:
    """Names for the pieces a feature-angle split produced.

    A source solid that yielded exactly one region keeps its own name; one
    that yielded several numbers them `<solid>_1`, `<solid>_2`; a region with
    no source solid falls back to `<base>_<n>`, which is what every split
    produced before R108.
    """
    proposed = [region_solids.get(int(region.face_id)) for region in regions]
    seen = {}
    for name in proposed:
        if name:
            seen[name] = seen.get(name, 0) + 1
    used = {}
    out = []
    for position, name in enumerate(proposed):
        if not name:
            out.append(f'{base}_{position + 1}')
        elif seen[name] == 1:
            out.append(name)
        else:
            used[name] = used.get(name, 0) + 1
            out.append(f'{name}_{used[name]}')
    return out


#: A solid block the artifact writer named after a CAD face. The number is
#: the ``cadFaceId`` its triangles carry (DP-396).
_FACE_BLOCK = re.compile(r'face(\d+)$')


def region_cell_ids(entry: dict, polydata) -> dict:
    """Region name -> the ids of the surface cells that bound it.

    DP-396. The store already knows a CAD assembly's structure -- one region
    record per solid, each naming the patches that bound it, each patch naming
    the ``face<N>`` solid block its triangles were written as -- and the
    surface carries the matching ``cadFaceId`` on every cell. Following that
    chain is all it takes to ask each region separately whether it closes,
    which is the question a mesher is actually asking.

    Cell *ids into the parent surface* are what is returned, not extracted
    sub-surfaces: the triangles of one region have to keep the merged
    surface's point numbering or the seams between its own faces read as
    holes.

    An empty dict when anything in the chain does not line up -- fewer than
    two regions, a patch with no solid name, a name no block has, a region
    with no cells, no ``cadFaceId`` at all. Nothing downstream may soften a
    verdict on a mapping that was guessed, so a mapping that cannot be made
    is reported as no mapping rather than as a partial one.
    """
    from collections import defaultdict

    from .cad.surface_split import FACE_ID_ARRAY

    regions = list(entry.get('regions') or ())
    if len(regions) < 2:
        return {}
    array = polydata.GetCellData().GetArray(FACE_ID_ARRAY)
    if array is None:
        return {}

    solids_of_patch: dict[str, list[str]] = {}
    for patch in entry.get('patches') or ():
        uuid = patch.get('patch_uuid')
        if not uuid:
            continue
        refs = patch.get('source_refs') or (
            [patch['source_ref']] if patch.get('source_ref') else [])
        names = [str(ref.get('original_name') or ref.get('xde_name') or '')
                 for ref in refs]
        if not names or not all(names):
            return {}
        solids_of_patch[str(uuid)] = names

    names_in_file = sorted(
        {name for names in solids_of_patch.values() for name in names})
    numbered = [_FACE_BLOCK.match(name) for name in names_in_file]
    if not names_in_file or not all(numbered):
        # The blocks were not written as ``face<N>``, so nothing ties a patch
        # to a ``cadFaceId``; their order in the file would only be a guess.
        return {}
    id_of_solid = {name: int(match.group(1))
                   for name, match in zip(names_in_file, numbered)}

    cells_of_face: dict[int, list[int]] = defaultdict(list)
    for cell_id in range(polydata.GetNumberOfCells()):
        cells_of_face[int(array.GetTuple1(cell_id))].append(cell_id)

    out: dict[str, list[int]] = {}
    for region in regions:
        cells: list[int] = []
        for uuid in region.get('boundary_patch_uuids') or ():
            for solid in solids_of_patch.get(str(uuid), ()):
                face_id = id_of_solid.get(solid)
                if face_id is None or face_id not in cells_of_face:
                    return {}
                cells.extend(cells_of_face[face_id])
        if not cells:
            return {}
        out[str(region.get('name') or region.get('region_uuid'))] = cells
    return out


def _encloses(outer, inner) -> bool:
    """True when box ``outer`` strictly contains box ``inner`` (xmin,xmax,...)."""
    try:
        return all(float(outer[2 * i]) < float(inner[2 * i])
                   and float(inner[2 * i + 1]) < float(outer[2 * i + 1])
                   for i in range(3))
    except (TypeError, ValueError, IndexError):
        return False


def _excuse_enclosed_seeds(reports, entries, carried, engine) -> list[dict]:
    """DP-660. A closed body inside another closed geometry needs no seed.

    The fluid of an external case lies between the body and the farfield
    around it; the body's own inside is solid and is never meshed, so
    finding no probe point inside thin fins is not a fault of the geometry.
    The body is excused only while an enclosing *closed* geometry exists: a
    body standing alone is its own domain and is still asked.
    """
    from .diagnostics.checks import Finding, Severity
    from .diagnostics.readiness import classify

    known = {item['geometry_id']: item for item in list(carried) + list(reports)}
    closed = {}
    for entry in entries:
        seen = known.get(entry['geometry_id']) or entry
        diagnostics = seen.get('diagnostics') or {}
        if diagnostics.get('watertight') and entry.get('bbox'):
            closed[entry['geometry_id']] = entry
    result = []
    for report in reports:
        diagnostics = report.get('diagnostics') or {}
        findings = diagnostics.get('findings') or []
        seed = next((item for item in findings
                     if item.get('kind') == 'fluid_seed'
                     and item.get('severity') == Severity.ERROR.value
                     and item.get('count')), None)
        box = report.get('bbox')
        outer = next((entry for key, entry in closed.items()
                      if key != report['geometry_id'] and box
                      and _encloses(entry['bbox'], box)), None)
        if seed is None or outer is None:
            result.append(report)
            continue
        name = outer.get('name') or Path(str(outer.get('artifact', ''))).stem
        excused = dict(seed, count=0, severity=Severity.INFO.value,
                       evaluated=False,
                       message=('Interior seed not needed: this body sits '
                                f'inside {name}, so the fluid is around it, '
                                'not in it.'),
                       details=dict(seed.get('details') or {},
                                    enclosed_by=outer['geometry_id']))
        graded = [excused if item is seed else item for item in findings]
        objects = [Finding(
            item['kind'], int(item.get('count') or 0),
            Severity(item['severity']), item.get('message', ''),
            tuple(tuple(point) for point in item.get('locations') or ()),
            item.get('characteristic_size'), item.get('characteristic_unit'),
            tuple(item.get('repairable_by') or ()),
            item.get('engine_impact'), item.get('evaluated', True),
            item.get('details')) for item in graded]
        readiness = classify(objects, cell_count=int(report.get('cells') or 1),
                             engine=engine)
        result.append({**report, 'diagnostics': {
            **diagnostics, 'findings': graded,
            'readiness': readiness.to_dict(),
            'score': score_for(objects,
                               watertight=bool(diagnostics.get('watertight')))}})
    return result


class GeometryArtifactStore:
    def __init__(self, case_path: str | Path):
        self.case_path = Path(case_path)
        self.root = self.case_path / 'foammesh' / 'geometry'
        self.manifest_path = self.root / 'manifest.json'
        self.readiness_path = self.root / 'readiness.json'

    def entries(self) -> list[dict]:
        if not self.manifest_path.is_file():
            return []
        document = json.loads(self.manifest_path.read_text(encoding='utf-8'))
        return list(document.get('geometries', ()))

    def garbage_collect_revisions(self, keep_recent: int = 3) -> dict:
        """After an explicit case save, retain r1, current, and newest revisions.

        Manifest references are removed before files are unlinked.  Only files
        below this store's geometry root are eligible, so legacy/external paths
        can never be deleted by revision retention.
        """
        keep_recent = int(keep_recent)
        if keep_recent < 1:
            raise ValueError('revision retention count must be at least 1')
        entries = self.entries()
        removed_records, candidate_paths = [], set()
        retained_paths = set()
        for entry in entries:
            revisions = list(entry.get('revisions', self._legacy_revision(entry)))
            newest = sorted(revisions, key=lambda item: int(item['revision']), reverse=True)
            keep_ids = {1, int(entry.get('revision', 1))}
            keep_ids.update(int(item['revision']) for item in newest[:keep_recent])
            kept, removed = [], []
            for item in revisions:
                target = kept if int(item['revision']) in keep_ids else removed
                target.append(item)
            entry['revisions'] = kept
            for item in kept:
                retained_paths.update(str(item.get(key)) for key in ('artifact', 'cad_artifact')
                                      if item.get(key))
            for item in removed:
                removed_records.append({'geometry_id': entry['geometry_id'],
                                        'revision': int(item['revision'])})
                candidate_paths.update(str(item.get(key)) for key in ('artifact', 'cad_artifact')
                                       if item.get(key))
        if removed_records:
            self._save(entries)
        root = self.root.resolve()
        deleted = []
        for value in sorted(candidate_paths - retained_paths):
            path = Path(value).resolve()
            if path.is_file() and path.is_relative_to(root):
                path.unlink()
                deleted.append(str(path))
        return {'keep_recent': keep_recent, 'removed': removed_records,
                'deleted_artifacts': deleted}

    def import_file(self, source: str | Path, *, budget=None,
                    unit: str | None = None, tessellation=None) -> dict:
        """Persist a geometry file and assess it.

        ``budget`` bounds the surface diagnostics and carries the
        cancellation flag, so an import that turns out to be pathological
        returns instead of wedging the caller.

        ``unit`` is the length unit the *file* is written in. STL and OBJ
        declare none, and FoamMesh works in SI metres, so importing a
        millimetre part as written puts the whole case a thousand times out of
        scale -- every refinement level and layer thickness computed from it.
        CAD declares its own and is left to it.

        ``tessellation`` is the deflection a CAD source is faceted at, as a
        mapping of :class:`TessellationParams` fields. Ignored by STL and
        OBJ, which arrive already faceted.
        """
        source = Path(source).resolve()
        suffix = source.suffix.lower()
        if suffix not in set(SUPPORTED_SUFFIXES) | CAD_SUFFIXES:
            raise ValueError(
                f'unsupported persistent geometry format: {suffix}')
        # Checked for every format, not only the one that uses it: a misspelt
        # field would otherwise be dropped in silence, and the part would be
        # faceted at the default while the caller believed it had asked for
        # something else.
        tessellation = tessellation_params(tessellation) if tessellation else None
        if suffix in CAD_SUFFIXES:
            return self._import_cad(source, budget=budget, unit=unit,
                                    tessellation=tessellation)
        result = import_surface(source)
        if not result.surfaces or sum(item.n_cells for item in result.surfaces) <= 0:
            raise ValueError('geometry contains no readable surface cells')
        # DP-09. The format the *user handed over*, held before anything can
        # be re-read. MEASURED on `test_cases/_geometry/formats/pipe.obj`:
        # the entry said `format: 'stl'`, because an OBJ with groups is
        # rewritten as a named-solid STL and the re-read below then reported
        # the artifact's family instead of the source's. The geometry was
        # right (0.1 x 0.1 x 0.6 m); the provenance said the user had
        # imported a file they never chose.
        source_format = result.source_format
        geometry_id = uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        artifact_root = self.root / geometry_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        # A binary STL's shells and an OBJ's groups exist only in memory: the
        # format they came in cannot name them, so the artifact is written as
        # named solids or the next read loses the boundaries again.
        derived = bool(getattr(result.surfaces[0], 'derived_names', False))
        destination = artifact_root / (
            'rev1.stl' if derived else f'rev1{source.suffix.lower()}')
        scale = self._unit_factor(unit)
        # Plan 28 WP7. A file with several `solid` blocks is several
        # sub-surfaces, and their names are the only names the file offers.
        # Solver dictionaries need plain words, so a name that is not one
        # is rewritten into the artifact rather than carried as-is.
        solids = self._solid_names(result.surfaces[0])
        names = list(result.surfaces[0].solid_names)
        # DP-64. One `solid` block is one boundary, and its name is the key
        # snappy writes its `regions` entry under -- resolved by OpenFOAM
        # against the blocks in the staged triSurface, not against anything
        # FoamMesh remembers. `_solid_names` answers nothing below two solids
        # because what it answers is the per-solid *patch records*, and one
        # solid has none to make. The name is still needed, and the file
        # already carries it, so it is carried through to the conversion
        # below rather than being rebuilt from the file name.
        single = names[:1] if len(names) == 1 else []
        if scale == 1.0 and not derived and (len(names) < 2 or solids == names):
            # No conversion, so the artifact stays a byte-for-byte copy of what
            # the user handed over -- the strongest provenance there is.
            temporary = destination.with_suffix(destination.suffix + '.tmp')
            shutil.copy2(source, temporary)
            os.replace(temporary, destination)
        else:
            # The artifact is what gets meshed, so it is the thing that has to
            # be in metres. The declared unit is recorded below, so the
            # conversion is traceable rather than a silently different file.
            self._write_converted(result, destination, unit,
                                  names=dict(enumerate(solids or single)))
            # Re-read what was actually written. The bounding box, cell counts
            # and diagnostics all go into the entry, and reporting the ones
            # measured before the conversion would describe a geometry that no
            # longer exists -- a bbox in millimetres beside an artifact in
            # metres is the kind of disagreement nothing downstream can detect.
            result = import_surface(destination)
        # DP-44. Against `destination`, not the polydata alone: in the copy
        # branch above the artifact is the user's bytes and the polydata is
        # what the reader kept, and the difference between them is what the
        # mesher will be handed and nothing had ever counted.
        health = assess(result.surfaces[0].polydata, budget=budget,
                        source_file=destination)
        fingerprint = self.entry_fingerprint({'artifact': str(destination)})
        patch_uuid = str(uuid4())
        patches, regions = self._solid_records(
            result.surfaces[0], solids, source=source, name=source.stem)
        # DP-64. Off the artifact that was written, not off the file that was
        # picked. `result` is the re-read of `destination` wherever anything
        # was rewritten, so this is the file's own answer rather than a second
        # guess at it -- and a record that cannot drift from the block it
        # names is the whole point. Two or more solids keep the stem: there
        # the names live on the patch records, one each.
        written = [str(name) for name in
                   (getattr(result.surfaces[0], 'solid_names', None) or ())]
        written_name = written[0] if len(written) == 1 else source.stem
        revision_record = {
            'revision': 1, 'kind': 'imported', 'parent_revision': None,
            'artifact': str(destination), 'fingerprint': fingerprint,
            'provenance': {'operation': 'geometry.import', 'source': str(source),
                           'source_unit': unit or 'm',
                           'unit_factor': scale},
            **({'patches': patches, 'regions': regions} if patches else {}),
        }
        entry = {
            'geometry_id': geometry_id, 'name': source.stem,
            'format': source_format, 'source': str(source),
            # What was written beside it, which is not always the same thing:
            # a grouped OBJ and a binary STL are both stored as a named-solid
            # ASCII STL so the next read still knows the boundaries.
            'artifact_format': result.source_format,
            'source_unit': unit or 'm',
            'artifact': str(destination), 'cells': sum(s.n_cells for s in result.surfaces),
            'points': sum(s.n_points for s in result.surfaces),
            'bbox': list(result.bbox.to_tuple()) if result.bbox else None,
            'diagnostics': health.to_dict(), 'revision': 1, 'kind': 'imported',
            'fingerprint': fingerprint, 'revisions': [revision_record],
            'patch_uuid': patch_uuid,
            'source_ref': {'file': str(source), 'body_index': 0,
                           'face_index': None,
                           'original_name': written_name},
        }
        if patches:
            entry['patches'] = patches
            entry['regions'] = regions
        entries = self.entries()
        entries.append(entry)
        self._save(entries)
        return entry

    def _import_cad(self, source: Path, *, budget=None, unit=None,
                    tessellation=None) -> dict:
        from .cad import read_cad
        from .cad.tessellate import tessellate
        # STEP and IGES say what they are written in; the unit given only
        # reaches a BREP, which does not.
        shape, model = read_cad(source, unit)
        # F-10. The deflection is the one thing that decides whether the
        # facets the mesher sees are the part or a caricature of it, and it
        # used to be a constant nobody could reach and nothing recorded.
        params = tessellation_params(tessellation)
        # F-11. A BREP declares no unit, so the Gmsh runner's
        # `Geometry.OCCTargetUnit` -- which converts what a STEP or IGES
        # header declares -- has nothing to act on, and a millimetre BREP
        # reached the mesher a thousand times too large. Scale the shape
        # itself and the artifact is in metres like everything else, with no
        # reader left to be told.
        cad_scale = 1.0
        if detect_format(source) == 'brep':
            factor = self._unit_factor(model.unit)
            if factor != 1.0:
                shape = _scaled_shape(shape, factor)
                cad_scale = factor
        # DP-520. The deflection is in metres and the shape is in whatever the
        # reader handed back -- millimetres for STEP and IGES -- so it is
        # converted into the shape's units rather than applied as a raw
        # number. It was applied raw: the panel's `0.1 m` faceted a STEP at
        # 0.1 mm, and the same panel's Re-tessellate, which goes through the
        # repair route below on a shape already in metres, at 0.1 m.
        shape_unit = 'm' if cad_scale != 1.0 else model.unit
        polydata = tessellate(shape, params,
                              unit_factor=self._unit_factor(shape_unit))
        if polydata.GetNumberOfCells() <= 0:
            raise ValueError('CAD geometry tessellation contains no surface cells')
        # The coordinates arrive in whatever unit the reader emits, which for
        # STEP and IGES is the pinned OCCT cascade unit and not the file's
        # declaration (R193). Recording those raw numbers as metres would put
        # the project a thousand times out of scale, and Gmsh -- which pins
        # metres when it imports the same file -- would then disagree with
        # every size the rest of the application computes.
        polydata = self._to_metres(polydata, shape_unit)
        geometry_id = uuid4().hex
        artifact_root = self.root / geometry_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        cad_artifact = artifact_root / f'rev1{source.suffix.lower()}'
        if cad_scale != 1.0:
            # What is stored is the scaled solid, not the file that was
            # picked, so it is written rather than copied.
            self._write_brep(shape, cad_artifact)
        else:
            temporary_cad = cad_artifact.with_suffix(cad_artifact.suffix + '.tmp')
            shutil.copy2(source, temporary_cad)
            os.replace(temporary_cad, cad_artifact)
        artifact = artifact_root / 'rev1.stl'
        temporary_surface = artifact.with_suffix('.new.stl')
        self._write_polydata(polydata, temporary_surface)
        os.replace(temporary_surface, artifact)
        fingerprint = self._combined_fingerprint((cad_artifact, artifact))
        health = assess(polydata, budget=budget)
        solid_names = self._cad_solid_names(polydata, model)
        patches = []
        regions = []
        solid_position = 0
        # DP-92. The CAD ids are per-body and the patch uuids are what every
        # reader downstream has, so the face-level answers have to change
        # address here, once, while both are in the same hand.
        uuid_of = {face.id: face.patch_uuid
                   for body in model.bodies for face in body.faces}
        for body_index, body in enumerate(model.bodies):
            body_patch_uuids = []
            for face_index, face in enumerate(body.faces):
                body_patch_uuids.append(face.patch_uuid)
                source_ref = {
                    **face.source_ref, 'file': str(source),
                    'body_index': body_index, 'face_index': face_index}
                if solid_names is not None:
                    source_ref['original_name'] = solid_names[solid_position]
                solid_position += 1
                patches.append({
                    'patch_uuid': face.patch_uuid, 'name': face.patch,
                    'source_ref': source_ref,
                    # DP-92. Advisory: absent where OCCT was not asked, and
                    # every reader treats absence as "nobody measured".
                    **({'planar': bool(face.planar)}
                       if face.planar is not None else {}),
                    **({'area': float(face.area)}
                       if face.area is not None else {}),
                    **({'face_order': int(face.face_order)}
                       if face.face_order >= 0 else {}),
                    **({'adjacent_patch_uuids': [
                        uuid_of[other] for other in face.adjacent_ids
                        if other in uuid_of]}
                       if face.adjacent_ids else {}),
                    **({'interface_patch_uuid': uuid_of[face.interface_id]}
                       if face.interface_id in uuid_of else {}),
                })
            regions.append({
                'region_uuid': str(uuid4()),
                'name': body.name or f'Body {body_index + 1}',
                'region_type': 'fluid',
                'source_ref': {
                    'file': str(source), 'body_index': body_index,
                    'solid_index': body_index,
                },
                'boundary_patch_uuids': body_patch_uuids,
                # DP-900. Whether OCCT found a closed solid here: a STEP
                # holding one planar face still becomes a body, and counting
                # it as a volume let a 3D run through on a plate.
                **({'solid': bool(body.solid)}
                   if body.solid is not None else {}),
            })
        revision_record = {
            'revision': 1, 'kind': 'imported', 'parent_revision': None,
            'artifact': str(artifact), 'cad_artifact': str(cad_artifact),
            'fingerprint': fingerprint,
            'provenance': {'operation': 'geometry.import', 'source': str(source),
                           'format': model.source_format,
                           'tessellation': dataclasses.asdict(params),
                           'cad_unit_factor': float(cad_scale)},
        }
        entry = {
            'geometry_id': geometry_id, 'name': source.stem,
            'format': model.source_format, 'source': str(source),
            # What the file said, and what its numbers were actually in.
            # The report has had a blank 'Unit as declared' row since it was
            # written because nothing ever recorded either of them.
            'declared_unit': model.declared_unit or model.unit,
            # DP-08. What the stored CAD artifact is actually written in.
            # A STEP or IGES artifact is a byte-for-byte copy of the file the
            # user picked, so it is in whatever that file declares; a BREP
            # scaled on the way in is in metres. This field used to hold
            # `model.unit`, which for STEP and IGES is the pinned OCCT
            # cascade unit and not a property of the artifact at all.
            # MEASURED on `test_cases/gmsh/duct_step`: `declared_unit: 'm'`,
            # `unit: 'mm'`, and a `rev1.step` declaring `SI_UNIT(*,.METRE.)`
            # -- so every reader that believed the comment was out by a
            # thousand.
            'unit': 'm' if cad_scale != 1.0 else (
                model.declared_unit or model.unit),
            # DP-08. The cascade setting, under its own name: the unit a
            # *re-read* of the stored artifact comes back in. The XSTEP
            # readers convert every length into `xstep.cascade.unit`, which
            # the importer pins to MM, so a STEP or IGES artifact reads back
            # in millimetres however its header is written; a BREP carries
            # raw coordinates and so reads back in the unit it was stored in.
            # This is the number anything re-tessellating the artifact needs,
            # and it is not the same fact as `unit` above.
            'reader_unit': 'm' if cad_scale != 1.0 else model.unit,
            'cad_unit_factor': float(cad_scale),
            'tessellation': dataclasses.asdict(params),
            # DP-520. Says the deflection above is in metres. An entry
            # without it was written when the number was applied raw to the
            # reader's shape, so it is in `reader_unit` (see
            # `stored_tessellation`).
            'tessellation_unit': 'm',
            'artifact': str(artifact), 'cad_artifact': str(cad_artifact),
            'cells': int(polydata.GetNumberOfCells()),
            'points': int(polydata.GetNumberOfPoints()),
            'bbox': list(polydata.GetBounds()), 'diagnostics': health.to_dict(),
            'revision': 1, 'kind': 'imported', 'fingerprint': fingerprint,
            'revisions': [revision_record], 'patches': patches,
            'regions': regions,
        }
        entries = self.entries()
        entries.append(entry)
        self._save(entries)
        return entry

    @staticmethod
    def _cad_solid_names(polydata, model) -> list[str] | None:
        """The solid name each CAD face is written under, in face order.

        The tessellation numbers faces by one walk over the whole shape, so
        the ``cadFaceId`` a face's triangles carry -- and the ``face<id>``
        solid the artifact writer names after it -- is the face's position
        across all bodies, not its ``face_index`` within its body. Recording
        that name on the patch record is what lets the snappy ``regions``
        dictionary key the staged solid instead of the display name; for a
        CAD import the key never matched before. ``None`` when the walk
        and the model disagree about how many faces there are (a face with
        no triangulation), so nothing is claimed that cannot be checked.
        """
        from .cad.surface_split import face_id_range

        ids = face_id_range(polydata)
        if ids is None:
            return None
        lo, hi = ids
        if lo != 0 or hi - lo + 1 != int(model.n_faces):
            return None
        return [f'face{index}' for index in range(lo, hi + 1)]

    @staticmethod
    def _combined_fingerprint(paths) -> str:
        digest = hashlib.sha256()
        for path in paths:
            with Path(path).open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
            digest.update(b'\0')
        return f'sha256:{digest.hexdigest()}'

    def diagnose(self, geometry_id: str | None = None, *,
                 target_cell_size: float | None = None,
                 engine: str | None = None) -> list[dict]:
        selected = self.entries()
        if geometry_id:
            selected = [item for item in selected if item['geometry_id'] == geometry_id]
            if not selected:
                raise KeyError(geometry_id)
        # DP-409. Every one of these entries is re-imported from disk and
        # re-assessed on the caller's thread, and on the owner loop that is
        # the whole of the blocking this fault measured. The result was
        # already being cached after every call and never read back: see
        # `_persist_readiness`, which has written a fingerprinted document
        # since the beginning. So read it. The key is the artifact's own
        # content hash, so a geometry that has not changed is not diagnosed
        # twice, and a geometry that has changed is not served from a stale
        # answer.
        cached = self._cached_readiness(target_cell_size, engine)
        reports = []
        for item in selected:
            fingerprint = self.entry_fingerprint(item)
            remembered = cached.get(item['geometry_id'])
            if (remembered is not None
                    and remembered.get('fingerprint') == fingerprint
                    and isinstance(remembered.get('diagnostics'), dict)):
                reports.append({**item, 'fingerprint': fingerprint,
                                'diagnostics': remembered['diagnostics']})
                continue
            imported = import_surface(item['artifact'])
            polydata = imported.surfaces[0].polydata
            health = assess(polydata, target_cell_size=target_cell_size,
                            source_file=item['artifact'], engine=engine)
            # DP-396. A conjugate assembly's merged surface cannot close where
            # its bodies touch, so grading it as one surface refuses exactly
            # the geometry a conjugate mesh is made of. Grade the regions.
            region_cells = region_cell_ids(item, polydata)
            if region_cells:
                from .diagnostics.checks import region_shells
                from .diagnostics.readiness import classify
                health.findings.append(region_shells(polydata, region_cells))
                health.readiness = classify(
                    health.findings,
                    cell_count=int(polydata.GetNumberOfCells()), engine=engine)
            if item.get('cad_artifact'):
                try:
                    from .cad import read_cad
                    from .diagnostics.cad_checks import check_cad
                    from .diagnostics.readiness import classify
                    shape, _model = read_cad(item['cad_artifact'])
                    # DP-530. The shape comes back in `reader_unit` --
                    # millimetres for STEP and IGES -- and the census the
                    # Repair plan suggests a tolerance from is in metres.
                    health.findings.extend(check_cad(
                        shape, unit_factor=self._unit_factor(
                            read_back_unit(item))))
                    health.readiness = classify(
                        health.findings,
                        cell_count=int(polydata.GetNumberOfCells()),
                        engine=engine)
                except RuntimeError:
                    # The tessellation remains diagnosable when CAD support is
                    # absent; capability status is exposed separately.
                    pass
            # DP-434. Every finding is in hand here and not before, so this is
            # where the conjugate proof can reach the three findings it
            # accounts for. Until now it reached only the verdict: six STEP
            # models came out of this method carrying `surface_not_closed`,
            # `non_manifold_edges` and `duplicate_triangles` at severity
            # `error` with `tess.fix_nonmanifold` and `tess.dedupe` offered as
            # the cure -- repairs that would delete one copy of the shared
            # surface -- beside a readiness badge that said `ready`. The score
            # is taken once, afterwards, for the same reason.
            health.findings = explain_conjugate(health.findings)
            health.score = score_for(health.findings,
                                     watertight=health.watertight)
            reports.append({
                **item,
                'fingerprint': fingerprint,
                'diagnostics': health.to_dict(),
            })
        carried = [entry for key, entry in cached.items()
                   if key not in {item['geometry_id'] for item in selected}]
        self._persist_readiness(reports, carried, target_cell_size, engine)
        # DP-660. Graded after the cache, not stored in it: whether a body is
        # enclosed depends on the other geometries, which the per-entry
        # fingerprint cannot see.
        return _excuse_enclosed_seeds(
            reports, self.entries(), list(carried), engine)

    def _cached_readiness(self, target_cell_size: float | None,
                          engine: str | None) -> dict[str, dict]:
        """The last diagnostics written for these same inputs, by id (DP-409).

        The inputs are compared as a whole rather than ignored, because
        `assess` is given the target cell size and the engine and grades
        differently for each. A document written before this method existed
        carries no record of what it was diagnosed with, so it reads as a
        miss, which is the right answer for a document that cannot say.
        """
        try:
            document = json.loads(self.readiness_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if not isinstance(document, dict) or document.get('schema_version') != 1:
            return {}
        if document.get('diagnosed_with') != self._diagnose_inputs(
                target_cell_size, engine):
            return {}
        entries = document.get('geometries')
        if not isinstance(entries, list):
            return {}
        return {entry['geometry_id']: entry for entry in entries
                if isinstance(entry, dict) and entry.get('geometry_id')
                and entry.get('fingerprint')}

    @staticmethod
    def _diagnose_inputs(target_cell_size: float | None,
                         engine: str | None) -> dict:
        """What `diagnose` was asked, in the form the cache compares (DP-409).

        DP-530. ``cad_census_unit`` says the CAD census is in metres. A
        document written before carries a census in whatever unit the reader
        handed back -- millimetres for STEP -- and the Repair plan would
        suggest a tolerance from it, so it is not trusted.
        """
        return {
            'target_cell_size': (None if target_cell_size is None
                                 else float(target_cell_size)),
            'engine': None if engine is None else str(engine),
            'cad_census_unit': 'm',
            # DP-684. A verdict graded under older rules is graded again.
            'rules_version': RULES_VERSION,
        }

    def geometry_fingerprint(self) -> str:
        """Fingerprint the ordered active geometry set, independent of paths."""
        digest = hashlib.sha256()
        for entry in sorted(self.entries(), key=lambda item: item['geometry_id']):
            digest.update(entry['geometry_id'].encode('utf-8'))
            digest.update(b'\0')
            digest.update(self.entry_fingerprint(entry).encode('ascii'))
            digest.update(b'\0')
        return f'sha256:{digest.hexdigest()}'

    @staticmethod
    def entry_fingerprint(entry: dict) -> str:
        if entry.get('cad_artifact'):
            return GeometryArtifactStore._combined_fingerprint(
                (entry['cad_artifact'], entry['artifact']))
        digest = hashlib.sha256()
        with Path(entry['artifact']).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return f'sha256:{digest.hexdigest()}'

    def readiness_report(self, geometry_id: str | None = None, *,
                         target_cell_size: float | None = None,
                         engine: str | None = None) -> dict:
        geometries = self.diagnose(geometry_id, target_cell_size=target_cell_size,
                                   engine=engine)
        return {
            'schema_version': 1,
            'geometry_fingerprint': self.geometry_fingerprint(),
            'geometry_count': len(geometries),
            'geometries': geometries,
        }

    def _persist_readiness(self, reports: list[dict],
                          carried: list[dict] | None = None,
                          target_cell_size: float | None = None,
                          engine: str | None = None) -> None:
        """Atomically cache diagnostics; consumers detect stale data by fingerprint.

        DP-409 made this document readable as well as written, so it carries
        two more things. `diagnosed_with` says what the diagnostics were taken
        with, because the same geometry grades differently under a different
        target cell size or engine. `carried` holds the entries a single-id
        call did not look at, so diagnosing one geometry no longer erases the
        cache for every other one.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        document = {
            'schema_version': 1,
            'geometry_fingerprint': self.geometry_fingerprint(),
            'diagnosed_with': self._diagnose_inputs(target_cell_size, engine),
            'geometries': list(reports) + list(carried or []),
        }
        temporary = self.readiness_path.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(temporary, self.readiness_path)

    def classify(self, geometry_id: str | None = None) -> list[dict]:
        """Classify durable surfaces without relying on GUI-owned VTK actors."""
        selected = self._selected(geometry_id)
        from vtkmodules.vtkFiltersCore import vtkPolyDataConnectivityFilter
        classifications = []
        for item in selected:
            polydata = self._polydata(item)
            connectivity = vtkPolyDataConnectivityFilter()
            connectivity.SetInputData(polydata)
            connectivity.SetExtractionModeToAllRegions()
            connectivity.Update()
            components = int(connectivity.GetNumberOfExtractedRegions())
            closed = is_watertight(polydata)
            classifications.append({
                'geometry_id': item['geometry_id'],
                'classification': 'closed_volume' if closed else 'open_surface',
                'watertight': closed,
                'connected_components': components,
                'cells': int(polydata.GetNumberOfCells()),
                'points': int(polydata.GetNumberOfPoints()),
                'bbox': list(polydata.GetBounds()) if polydata.GetNumberOfPoints() else None,
            })
        return classifications

    def transform(self, geometry_id: str, operations: list[dict]) -> dict:
        """Bake a validated transform into a recoverable artifact revision."""
        if not isinstance(operations, list) or not operations:
            raise ValueError('at least one geometry transform operation is required')
        stack = TransformStack()
        for item in operations:
            if not isinstance(item, dict):
                raise ValueError('geometry transform operations must be objects')
            op = TransformOp.from_dict(item)
            if not all(math.isfinite(value) for value in op.values):
                raise ValueError('geometry transform values must be finite')
            if op.kind == 'scale' and any(value == 0 for value in op.values):
                raise ValueError('geometry scale components must be non-zero')
            stack.push(op)
        entries = self.entries()
        index, entry = self._entry(entries, geometry_id)
        polydata = self._polydata(entry)
        from vtkmodules.vtkCommonTransforms import vtkTransform
        from vtkmodules.vtkFiltersGeneral import vtkTransformPolyDataFilter
        transform = vtkTransform()
        transform.SetMatrix(tuple(float(value) for value in stack.matrix().ravel()))
        transform_filter = vtkTransformPolyDataFilter()
        transform_filter.SetTransform(transform)
        transform_filter.SetInputData(polydata)
        transform_filter.Update()
        output = transform_filter.GetOutput()
        updated = self._replace_artifact_revision(
            entries, index, entry, output,
            {'last_transform': stack.to_list()})
        return updated

    def split(self, geometry_id: str, *, replace_source: bool = False) -> list[dict]:
        """Split an imported artifact into connected-component artifacts."""
        entries = self.entries()
        index, entry = self._entry(entries, geometry_id)
        polydata = self._polydata(entry)
        from vtkmodules.vtkFiltersCore import vtkPolyDataConnectivityFilter
        all_regions = vtkPolyDataConnectivityFilter()
        all_regions.SetInputData(polydata)
        all_regions.SetExtractionModeToAllRegions()
        all_regions.Update()
        count = int(all_regions.GetNumberOfExtractedRegions())
        if count < 2:
            raise ValueError('geometry has fewer than two connected components')
        created = []
        for region in range(count):
            selected = vtkPolyDataConnectivityFilter()
            selected.SetInputData(polydata)
            selected.SetExtractionModeToSpecifiedRegions()
            selected.AddSpecifiedRegion(region)
            selected.Update()
            output = selected.GetOutput()
            created.append(self._new_derived_entry(
                output, name=f'{entry["name"]}_part_{region + 1}',
                suffix=Path(entry['artifact']).suffix,
                derived_from=[geometry_id], operation='split'))
        if replace_source:
            entries.pop(index)
        entries.extend(created)
        self._save(entries)
        return created

    def split_by_angle(self, geometry_id: str, *, angle_deg=None,
                       min_area_fraction: float = 0.0,
                       preview: bool = False) -> dict:
        """Cut a tessellated surface into boundaries along its feature edges.

        Plan 28 WP7. The pass is :func:`split_by_feature_angle`; this method
        is what makes its result *the geometry*: a new revision of kind
        ``split`` whose STL is one named solid per region, and a patch record
        per region so both engines receive the pieces through the seams
        they already read. ``preview`` returns the count and areas and
        writes nothing.

        F-27. A CAD entry may be cut too. Its tessellation is what gets
        cut, never the solid model, and the cut is constrained to stay
        inside one CAD face, so every piece still knows which face it came
        from -- recorded as ``cad_face_index`` on each patch and as
        ``cad_face_map`` on the entry. The cut surface then becomes the
        geometry both engines mesh (``cad_superseded_by``): a user who asks
        for boundaries along feature edges means them for the mesh, and
        leaving Gmsh to mesh the uncut solid would have honoured the request
        in one engine and ignored it in the other.
        """
        from .features.manifest import DEFAULT_FEATURE_ANGLE_DEG
        from .patches.feature_split import split_by_feature_angle

        entries = self.entries()
        index, entry = self._entry(entries, geometry_id)
        angle = DEFAULT_FEATURE_ANGLE_DEG if angle_deg is None else angle_deg
        polydata = self._polydata(entry)
        # R107/R108. A multi-solid STL already says where its boundaries are.
        # Splitting across those solids merged `wall_converging` and
        # `wall_diverging` into one 480-facet face and threw all four names
        # away; the source solids constrain the cut and supply the names.
        solids = self._source_solid_names(entry)
        groups = (self._face_id_array(polydata)
                  if len(solids) > 1 or entry.get('cad_artifact') else None)
        result = split_by_feature_angle(
            polydata, angle_deg=angle,
            min_area_fraction=min_area_fraction, groups=groups)
        summary = result.summary()
        if preview:
            return {'geometry_id': geometry_id, 'revision': int(entry['revision']),
                    'preview': True, **summary}
        if result.count < 2:
            raise ValueError(
                f'no feature edge sharper than {float(angle):g} degrees: the '
                'surface stays one boundary')
        owners = self._region_owners(result, groups)
        patches, regions = self._split_records(
            entry, result, region_solids={
                region: solids[owner] for region, owner in owners.items()
                if owner in solids})
        metadata = {
            'kind': 'split', 'patches': patches, 'regions': regions,
            'split_report': summary,
            'last_split': {'angle_deg': float(result.angle_deg),
                           'min_area_fraction': float(result.min_area_fraction),
                           'count': result.count},
        }
        if entry.get('cad_artifact'):
            for record in patches:
                face = int(record['source_ref']['face_index'])
                if face in owners:
                    record['source_ref']['cad_face_index'] = int(owners[face])
            metadata['cad_face_map'] = {str(region): int(owner)
                                        for region, owner in owners.items()}
            metadata['cad_superseded_by'] = 'geometry.patches.split_by_angle'
        return self._replace_artifact_revision(
            entries, index, entry, result.polydata, metadata)

    def split_interfaces(self, geometry_ids=None, *, preview: bool = False) -> dict:
        """Cut an assembly's shared walls away from the walls it shares with nobody.

        DP-421. OpenFOAM 13 writes a conjugate assembly as one surface per
        interface plus one for the outer skin (``multiRegion/CHT/heatedDuct``),
        and FoamMesh had no way to produce that shape: an imported body is one
        closed surface, and one closed surface can be typed one thing. This is
        the cut that makes the shape authorable.

        The cut is exact and needs no tolerance. A conformal interface is drawn
        twice, once by each body, from the *same* welded nodes -- the copies
        differ only in winding -- so a node triple carried by two shells is a
        shared face and nothing else is.

        Each body keeps a new revision holding only the faces it shares with
        nobody; each interface becomes a geometry of its own, named
        ``<a>_to_<b>``, and carries the point inside the region it encloses
        that ``mode insidePoint`` needs. A body that shares nothing is left
        untouched, which is the answer for the single-region models that are
        most of the catalogue. ``preview`` measures and writes nothing.
        """
        from .interface_split import split_assembly

        entries = self.entries()
        if geometry_ids:
            chosen = [self._entry(entries, str(item)) for item in geometry_ids]
        else:
            chosen = list(enumerate(entries))
        if len(chosen) < 2:
            raise ValueError(
                'an interface is a wall two bodies share, so the cut needs at '
                'least two geometries; this case has '
                f'{len(chosen)}')
        labels = self._interface_labels([entry for _index, entry in chosen])
        staging = None
        if not preview:
            staging = self.root / '_interface_split'
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True, exist_ok=True)
        paths = [entry['artifact'] for _index, entry in chosen]
        record = split_assembly(
            paths, labels=dict(zip(paths, labels)), directory=staging)
        by_label = {label: (index, entry)
                    for label, (index, entry) in zip(labels, chosen)}
        result = {
            'is_assembly': bool(record['is_assembly']),
            'warnings': list(record['warnings']),
            'bodies': [], 'interfaces': [],
        }
        for item in record['externals']:
            index, entry = by_label[item['name']]
            body = {'geometry_id': entry['geometry_id'], 'name': item['name'],
                    'faces': int(item['triangles']),
                    'removed': int(item['removed']),
                    'revision': int(entry['revision'])}
            if not preview and item['removed'] and item.get('path'):
                # Only a body that lost faces gets a revision. Re-writing one
                # that shares nothing would change its fingerprint, and every
                # downstream record keyed on that fingerprint, to say the same
                # thing it already said.
                written = self._replace_artifact_revision(
                    entries, index, entry,
                    self._polydata({'artifact': item['path']}),
                    {'kind': 'interface_split',
                     'solid_names': {0: item['name']},
                     'interface_split': {'removed': int(item['removed']),
                                         'kept': int(item['triangles'])}})
                body['revision'] = int(written['revision'])
            result['bodies'].append(body)
        for item in record['interfaces']:
            joined = {'name': item['name'], 'between': list(item['between']),
                      'faces': int(item['triangles']),
                      'inside_shell': item['insideShell'],
                      'inside_point': item['inside'],
                      'geometry_id': None}
            if not preview and item.get('path'):
                imported = self.import_file(item['path'])
                joined['geometry_id'] = imported['geometry_id']
                joined['artifact'] = imported['artifact']
                enclosed = by_label.get(item['insideShell'])
                joined['inside_geometry_id'] = (
                    enclosed[1]['geometry_id'] if enclosed else None)
            result['interfaces'].append(joined)
        return result

    @staticmethod
    def _interface_labels(entries: list[dict]) -> list[str]:
        """One word per geometry, distinct, for the pieces to be named after.

        The names end up in ``<a>_to_<b>``, in a solid block, in a face zone
        and in a cell zone, so they have to be words OpenFOAM can read and
        they have to tell two bodies apart. A name that is neither is
        replaced rather than refused: the cut is worth more than the label,
        and the entry keeps its own name whatever this says.
        """
        labels: list[str] = []
        for position, entry in enumerate(entries, 1):
            name = str(entry.get('name') or '').strip()
            cleaned = ''.join(
                character if character.isalnum() or character == '_' else '_'
                for character in name).strip('_')
            if not cleaned or not cleaned[0].isalpha():
                cleaned = f'body{position}'
            if cleaned in labels:
                cleaned = f'{cleaned}{position}'
            labels.append(cleaned)
        return labels

    @staticmethod
    def _source_solid_names(entry: dict) -> dict:
        """Face id -> solid name, for the solids the FILE carried.

        Only import-time records count. A record written by an earlier
        feature-angle split carries ``feature_angle_deg``; treating those as
        source solids would stop a re-split at a coarser angle from ever
        rejoining what a finer one cut.
        """
        names = {}
        for record in entry.get('patches') or ():
            ref = record.get('source_ref') or {}
            face_index = ref.get('face_index')
            if not isinstance(face_index, int) or 'feature_angle_deg' in ref:
                continue
            name = ref.get('original_name') or record.get('name')
            if name:
                names[int(face_index)] = str(name)
        return names

    @staticmethod
    def _face_id_array(polydata):
        """The per-cell ``cadFaceId`` values as numpy, or None if untagged."""
        from vtkmodules.util.numpy_support import vtk_to_numpy

        from .cad.surface_split import FACE_ID_ARRAY
        array = polydata.GetCellData().GetArray(FACE_ID_ARRAY)
        if array is None:
            return None
        return vtk_to_numpy(array).reshape(-1)

    @classmethod
    def _region_owners(cls, result, groups) -> dict:
        """New region id -> the source face id it was cut out of.

        Empty unless the cut was constrained by source faces, in which case
        every region lies wholly inside one of them. This is the face map a
        CAD split keeps, and the route by which a piece of a named STL solid
        gets its name back.
        """
        if groups is None:
            return {}
        import numpy as np

        owners = np.asarray(groups).reshape(-1)
        regions = cls._face_id_array(result.polydata)
        if regions is None or regions.size != owners.size:
            return {}
        found = {}
        for region in np.unique(regions):
            inside = np.unique(owners[regions == region])
            if inside.size == 1:
                found[int(region)] = int(inside[0])
        return found

    @classmethod
    def _region_solids(cls, result, groups, solids: dict) -> dict:
        """New region id -> the name of the source solid it came from."""
        return {region: solids[owner]
                for region, owner in cls._region_owners(result, groups).items()
                if owner in solids}

    @staticmethod
    def _split_records(entry: dict, result,
                       region_solids: dict = None) -> tuple[list[dict], list[dict]]:
        """Patch and region records for a feature-angle split.

        ``original_name`` is the solid name the artifact is written with. A
        region cut out of a named source solid keeps that solid's name (R108:
        `venturi.stl` used to come back as `venturi_1..3`, having discarded
        `inlet`, `outlet`, `wall_converging` and `wall_diverging`); a solid cut
        into several pieces numbers them, and anything unattributed keeps the
        old ``face<id>`` convention the CAD path uses.
        """
        base = str(entry.get('name') or entry['geometry_id'])
        owned = region_solids or {}
        names = _split_patch_names(result.regions, owned, base)
        patches = []
        for position, region in enumerate(result.regions):
            name = names[position]
            patches.append({
                'patch_uuid': str(uuid4()),
                'name': name,
                'source_ref': {
                    'file': entry.get('source'), 'body_index': 0,
                    'face_index': int(region.face_id),
                    'original_name': (name if owned.get(int(region.face_id))
                                      else f'face{int(region.face_id)}'),
                    'feature_angle_deg': float(result.angle_deg),
                    'area': float(region.area),
                },
            })
        regions = [{
            'region_uuid': str(uuid4()), 'name': base, 'region_type': 'fluid',
            'source_ref': {'file': entry.get('source'), 'body_index': 0,
                           'solid_index': 0},
            'boundary_patch_uuids': [item['patch_uuid'] for item in patches],
        }]
        return patches, regions

    @staticmethod
    def _solid_names(surface) -> list[str]:
        """The solid names an imported surface will be recorded under.

        Empty unless the file held two or more solids. Each name is reduced
        to a solver word and made unique, so it can key a snappy region or a
        Gmsh physical group as it is.
        """
        names = list(getattr(surface, 'solid_names', ()) or ())
        if len(names) < 2:
            return []
        out: list[str] = []
        for position, raw in enumerate(names):
            word = _SOLID_WORD.sub('_', str(raw)).strip('_') or f'solid_{position + 1}'
            if not word[0].isalpha():
                word = f'solid_{word}'
            candidate = word
            suffix = 2
            while candidate in out:
                candidate = f'{word}_{suffix}'
                suffix += 1
            out.append(candidate)
        return out

    @staticmethod
    def _solid_records(surface, solids: list[str], *, source,
                       name: str) -> tuple[list[dict], list[dict]]:
        """Patch and region records for a multi-solid tessellated import."""
        from .cad.surface_split import FACE_ID_ARRAY
        from .importers.stl_importer import face_ids_for

        polydata = surface.polydata
        if not solids or polydata.GetCellData().GetArray(FACE_ID_ARRAY) is None:
            return [], []
        ids = face_ids_for(list(surface.solid_names))
        patches = [{
            'patch_uuid': str(uuid4()), 'name': solid,
            'source_ref': {'file': str(source), 'body_index': 0,
                           'face_index': int(face_id), 'original_name': solid},
        } for solid, face_id in zip(solids, ids)]
        regions = [{
            'region_uuid': str(uuid4()), 'name': name, 'region_type': 'fluid',
            'source_ref': {'file': str(source), 'body_index': 0,
                           'solid_index': 0},
            'boundary_patch_uuids': [item['patch_uuid'] for item in patches],
        }]
        return patches, regions

    @staticmethod
    def _artifact_solid_names(entry: dict, polydata) -> dict[int, str]:
        """Face id -> solid name, for the solids the entry's artifact carries.

        DP-486. The fallback when the entry's patch records name nothing:
        the names are read off the revision being replaced, keyed the way
        the reader keyed them, so the revision written next carries the same
        ``solid`` blocks. A single-solid surface is named by the entry's
        ``source_ref`` -- the record the snappy ``regions`` key is generated
        from -- and by its file's header only when that record is silent.
        """
        artifact = Path(str(entry.get('artifact') or ''))
        if artifact.suffix.lower() != '.stl' or not artifact.is_file():
            return {}
        from .cad.surface_split import face_id_range
        from .importers.stl_importer import face_ids_for, solid_names
        names = solid_names(artifact)
        if len(names) >= 2:
            if face_id_range(polydata) is None:
                # The solids were not kept apart, and no one of their names
                # is the name of all of them.
                return {}
            return dict(zip(face_ids_for(names), names))
        recorded = (entry.get('source_ref') or {}).get('original_name')
        name = str(recorded) if recorded else (names[0] if names else None)
        return {0: name} if name else {}

    @staticmethod
    def _patch_solid_names(entry: dict) -> dict[int, str]:
        """Face id -> solid name, from the entry's patch records.

        The names the artifact's solids must carry for the snappy ``regions``
        dictionary generated from the same records to resolve. A record
        without an ``original_name`` (a CAD face) keeps the ``face<id>``
        default.
        """
        names: dict[int, str] = {}
        for record in entry.get('patches') or ():
            for member in (record.get('members') or [record]):
                ref = member.get('source_ref') or {}
                face_index = ref.get('face_index')
                original = ref.get('original_name')
                if isinstance(face_index, int) and original:
                    names.setdefault(int(face_index), str(original))
        return names

    def combine(self, geometry_ids: list[str], *, name: str | None = None,
                replace_sources: bool = False) -> dict:
        """Combine two or more durable artifacts into one cleaned surface."""
        ids = [str(item) for item in geometry_ids]
        if len(ids) < 2 or len(ids) != len(set(ids)):
            raise ValueError('combine requires at least two distinct geometry ids')
        entries = self.entries()
        selected = []
        for geometry_id in ids:
            _, entry = self._entry(entries, geometry_id)
            selected.append(entry)
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData, vtkCleanPolyData
        append = vtkAppendPolyData()
        for entry in selected:
            append.AddInputData(self._polydata(entry))
        append.Update()
        clean = vtkCleanPolyData()
        clean.SetInputData(append.GetOutput())
        clean.Update()
        suffixes = {Path(item['artifact']).suffix.lower() for item in selected}
        suffix = suffixes.pop() if len(suffixes) == 1 else '.stl'
        combined = self._new_derived_entry(
            clean.GetOutput(), name=name or 'combined_geometry', suffix=suffix,
            derived_from=ids, operation='combine')
        if replace_sources:
            entries = [item for item in entries if item['geometry_id'] not in set(ids)]
        entries.append(combined)
        self._save(entries)
        return combined

    def repair(self, geometry_id: str, operation: str, *, hole_size: float = 1e6,
               flip_normals: bool = False) -> dict:
        # Plan 26 WP7.1. Was `core/mesh/surface_repair.py`, the second of two
        # implementations of one job: three operations against this
        # catalogue's eight, dispatching to these same functions. The legacy
        # names still resolve, so this operation's public contract is
        # unchanged and the other five actions become reachable through it.
        from foammesh.core.geometry.diagnostics.repair import (
            apply_action, write_surface)
        entries = self.entries()
        index = next((i for i, item in enumerate(entries)
                      if item['geometry_id'] == geometry_id), None)
        if index is None:
            raise KeyError(geometry_id)
        entry = entries[index]
        imported = import_surface(entry['artifact'])
        result = apply_action(
            imported.surfaces[0].polydata, operation,
            hole_size=hole_size, flip_normals=flip_normals)
        artifact = Path(entry['artifact'])
        revision = self._allocate_revision(entry)
        destination = self.root / geometry_id / f'rev{revision}{artifact.suffix}'
        temporary = destination.with_suffix(f'.repair{artifact.suffix}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Plan 28 WP7. A split or multi-solid surface is written back as
        # named solids, so the patch records still describe the file. A
        # repair that discarded the ids leaves an artifact the records no
        # longer describe, and those records go rather than lie.
        from .cad.surface_split import face_id_range
        keeps_ids = face_id_range(result.polydata) is not None
        if keeps_ids and artifact.suffix.lower() == '.stl':
            self._write_polydata(result.polydata, temporary,
                                 names=self._patch_solid_names(entry))
        else:
            # DP-362. A single-solid STL carries its patch identity in the
            # one name the file already has, and `_patch_solid_names` cannot
            # supply it -- that reads face indices, and a plain STL import
            # has none. Read it off the surface that was repaired.
            solid_names = [name for name in imported.surfaces[0].solid_names
                           if name]
            write_surface(
                result.polydata, temporary,
                solid_name=(solid_names[0] if len(solid_names) == 1
                            and not imported.surfaces[0].derived_names
                            else None))
        os.replace(temporary, destination)
        fingerprint = self.entry_fingerprint({'artifact': str(destination)})
        revision_record = {
            'revision': revision, 'kind': 'repaired',
            'parent_revision': int(entry['revision']), 'artifact': str(destination),
            'fingerprint': fingerprint,
            'provenance': {
                'operation': 'geometry.repair.apply',
                'params': {'repair': operation, 'hole_size': hole_size,
                           'flip_normals': flip_normals},
            },
            'report': result.to_dict(),
        }
        updated = {
            **entry, 'revision': revision, 'kind': 'repaired',
            'artifact': str(destination), 'fingerprint': fingerprint,
            'diagnostics': result.after.to_dict(),
            'last_repair': result.to_dict(),
            'revisions': [*entry.get('revisions', self._legacy_revision(entry)),
                          revision_record],
        }
        if not entry.get('cad_artifact') and entry.get('patches') and not keeps_ids:
            updated.pop('patches', None)
            updated.pop('regions', None)
            revision_record['patches_dropped'] = True
        self._snapshot_patches(revision_record, updated)
        entries[index] = updated
        self._save(entries)
        return updated

    def preview_repair(self, geometry_id: str, operation: str, *,
                       hole_size: float = 1e6, flip_normals: bool = False) -> dict:
        from foammesh.core.geometry.diagnostics.repair import apply_action
        _, entry = self._entry(self.entries(), geometry_id)
        result = apply_action(
            self._polydata(entry), operation,
            hole_size=hole_size, flip_normals=flip_normals)
        return {
            **result.to_dict(), 'geometry_id': geometry_id,
            'base_revision': int(entry['revision']),
            'base_fingerprint': self.entry_fingerprint(entry),
        }

    def preview_repair_plan(self, plan: dict, *, progress=None, cancelled=None) -> dict:
        if plan.get('route') == 'cad':
            return self._preview_cad_repair_plan(
                plan, progress=progress, cancelled=cancelled)
        import time
        from .diagnostics.repair import TESSELLATED_ACTIONS, execute_action
        geometry_id = str(plan.get('geometry_id') or '')
        _, entry = self._entry(self.entries(), geometry_id)
        if plan.get('base_revision') is not None and \
                int(plan['base_revision']) != int(entry['revision']):
            raise ValueError('preview_stale')
        polydata = self._polydata(entry)
        # MEASURED: repairing a 37,240-triangle propeller with four
        # interpenetrating shells ran 57 minutes at a pegged core and 5.1 GB
        # before it was killed. `assess` runs before and after *every* action,
        # and each assessment re-runs the pairwise shell-intersection check, so
        # eight actions cost sixteen assessments of the most expensive check in
        # the product. One budget is shared across the whole repair, so that
        # cost is bounded once instead of per call.
        from .diagnostics.budget import (
            BudgetExceeded, budget_from_settings,
        )

        # The repair progress callback is ``progress(stage, fraction)``; the
        # budget reports ``(check, fraction, message)``. Adapted here, with the
        # message carried as the stage so a long check says what it is doing.
        budget = budget_from_settings('geometry.repair', on_progress=(
            (lambda check, fraction, message: progress(
                message or check, 0.0 if fraction is None else fraction))
            if callable(progress) else None))
        # Repair is work the user asked for and waits on, unlike an import that
        # happens on opening a file, so it gets a longer allowance than the
        # diagnostics default. Still bounded: the point is that it ends.
        if budget.maximum_seconds:
            budget.maximum_seconds *= 5
        before = assess(polydata, budget=budget)
        reports = []
        highlights = {}
        enabled_actions = [item for item in plan.get('actions', ())
                           if item.get('enabled', True)]
        for action_index, action in enumerate(enabled_actions):
            if cancelled and cancelled():
                raise ValueError('operation_cancelled')
            action_id = action.get('action')
            if action_id not in TESSELLATED_ACTIONS:
                reports.append({'action': action.get('action'), 'status': 'skipped',
                                'detail': 'action is not available for tessellated geometry'})
                continue
            params = action.get('params') or {}
            diagnostics_before = assess(polydata, budget=budget).to_dict()
            started = time.perf_counter()
            try:
                budget.check_in(progress=f'{action_index} of '
                                         f'{len(enabled_actions)} actions')
            except BudgetExceeded as error:
                # Report what was repaired and stop, rather than run on. The
                # alternative is what this replaced: a repair with no upper
                # bound and nothing on screen.
                reports.append({'action': action_id, 'status': 'skipped',
                                'detail': str(error)})
                break
            output, changes = execute_action(polydata, action_id, params)
            elapsed = time.perf_counter() - started
            timeout = float(params.get('timeout_per_action_s', 600))
            if timeout < 30:
                raise ValueError('timeout_per_action_s must be at least 30 seconds')
            if elapsed > timeout:
                # Checked after the fact, so this reports an overrun rather
                # than preventing one; the budget above is the real bound.
                raise ValueError(
                    f'action_timeout: {action_id} exceeded {timeout:g} seconds')
            diagnostics_after = assess(output, budget=budget).to_dict()
            duration_ms = int((time.perf_counter() - started) * 1000)
            cell_ids = list(changes.get('cell_ids', ()))[:50_000]
            if cell_ids:
                highlights[action_id] = cell_ids
            reports.append({
                'action': action_id, 'params': dict(params),
                # DP-487. Judged on the surface, not the cell count: a weld
                # that merges points keeps every triangle and still changed
                # the surface, and one that merges nothing did not.
                'status': 'applied' if _surface_changed(polydata, output)
                or changes.get('cells_flipped') else 'no_effect',
                'metrics_before': {
                    'points': polydata.GetNumberOfPoints(),
                    'cells': polydata.GetNumberOfCells(),
                    'finding_count': sum(f['count'] for f in diagnostics_before['findings'])},
                'metrics_after': {
                    'points': output.GetNumberOfPoints(),
                    'cells': output.GetNumberOfCells(),
                    'finding_count': sum(f['count'] for f in diagnostics_after['findings'])},
                'entities_touched': max(
                    abs(output.GetNumberOfCells() - polydata.GetNumberOfCells()),
                    len(cell_ids), int(changes.get('cells_flipped', 0))),
                'duration_ms': duration_ms, 'detail': changes,
                'diagnostics_before': diagnostics_before,
                'diagnostics_after': diagnostics_after,
            })
            polydata = output
            if progress:
                progress(action_id, (action_index + 1) / max(1, len(enabled_actions)))
        from .wrap import _deviation
        final_diagnostics = assess(polydata).to_dict()
        result = {
            'plan_digest': plan.get('digest'), 'route': 'tessellated',
            'geometry_id': geometry_id, 'base_revision': int(entry['revision']),
            'base_fingerprint': self.entry_fingerprint(entry), 'entries': reports,
            'diagnostics_before': before.to_dict(),
            'diagnostics_after': final_diagnostics,
            'deviation': _deviation(self._polydata(entry), polydata),
            'remaining_findings': [
                f"{finding['kind']}: {finding['count']}"
                for finding in final_diagnostics['findings'] if finding['count']],
            'tolerances': {'working': next((
                float(item.get('params', {}).get('tolerance'))
                for item in plan.get('actions', ())
                if item.get('params', {}).get('tolerance') is not None), None),
                'model_census': {}},
            'highlights': highlights,
            '_polydata': polydata,
        }
        result['effect'] = self._repair_effect(entry, reports)
        result['preview_digest'] = self._preview_digest(result, polydata)
        return result

    def _repair_effect(self, entry: dict, reports: list[dict]) -> dict:
        """What a tessellated repair plan would actually change, in one place.

        DP-487. Audit 0923 MA-03, case S1: the only selected action, a weld,
        merged 0 points, yet a new revision was written and shown as a
        repair. Two separate things can make a revision differ from its
        parent, and they are reported apart: what the actions did to the
        surface, and ``normalization`` -- the file being re-written from the
        surface as it was read, which drops facets the reader already
        discarded (``dropped_facets``) whatever the actions did.

        ``outcome`` is ``changed`` when an action changed the surface,
        ``normalized`` when none did but the re-write still resolves a
        finding, and ``none`` when a revision would be a copy of its parent.
        """
        applied = [item['action'] for item in reports if item['status'] == 'applied']
        idle = [item['action'] for item in reports if item['status'] == 'no_effect']
        normalization = None
        artifact = Path(str(entry.get('artifact') or ''))
        if artifact.suffix.lower() == '.stl' and artifact.is_file():
            from .importers.stl_importer import facet_count
            declared = facet_count(artifact)
            kept = int(self._polydata(entry).GetNumberOfCells())
            if declared is not None and declared > kept:
                normalization = {'dropped_facets': declared - kept,
                                 'declared': declared, 'read': kept}
        return {
            'outcome': ('changed' if applied else
                        'normalized' if normalization else 'none'),
            'applied': applied, 'no_effect': idle,
            'normalization': normalization,
        }

    def apply_repair_plan(self, plan: dict, *, progress=None, cancelled=None) -> dict:
        if plan.get('route') == 'cad':
            return self._apply_cad_repair_plan(
                plan, progress=progress, cancelled=cancelled)
        preview = self.preview_repair_plan(
            plan, progress=progress, cancelled=cancelled)
        expected_preview = plan.get('expected_preview_digest')
        if expected_preview and expected_preview != preview.get('preview_digest'):
            raise ValueError('preview_stale')
        entries = self.entries()
        index, entry = self._entry(entries, preview['geometry_id'])
        if (preview.get('effect') or {}).get('outcome') == 'none':
            # DP-487. Every selected action left the surface as it was and
            # the file needs no re-write, so a new revision would be a copy
            # of its parent presented as a repair.
            raise ValueError(
                'repair_no_effect: no selected repair changed the surface, '
                'so no revision was written')
        public_report = {key: value for key, value in preview.items()
                         if key != '_polydata'}
        output = preview['_polydata']
        face_ids = output.GetCellData().GetArray('cadFaceId')
        present = ({int(face_ids.GetTuple1(i)) for i in range(output.GetNumberOfCells())}
                   if face_ids is not None else set())
        patches = list(entry.get('patches', ()))
        if patches:
            preserved = [{'patch_uuid': patch['patch_uuid'], 'new_face_ids': [face_id]}
                         for face_id, patch in enumerate(patches) if face_id in present]
            lost = [{'patch_uuid': patch['patch_uuid'],
                     'reason': 'no cells remain after tessellated repair'}
                    for face_id, patch in enumerate(patches) if face_id not in present]
        else:
            preserved = ([{'patch_uuid': entry.get('patch_uuid'), 'new_face_ids': []}]
                         if entry.get('patch_uuid') else [])
            lost = []
        unmapped = sum(1 for i in range(output.GetNumberOfCells())
                       if face_ids is not None and int(face_ids.GetTuple1(i)) < 0)
        public_report['patch_map'] = {
            'preserved': preserved, 'split': [], 'merged': [], 'lost': lost,
            'created': ([{'new_face_id': -1, 'assigned_patch': 'repair_fill_1'}]
                        if unmapped else []),
            'unmapped_triangles': unmapped,
            'method': 'topology_preserved' if not unmapped else 'geometric_match',
        }
        return self._replace_artifact_revision(
            entries, index, entry, preview['_polydata'], {
                'kind': 'repaired', 'last_repair': public_report,
                'repair_report': public_report,
                'plan_digest': plan.get('digest'),
            })

    def _preview_cad_repair_plan(self, plan: dict, *, progress=None, cancelled=None) -> dict:
        from .cad import read_cad
        from .cad.healing_pipeline import (
            CadRepairAction, OcctHealingBackend, execute)
        from .cad.tessellate import tessellate
        from .wrap import _deviation
        geometry_id = str(plan.get('geometry_id') or '')
        _, entry = self._entry(self.entries(), geometry_id)
        if not is_cad_entry(entry):
            raise ValueError('CAD repair requires a B-Rep source revision')
        if plan.get('base_revision') is not None and \
                int(plan['base_revision']) != int(entry['revision']):
            raise ValueError('preview_stale')
        shape, _model = read_cad(entry['cad_artifact'])
        actions = [CadRepairAction(
            str(item.get('action')), dict(item.get('params') or {}),
            bool(item.get('enabled', True))) for item in plan.get('actions', ())]
        # F-11. The healed solid comes back in the unit the artifact reads
        # back in, while everything it is about to be compared and meshed
        # against is in metres. DP-08: that is `reader_unit`, not `unit` --
        # a metre-declaring STEP still hands OCCT millimetres.
        unit_factor = self._unit_factor(read_back_unit(entry))
        # DP-530. The plan's tolerances are in metres, as the Repair page
        # labels them; the shape is in `reader_unit`. They were handed to
        # OCCT raw, so the audit's G3 sew "at 0.0001 m" sewed its millimetre
        # STEP at 0.0001 mm -- and the same number on the repaired revision,
        # a BREP written in metres, would have meant metres.
        backend = OcctHealingBackend(unit_factor)
        healed, healing = execute(
            shape, actions, backend=backend,
            progress=progress, cancelled=cancelled)
        if healing.cancelled:
            raise ValueError('operation_cancelled')
        timeout_failure = next((entry for entry in healing.entries
                                if entry['status'] == 'failed'
                                and 'wall-clock guard' in entry.get('detail', '')), None)
        if timeout_failure:
            raise ValueError(
                f"action_timeout: {timeout_failure['action']} {timeout_failure['detail']}")
        retessellate = next((item.get('params', {}) for item in plan.get('actions', ())
                             if item.get('action') == 'cad.retessellate'), {})
        # F-10. A repair re-facets the solid, and it does so at the deflection
        # the part was imported and judged at unless the plan asks for
        # another one; the fixed constants that used to stand here silently
        # coarsened or refined every repaired part.
        requested = {key: value for key, value in retessellate.items()
                     if key in {'linear_deflection', 'angular_deflection_deg',
                                'relative', 'parallel'}}
        params = tessellation_params({**stored_tessellation(entry), **requested})
        healed_metres = (_scaled_shape(healed, unit_factor)
                         if unit_factor != 1.0 else healed)
        after_surface = tessellate(healed_metres, params)
        face_ids = after_surface.GetCellData().GetArray('cadFaceId')
        histogram = {}
        if face_ids is not None:
            for cell_id in range(after_surface.GetNumberOfCells()):
                face_id = str(int(face_ids.GetTuple1(cell_id)))
                histogram[face_id] = histogram.get(face_id, 0) + 1
        before_surface = self._polydata(entry)
        diagnostics_after = assess(after_surface).to_dict()
        deviation = _deviation(before_surface, after_surface)
        max_deviation = plan.get('max_deviation')
        if max_deviation is not None and deviation['max'] > float(max_deviation):
            raise ValueError(
                f"deviation_exceeded: measured {deviation['max']}, "
                f"permitted {float(max_deviation)}")
        analysis_entry = next((item for item in healing.entries
                               if item['action'] == 'cad.analyze'), {})
        tolerance_census = analysis_entry.get('metrics_after', {}).get(
            'tolerance_census', {})
        result = {
            'plan_digest': plan.get('digest'), 'route': 'cad',
            'geometry_id': geometry_id, 'base_revision': int(entry['revision']),
            'base_fingerprint': self.entry_fingerprint(entry),
            'entries': healing.entries,
            'diagnostics_before': assess(before_surface).to_dict(),
            'diagnostics_after': diagnostics_after,
            'deviation': deviation,
            'remaining_findings': [
                f"{finding['kind']}: {finding['count']}"
                for finding in diagnostics_after['findings'] if finding['count']],
            'tolerances': {
                'working': next((float(item.get('params', {}).get('tolerance'))
                                 for item in plan.get('actions', ())
                                 if item.get('params', {}).get('tolerance') is not None),
                                1e-6),
                'model_census': tolerance_census},
            'measurement_boundary': healing.measurement_boundary,
            'retessellation': {
                **dataclasses.asdict(params),
                'unit_factor': float(unit_factor),
                'triangle_count': int(after_surface.GetNumberOfCells()),
                'per_face_triangles': histogram},
            '_base_shape': shape, '_shape': healed_metres,
            '_polydata': after_surface, '_params': params,
            '_unit_factor': float(unit_factor),
        }
        result['_patch_map'] = backend.patch_map(
            shape, healed, list(entry.get('patches', ())),
            result['tolerances']['working'])
        result['preview_digest'] = self._preview_digest(result, after_surface)
        return result

    def _apply_cad_repair_plan(self, plan: dict, *, progress=None, cancelled=None) -> dict:
        preview = self._preview_cad_repair_plan(
            plan, progress=progress, cancelled=cancelled)
        expected_preview = plan.get('expected_preview_digest')
        if expected_preview and expected_preview != preview.get('preview_digest'):
            raise ValueError('preview_stale')
        entries = self.entries()
        index, entry = self._entry(entries, preview['geometry_id'])
        revision = self._allocate_revision(entry)
        root = self.root / entry['geometry_id']
        cad_artifact = root / f'rev{revision}.brep'
        surface_artifact = root / f'rev{revision}.stl'
        self._write_brep(preview['_shape'], cad_artifact)
        self._write_polydata(preview['_polydata'], surface_artifact)
        fingerprint = self._combined_fingerprint((cad_artifact, surface_artifact))
        report = {key: value for key, value in preview.items()
                  if not key.startswith('_')}
        report['patch_map'] = preview['_patch_map']
        if report['patch_map']['lost']:
            report['patch_map']['unmapped_triangles'] = int(
                preview['_polydata'].GetNumberOfCells())
        revision_record = {
            'revision': revision, 'kind': 'repaired',
            'parent_revision': int(entry['revision']),
            'artifact': str(surface_artifact), 'cad_artifact': str(cad_artifact),
            'fingerprint': fingerprint,
            'provenance': {'operation': 'geometry.repair.apply',
                           'plan_digest': plan.get('digest'),
                           'params': {'actions': plan.get('actions', ())}},
            'report': report,
        }
        polydata = preview['_polydata']
        # F-11. What was written is the healed solid in metres, so the entry
        # says metres; leaving the source unit there would scale it a second
        # time on every later read. DP-08: the repaired artifact is a BREP,
        # which reads back in the unit it was written in, so both facts move
        # together here -- unlike the copied STEP or IGES they came from.
        unit_factor = float(preview.get('_unit_factor', 1.0))
        repaired_unit = ('m' if unit_factor != 1.0
                         else read_back_unit(entry) or entry.get('unit'))
        updated = {
            **entry, 'revision': revision, 'kind': 'repaired',
            'unit': repaired_unit, 'reader_unit': repaired_unit,
            'cad_unit_factor': float(entry.get('cad_unit_factor', 1.0)) * unit_factor,
            'tessellation': dataclasses.asdict(
                tessellation_params(preview.get('_params')
                                    or stored_tessellation(entry))),
            'tessellation_unit': 'm',
            'artifact': str(surface_artifact), 'cad_artifact': str(cad_artifact),
            'fingerprint': fingerprint, 'cells': int(polydata.GetNumberOfCells()),
            'points': int(polydata.GetNumberOfPoints()),
            'bbox': list(polydata.GetBounds()),
            'diagnostics': assess(polydata).to_dict(), 'healing_report': report,
            'last_repair': report,
            'revisions': [*entry.get('revisions', self._legacy_revision(entry)),
                          revision_record],
        }
        entries[index] = updated
        self._save(entries)
        return updated

    @staticmethod
    def _write_brep(shape, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp.brep')
        try:
            try:
                from OCC.Core.BRepTools import breptools
                written = breptools.Write(shape, str(temporary))
            except ImportError:
                from OCC.Core.BRepTools import BRepTools
                written = BRepTools.Write(shape, str(temporary))
            if written is False or not temporary.is_file():
                raise OSError('OCCT could not serialize repaired B-Rep')
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _preview_digest(report: dict, polydata) -> str:
        """Digest deterministic geometry + report fields, excluding timings."""
        import struct
        digest = hashlib.sha256()
        digest.update(str(report.get('plan_digest')).encode())
        for point_id in range(polydata.GetNumberOfPoints()):
            digest.update(struct.pack('<3d', *map(float, polydata.GetPoint(point_id))))
        for cell_id in range(polydata.GetNumberOfCells()):
            cell = polydata.GetCell(cell_id)
            ids = [cell.GetPointId(i) for i in range(cell.GetNumberOfPoints())]
            digest.update(struct.pack('<I', len(ids)))
            digest.update(struct.pack(f'<{len(ids)}q', *ids))
        return f'sha256:{digest.hexdigest()}'

    def rollback(self, geometry_id: str, target_revision: int) -> dict:
        entries = self.entries()
        index, entry = self._entry(entries, geometry_id)
        revisions = entry.get('revisions', self._legacy_revision(entry))
        target = next((item for item in revisions
                       if int(item['revision']) == int(target_revision)), None)
        if target is None:
            raise ValueError(f'geometry revision does not exist: {target_revision}')
        polydata = self._polydata({'artifact': target['artifact']})
        health = assess(polydata)
        updated = {
            **entry, 'revision': int(target['revision']),
            'kind': target.get('kind', 'imported'), 'artifact': target['artifact'],
            'fingerprint': target.get('fingerprint') or self.entry_fingerprint(target),
            'cells': int(polydata.GetNumberOfCells()),
            'points': int(polydata.GetNumberOfPoints()),
            'bbox': list(polydata.GetBounds()) if polydata.GetNumberOfPoints() else None,
            'diagnostics': health.to_dict(), 'revisions': revisions,
            'last_rollback': {'from_revision': entry['revision'],
                              'to_revision': int(target_revision)},
        }
        if target.get('cad_artifact'):
            updated['cad_artifact'] = target['cad_artifact']
        else:
            updated.pop('cad_artifact', None)
            # Plan 28 WP7. A tessellated entry's patches belong to a
            # revision: a split made them and the revision before it had
            # none (or a different set). Returning to that revision returns
            # to its patches, or the records would name six solids in a
            # file that holds one.
            if target.get('patches'):
                updated['patches'] = [dict(item) for item in target['patches']]
                updated['regions'] = [dict(item) for item in target.get('regions') or ()]
            else:
                updated.pop('patches', None)
                updated.pop('regions', None)
        entries[index] = updated
        self._save(entries)
        return updated

    def preview_wrap(self, geometry_id: str, *, cancelled=None, **parameters) -> dict:
        from .wrap import estimate, wrap
        _, entry = self._entry(self.entries(), geometry_id)
        source = self._polydata(entry)
        resolution = int(parameters.get('resolution', 64))
        sizing = estimate(
            source, resolution=resolution,
            smallest_feature=parameters.get('smallest_feature'))
        coarse_parameters = dict(parameters)
        coarse_parameters.pop('expected_revision', None)
        coarse_parameters.pop('smallest_feature', None)
        # The acceptance preview is meshed on a grid of at most
        # PREVIEW_RESOLUTION cells so that it stays quick; the smallest
        # feature has already been turned into `sizing.resolution`. DP-640
        # (field audit 0924 D-SH-10): the payload says which grid apply will
        # use, so nobody reads a coarse triangle count as the applied one.
        coarse_parameters['resolution'] = min(PREVIEW_RESOLUTION, sizing.resolution)
        coarse, report = wrap(source, cancelled=cancelled, **coarse_parameters)
        return {
            'geometry_id': geometry_id, 'base_revision': int(entry['revision']),
            'base_fingerprint': self.entry_fingerprint(entry),
            'experimental': True, **sizing.to_dict(),
            'coarse_preview': {
                'resolution': coarse_parameters['resolution'],
                'applied_resolution': int(sizing.resolution),
                'same_as_apply': (int(coarse_parameters['resolution'])
                                  == int(sizing.resolution)),
                'points': int(coarse.GetNumberOfPoints()),
                'cells': int(coarse.GetNumberOfCells()),
                'watertight': bool(report['diagnostics_after']['watertight']),
                'deviation': report['deviation'],
                'deviation_budget': report['smoothing_deviation_budget'],
                'patch_transfer': report['patch_transfer'],
                'new_skin_fraction': report['new_skin_fraction'],
                'triangle_count': int(coarse.GetNumberOfCells())},
        }

    def estimate_wrap(self, geometry_id: str, resolution: int = 64,
                      smallest_feature: float | None = None) -> dict:
        from .wrap import estimate
        _, entry = self._entry(self.entries(), geometry_id)
        return {'geometry_id': geometry_id, **estimate(
            self._polydata(entry), resolution=int(resolution),
            smallest_feature=smallest_feature).to_dict()}

    def apply_wrap(self, geometry_id: str, *, cancelled=None, **parameters) -> dict:
        from .wrap import wrap
        entries = self.entries()
        index, entry = self._entry(entries, geometry_id)
        expected = parameters.pop('expected_revision', None)
        if expected is not None and int(expected) != int(entry['revision']):
            raise ValueError('preview_stale')
        output, report = wrap(self._polydata(entry), cancelled=cancelled, **parameters)
        counts = report.get('patch_transfer', {}).get('per_patch_cells', {})
        preserved, lost = [], []
        for face_id, patch in enumerate(entry.get('patches', ())):
            if counts.get(str(face_id), 0):
                preserved.append({'patch_uuid': patch['patch_uuid'],
                                  'new_face_ids': [face_id]})
            else:
                lost.append({'patch_uuid': patch['patch_uuid'],
                             'reason': 'no wrap triangles accepted nearest projection'})
        unassigned = int(report.get('patch_transfer', {}).get('unassigned', 0))
        report['patch_map'] = {
            'preserved': preserved, 'split': [], 'merged': [], 'lost': lost,
            'created': ([{'new_face_id': -1, 'assigned_patch': 'wrap_unassigned'}]
                        if unassigned else []),
            'unmapped_triangles': unassigned, 'method': 'nearest_projection',
        }
        return self._replace_artifact_revision(entries, index, entry, output, {
            'kind': 'wrapped', 'wrap_report': report,
        })

    @staticmethod
    def _legacy_revision(entry: dict) -> list[dict]:
        return [{
            'revision': int(entry.get('revision', 1)),
            'kind': entry.get('kind', 'imported'), 'parent_revision': None,
            'artifact': entry['artifact'], 'fingerprint': entry.get('fingerprint'),
            'provenance': {'operation': 'legacy.import'},
        }]

    @classmethod
    def _allocate_revision(cls, entry: dict) -> int:
        """Return a fresh revision number above every recorded revision.

        Using ``max(existing) + 1`` rather than ``current + 1`` prevents a new
        apply from overwriting an intermediate revision's artifact after a
        rollback re-pointed ``current`` to an earlier revision (Appendix A §1.1).
        """
        numbers = [int(entry.get('revision', 0))]
        for record in entry.get('revisions') or ():
            try:
                numbers.append(int(record.get('revision', 0)))
            except (TypeError, ValueError):
                continue
        return max(numbers) + 1

    def _selected(self, geometry_id: str | None) -> list[dict]:
        entries = self.entries()
        if geometry_id is None:
            return entries
        selected = [item for item in entries if item['geometry_id'] == geometry_id]
        if not selected:
            raise KeyError(geometry_id)
        return selected

    @staticmethod
    def _entry(entries: list[dict], geometry_id: str) -> tuple[int, dict]:
        index = next((i for i, item in enumerate(entries)
                      if item['geometry_id'] == geometry_id), None)
        if index is None:
            raise KeyError(geometry_id)
        return index, entries[index]

    @staticmethod
    def _polydata(entry: dict):
        imported = import_surface(entry['artifact'])
        if not imported.surfaces:
            raise ValueError('geometry artifact contains no readable surfaces')
        return imported.surfaces[0].polydata

    @staticmethod
    def _unit_factor(unit: str | None) -> float:
        """Metres per *unit*, or 1.0 when there is nothing to convert.

        An unrecognised unit converts nothing rather than guessing: a wrong
        factor is silent and total, and every length in the case inherits it.
        """
        from .units import si_factor
        try:
            return si_factor(unit) if unit else 1.0
        except (KeyError, ValueError):
            return 1.0

    def _write_converted(self, result, destination: Path,
                         unit: str | None, names: dict | None = None) -> None:
        """Write the imported surfaces to *destination*, scaled into metres."""
        from vtkmodules.vtkFiltersCore import vtkAppendPolyData

        from .units import to_metres

        surfaces = [item.polydata for item in result.surfaces]
        if len(surfaces) == 1:
            merged = surfaces[0]
        else:
            append = vtkAppendPolyData()
            for polydata in surfaces:
                append.AddInputData(polydata)
            append.Update()
            merged = append.GetOutput()
        # The temporary keeps the real suffix: `_write_polydata` chooses its
        # writer from it, so a `.tmp` extension silently produces an OBJ in a
        # file everything downstream then reads as STL -- which parses as an
        # empty surface rather than failing.
        temporary = destination.with_name(
            f'{destination.stem}.new{destination.suffix}')
        self._write_polydata(to_metres(merged, unit), temporary, names=names)
        os.replace(temporary, destination)

    @staticmethod
    def _to_metres(polydata, unit: str | None):
        """Scale a surface from its declared unit into metres.

        Delegates so the viewport's copy of the same surface is scaled by the
        identical code -- see ``units.to_metres``.
        """
        from .units import to_metres

        return to_metres(polydata, unit)

    @staticmethod
    def _write_polydata(polydata, destination: Path,
                        names: dict | None = None) -> None:
        from vtkmodules.vtkIOGeometry import vtkOBJWriter, vtkSTLWriter
        if destination.suffix.lower() == '.stl':
            from foammesh.core.geometry.cad.surface_split import face_id_range
            if face_id_range(polydata) is not None:
                # A CAD import keeps one patch per face, and snappy resolves
                # those patches by solid name inside the STL. vtkSTLWriter emits
                # a single unnamed solid, so every generated ``regions`` entry
                # would fail with "Unknown region name".
                GeometryArtifactStore._write_named_solid_stl(
                    polydata, destination, names=names)
                return
        writer = vtkSTLWriter() if destination.suffix.lower() == '.stl' else vtkOBJWriter()
        writer.SetFileName(str(destination))
        writer.SetInputData(polydata)
        if names and len(names) == 1 and destination.suffix.lower() == '.stl':
            # DP-64. One surface, one name and no face tags to group by: a
            # single-solid STL being rewritten for its units. In an ASCII STL
            # the header *is* the solid name, and left alone vtkSTLWriter puts
            # its own there -- MEASURED, `solid Visualization Toolkit
            # generated SLA File`, four words OpenFOAM cannot key a `regions`
            # entry on and the user never chose. A unit conversion changes the
            # coordinates and nothing else, so the block keeps the name it
            # arrived with.
            writer.SetHeader(str(next(iter(names.values()))))
        if writer.Write() != 1:
            raise OSError(f'could not write geometry artifact: {destination}')

    @staticmethod
    def _write_named_solid_stl(polydata, destination: Path,
                               names: dict | None = None) -> None:
        """Write one ``solid <patch>`` block per tagged face.

        ``names`` maps a face id to the solid name it must carry; a face
        without one is ``face<id>``. Solids are written in face-id order,
        which is also the order the reader numbers them in when the names
        do not carry the id themselves.
        """
        from foammesh.core.geometry.cad.surface_split import FACE_ID_ARRAY
        face_ids = polydata.GetCellData().GetArray(FACE_ID_ARRAY)
        points = polydata.GetPoints()
        grouped: dict[int, list[int]] = {}
        for cell_id in range(polydata.GetNumberOfCells()):
            grouped.setdefault(int(face_ids.GetTuple1(cell_id)), []).append(cell_id)
        temporary = destination.with_suffix(destination.suffix + '.solids')
        with temporary.open('w', encoding='utf-8') as stream:
            # DP-486. A negative id marks cells a repair created (a filled
            # hole) and that no source solid owns. They are written last, so
            # the solids the file already had keep their positions on the
            # next read -- which is how a record without a ``face<N>`` name
            # finds its solid again -- and they carry the patch name the
            # repair report gives them rather than ``face-1``.
            for face_id in sorted(grouped, key=lambda value: (value < 0, abs(value))):
                name = ((names or {}).get(face_id) or
                        (f'repair_fill_{-face_id}' if face_id < 0
                         else f'face{face_id}'))
                stream.write(f'solid {name}\n')
                for cell_id in grouped[face_id]:
                    cell = polydata.GetCell(cell_id)
                    ids = cell.GetPointIds()
                    if ids.GetNumberOfIds() != 3:
                        continue
                    vertices = [points.GetPoint(ids.GetId(index)) for index in range(3)]
                    normal = _triangle_normal(*vertices)
                    stream.write('  facet normal '
                                 f'{normal[0]:.16g} {normal[1]:.16g} {normal[2]:.16g}\n')
                    stream.write('    outer loop\n')
                    for vertex in vertices:
                        stream.write('      vertex '
                                     f'{vertex[0]:.16g} {vertex[1]:.16g} {vertex[2]:.16g}\n')
                    stream.write('    endloop\n  endfacet\n')
                stream.write(f'endsolid {name}\n')
        os.replace(temporary, destination)

    def _replace_artifact_revision(self, entries: list[dict], index: int, entry: dict,
                                   polydata, metadata: dict) -> dict:
        artifact = Path(entry['artifact'])
        revision = self._allocate_revision(entry)
        destination = self.root / entry['geometry_id'] / f'rev{revision}{artifact.suffix}'
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(f'.transform{artifact.suffix}')
        # The solids are named after the records that will describe them:
        # the new records when this revision brings its own (a split), the
        # entry's otherwise.
        names = self._patch_solid_names(
            {'patches': metadata['patches']} if metadata.get('patches') else entry)
        if metadata.get('solid_names'):
            # DP-421. An STL import's patch record carries no `face_index` --
            # only a CAD face has one -- so `_patch_solid_names` answers with
            # nothing for it and the block below writes vtkSTLWriter's own
            # header instead: MEASURED, `solid Visualization Toolkit generated
            # SLA File`, on the body revisions the interface cut produced.
            # The manifest went on calling the same solid `left`, so nothing
            # downstream could find the block the row named. An operation that
            # knows the name says it here rather than inferring it.
            names = dict(metadata['solid_names'])
        if not names:
            # DP-486. A plain STL's identity is not in a patch record with a
            # face index -- a single-solid import has no patch records at
            # all -- so the lookups above answer nothing for it and the
            # revision went out under vtkSTLWriter's own header, MEASURED on
            # the audit's repaired `box_with_cavity` and `open_box`:
            # `Unknown region name box_with_cavity`, valid region
            # `VisualizationToolkitgeneratedSLAFile`. A new revision is the
            # same surface under the same names.
            names = self._artifact_solid_names(entry, polydata)
        elif not metadata.get('patches') and not metadata.get('solid_names'):
            # The records describe the solids the file already has, so any
            # solid they do not name -- a hole an earlier repair filled --
            # keeps the name the file gave it rather than becoming face<N>.
            names = {**self._artifact_solid_names(entry, polydata), **names}
        try:
            self._write_polydata(polydata, temporary, names=names)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        health = assess(polydata)
        fingerprint = self.entry_fingerprint({'artifact': str(destination)})
        revision_kind = metadata.get('kind', 'tessellated')
        revision_record = {
            'revision': revision, 'kind': revision_kind,
            'parent_revision': int(entry['revision']), 'artifact': str(destination),
            'fingerprint': fingerprint,
            'provenance': {
                'operation': ('geometry.repair.apply' if revision_kind == 'repaired'
                              else 'geometry.wrap.apply' if revision_kind == 'wrapped'
                              else 'geometry.patches.split_by_angle'
                              if revision_kind == 'split'
                              else 'geometry.split_interfaces'
                              if revision_kind == 'interface_split'
                              else 'geometry.transform'),
                **{key: value for key, value in metadata.items()
                   if key not in {'last_repair', 'repair_report', 'patches',
                                  'regions', 'split_report', 'solid_names'}},
            },
            **({'report': metadata['repair_report']}
               if 'repair_report' in metadata else {}),
            **({'report': metadata['wrap_report']}
               if 'wrap_report' in metadata else {}),
            **({'report': metadata['split_report']}
               if 'split_report' in metadata else {}),
        }
        updated = {
            **entry, 'revision': revision, 'kind': 'tessellated',
            'artifact': str(destination), 'fingerprint': fingerprint,
            'cells': int(polydata.GetNumberOfCells()),
            'points': int(polydata.GetNumberOfPoints()),
            'bbox': list(polydata.GetBounds()) if polydata.GetNumberOfPoints() else None,
            'diagnostics': health.to_dict(), **metadata,
            'revisions': [*entry.get('revisions', self._legacy_revision(entry)),
                          revision_record],
        }
        if revision_kind == 'wrapped':
            # F-27. The wrap is the geometry from here on, but the CAD file
            # it was wrapped from is still the provenance of everything in
            # this entry -- its faces, its declared unit, its report. It used
            # to be deleted from the record, so a wrapped CAD import could no
            # longer say what it had been.
            updated['cad_superseded_by'] = 'geometry.wrap.apply'
        self._snapshot_patches(revision_record, updated)
        entries[index] = updated
        self._save(entries)
        return updated

    @staticmethod
    def _snapshot_patches(revision_record: dict, entry: dict) -> None:
        """Record a tessellated entry's patch set on its revision.

        Plan 28 WP7. Rollback reads it back. A CAD entry's patches are the
        model's faces and do not move with the surface revision, so they
        are not snapshotted.
        """
        if is_cad_entry(entry) or not entry.get('patches'):
            return
        revision_record['patches'] = [dict(item) for item in entry['patches']]
        revision_record['regions'] = [dict(item) for item in entry.get('regions') or ()]

    def _new_derived_entry(self, polydata, *, name: str, suffix: str,
                           derived_from: list[str], operation: str) -> dict:
        geometry_id = uuid4().hex
        artifact_root = self.root / geometry_id
        artifact_root.mkdir(parents=True, exist_ok=True)
        artifact = artifact_root / f'rev1{suffix}'
        temporary = artifact.with_suffix(f'.new{suffix}')
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            # DP-486. The new entry is recorded under *name*, and a
            # single-solid STL's header is the only place the file can say
            # it; left out, vtkSTLWriter writes its own. A tagged surface
            # keeps its per-face solids and their ``face<N>`` names.
            from .cad.surface_split import face_id_range
            self._write_polydata(
                polydata, temporary,
                names=None if face_id_range(polydata) is not None else {0: name})
            os.replace(temporary, artifact)
        finally:
            if temporary.exists():
                temporary.unlink()
        health = assess(polydata)
        fingerprint = self.entry_fingerprint({'artifact': str(artifact)})
        patch_uuid = str(uuid4())
        revision_record = {
            'revision': 1, 'kind': 'tessellated', 'parent_revision': None,
            'artifact': str(artifact), 'fingerprint': fingerprint,
            'provenance': {'operation': f'geometry.{operation}',
                           'derived_from': list(derived_from)},
        }
        return {
            'geometry_id': geometry_id, 'name': name,
            'format': suffix.removeprefix('.'), 'source': None,
            'artifact': str(artifact), 'cells': int(polydata.GetNumberOfCells()),
            'points': int(polydata.GetNumberOfPoints()),
            'bbox': list(polydata.GetBounds()) if polydata.GetNumberOfPoints() else None,
            'diagnostics': health.to_dict(), 'revision': 1, 'kind': 'tessellated',
            'fingerprint': fingerprint, 'revisions': [revision_record],
            'patch_uuid': patch_uuid,
            'source_ref': {'file': None, 'body_index': 0, 'face_index': None,
                           'original_name': name},
            'derived_from': list(derived_from), 'derivation': operation,
        }

    def save_entries(self, entries: list[dict]) -> None:
        """Persist an edited manifest.

        Plan 28 WP5. Boundary-patch editing lives in
        :mod:`foammesh.core.geometry.patches.manifest` -- it is a set of rules
        about names and membership, not about artifacts, and the store has no
        opinion on either. It still needs somewhere to put the result, and
        reaching into ``_save`` from outside would make a private method part
        of the contract without saying so.
        """
        self._save(entries)

    def _save(self, entries: list[dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix('.tmp')
        temporary.write_text(
            json.dumps({'schema_version': 1, 'geometries': entries},
                       indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(temporary, self.manifest_path)
