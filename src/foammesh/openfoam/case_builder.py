#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Headless OpenFOAM case generation from project state.

Reads the project configuration (the SimpleDB) + a geometry bounding box and
produces deterministic OpenFOAM v13 dictionaries (blockMesh, snappyHexMesh,
surfaceFeatures, decomposePar) via the dict_format serializer — no Qt, no
``app.window`` coupling. This is the headless counterpart to the GUI dict writers
and is what the API/CLI use to generate a case.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import math
import os
import re
import shutil
from dataclasses import dataclass, replace as _replace_dataclass
from pathlib import Path
from uuid import uuid4

from . import background_mesh
from .dict_format import format_dictionary_file
from foammesh.core.layer_patterns import (
    PatternError, matching_patches, quoted_key)
from foammesh.core.quantities import agreeing, count_text
from .snappy_controls import (
    BACKGROUND_PATCH_GROUP, BOUNDARY_FACES, DEBUG_FLAGS, WRITE_FLAGS)
from .target import FoamTarget, DEFAULT_TARGET, mesh_quality_controls


@dataclass(frozen=True)
class DictionaryManifest:
    files: tuple[dict[str, object], ...]
    target: str
    surfaces: tuple[dict[str, object], ...] = ()
    fluid_seed: tuple[float, float, float] | None = None
    bbox: tuple[float, float, float, float, float, float] | None = None
    configuration_sha256: str | None = None
    stage_sha256: dict[str, str] | None = None
    warnings: tuple[dict[str, object], ...] = ()

    def to_dict(self) -> dict:
        return {
            'schema_version': 2,
            'target': self.target,
            'files': list(self.files),
            'surfaces': list(self.surfaces),
            'fluid_seed': (
                list(self.fluid_seed)
                if self.fluid_seed is not None else None),
            'bbox': list(self.bbox) if self.bbox is not None else None,
            'configuration_sha256': self.configuration_sha256,
            'stage_sha256': dict(self.stage_sha256 or {}),
            'warnings': list(self.warnings),
        }


def group_manifest_path(case_dir) -> Path:
    """The generated case's own group manifest.

    The prepared store publishes an immutable group manifest per revision;
    this is the case-level copy of it, plus the interface pairs the project
    authored after preparation. Keeping it beside the case is what lets the
    non-conformal step read groups and pairs from one document without
    reopening -- or invalidating -- the prepared revision.
    """
    return Path(case_dir) / 'foammesh' / 'geometry' / 'group-manifest.json'


class CaseBuilder:
    def __init__(self, db, bbox, *, target: FoamTarget = DEFAULT_TARGET,
                 surface_file: str = 'geometry.stl'):
        self.db = db
        self.bbox = bbox
        self.target = target
        self.surface_file = surface_file
        self.surfaces = ({'name': Path(surface_file).stem,
                          'file': surface_file, 'groups': (),
                          'geometry_id': None},)
        self.prepared_geometry = None
        self.warnings: list[dict[str, object]] = []
        #: The ranks a run will actually start, when the caller knows them.
        #: Plan 31 CP-07: left ``None``, the decomposition is written serial
        #: rather than guessed from the CPU ceiling, so the dictionary never
        #: claims a rank count no launcher agreed to.
        self.requested_ranks: int | None = None

    # -- generation warnings ------------------------------------------------
    def warn(self, code: str, message: str, *, field_id: str = '',
             requested=None, applied=None, severity: str = 'warning') -> None:
        """Record a deviation between what was asked for and what was written.

        Plan 26 WP2.2. This list was initialised, serialised into every
        manifest and rendered under "Validation warnings:" by the Effective
        Meshing Setup dialog -- and nothing ever appended to it, so a working
        display sat permanently empty and every silent substitution below
        looked like an exact translation of the request.

        The shape is stable so the GUI, the CLI and the API can all read it
        without each inventing a format: a machine-readable ``code``, the
        ``field_id`` a user would go and change, and the requested and applied
        values side by side so the substitution is visible rather than merely
        described.
        """
        entry = {'code': code, 'severity': severity, 'message': message,
                 'field_id': field_id}
        if requested is not None:
            entry['requested'] = requested
        if applied is not None:
            entry['applied'] = applied
        if entry not in self.warnings:
            self.warnings.append(entry)

    # helpers --------------------------------------------------------------
    def _v(self, path, default=None):
        try:
            v = self.db.getValue(path)
            return v if v is not None else default
        except Exception:
            return default

    def _int(self, path, default: int) -> int:
        value = self._v(path, default)
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return int(default)

    def _elements(self, path) -> dict:
        try:
            return dict(self.db.getElements(path))
        except Exception:
            return {}

    @staticmethod
    def _enum_value(value):
        return getattr(value, 'value', value)

    @staticmethod
    def _item_value(item, name, default=None):
        try:
            value = item.value(name)
            return getattr(value, 'value', value)
        except Exception:
            return default

    @staticmethod
    def _item_element(item, name):
        try:
            return item.element(name)
        except Exception:
            return None

    def _collection_item(self, path, key):
        if key is None:
            return None
        collection = self._elements(path)
        if key in collection:
            return collection[key]
        token = str(key)
        for candidate, value in collection.items():
            if str(candidate) == token:
                return value
        return None

    def _configuration_sha256(self) -> str:
        try:
            value = self.db.toYaml()
        except Exception:
            value = repr(self.db)
        return hashlib.sha256(str(value).encode('utf-8')).hexdigest()

    def _configuration_document(self) -> dict:
        try:
            import yaml
            document = yaml.safe_load(self.db.toYaml())
            return document if isinstance(document, dict) else {}
        except Exception:
            return {}

    def stage_input_sha256(self, stage: str) -> str:
        """Fingerprint only the fields capable of changing one stage."""
        document = self._configuration_document()
        geometry = document.get('geometry', {})

        def geometry_fields(*extra: str) -> dict:
            names = (
                'name', 'gType', 'shape', 'cfdType', 'nonConformal',
                'interRegion', 'point1', 'point2', 'radius') + extra
            return {
                str(key): {name: value.get(name) for name in names}
                for key, value in geometry.items()
                if isinstance(value, dict)
            }

        # R83. Every stage used to fingerprint one shared field list that
        # included `layerGroup` and `slaveLayerGroup`. Those two say which
        # boundary-layer group a surface belongs to and reach only the
        # addLayers stage -- but because castellation hashed them too,
        # assigning surfaces to a boundary-layer group (the one thing the
        # Boundary Layers task exists to do) changed castellation's
        # fingerprint and marked it stale. `layers` then refused to run at
        # all: "upstream mesh stages are stale and must be rerun first:
        # castellation". MEASURED on venturi.stl -- castellation and snap had
        # both completed, a 3-layer group was added, and Run & Proceed failed
        # with exactly that message. Re-running castellation would in turn
        # mark snap stale, so the workflow could never reach a layered mesh
        # by following its own prescribed order. Each stage now hashes only
        # the geometry fields it actually consumes.
        castellation_geometry = geometry_fields('castellationGroup')
        layer_geometry = geometry_fields(
            'castellationGroup', 'layerGroup', 'slaveLayerGroup')
        payloads = {
            'blockMesh': {
                'baseGrid': document.get('baseGrid', {}),
            },
            'surfaceFeatures': {
                'refinementSurfaces': (
                    document.get('castellation', {})
                    .get('refinementSurfaces', {})),
                'surfaces': self.surfaces,
            },
            'castellation': {
                'castellation': document.get('castellation', {}),
                'geometry': castellation_geometry,
                'region': document.get('region', {}),
                'surfaces': self.surfaces,
            },
            'snappyHexMesh': {
                'castellation': document.get('castellation', {}),
                'snap': document.get('snap', {}),
                'addLayers': document.get('addLayers', {}),
                'meshQuality': document.get('meshQuality', {}),
                'geometry': layer_geometry,
                'region': document.get('region', {}),
                'surfaces': self.surfaces,
            },
            'snap': {
                'snap': document.get('snap', {}),
                'surfaces': self.surfaces,
            },
            'layers': {
                'addLayers': document.get('addLayers', {}),
                'meshQuality': document.get('meshQuality', {}),
                'geometry': layer_geometry,
                'surfaces': self.surfaces,
            },
            'checkMesh': {
                'meshQuality': document.get('meshQuality', {}),
            },
        }
        if stage not in payloads:
            raise ValueError(f'unknown stage fingerprint: {stage}')
        encoded = json.dumps(
            payloads[stage], sort_keys=True, separators=(',', ':'),
            ensure_ascii=False, default=str).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _bbox_tuple(bbox) -> tuple[float, float, float, float, float, float]:
        return (
            float(bbox.xmin), float(bbox.xmax),
            float(bbox.ymin), float(bbox.ymax),
            float(bbox.zmin), float(bbox.zmax),
        )

    def _effective_bbox(self):
        """Honor a selected Hex6 background box for every frontend.

        The desktop traditionally passed the selected actor bounds while
        CLI/API callers supplied only the overall geometry bounds. Reading the
        selected Hex6 from the shared configuration closes that semantic gap.
        """
        selected = self._bounding_hex6_key()
        if selected is None:
            return self._stood_off_bbox()
        geometry = self._collection_item('geometry', selected)
        try:
            from foammesh.core.geometry import BBox
            p1 = geometry.vector('point1')
            p2 = geometry.vector('point2')
            return BBox(
                min(float(p1[0]), float(p2[0])),
                max(float(p1[0]), float(p2[0])),
                min(float(p1[1]), float(p2[1])),
                max(float(p1[1]), float(p2[1])),
                min(float(p1[2]), float(p2[2])),
                max(float(p1[2]), float(p2[2])))
        except Exception:
            return self.bbox

    def _bounding_hex6_key(self):
        """The bounding Hex6's key, only when it names a ``hex6`` row.

        DP-577 (field audit 0924 snappy-front D4). The block honoured the id
        only for a ``hex6`` row, while the geometry and refinement writers
        skipped any volume with that id whatever its shape: an id pointing at
        a plain ``hex`` refinement box left the block alone and silently
        dropped that box's refinement, and an id naming no row changed
        nothing and said nothing. One rule now serves every site, and an id
        it cannot honour is reported rather than ignored.
        """
        selected = self._v('baseGrid/boundingHex6')
        if selected is None:
            return None
        geometry = self._collection_item('geometry', selected)
        if geometry is not None and self._item_value(geometry, 'shape') == 'hex6':
            return selected
        described = ('names no geometry row' if geometry is None else
                     'names a {0} row, not a hex6'.format(
                         self._item_value(geometry, 'shape', 'non-hex6')))
        self.warn(
            'baseGrid.boundingHex6.unresolved',
            'Bounding hex6 {0} {1}; the background block was derived from the '
            'geometry instead.'.format(selected, described),
            field_id='meshing.base_grid.bounding_hex6', requested=selected)
        return None

    def _stood_off_bbox(self):
        """The derived block: the geometry extent pushed out by the standoff.

        DP-576 (field audit 0924 snappy-front D3). ``baseGrid/standoff`` was
        applied only by the desktop window, through a hidden legacy page's
        cached box, so the facade, the CLI and the recipes wrote a flush block
        from the same saved project. Every frontend now passes the geometry
        extent and the standoff is applied here, once.
        """
        if self.bbox is None:
            return self.bbox
        try:
            standoff = float(self._v('baseGrid/standoff', 0.0) or 0.0)
        except (TypeError, ValueError):
            standoff = 0.0
        if standoff <= 0:
            return self.bbox
        from foammesh.core.geometry import BBox
        from foammesh.core.mesh.sizing import stand_off_bounds
        return BBox(*stand_off_bounds(self._bbox_tuple(self.bbox), standoff))

    def _prepared_groups(self) -> tuple[dict, ...]:
        groups = []
        for surface in self.surfaces:
            for item in surface.get('groups', ()):
                if isinstance(item, str):
                    item = {
                        'patch_uuid': item,
                        'native_token': item,
                        'display_name': item,
                        'solver_name': item,
                        'geometry_id': surface.get('geometry_id'),
                    }
                record = dict(item)
                record.setdefault('geometry_id', surface.get('geometry_id'))
                record.setdefault(
                    'solver_name',
                    record.get('display_name') or record.get('native_token') or
                    record.get('patch_uuid'))
                record.setdefault(
                    'display_name',
                    record.get('solver_name') or record.get('native_token'))
                record['source_region'] = self._source_region(record)
                record['source_regions'] = self._source_regions(record)
                groups.append(record)
        return tuple(groups)

    @classmethod
    def _source_regions(cls, group: dict) -> tuple[str, ...]:
        """Every solid name in the staged STL that this group's faces carry.

        R141. A merged boundary covers several imported sub-surfaces and each
        of them is a separate solid in the staged STL, so snappy needs a
        ``regions`` entry for every one of them. Naming only the first is what
        lost the bore on annulus.stl: merging ``wall_shell`` + ``wall_bore``
        into ``wall`` produced a snappyHexMeshDict whose geometry block listed
        ``inlet``, ``outlet``, ``wall_shell`` and never mentioned
        ``wall_bore`` at all, so snappy auto-named the orphan's 1,080 faces
        ``surface_<uuid>_wall_bore`` and neither the wall_ref (1 2)
        refinement nor the 3 boundary layers attached to that wall reached
        the bore -- half a wall, delivered.

        The name pass runs before the index pass so that a name still always
        wins over an index, which is what ``_source_region`` promised.
        """
        refs = [ref for ref in (group.get('source_refs') or ())
                if isinstance(ref, dict)]
        names = [str(ref['original_name']) for ref in refs
                 if ref.get('original_name')]
        if not names:
            names = [f'face{ref["face_index"]}' for ref in refs
                     if isinstance(ref.get('face_index'), int)
                     and not isinstance(ref.get('face_index'), bool)]
        if not names:
            names = [str(group.get('display_name') or
                         group.get('native_token') or group['solver_name'])]
        return tuple(dict.fromkeys(names))

    @classmethod
    def _source_region(cls, group: dict) -> str:
        """The solid name in the staged STL that this group's faces carry.

        The key of the group's entry in snappy's ``regions`` dictionary, so
        it has to be the name the artifact writer gave the solid. That is
        the record's ``original_name`` where the import recorded one (an
        STL solid, a feature-angle piece, a CAD face imported since the
        name was stamped). A CAD record from before that carries only its
        ``face_index``; the writer named its solid ``face<index>``, which is
        the same number for a single body. Only a record with neither falls
        back to the display name, which was the old behaviour for every
        CAD face and never matched a staged solid.

        One name, because this is also how a group is bound to its
        configuration geometry: a merged group that offered every member's
        name would match two geometry rows and be refused as ambiguous. The
        dictionaries themselves take ``_source_regions``.
        """
        return cls._source_regions(group)[0]

    def _group_geometry_bindings(self) -> dict[str, object]:
        """Resolve stable prepared groups onto legacy configuration entities.

        Names are the primary bridge. A deterministic ordinal fallback is
        permitted only when the cardinalities match exactly; ambiguous or
        orphaned assignments fail before dictionary generation.
        """
        groups = list(self._prepared_groups())
        geometries = [
            (key, value) for key, value in self._elements('geometry').items()
            if self._item_value(value, 'gType') == 'surface']
        by_name = {}
        for key, geometry in geometries:
            by_name.setdefault(
                str(self._item_value(geometry, 'name', '')).casefold(),
                []).append((key, geometry))
        bindings = {}
        used = set()
        unmatched = []
        # DP-661. The import writes the prepared group's patch uuid onto
        # the configuration surface row it made, which is an exact bridge;
        # names (``<file>_surface`` against ``<file>``) often are not.
        by_patch = {}
        for key, geometry in geometries:
            uuid = self._item_value(geometry, 'patchUuid')
            if uuid:
                by_patch.setdefault(str(uuid), []).append((key, geometry))
        for group in groups:
            candidates = [
                pair for pair in by_patch.get(str(group.get('patch_uuid')), ())
                if pair[0] not in used]
            if not candidates:
                names = {
                    str(group.get(name) or '').casefold()
                    for name in ('display_name', 'solver_name',
                                 'source_region')
                    if group.get(name)}
                candidates = [
                    pair for name in names for pair in by_name.get(name, ())
                    if pair[0] not in used]
            unique = {str(pair[0]): pair for pair in candidates}
            if len(unique) == 1:
                key, geometry = next(iter(unique.values()))
                bindings[group['solver_name']] = geometry
                used.add(key)
            else:
                unmatched.append(group)
        # A drawn shape (a refinement box, a sphere) has no artifact and so
        # no prepared group; it must not spoil the count the ordinal
        # fallback below depends on (DP-661).
        remaining = [
            (key, geometry) for key, geometry in geometries if key not in used
            and (self._item_value(geometry, 'geometryId') or
                 self._item_value(geometry, 'shape', 'triSurfaceMesh') in
                 (None, 'triSurfaceMesh'))]
        if unmatched and len(unmatched) == len(remaining):
            for group, (key, geometry) in zip(
                    sorted(unmatched, key=lambda item: item['solver_name']),
                    sorted(remaining, key=lambda item: str(item[0]))):
                bindings[group['solver_name']] = geometry
                used.add(key)
            unmatched = []
        if unmatched and geometries:
            raise ValueError(
                'prepared surface groups cannot be mapped unambiguously to '
                'configuration geometry: ' +
                ', '.join(item['solver_name'] for item in unmatched))
        return bindings

    def _surface_groups(self, surface: dict) -> tuple[dict, ...]:
        records = []
        for group in surface.get('groups', ()):
            if isinstance(group, str):
                group = {
                    'patch_uuid': group, 'native_token': group,
                    'display_name': group, 'solver_name': group,
                    'geometry_id': surface.get('geometry_id'),
                }
            item = dict(group)
            item.setdefault('geometry_id', surface.get('geometry_id'))
            item.setdefault(
                'solver_name',
                item.get('display_name') or item.get('native_token') or
                item.get('patch_uuid') or surface['name'])
            item.setdefault('display_name', item['solver_name'])
            item['source_region'] = self._source_region(item)
            item['source_regions'] = self._source_regions(item)
            records.append(item)
        if not records:
            records.append({
                'patch_uuid': surface['name'],
                'native_token': surface['name'],
                'display_name': surface['name'],
                'solver_name': surface['name'],
                'source_region': surface['name'],
                'source_regions': (surface['name'],),
                'geometry_id': surface.get('geometry_id'),
            })
        return tuple(records)

    def _surface_refinement(self, group: dict, bindings: dict):
        geometry = bindings.get(group['solver_name'])
        refinement = self._collection_item(
            'castellation/refinementSurfaces',
            self._item_value(geometry, 'castellationGroup'))
        return geometry, refinement

    def _surface_angle(self, surface: dict) -> float:
        bindings = self._group_geometry_bindings()
        angles = []
        for group in self._surface_groups(surface):
            _, refinement = self._surface_refinement(group, bindings)
            if refinement is not None:
                angles.append(float(
                    self._item_value(refinement, 'includedAngle', 150.0)))
        if not angles and not surface.get('groups'):
            angles = [
                float(self._item_value(item, 'includedAngle', 150.0))
                for item in self._elements(
                    'castellation/refinementSurfaces').values()]
            # Legacy DB-only cases have one combined surface with no stable
            # group map. Keep their historical inclusive policy; prepared
            # geometry never takes this compatibility branch.
            return max(angles) if angles else 150.0
        unique = sorted(set(angles))
        if len(unique) > 1:
            raise ValueError(
                f'Foundation 13 surfaceFeatures applies one includedAngle per '
                f'surface file; split {surface["file"]} into per-group sources '
                f'or use one angle (configured: {unique})')
        return unique[0] if unique else 150.0

    def _surface_feature_level(self, surface: dict) -> int:
        bindings = self._group_geometry_bindings()
        levels = []
        for group in self._surface_groups(surface):
            _, refinement = self._surface_refinement(group, bindings)
            if refinement is not None:
                levels.append(int(self._item_value(
                    refinement, 'featureEdgeRefinementLevel', 0)))
        if not levels and not surface.get('groups'):
            return max((
                int(self._item_value(
                    item, 'featureEdgeRefinementLevel', 0))
                for item in self._elements(
                    'castellation/refinementSurfaces').values()),
                default=0)
        unique = sorted(set(levels))
        if len(unique) > 1:
            raise ValueError(
                f'Foundation 13 explicit feature level is per eMesh file; '
                f'split {surface["file"]} into per-group sources or use one '
                f'level (configured: {unique})')
        return unique[0] if unique else 0

    # dicts ----------------------------------------------------------------
    #: DP-587: fewer background cells than this on an axis is warned.
    _FEW_BACKGROUND_CELLS = 3

    def _background_cell_counts(self, b) -> tuple[int, int, int]:
        mode = self._enum_value(self._v('baseGrid/sizingMode', 'counts'))
        if mode == 'target_size':
            stored = self._v('baseGrid/targetCellSize')
            if stored is None or str(stored).strip() == '':
                target = self._auto_target_cell_size(b)
            else:
                target = float(stored)
            if not math.isfinite(target) or target <= 0:
                raise ValueError('base-grid target cell size must be positive')
            counts = (
                max(1, math.ceil((float(b.xmax) - float(b.xmin)) / target)),
                max(1, math.ceil((float(b.ymax) - float(b.ymin)) / target)),
                max(1, math.ceil((float(b.zmax) - float(b.zmin)) / target)))
            # DP-587 (field audit 0924 shared-and-harness D-SH-08). The size
            # defaults to 1 m whatever the part, so switching to target size
            # on a 0.3 m elbow wrote one cell across it without a word.
            if min(counts) < self._FEW_BACKGROUND_CELLS:
                self.warn(
                    'base_grid.target_cell_size.coarse',
                    f'the base-grid target cell size {target:g} m gives only '
                    f'{counts[0]} × {counts[1]} × {counts[2]} background '
                    f'cells across the domain; lower Target cell size, or '
                    f'use direct cell counts',
                    field_id='meshing.base_grid.target_cell_size',
                    requested=target, applied=list(counts))
            return counts
        return (self._int('baseGrid/numCellsX', 10),
                self._int('baseGrid/numCellsY', 10),
                self._int('baseGrid/numCellsZ', 10))

    def _auto_target_cell_size(self, b) -> float:
        """The base cell an unset ("Auto") target size stands for (DP-669).

        The block's bounding-box diagonal / 40, the rule the Gmsh global size
        follows (DP-614), and said with the number so the manifest shows what
        "Auto" came to.
        """
        from foammesh.core.mesh.sizing import (
            AUTO_CELL_DIAGONAL_DIVISOR, auto_target_cell_size,
        )

        size = auto_target_cell_size(
            (b.xmin, b.xmax, b.ymin, b.ymax, b.zmin, b.zmax))
        if size is None:
            raise ValueError(
                'base-grid target cell size is Auto, and the background '
                'block has no extent to derive it from; type a target cell '
                'size')
        self.warn(
            'base_grid.target_cell_size.auto',
            f'target cell size {size:.4g} m derived from the background '
            f'block diagonal (/ {AUTO_CELL_DIAGONAL_DIVISOR:g}); type a size '
            f'to override it',
            field_id='meshing.base_grid.target_cell_size',
            requested='Auto', applied=size, severity='info')
        return size

    def _derived_background_patches(self) -> tuple:
        """The six faces of the derived box, and who owns each name.

        Plan 31 CP-07 item 3. ``xMin``..``zMax`` are labels this product
        invented for a box it derived from the geometry's bounding box. A user
        may now name a face and say what it is for; when they have not, the
        record says so, so nothing downstream can present a generated label as
        an intended inlet, outlet or wall.
        """
        patches = []
        for label, faces in BOUNDARY_FACES:
            authored_name = str(
                self._v(f'baseGrid/boundaryNames/{label}', '') or '').strip()
            role = str(self._enum_value(
                self._v(f'baseGrid/boundaryCategories/{label}',
                        'unclassified')) or 'unclassified')
            if role == 'unclassified':
                role = ''
            if role and not authored_name:
                raise ValueError(
                    f'the background face {label} is declared a {role} but '
                    f'still carries the name this product generated for it. '
                    f'Name it, so the delivered boundary says whose {role} it '
                    f'is rather than offering {label} as one.')
            patches.append(background_mesh.BoundaryFace(
                name=authored_name or label,
                patch_type=str(self._enum_value(
                    self._v(f'baseGrid/boundaryTypes/{label}', 'patch'))),
                faces=(tuple(int(item) for item in faces.strip('()').split()),),
                group=BACKGROUND_PATCH_GROUP,
                role=role,
                authored=bool(authored_name),
                origin_label=label))
        return tuple(patches)

    def background_topology(self):
        """The background domain as a validated topology.

        Authored blocks win; a project that has none gets the single derived
        box this has always written, with the same numbers rendered the same
        way, so its dictionary is byte-identical.
        """
        scale = self._number('baseGrid/scale', 1)
        authored = background_mesh.from_records(
            lambda name: self._elements(f'baseGrid/{name}'), scale=scale)
        if authored is None:
            b = self._effective_bbox()
            nx, ny, nz = self._background_cell_counts(b)
            # The background block used to be written with these three things
            # hard-coded: unit scale, uniform grading, and every face a plain
            # patch. Each is now a control, and each still defaults to what
            # was hard-coded, so a project that never opens the page writes
            # the same file it wrote before.
            grading = ' '.join(
                self._number(f'baseGrid/grading/{axis}', 1) for axis in 'xyz')
            authored = background_mesh.single_block(
                [[b.xmin, b.ymin, b.zmin], [b.xmax, b.ymin, b.zmin],
                 [b.xmax, b.ymax, b.zmin], [b.xmin, b.ymax, b.zmin],
                 [b.xmin, b.ymin, b.zmax], [b.xmax, b.ymin, b.zmax],
                 [b.xmax, b.ymax, b.zmax], [b.xmin, b.ymax, b.zmax]],
                (nx, ny, nz), grading, self._derived_background_patches(),
                scale)
        return authored.validate()

    def background_boundaries(self) -> tuple[dict, ...]:
        """Every background patch, its declared role, and who named it."""
        try:
            return self.background_topology().ownership()
        except (ValueError, background_mesh.BackgroundMeshError):
            return ()

    def block_mesh_dict(self) -> str:
        # Foundation v13 uses ``scale``. ``convertToMeters`` is retained by
        # other distributions but is not emitted by the v13 writer.
        #
        # R106. The six faces of the background block used to reach the
        # delivered `constant/polyMesh/boundary` as bare `xMin`..`zMax`
        # entries belonging to nothing: the app's own patch-identity check
        # called them "fabricated" and downgraded the whole report, and a user
        # handed the mesh saw a boundary with no owner. Each face now carries
        # the same `patchInfo` shape snappy writes for a meshed surface -- a
        # type and an `inGroups` owner -- so the background domain owns them
        # by name and a solver can address all six at once as `background`.
        return format_dictionary_file(
            'blockMeshDict', self.background_topology().render())

    def _number(self, path, default):
        """A stored number, rendered the way it was typed.

        Values live in the database as strings, so ``0.001`` must not come back
        as ``0.001000000001`` through a float round trip; but an integer-valued
        float should still print as ``1`` rather than ``1.0``, or every default
        dictionary changes the day these controls arrive.
        """
        raw = self._v(path, None)
        if raw is None or str(raw).strip() == '':
            raw = default
        text = str(raw).strip()
        try:
            value = float(text)
        except (TypeError, ValueError):
            return text
        return str(int(value)) if value == int(value) else text

    def surface_features_dict(self) -> str:
        # includedAngle selects edges whose adjacent faces meet at LESS than
        # this angle (v13 semantics; standard 150). It is NOT the snappy
        # resolveFeatureAngle, which is a castellation deviation control —
        # reusing that value (30) extracts zero edges from typical geometry
        # (verified live on v13 with a cube: 30 -> 0 edges, 150 -> 12 edges).
        # Foundation 13 accepts multiple named extraction entries, each with
        # an independent surface list and includedAngle.
        d = {
            item['name']: {
                'surfaces': [f'"{item["file"]}"'],
                'includedAngle': self._surface_angle(item),
            }
            for item in self.surfaces
        }
        # C31-08. A span refinement region reads
        # `<surface>.closeness.internalPointCloseness` MUST_READ, and this
        # utility is the only thing that writes it. The names line up because
        # `surfaceFeatures` builds the field from its own dictionary key
        # (`sFeatFileName + ".closeness"`, surfaceFeatures.C:474-482) and
        # snappy looks it up under the geometry{} entry name -- which is the
        # same string, the staged file stem, in both dictionaries.
        for name, mode in self._span_closeness_surfaces().items():
            entry = d.get(name)
            if entry is None:
                continue
            entry.setdefault('closeness', {})['pointCloseness'] = True
            self.warn(
                'refinement.region.span_closeness',
                f'{name} is refined by {mode}, so surfaceFeatures is asked '
                f'for its point-closeness field as well as its feature edges; '
                f'snappyHexMesh reads that field before it starts and stops '
                f'if it is missing',
                field_id='castellation/refinementVolumes',
                requested=mode, applied='closeness/pointCloseness',
                severity='info')
        self._apply_surface_feature_options(d)
        return format_dictionary_file('surfaceFeaturesDict', d)

    def _apply_surface_feature_options(self, d: dict) -> None:
        """Add the rest of v13's surfaceFeatures keys to every surface entry.

        Plan 31 (``surface_features.rest``). This dictionary carried two keys
        per surface -- the file and the included angle -- and the utility
        reads a dozen more. The ones that matter here are the filters: a
        tessellation with open or non-manifold edges hands snappyHexMesh a
        feature set full of edges that are not features, and there was no way
        to say so.

        The utility reads these per named extraction entry (it iterates the
        sub-dictionaries when the top level has no ``surfaces`` key,
        surfaceFeatures.C:741-761), so they go on each entry rather than at
        the top level, where they would be silently ignored.

        Nothing is written unless it differs from the utility's own default,
        which is what keeps every existing case's dictionary byte-identical.
        """
        if not d:
            return

        def flag(key: str, default: bool) -> bool:
            raw = self._v(f'surfaceFeatures/{key}', default)
            if isinstance(raw, str):
                return raw.strip().lower() in ('true', '1', 'yes', 'on')
            return bool(raw)

        def number(key: str, default: float) -> float:
            try:
                return float(self._v(f'surfaceFeatures/{key}', default))
            except (TypeError, ValueError):
                return float(default)

        shared: dict = {}
        if flag('geometricTestOnly', False):
            shared['geometricTestOnly'] = True

        trim = {}
        min_length = number('trimMinLength', 0)
        min_elements = int(number('trimMinElements', 0))
        if min_length > 0:
            trim['minLen'] = min_length
        if min_elements > 0:
            trim['minElem'] = min_elements
        if trim:
            shared['trimFeatures'] = trim

        subset = {}
        if not flag('keepNonManifoldEdges', True):
            subset['nonManifoldEdges'] = False
        if not flag('keepOpenEdges', True):
            subset['openEdges'] = False
        if subset:
            shared['subsetFeatures'] = subset

        closeness = {}
        if flag('faceCloseness', False):
            closeness['faceCloseness'] = True
        internal = number('internalAngleTolerance', 80)
        external = number('externalAngleTolerance', 80)
        if abs(internal - 80) > 1e-9:
            closeness['internalAngleTolerance'] = internal
        if abs(external - 80) > 1e-9:
            closeness['externalAngleTolerance'] = external

        if flag('featureProximity', False):
            # MUST_READ once the switch is on (surfaceFeatures.C:617): the
            # utility aborts on the missing key rather than choosing a
            # distance, so the pair is written together or not at all.
            shared['featureProximity'] = True
            shared['maxFeatureProximity'] = number('maxFeatureProximity', 1)

        verbose = flag('verboseObj', False)
        if flag('writeObj', False):
            shared['writeObj'] = True
            if verbose:
                shared['verboseObj'] = True
        elif verbose:
            self.warn(
                'surfaceFeatures.verboseObj.orphan',
                'verbose OBJ output was requested without OBJ output, and '
                'surfaceFeatures only writes the verbose files alongside the '
                'plain ones, so nothing extra would be written; the key was '
                'left out',
                field_id='surfaceFeatures/verboseObj',
                requested='true', applied='(no obj output)',
                severity='info')
        if flag('writeVtk', False):
            shared['writeVTK'] = True

        if not shared and not closeness:
            return
        for entry in d.values():
            entry.update(shared)
            if closeness:
                entry.setdefault('closeness', {}).update(closeness)

    #: The quality thresholds both dictionaries carry, in the order
    #: OpenFOAM 13 lists them.
    QUALITY_KEYS = (
        'maxNonOrtho', 'maxBoundarySkewness', 'maxInternalSkewness',
        'maxConcave', 'minVol', 'minTetQuality', 'minVolCollapseRatio',
        'minArea', 'minTwist', 'minDeterminant', 'minFaceWeight',
        'minVolRatio', 'nSmoothScale', 'errorReduction',
    )

    def _quality_controls(self) -> dict:
        """The mesh-quality thresholds, for whichever dictionary wants them.

        Plan 31 (``checkmesh.thresholds_and_region``). This used to be inline
        in :meth:`snappy_hex_mesh_dict` and so was reachable only by the
        mesher. ``checkMesh -meshQuality`` reads the very same keys from
        ``system/meshQualityDict``, and the point of that flag is to judge the
        finished mesh against the limits the mesher was given -- which means
        one source, not a second copy that can drift.
        """
        overrides = {
            key: self._v(f'meshQuality/{key}') for key in self.QUALITY_KEYS}
        quality = mesh_quality_controls(self.target, overrides)
        # The relaxed block takes the same thresholds as the strict one, and
        # layer addition falls back to it after nRelaxedIter. Only maxNonOrtho
        # has ever been written; the rest appear only once someone sets them,
        # so an existing case keeps the mesh it had.
        quality['relaxed'] = {
            'maxNonOrtho': self._v(
                'meshQuality/relaxed/maxNonOrtho',
                quality['relaxed'].get('maxNonOrtho', 75))}
        self._add_optional(
            quality['relaxed'], 'meshQuality/relaxed',
            tuple(key for key in self.QUALITY_KEYS
                  if key not in ('maxNonOrtho', 'nSmoothScale',
                                 'errorReduction')))
        return quality

    def wants_mesh_quality_dict(self) -> bool:
        """Whether the project asked checkMesh to use its own criteria."""
        raw = self._v('meshCheck/userDefinedChecks', False)
        if isinstance(raw, str):
            return raw.strip().lower() in ('true', '1', 'yes', 'on')
        return bool(raw)

    def mesh_quality_dict(self) -> str:
        """``system/meshQualityDict`` -- what ``checkMesh -meshQuality`` reads.

        Plan 31 (``checkmesh.thresholds_and_region``). MEASURED on OpenFOAM 13
        build ``13-58ed5c2046ef``: with this file present, ``checkMesh
        -meshQuality`` prints "Enabling user-defined geometry checks" and
        writes the offending faces to a ``meshQualityFaces`` set; without it
        the flag aborts the run. The keys are the mesher's own, verbatim, so
        the verdict is against the limits snappyHexMesh was asked to hold --
        and the ``relaxed`` sub-dictionary is carried through, which v13
        accepts (measured) and ignores.
        """
        return format_dictionary_file(
            'meshQualityDict', self._quality_controls())

    def _feature_files(self) -> list:
        """Reference the OpenFOAM-generated ``.eMesh`` for explicit feature snapping.

        ``surfaceFeatures`` is stage one of the standard sequence and writes
        ``<surface-stem>.eMesh`` next to the tri-surface; snappy consumes it here
        rather than a VTK-derived ``.obj``. The level is the strongest configured
        feature-edge refinement (0 when none), i.e. capture features without extra
        refinement.
        """
        entries = []
        attached = False
        for item in self.surfaces:
            entry = {'file': f'"{Path(item["file"]).stem}.eMesh"'}
            bands = self._surface_feature_bands(item)
            # C31-11. ``refinementFeatures.C:188-231`` reads ``levels`` when
            # it is present and falls back to the single ``level`` only when
            # it is not, so the two are alternatives rather than a pair.
            if bands:
                attached = True
                entry['levels'] = [[distance, level] for distance, level in bands]
            else:
                entry['level'] = self._surface_feature_level(item)
            entries.append(entry)
        if entries and not attached and self._elements('castellation/featureBands'):
            # Bands exist and none of them reached a file. Silence here would
            # be a ramp the user authored, saw in the table, and never got.
            self.warn(
                'refinement.feature_bands.unattached',
                'feature refinement bands are configured, but none of them '
                'names a surface refinement group that a staged surface '
                'resolves to, so every feature file was written with its '
                'single level instead',
                field_id='castellation/featureBands',
                requested='levels ramp', applied='level',
                severity='warning')
        return entries

    def _feature_band_ramp(self, group_name: str) -> tuple:
        """The ``(distance level)`` ramp authored against *group_name*.

        Returned in the order OpenFOAM 13 demands and validated here rather
        than at the mesher: ``refinementFeatures.C:206-224`` aborts the run
        with a FatalError if distance does not strictly increase or level does
        not fall, and it does so after the dictionary is already on disk.
        """
        if not str(group_name or '').strip():
            return ()
        rows = []
        for band in self._elements('castellation/featureBands').values():
            if str(self._item_value(band, 'groupName', '')) != str(group_name):
                continue
            rows.append((float(self._item_value(band, 'distance', 0.0)),
                         int(self._item_value(band, 'level', 0))))
        rows.sort(key=lambda pair: pair[0])
        for index in range(1, len(rows)):
            if rows[index][0] <= rows[index - 1][0]:
                raise ValueError(
                    f'two feature refinement bands on {group_name} reach the '
                    f'same distance ({rows[index][0]}); OpenFOAM 13 requires '
                    f'each band to reach strictly further than the one before')
            if rows[index][1] > rows[index - 1][1]:
                raise ValueError(
                    f'feature refinement bands on {group_name} refine harder '
                    f'further away (level {rows[index][1]} at '
                    f'{rows[index][0]} after level {rows[index - 1][1]}); '
                    f'OpenFOAM 13 requires the level to fall with distance')
        return tuple(rows)

    def _surface_feature_bands(self, surface: dict) -> tuple:
        """The one ramp every group on this eMesh file agrees on, or ``()``.

        Same rule as :meth:`_surface_feature_level`, and for the same reason:
        ``features`` is a list of *files*, so two groups sharing a file cannot
        ask for two different ramps.

        Unlike the single level, a ramp is never guessed for a surface whose
        prepared groups do not resolve to a refinement row. The level has a
        defensible fallback -- the strongest one configured anywhere, which is
        the safe direction -- but a ramp has no such ordering, and picking one
        group's ramp for another group's edges would refine the wrong part of
        the mesh while looking deliberate. ``_feature_files`` says so out loud
        instead.
        """
        bindings = self._group_geometry_bindings()
        ramps = set()
        for group in self._surface_groups(surface):
            _, refinement = self._surface_refinement(group, bindings)
            if refinement is None:
                continue
            ramps.add(self._feature_band_ramp(
                self._item_value(refinement, 'groupName', '')))
        authored = sorted(ramp for ramp in ramps if ramp)
        if len(authored) > 1:
            raise ValueError(
                f'Foundation 13 reads one levels ramp per eMesh file; split '
                f'{surface["file"]} into per-group sources or use one ramp '
                f'(configured: {authored})')
        return authored[0] if authored else ()

    def interface_pairs(self) -> list[dict]:
        """The enabled ``interfacePairs`` rows, in group-manifest shape.

        F-13. The Geometry page's interface-pair editor had no consumer: the
        rows lived in project state, the non-conformal reader
        (``_apply_non_conformal_pairs``) looked for ``interface_pairs`` in the
        group manifest, and nothing ever put them there, so a pair the user
        authored changed nothing about the mesh. The scope tokens are prepared
        ``patch_uuid`` values -- the facade validates them against the prepared
        group manifest when the row is created -- which is exactly the key the
        reader joins on.
        """
        pairs = []
        for key, item in sorted(self._elements('interfacePairs').items(),
                                key=lambda entry: str(entry[0])):
            if not bool(self._item_value(item, 'enabled', True)):
                continue
            master = str(self._item_value(item, 'masterScopeToken', '') or '')
            slave = str(self._item_value(item, 'slaveScopeToken', '') or '')
            if not master.strip() or not slave.strip():
                continue
            name = str(self._item_value(item, 'name', '') or '').strip()
            pairs.append({
                'pair_id': name or str(key),
                'name': name,
                'coupling': str(
                    self._item_value(item, 'coupling', 'conformal')),
                'transform': str(
                    self._item_value(item, 'transform', 'coincident')),
                'master_scope_id': master.strip(),
                'slave_scope_id': slave.strip(),
            })
        return pairs

    def _fluid_seeds(self) -> tuple[tuple, ...]:
        """Every material point the project defines, in Region page order.

        OpenFOAM Foundation 13 keeps *all* of them. ``refinementParameters.C``
        (v13, lines 81-95) reads ``insidePoints`` as a ``List<point>`` in
        preference to ``insidePoint``, which it reads in preference to the
        ``locationInMesh`` alias; ``outsidePoints``/``outsidePoint`` are the
        matching exclusion keys. The ESI ``locationsInMesh`` key is a different
        feature and does not exist in ``libsnappyHexMesh.so`` at all, so the
        old ``multi_region_seed_truncated`` warning -- which dropped every
        region after the first and justified it with that key's absence -- was
        both lossy and wrong, and is gone.

        The Region page owns the material points, so the current project state
        wins over any value cached in the generation manifest; otherwise a
        stage rerun would silently keep seeding the mesh at the old points.
        """
        try:
            regions = list(self.db.getElements('region').values())
            seeds = tuple(tuple(region.vector('point')) for region in regions)
            if seeds:
                return seeds
        except Exception:
            pass
        if self.prepared_geometry is not None:
            seed = self.prepared_geometry.manifest.get('fluid_seed')
            if seed is not None:
                # DP-582 (field audit 0924 snappy-front D9). The import's
                # suggestion is the geometry's centre -- inside a closed body,
                # which is the solid for an external flow. The launch gate
                # refuses a run with no region, but a preview, an exported
                # dictionary or the CLI wrote this point with no word.
                self.warn(
                    'region.seed.suggested',
                    'No fluid region is defined, so the material point was '
                    "taken from the import's suggestion ("
                    + ', '.join(f'{float(value):g}' for value in seed)
                    + '), the centre of the geometry. For an external flow '
                    'that point is inside the body and snappy keeps the '
                    'wrong side. Add a region on the Region page.',
                    field_id='regions.items',
                    requested='no region',
                    applied=[float(value) for value in seed])
                return (tuple(seed),)
        restored = getattr(self, '_restored_fluid_seed', None)
        return () if restored is None else (tuple(restored),)

    def _fluid_seed(self):
        """The first material point, for the single-valued manifest record.

        Kept as it was -- one point or ``None`` -- because the generation
        manifest records one seed. Only the dictionary writer needs them all.
        """
        seeds = self._fluid_seeds()
        return seeds[0] if seeds else None

    def _owning_volume(self, geometry):
        """The volume element a surface belongs to, or ``None``.

        An imported closed surface arrives as a volume row with the surfaces
        that bound it hanging off it (``geometry_page.py:803-837``), and the
        back-reference is the child's ``volume`` field.
        """
        volume_id = self._item_value(geometry, 'volume')
        if volume_id is None or str(volume_id) == '':
            return None
        elements = self._elements('geometry')
        volume = elements.get(str(volume_id))
        if volume is None:
            volume = elements.get(volume_id)
        if volume is None:
            return None
        kind = self._item_value(volume, 'gType')
        return volume if kind == 'volume' else None

    def _effective_cfd_type(self, geometry) -> tuple:
        """What this surface is for, and the zone it belongs to if it is one.

        DP-387. The CellZone radio in the geometry editor lives on the
        *volume* (``volume_dialog.py:54-57``), because "the cells inside this
        shape" is a question about a volume. The surfaces that bound an
        imported volume are written ``cfdType: boundary`` at import and there
        is no control anywhere that changes that -- ``SurfaceDialog`` offers
        none, boundary and interface and nothing else
        (``surface_dialog.py:50-52``). So the cell-zone branch below, which
        reads the *bound surface's* own ``cfdType``, could not be reached from
        the GUI by any sequence of clicks: MEASURED by building the project
        shape the importer produces -- a ``triSurfaceMesh`` volume typed
        ``cellZone`` with one boundary surface under it -- and generating the
        dictionary, which came out byte-identical to the same project with the
        volume typed ``none``. The radio wrote a value no writer read, and the
        user's request for a cell zone became a plain patch with nothing said.

        A surface that carries its own non-boundary type keeps it; that is the
        interface and internal-face case and it is decided on the surface. A
        plain boundary surface asks the volume it bounds, which is where the
        only control that can answer sits. The returned second value is the
        name the zone takes -- the volume's, because a volume with several
        bounding surfaces is one zone and not one per surface.
        """
        cfd_type = str(self._item_value(geometry, 'cfdType', 'boundary'))
        volume = self._owning_volume(geometry)
        zoned = volume is not None and str(
            self._item_value(volume, 'cfdType', 'none')) == 'cellZone'
        if cfd_type == 'interface':
            # DP-421/DP-422. An interface that bounds a zoned volume is the
            # surface that *carves* that zone -- heatedDuct's
            # ``fluidToMetal.stl`` -- so it needs the zone name here. An
            # interface bounding nothing zoned is the ordinary baffle, and is
            # unchanged.
            return 'interface', (self._zone_name(volume) if zoned else None)
        if cfd_type != 'boundary':
            return cfd_type, None
        if not zoned:
            return cfd_type, None
        if self._carved_by_an_interface(self._item_value(geometry, 'volume')):
            # DP-421. The zone is already carved by the interface surface
            # beside this one, so this surface is the body own outer wall and
            # nothing else. Promoting it too would write that wall into an
            # internal faceZone -- which is what every multiregion snappy mesh
            # on disk did, publishing the six background faces and nothing a
            # solver could put a condition on.
            return 'boundary', None
        return 'cellZone', self._zone_name(volume)

    def _zone_name(self, volume) -> str:
        """The cell zone a volume row stands for, checked as a word."""
        name = str(self._item_value(volume, 'name', '') or '')
        if not name.replace('_', '').replace('.', '').isalnum():
            raise ValueError(
                f'{name!r} is not a valid OpenFOAM cell zone name; a zone '
                f'name is a word, so it may not contain spaces or '
                f'punctuation other than _ and .')
        return name

    def _carved_by_an_interface(self, volume_id) -> bool:
        """Does a surface typed ``interface`` hang off this volume?

        DP-421. That is the shape the geometry split produces and the one v13
        writes in ``multiRegion/CHT/heatedDuct``: the interface carries the
        zone keys, and the faces the body shares with nobody carry a patch.
        Without an interface among its children a zoned volume is the ordinary
        single-body case -- a porous or MRF region inside a larger domain --
        where the whole closed surface is the zone boundary, and that reading
        is left exactly as it was.

        Asked by the volume's *key*, not by the element object: ``_elements``
        rebuilds its dictionary on every call, so two lookups of the same row
        are equal rows and not the same object, and an identity test here
        silently answered "no" for every project.
        """
        if volume_id is None or str(volume_id) == '':
            return False
        wanted = str(volume_id)
        for item in self._elements('geometry').values():
            if str(self._item_value(item, 'gType', '')) != 'surface':
                continue
            owner = self._item_value(item, 'volume')
            if owner is None or str(owner) != wanted:
                continue
            if str(self._item_value(item, 'cfdType', 'boundary')) == 'interface':
                return True
        return False

    def _refinement_surfaces(self) -> dict:
        from foammesh.core.export.poly_mesh_writer import _foam_patch_type

        result: dict[str, dict] = {}
        bindings = self._group_geometry_bindings()
        for surface in self.surfaces:
            groups = self._surface_groups(surface)
            group_entries = {}
            for group in groups:
                geometry, refinement = self._surface_refinement(group, bindings)
                minimum = maximum = 0
                if refinement is not None:
                    levels = self._item_element(refinement, 'surfaceRefinement')
                    minimum = int(self._item_value(
                        levels, 'minimumLevel', 0))
                    maximum = int(self._item_value(
                        levels, 'maximumLevel', minimum))
                if minimum > maximum:
                    # DP-575: OF13 refinementSurfaces.C refuses this at launch
                    # ("Illegal level specification"); say it here, by name.
                    name = self._item_value(refinement, 'groupName') or 'a surface group'
                    raise ValueError(
                        f'surface refinement group {name}: minimum level '
                        f'{minimum} is above maximum level {maximum}')
                entry: dict[str, object] = {'level': [minimum, maximum]}
                # C31-08. Gap refinement, in Foundation 13's own terms. The
                # case-wide `castellation/gapLevelIncrement` was already
                # written into castellatedMeshControls, but v13 reads the same
                # key again on each surface -- and again inside each `regions`
                # entry -- and that per-surface value is the one a user needs:
                # a narrow seal between two walls wants more levels than the
                # farfield does. `refinementSurfaces.C:100-110, 160-168` reads
                # it with `lookupOrDefault(..., gapLevelIncrement)`, so leaving
                # it unset here is exactly "inherit the case value".
                increment = self._item_value(refinement, 'gapLevelIncrement')
                if increment is not None and str(increment).strip() != '':
                    increment = int(increment)
                    if increment < 0:
                        raise ValueError(
                            f'gap level increment for {group["solver_name"]} '
                            f'must not be negative; Foundation 13 rejects a '
                            f'negative levelincrement outright')
                    entry['gapLevelIncrement'] = increment
                # C31-11. Extra refinement where the surface meets the base
                # grid at a shallow angle. ``refinementSurfaces.C:141`` reads
                # it per surface with ``readIfPresent``, and
                # ``snappyRefineDriver.C:1005`` hands it to the baffle removal
                # pass. Unset leaves the key out, so v13 keeps its own
                # ``-great`` sentinel and the pass does nothing.
                self._add_perpendicular_angle(entry, refinement, group)
                cfd_type, zone_name = self._effective_cfd_type(geometry)
                if cfd_type == 'none':
                    entry.update({
                        'faceZone': group['solver_name'],
                        'faceType': 'internal',
                    })
                elif cfd_type == 'interface':
                    non_conformal = bool(
                        self._item_value(geometry, 'nonConformal', False))
                    inter_region = bool(
                        self._item_value(geometry, 'interRegion', False))
                    seed = (None if non_conformal or inter_region
                            else self._interface_seed(geometry, refinement))
                    if zone_name and seed is not None:
                        # DP-421/DP-422, and the shape v13 writes for a
                        # conjugate assembly. The interface is its own surface,
                        # carrying the face zone under its *own* name -- not
                        # once under each region name, which is DP-422 -- and
                        # the cell zone it carves, seeded by a point inside
                        # that region. No ``patchInfo``, and ``faceType`` left
                        # at its default ``internal``: these faces are
                        # internal, which is what makes the mesh conformal
                        # across them.
                        entry.update({
                            'faceZone': group['solver_name'],
                            'cellZone': zone_name,
                        })
                        entry.update(seed)
                    else:
                        entry.update({
                            'faceZone': group['solver_name'],
                            'faceType': (
                                'boundary' if non_conformal or inter_region
                                else 'baffle'),
                            'patchInfo': {'type': 'patch'},
                        })
                elif cfd_type == 'cellZone':
                    # A closed surface asked to become a cell zone. v13 needs
                    # the zone name, the faces that bound it, and which side of
                    # the surface the zone is on -- ``mode``, not the ESI
                    # ``cellZoneInside``. Without this branch the surface fell
                    # through to a plain patch and no zone was created at all,
                    # so a porous or MRF region silently did not exist.
                    entry.update({
                        'cellZone': zone_name or group['solver_name'],
                        'faceZone': group['solver_name'],
                        'faceType': 'internal',
                    })
                    # C31-11. ``mode`` was hard-coded to ``inside``, so the
                    # cell zone could only ever be the volume the surface
                    # encloses. ``surfaceZonesInfo.C:34-40`` names four
                    # algorithms and reads the choice at ``:70-82``; a jacket,
                    # an annulus, or a zone seeded by a point inside an open
                    # surface needs one of the other three.
                    entry.update(self._zone_selection(refinement, group))
                else:
                    # R98. The prepared group already carries the boundary
                    # category the user's naming produced -- wall, inlet,
                    # outlet -- and this line threw it away, so every snappy
                    # patch reached the solver as a plain `patch`. MEASURED
                    # on venturi.stl: `wall_converging` and `wall_diverging`
                    # were both written `type patch` into the exported
                    # constant/polyMesh/boundary, while the app's own
                    # quality/geometry/patch-identity.json recorded
                    # `"category": "wall"` for each of them. In OpenFOAM a
                    # wall and a patch are not interchangeable -- wall
                    # functions, nut conditions and wall distance all key off
                    # the type -- so a viscous run on that mesh is wrong and
                    # nothing about the mesh looks wrong. The MED/Gmsh export
                    # path has mapped categories to OpenFOAM types all along;
                    # this is the same mapping, not a second one.
                    entry['patchInfo'] = {
                        'type': self._snappy_patch_type(group)}
                # C31-11. ``patchInfo`` is handed straight to
                # ``polyPatch::New`` (``meshRefinement.C:1947``), so this is
                # the patch group the meshed patch joins.
                self._add_patch_groups(entry, refinement, group)
                # R141. One entry per solid the group covers, not one per
                # group: a merged wall's second solid was reaching snappy with
                # no refinement entry of its own, so `wall_bore` came out at
                # base level (1,080 faces) against 9,708 for the refined
                # `wall_shell` beside it.
                for region in (group.get('source_regions') or
                               (group['source_region'],)):
                    group_entries[region] = dict(entry)
            if len(group_entries) == 1:
                result[surface['name']] = next(iter(group_entries.values()))
            else:
                result[surface['name']] = {
                    'level': [0, 0],
                    'regions': group_entries,
                }
        # Plan 31. The open primitives refine as surfaces, not as regions.
        result.update(self._open_primitive_refinement_surfaces())
        result.update(self._closed_primitive_refinement_surfaces())
        self._warn_primitive_surface_bindings()
        return result

    #: DP-668. The modelled shapes whose ``<name>_surface`` row a surface
    #: refinement group can refine: each is registered in ``geometry{}`` as a
    #: closed searchable surface (searchableBox, searchableSphere,
    #: searchableCylinder), which OpenFOAM 13 accepts in
    #: ``refinementSurfaces`` exactly as it accepts a triSurface.
    _CLOSED_PRIMITIVE_SHAPES = ('hex', 'sphere', 'cylinder')

    def _bound_closed_primitive_surfaces(self):
        """``(volume_key, volume, surface, group)`` per bound shape surface.

        DP-668. The surface row a box, sphere or cylinder carries, bound to a
        surface refinement group, on a shape the writer registers under its
        volume's name. The bounding Hex6 is the block, never a shape.
        """
        bounding = self._bounding_hex6_key()
        elements = self._elements('geometry')
        for geometry_id, surface in elements.items():
            if self._item_value(surface, 'gType') != 'surface':
                continue
            if self._item_value(surface, 'shape') not in self._CLOSED_PRIMITIVE_SHAPES:
                continue
            group = self._collection_item(
                'castellation/refinementSurfaces',
                self._item_value(surface, 'castellationGroup'))
            if group is None:
                continue
            volume_key = self._item_value(surface, 'volume')
            volume = self._owning_volume(surface)
            if volume is None:
                continue
            if bounding is not None and str(volume_key) == str(bounding):
                continue
            if self._item_value(volume, 'shape') not in self._CLOSED_PRIMITIVE_SHAPES:
                continue
            yield str(volume_key), volume, surface, group

    def _closed_primitive_refinement_surfaces(self) -> dict:
        """``refinementSurfaces`` rows for a box, sphere or cylinder surface.

        DP-668 (supersedes the DP-578 refusal). The picker offers the
        ``<name>_surface`` row a modelled shape carries, and a group bound to
        it promises "refine the cells this shape's surface cuts between these
        two levels". The shape is already a closed searchable surface in
        ``geometry{}`` under the volume's name, so that key takes the group's
        levels here.

        What the faces become: a surface refinement group is a request for
        resolution, so the shape's surface is written as an *internal*
        faceZone named after the surface row -- the mesh conforms to the
        shape and keeps the cells on both sides of it. Written as a plain
        patch instead, snappy would make it a wall and delete everything on
        the far side of it from the location in mesh, carving the shape out
        of the domain; the volume dialog types every shape surface
        ``boundary`` by default, so that would carve every refinement box.
        When the row says ``boundary`` the substitution is said. A row the
        user typed ``interface`` becomes a baffle, as an imported one does.
        """
        result: dict[str, dict] = {}
        for _key, volume, surface, group in self._bound_closed_primitive_surfaces():
            volume_name = str(self._item_value(volume, 'name', f'volume_{_key}'))
            surface_name = str(self._item_value(surface, 'name', f'{volume_name}_surface'))
            group_name = self._item_value(group, 'groupName') or 'a surface group'
            levels = self._item_element(group, 'surfaceRefinement')
            minimum = int(self._item_value(levels, 'minimumLevel', 0))
            maximum = int(self._item_value(levels, 'maximumLevel', minimum))
            if minimum > maximum:
                raise ValueError(
                    f'surface refinement group {group_name}: minimum level '
                    f'{minimum} is above maximum level {maximum}')
            entry: dict[str, object] = {'level': [minimum, maximum]}
            increment = self._item_value(group, 'gapLevelIncrement')
            if increment is not None and str(increment).strip() != '':
                if int(increment) < 0:
                    raise ValueError(
                        f'gap level increment for {surface_name} must not be '
                        f'negative; Foundation 13 rejects a negative '
                        f'levelincrement outright')
                entry['gapLevelIncrement'] = int(increment)
            cfd_type = str(self._item_value(surface, 'cfdType', 'boundary'))
            if cfd_type == 'interface':
                entry.update({'faceZone': surface_name, 'faceType': 'baffle',
                              'patchInfo': {'type': 'patch'}})
            else:
                entry.update({'faceZone': surface_name, 'faceType': 'internal'})
                if cfd_type == 'boundary':
                    self.warn(
                        'refinement.primitive_surface.kept_internal',
                        f'Surface {surface_name} of the modelled '
                        f'{self._item_value(surface, "shape")} {volume_name} '
                        f'is refined at level ({minimum} {maximum}) by group '
                        f'{group_name} and kept as internal faces (faceZone '
                        f'{surface_name}); it is not made a wall, which would '
                        f'cut the shape out of the domain.',
                        field_id='meshing.castellation.surface_refinements',
                        requested='boundary', applied='internal faceZone',
                        severity='info')
            result[volume_name] = entry
        return result

    def _warn_primitive_surface_bindings(self) -> None:
        """Say so when a surface group is bound to a modelled shape's surface.

        DP-578 (field audit 0924 snappy-front D5). Only imported surfaces
        reach ``refinementSurfaces`` through a surface group. The surface row
        the volume dialog makes for a box, sphere or cylinder (or a plane,
        disk, plate or Hex6 face) has no writer, so a group bound to one wrote
        nothing and nothing said so. Entry now refuses the binding; a project
        saved before that is reported here.
        """
        written = {
            str(self._item_value(surface, 'name'))
            for _key, _volume, surface, _group
            in self._bound_closed_primitive_surfaces()}
        for geometry_id, geometry in self._elements('geometry').items():
            if self._item_value(geometry, 'gType') != 'surface':
                continue
            shape = self._item_value(geometry, 'shape')
            if shape in ('triSurfaceMesh', '', None):
                continue
            if str(self._item_value(geometry, 'name')) in written:
                continue  # DP-668: a box, sphere or cylinder surface is written
            group = self._collection_item(
                'castellation/refinementSurfaces',
                self._item_value(geometry, 'castellationGroup'))
            if group is None:
                continue
            name = self._item_value(geometry, 'name', f'surface_{geometry_id}')
            self.warn(
                'refinement.primitive_surface.unwritten',
                f'Surface {name} is the {shape} surface of a modelled shape; '
                f'surface refinement group '
                f'{self._item_value(group, "groupName", "")} refines imported '
                f'surfaces and the surface of a box, sphere or cylinder only, '
                f'so nothing was written for it. Refine the '
                f'shape through a volume refinement group instead.',
                field_id='meshing.castellation.surface_refinements',
                requested=name, applied='not written')

    # -- C31-11: the three per-surface settings v13 reads and we did not --- #

    def _add_perpendicular_angle(self, entry, refinement, group) -> None:
        """Write ``perpendicularAngle``, in radians, when the row asks for one.

        DP-21. The user is asked for degrees -- the field metadata carries
        ``'unit': 'deg'``, the schema range is a half-turn, and the message
        below says so -- but Foundation 13 reads this one key raw.
        ``refinementSurfaces.C:141`` takes it with a plain ``readIfPresent``
        and nothing on the path to ``meshRefinementProblemCells.C:252-256``
        applies ``degToRad``; the comparison there is
        ``mag(n & nearestNormal) < Foam::sin(angle)``. Every other angle v13
        reads -- ``resolveFeatureAngle``, ``planarAngle``, ``featureAngle``,
        ``slipFeatureAngle``, ``minMedialAxisAngle``, ``includedAngle`` --
        goes through ``lookup<scalar>(..., unitDegrees)``. This one does not,
        so the conversion has to happen here.

        Written unconverted, the documented value ``10`` is silently inert:
        ``sin(10 rad)`` is -0.544 and no magnitude is below a negative
        number. Converted, ``10`` is ``sin(10 deg) = 0.174`` -- a shallow
        threshold that removes cells, which is what the label promises.

        The range stays 0..180 for the reason recorded in the schema: the
        control saturates at 90 degrees (``sin`` = 1, every face admitted)
        and 90..180 mirrors 0..90, so nothing in the shipped range is
        undefined and no stored project has to be rewritten.
        """
        angle = self._item_value(refinement, 'perpendicularAngle')
        if angle is None or str(angle).strip() == '':
            return
        angle = float(angle)
        if not 0.0 <= angle <= 180.0:
            raise ValueError(
                f'perpendicular angle for {group["solver_name"]} must be '
                f'between 0 and 180 degrees; it is compared against the angle '
                f'between the surface normal and the base-grid direction')
        entry['perpendicularAngle'] = math.radians(angle)

    def _add_patch_groups(self, entry, refinement, group) -> None:
        """Write ``patchInfo/inGroups`` when the row names any groups.

        The names are whitespace- or comma-separated because a patch group is
        a ``word``: it may not contain either, so both are unambiguous
        separators and a user typing one list or the other gets the same
        result.
        """
        names = self._patch_group_names(refinement)
        if not names:
            return
        patch_info = entry.get('patchInfo')
        if not isinstance(patch_info, dict):
            # A faceZone-only entry creates no patch, so there is nothing for
            # a patch group to name. Saying so beats writing a key into a
            # dictionary snappy will never look at.
            self.warn(
                'refinement.surface.in_groups_unused',
                f'{group["solver_name"]} names the '
                f'{agreeing(len(names), "patch group")} '
                f'{" ".join(names)}, but it is written as an internal face '
                f'zone rather than a patch, so snappyHexMesh creates no patch '
                f'for the group to hold',
                field_id='castellation/refinementSurfaces',
                requested=' '.join(names), applied='(no patch created)',
                severity='warning')
            return
        patch_info['inGroups'] = list(names)

    def _patch_group_names(self, refinement) -> tuple:
        raw = self._item_value(refinement, 'patchGroups', '')
        text = str(raw or '').replace(',', ' ')
        names = tuple(dict.fromkeys(part for part in text.split() if part))
        for name in names:
            if not name.replace('_', '').replace('.', '').isalnum():
                raise ValueError(
                    f'{name!r} is not a valid OpenFOAM patch group name; a '
                    f'group name is a word, so it may not contain spaces or '
                    f'punctuation other than _ and .')
        return names

    #: ``surfaceZonesInfo.C:34-40`` -- the four names v13 registers, and the
    #: only four this writer may emit.
    _ZONE_MODES = ('inside', 'outside', 'insidePoint', 'none')

    def _interface_seed(self, geometry, refinement):
        """``mode insidePoint`` and the seed, for an interface that carves.

        DP-421. ``mode inside`` means "the volume this surface encloses", and
        an interface encloses nothing -- it is a patch between two bodies, and
        it is open. ``surfaceZonesInfo.C:79-82`` makes the point mandatory
        under ``insidePoint``, so the mode is not a choice here and the seed
        has to come from somewhere: the volume the interface bounds records
        one when the geometry split that produced this surface found it, and
        the refinement group ``zoneInsidePoint`` answers for a user who placed
        it by hand.

        ``None`` when neither said anything, and that answer is the whole
        reason this returns rather than raises. An ``interface`` on a zoned
        volume with no seed anywhere is an older project, or a hand-typed
        baffle inside a porous region, and DP-387 pinned what it has always
        produced: ``faceType baffle`` and a patch. Carving is the new reading
        and it needs a fact the old shape does not carry, so the absence of
        that fact is what tells the two apart. Nothing that meshes today
        changes; only a project that records a seed gets the v13 shape.
        """
        volume = self._owning_volume(geometry)
        point = None
        if volume is not None and bool(
                self._item_value(volume, 'zoneInsidePointSet', False)):
            point = self._item_element(volume, 'zoneInsidePoint')
        if point is None:
            hand = self._item_element(refinement, 'zoneInsidePoint')
            if hand is not None and any(
                    float(self._item_value(hand, axis, 0.0))
                    for axis in ('x', 'y', 'z')):
                point = hand
        if point is None:
            return None
        coordinates = [float(self._item_value(point, axis, 0.0))
                       for axis in ('x', 'y', 'z')]
        if not all(math.isfinite(value) for value in coordinates):
            raise ValueError(
                f'the inside point for the interface '
                f'{self._item_value(geometry, "name", "")!r} is not a finite '
                f'coordinate, so OpenFOAM 13 cannot say which side of it the '
                f'cell zone lies on')
        return {'mode': 'insidePoint', 'insidePoint': coordinates}

    def _zone_selection(self, refinement, group) -> dict:
        """``mode`` (and ``insidePoint`` when it needs one) for a cell zone."""
        mode = str(self._enum_value(
            self._item_value(refinement, 'zoneMode', 'inside')) or 'inside')
        if mode not in self._ZONE_MODES:
            raise ValueError(
                f'{mode!r} is not a zone selection method OpenFOAM 13 knows; '
                f'surfaceZonesInfo registers {self._ZONE_MODES}')
        selection: dict = {'mode': mode}
        if mode == 'insidePoint':
            point = self._item_element(refinement, 'zoneInsidePoint')
            coordinates = [float(self._item_value(point, axis, 0.0))
                           for axis in ('x', 'y', 'z')]
            if not all(math.isfinite(value) for value in coordinates):
                raise ValueError(
                    f'the inside point for {group["solver_name"]} is not a '
                    f'finite coordinate')
            selection['insidePoint'] = coordinates
        return selection

    #: OpenFOAM patch types whose validity is a property of the *mesh*, not
    #: of the surface that names them. `wedge` needs an opposing pair of
    #: planar faces spanning a small angle about a common axis on a mesh one
    #: cell thick; `empty` needs the same one-cell-thick front and back.
    #: snappyHexMesh castellates and snaps a three-dimensional hex mesh and
    #: cannot produce either, so neither type can ever be honoured here.
    _MESH_CONSTRAINED_PATCH_TYPES = ('wedge', 'empty')

    def _snappy_patch_type(self, group) -> str:
        """The OpenFOAM type this snappy region may safely declare.

        The category behind it is *inferred from the patch name* -- the
        leading word decides (``core/geometry/patches/ops.py``). That is a
        fine way to reach `wall` or `inlet`, and a dangerous way to reach a
        type whose contract is geometric.

        MEASURED on the `wedge` fixture imported as STEP. The strict GUI
        harness names imported surfaces `<model>_wall<n>`, so the seven walls
        of that model came out `wedge_wall1 .. wedge_wall7`; the leading word
        `wedge` is a category, so all seven were written
        `patchInfo { type wedge; }`. snappyHexMesh died with SIGFPE inside
        `Foam::wedgePolyPatch::calcGeometry` during feature refinement --
        the wedge patch divides by an axis length that is zero for a wall
        that is not half of an axisymmetric pair. Seven wedges cannot be a
        pair in any case.

        So a name may not select a mesh-constrained type. Such a region is
        written `wall`, which is what these surfaces are, and the substitution
        is warned about rather than made silently: a user who really wants an
        axisymmetric sector needs blockMesh, not snappyHexMesh, and needs to
        be told so.
        """
        from foammesh.core.export.poly_mesh_writer import _foam_patch_type

        category = group.get('category')
        patch_type = _foam_patch_type(category)
        if patch_type not in self._MESH_CONSTRAINED_PATCH_TYPES:
            return patch_type
        name = str(group.get('solver_name') or group.get('name') or '?')
        self.warn(
            'snappy_patch_type_unmeshable',
            f'{name} is named for the {patch_type} category, but '
            f'snappyHexMesh cannot build a {patch_type} patch: that type '
            f'describes a one-cell-thick mesh this pipeline never produces, '
            f'and OpenFOAM aborts on it. It is written as a wall. Build an '
            f'axisymmetric sector with blockMesh, or rename the surface so '
            f'it does not lead with "{patch_type}".',
            requested=patch_type, applied='wall')
        return 'wall'

    def _record_layer_patch(self, name, layer, result, relative_modes,
                            frozen) -> None:
        """Place one patch into ``layers {}`` -- or deliberately leave it out.

        F-39. v13 reads three states off this block and the writer could only
        ever express one. ``inherit`` omits the patch, which is what makes it
        slide with its neighbours during layer addition; ``freeze`` mentions
        it with ``nSurfaceLayers 0``, which stops both the sliding and the
        extrusion; ``grow`` writes the count and the thicknesses.
        """
        if self._layer_selects_by_pattern(layer):
            # C31-11. This row names its patches by expression, so the
            # geometry binding that led us here says nothing about which
            # patches it covers. _record_layer_patterns writes it once,
            # under its quoted key; writing it here as well would put the
            # same group in the dictionary twice under two different keys.
            return
        values = self._canonical_layer_values(layer)
        if values is None:                       # inherit: not mentioned
            return
        if not values.get('nSurfaceLayers'):     # freeze: mentioned as zero
            frozen.add(name)
            result[name] = values
            return
        relative_modes.add(values.pop('relativeSizes'))
        result[name] = values

    def _layer_selects_by_pattern(self, layer) -> bool:
        """Whether this layer group names its patches by expression."""
        return str(self._item_value(
            layer, 'patchSelector', 'geometry')) == 'pattern'

    def known_patch_names(self) -> tuple[str, ...]:
        """Every patch name this case is expected to produce.

        Used to say what a pattern currently matches -- in the editor's match
        preview, and here to keep :meth:`frozen_layer_patches` honest about a
        frozen pattern. It is a prediction, not a reading of
        ``constant/polyMesh/boundary``: the mesh does not exist yet when the
        dictionary is written, and a preview that could only speak after the
        run would be no use while the pattern is being typed.
        """
        names: list[str] = []
        for group in self._prepared_groups():
            name = str(group.get('solver_name') or '').strip()
            if name and name not in names:
                names.append(name)
        if not names:
            for geometry in self._elements('geometry').values():
                name = str(self._item_value(geometry, 'name', '') or '').strip()
                if name and name not in names:
                    names.append(name)
        for label, _faces in BOUNDARY_FACES:
            if label not in names:
                names.append(label)
        if BACKGROUND_PATCH_GROUP not in names:
            names.append(BACKGROUND_PATCH_GROUP)
        return tuple(names)

    def _record_layer_patterns(self, result, relative_modes, frozen) -> None:
        """Write one ``"pattern"`` key per layer group that asks for one.

        ``layerParameters.C:265-282`` builds a ``wordRe`` from every key in
        ``layers`` and asks ``boundaryMesh.patchSet`` which patches it names,
        so a quoted key is a regular expression and an unquoted one is a
        literal patch name. When a pattern matches nothing v13 only issues an
        IOWarning and meshes on with no layers there, which is easy to miss in
        a long log -- so this warns at write time, while the user is still
        looking at the case.
        """
        # DP-492. Which patches each quoted key named when it was written, so
        # a request can be reported against the patch names snappy prints.
        self._layer_pattern_patches = {}
        for layer in self._elements('addLayers/layers').values():
            if not self._layer_selects_by_pattern(layer):
                continue
            pattern = str(self._item_value(layer, 'patchPattern', '') or '')
            name = str(self._item_value(layer, 'groupName', 'layers') or 'layers')
            try:
                key = quoted_key(pattern)
                matched = matching_patches(pattern, self.known_patch_names())
            except PatternError as error:
                raise ValueError(
                    f'the layer group {name} matches patches by pattern, but '
                    f'{error}') from error
            if key in result:
                raise ValueError(
                    f'two layer groups both match patches with the pattern '
                    f'{pattern}; OpenFOAM 13 reads one entry per key, so the '
                    f'second would silently replace the first')
            if not matched:
                self.warn(
                    'layers.pattern.matches_nothing',
                    f'the layer group {name} matches patches with '
                    f'{pattern!r}, which currently matches no patch '
                    f'({", ".join(self.known_patch_names())}); '
                    f'snappyHexMesh will add no layers for it',
                    field_id='addLayers/layers',
                    requested=pattern, applied='(no patch matched)',
                    severity='warning')
            values = self._canonical_layer_values(layer)
            if values is None:                   # inherit: not mentioned
                continue
            self._layer_pattern_patches[key] = matched
            if not values.get('nSurfaceLayers'):  # freeze: mentioned as zero
                # Record the names, not the pattern: the achieved-layer report
                # is keyed on the patch names checkMesh reports back.
                frozen.update(matched)
                result[key] = values
                continue
            relative_modes.add(values.pop('relativeSizes'))
            result[key] = values

    def _layer_surfaces(self) -> dict:
        result: dict[str, dict] = {}
        frozen: set[str] = set()
        if not any(surface.get('groups') for surface in self.surfaces):
            relative_modes = set()
            for geometry in self._elements('geometry').values():
                layer_id = self._item_value(geometry, 'layerGroup')
                slave_id = self._item_value(geometry, 'slaveLayerGroup')
                selected_id = layer_id if layer_id is not None else slave_id
                layer = self._collection_item('addLayers/layers', selected_id)
                if layer is None:
                    continue
                name = str(self._item_value(geometry, 'name', 'patch'))
                if slave_id is not None and layer_id is None:
                    name = f'{name}_slave'
                self._record_layer_patch(
                    name, layer, result, relative_modes, frozen)
            self._record_layer_patterns(result, relative_modes, frozen)
            if len(relative_modes) > 1:
                raise ValueError(
                    'Foundation 13 applies relativeSizes globally; all active '
                    'layer groups must use the same relativeSizes value')
            self._layer_relative_sizes = (
                next(iter(relative_modes)) if relative_modes else True)
            self._frozen_layer_patches = frozen
            return result
        bindings = self._group_geometry_bindings()
        relative_modes = set()
        for group in self._prepared_groups():
            geometry = bindings.get(group['solver_name'])
            layer_id = self._item_value(geometry, 'layerGroup')
            slave_id = self._item_value(geometry, 'slaveLayerGroup')
            selected_id = layer_id if layer_id is not None else slave_id
            layer = self._collection_item('addLayers/layers', selected_id)
            if layer is None:
                continue
            patch_name = group['solver_name']
            if slave_id is not None and layer_id is None:
                patch_name = f'{patch_name}_slave'
            self._record_layer_patch(
                patch_name, layer, result, relative_modes, frozen)
        self._record_layer_patterns(result, relative_modes, frozen)
        if len(relative_modes) > 1:
            raise ValueError(
                'Foundation 13 applies relativeSizes globally; all active '
                'layer groups must use the same relativeSizes value')
        self._layer_relative_sizes = (
            next(iter(relative_modes)) if relative_modes else True)
        self._frozen_layer_patches = frozen
        return result

    def frozen_layer_patches(self) -> set:
        """Patches written as ``nSurfaceLayers 0`` by the last dictionary.

        The achieved-layer report otherwise reads a frozen patch as a layer
        addition that failed, which is the opposite of what was asked for.
        """
        if not hasattr(self, '_frozen_layer_patches'):
            self._layer_surfaces()
        return set(getattr(self, '_frozen_layer_patches', ()))

    def requested_layer_counts(self) -> dict[str, int]:
        """Map each patch the last dictionary layers to its ``nSurfaceLayers``.

        DP-492. MEASURED on the audit's S2 case: a group matching
        ``box_inner`` by pattern asked for 3 layers, and the recorded coverage
        read "not recorded" for box_inner, because the request was keyed by
        the dictionary key ``"box_inner"`` while the report looks it up by the
        patch name snappy prints. A quoted key is expanded here to the patches
        it matched when it was written. v13 resolves the keys the same way
        (``layerParameters.C:267-292``): it walks ``layers`` in order and sets
        every patch each key matches, so where two keys match one patch the
        later one's count is the one the mesher uses.
        """
        counts: dict[str, int] = {}
        patterns = None
        for key, entry in (self._layer_surfaces() or {}).items():
            if patterns is None:
                patterns = dict(getattr(self, '_layer_pattern_patches', {}))
            count = int(entry.get('nSurfaceLayers') or 0)
            for name in patterns.get(key, (key,)):
                counts[str(name)] = count
        return counts

    def _canonical_layer_values(self, layer) -> dict | None:
        """Convert every UI thickness model to v13's first+expansion pair.

        Foundation 13 selects one global two-parameter model and permits
        per-patch overrides of those same parameters. Canonicalising all
        groups to first-layer thickness plus expansion ratio preserves mixed
        human input models without emitting unsupported ``thicknessModel``.

        Returns ``None`` for an ``inherit`` group -- the caller must leave the
        patch out of ``layers {}`` -- and a bare ``nSurfaceLayers 0`` for a
        frozen one. Zero layers carry no thickness: v13 reads the key as
        "disable any mesh shrinking and layer addition on any point of this
        patch" (annotated snappyHexMeshDict lines 394-399), so a first-layer
        thickness beside it would describe a stack that is not extruded.
        """
        policy = str(self._item_value(layer, 'layerPolicy', 'grow'))
        if policy == 'inherit':
            return None
        if policy == 'freeze':
            return {'nSurfaceLayers': 0}
        model = str(self._item_value(
            layer, 'thicknessModel', 'finalAndExpansion'))
        count = int(self._item_value(layer, 'nSurfaceLayers', 1))
        if count < 1:
            return {'nSurfaceLayers': 0}
        first = float(self._item_value(layer, 'firstLayerThickness', 0.3))
        final = float(self._item_value(layer, 'finalLayerThickness', 0.5))
        total = float(self._item_value(layer, 'thickness', 0.5))
        ratio = float(self._item_value(layer, 'expansionRatio', 1.2))
        minimum = float(self._item_value(layer, 'minThickness', 0.3))
        if model == 'firstAndOverall':
            ratio = self._expansion_from_first_total(first, total, count)
        elif model == 'firstAndExpansion':
            pass
        elif model == 'finalAndOverall':
            ratio = self._expansion_from_final_total(final, total, count)
            first = final / (ratio ** max(0, count - 1))
        elif model == 'finalAndExpansion':
            if ratio <= 0:
                raise ValueError('layer expansion ratio must be positive')
            first = final / (ratio ** max(0, count - 1))
        elif model == 'overallAndExpansion':
            if ratio <= 0:
                raise ValueError('layer expansion ratio must be positive')
            denominator = (
                count if abs(ratio - 1.0) < 1e-12
                else (ratio ** count - 1.0) / (ratio - 1.0))
            first = total / denominator
        elif model == 'firstAndRelativeFinal':
            if count == 1:
                ratio = 1.0
            else:
                if first <= 0 or final <= 0:
                    raise ValueError(
                        'first and final layer thickness must be positive')
                ratio = (final / first) ** (1.0 / (count - 1))
        else:
            raise ValueError(f'unknown layer thickness model: {model}')
        for name, value in (
                ('firstLayerThickness', first),
                ('expansionRatio', ratio),
                ('minThickness', minimum)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f'{name} must be positive and finite')
        return {
            'nSurfaceLayers': count,
            'firstLayerThickness': first,
            'expansionRatio': ratio,
            'minThickness': minimum,
            'relativeSizes': bool(
                self._item_value(layer, 'relativeSizes', True)),
        }

    @staticmethod
    def _expansion_from_first_total(
            first: float, total: float, count: int) -> float:
        from foammesh.core.meshing.layers import expansion_for_first_and_overall
        return float(expansion_for_first_and_overall(first, total, count))

    @staticmethod
    def _expansion_from_final_total(
            final: float, total: float, count: int) -> float:
        if min(final, total) <= 0 or count < 1:
            raise ValueError(
                'final, overall must be positive and layer count non-zero')
        if count == 1:
            if not math.isclose(final, total, rel_tol=1e-8, abs_tol=1e-12):
                raise ValueError(
                    'one layer requires final thickness equal overall thickness')
            return 1.0

        def stack(ratio):
            first = final / ratio ** (count - 1)
            if math.isclose(ratio, 1.0, abs_tol=1e-12):
                return first * count
            return first * (ratio ** count - 1.0) / (ratio - 1.0)

        lo, hi = 0.01, 100.0
        lower, upper = stack(lo), stack(hi)
        if not min(lower, upper) <= total <= max(lower, upper):
            raise ValueError(
                'final and overall thickness do not define a valid layer stack')
        increasing = upper > lower
        for _ in range(240):
            mid = (lo + hi) / 2.0
            value = stack(mid)
            if math.isclose(value, total, rel_tol=1e-10, abs_tol=1e-12):
                return mid
            if (value < total) == increasing:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    #: Plan 31. The optional per-entry keys OpenFOAM 13 reads out of a
    #: ``geometry`` entry, in the order the annotated dictionary lists them.
    #: MEASURED on OpenFOAM 13: ``tolerance`` and ``maxTreeDepth`` are read by
    #: ``triSurfaceSearch.C:154,160`` -- the search object
    #: ``triSurface_searchableSurface.C:330`` builds from this same dict --
    #: and ``scale``/``minQuality`` by
    #: ``triSurface_searchableSurface.C:347,359``. All four are
    #: ``readIfPresent``, so an unwritten key is the OpenFOAM default and the
    #: dictionary is unchanged.
    _SURFACE_SEARCH_OPTIONS = ('tolerance', 'maxTreeDepth', 'scale',
                               'minQuality')

    #: Suffix for the raw surface a ``withGaps`` entry wraps. The wrapper
    #: keeps the name, because ``refinementSurfaces``, ``refinementRegions``
    #: and the patch-identity sidecar all address a surface by it.
    _GAP_BASE_SUFFIX = '_gapBase'

    def _tri_surface_type(self) -> str:
        """The searchable-surface class the staged surfaces are declared as.

        F-18. ``triSurface`` is the type name the OpenFOAM 13 class declares
        and the name every shipped dictionary uses; ``triSurfaceMesh`` is a
        compatibility entry in the run-time selection table that constructs
        the same surface.

        Plan 31 adds the second answer. ``closedTriSurface`` is the same
        surface with ``hasVolumeType()`` forced true
        (``closedTriSurface.H:112-115``), which is the only thing that lets a
        surface with pinholes serve an inside/outside refinement region
        (``refinementRegions.C:59,117``) or a ``zoneInside`` cellZone
        (``surfaceZonesInfo.C:96``) instead of being warned about in a log and
        dropped. The user has to assert it, because OpenFOAM will not: for a
        genuinely watertight surface ``triSurface::hasVolumeType()`` already
        returns true on its own (``triSurface_searchableSurface.C:637-651``),
        so this switch only ever matters for the imperfect case.
        """
        declaration = self._enum_value(
            self._v('snappyGeometry/triSurfaceDeclaration', 'asImported'))
        return ('closedTriSurface' if declaration == 'assumeClosed'
                else 'triSurface')

    def _surface_search_options(self) -> dict:
        """The search options the user set, and only those."""
        options: dict[str, object] = {}
        self._add_optional(options, 'snappyGeometry',
                           self._SURFACE_SEARCH_OPTIONS)
        return options

    def _gap_width(self):
        """The ``withGaps`` gap, or ``None`` when gap detection is off.

        MEASURED on OpenFOAM 13, ``withGaps_searchableSurface.C:197,200``: a
        ``withGaps`` entry reads ``gap`` (a length) and ``surface`` (the name
        of another entry) and reports a pierce as a hit only when two probes
        offset by the gap both hit. That is what stops snappyHexMesh
        threading cells through a louvre slat or a door seal that the base
        grid is too coarse to see.
        """
        if not self._bool('snappyGeometry/gapDetection'):
            return None
        gap = self._v('snappyGeometry/gapWidth')
        if gap is None or str(gap).strip() == '':
            return None
        try:
            width = float(gap)
        except (TypeError, ValueError):
            return None
        return width if width > 0 else None

    def _geometry_dictionary(self) -> dict:
        data: dict[str, dict] = {}
        surface_type = self._tri_surface_type()
        search_options = self._surface_search_options()
        gap = self._gap_width()
        for surface in self.surfaces:
            entry: dict[str, object] = {
                'type': surface_type,
                'file': f'"{surface["file"]}"',
            }
            entry.update(search_options)
            if surface.get('groups'):
                groups = self._surface_groups(surface)
                # R141. Every solid a group covers is renamed onto that
                # group's patch. A merged boundary listed only its first
                # member here, so the second solid had no entry, kept its own
                # name, and snappy published it as a patch the user never
                # named -- `surface_<uuid>_wall_bore`, 1,080 faces, beside the
                # `wall` that was supposed to contain it.
                entry['regions'] = {
                    region: {'name': group['solver_name']}
                    for group in groups
                    for region in (group.get('source_regions') or
                                   (group['source_region'],))
                }
            if gap is None:
                data[surface['name']] = entry
            else:
                # Plan 31. The wrapper takes the name, and the raw surface is
                # defined *before* it: ``searchableSurfaceList.C:90-120``
                # constructs the entries in dictionary order and registers
                # each under its key, and ``withGaps`` resolves ``surface`` by
                # ``io.db().lookupObject`` in its own constructor
                # (``withGaps_searchableSurface.C:200-203``), so a wrapper
                # written first would look up a surface that does not exist
                # yet. ``regions`` stays on the wrapper because
                # ``withGaps::regions()`` forwards to the wrapped surface
                # (``withGaps_searchableSurface.H:130-139``), so the patch names
                # resolve there and nothing downstream has to know a wrapper
                # is in the way.
                base = f'{surface["name"]}{self._GAP_BASE_SUFFIX}'
                while base in data:
                    base += '_'
                base_entry = dict(entry)
                base_entry.pop('regions', None)
                data[base] = base_entry
                wrapper: dict[str, object] = {
                    'type': 'withGaps',
                    'gap': gap,
                    'surface': base,
                }
                if 'regions' in entry:
                    wrapper['regions'] = entry['regions']
                data[surface['name']] = wrapper

        bounding = self._bounding_hex6_key()
        # DP-668: a shape whose surface a surface group refines is needed in
        # ``geometry{}`` even when the volume itself asks for nothing.
        surface_bound = {key for key, *_rest
                         in self._bound_closed_primitive_surfaces()}
        for geometry_id, geometry in self._elements('geometry').items():
            if self._item_value(geometry, 'gType') != 'volume':
                continue
            group_id = self._item_value(geometry, 'castellationGroup')
            cfd_type = self._item_value(geometry, 'cfdType', 'none')
            if (group_id is None and cfd_type == 'none'
                    and str(geometry_id) not in surface_bound):
                continue
            if bounding is not None and str(geometry_id) == str(bounding):
                continue  # DP-577: only a real hex6 is taken as the block
            name = str(self._item_value(
                geometry, 'name', f'volume_{geometry_id}'))
            shape = self._item_value(geometry, 'shape')
            try:
                p1 = list(geometry.vector('point1'))
                p2 = list(geometry.vector('point2'))
            except Exception:
                p1 = p2 = None
            if shape in {'hex', 'hex6'} and p1 is not None:
                data[name] = {
                    'type': 'searchableBox',
                    'min': p1,
                    'max': p2,
                }
            elif shape == 'sphere' and p1 is not None:
                data[name] = {
                    'type': 'searchableSphere',
                    'centre': p1,
                    'radius': self._item_value(geometry, 'radius', 1.0),
                }
            elif shape == 'cylinder' and p1 is not None:
                data[name] = {
                    'type': 'searchableCylinder',
                    'point1': p1,
                    'point2': p2,
                    'radius': self._item_value(geometry, 'radius', 1.0),
                }
            elif shape in self._OPEN_PRIMITIVE_SHAPES and p1 is not None:
                data[name] = self._open_primitive_entry(geometry, shape, p1, p2)
        return data

    #: Plan 31. The three OpenFOAM 13 searchable surfaces that describe an
    #: open surface rather than a closed volume. None of them overrides
    #: ``hasVolumeType()``, so the base ``searchableSurface`` false stands and
    #: an inside/outside refinement region built on one is warned about and
    #: dropped (``refinementRegions.C:59,117``). They are refinement
    #: *surfaces*, and ``_open_primitive_refinement_surfaces`` is where they
    #: go.
    _OPEN_PRIMITIVE_SHAPES = ('plane', 'disk', 'plate')

    def _open_primitive_entry(self, geometry, shape: str, p1, p2) -> dict:
        """One ``geometry`` entry for a plane, disk or plate.

        The keys are OpenFOAM 13's, read out of its source rather than out of
        documentation:

        * ``plane`` -- ``planeType``/``point``/``normal``, read by
          ``Foam::plane(dict)`` at ``plane.C:123-146``. ``pointAndNormal`` is
          the only one of the three plane types (the others being
          ``planeEquation`` and ``embeddedPoints``) that this program's two
          stored vectors can express, so it is written explicitly rather than
          left to a default that does not exist -- ``planeType`` is a plain
          ``lookup`` and its absence is a FatalError.
        * ``disk`` -- ``origin``/``normal``/``radius``,
          ``disk_searchableSurface.C:179-181``.
        * ``plate`` -- ``origin``/``span``, ``plate_searchableSurface.C:257``.
          v13 requires exactly one zero component in the span, which names the
          plate's normal direction; that is checked before writing, because
          the alternative is a FatalError inside a meshing run.

        ``point1`` is the origin in every case and ``point2`` carries the
        normal or the span, which is why the two shapes share one stored pair
        with the box and the cylinder.
        """
        if shape == 'plane':
            return {
                'type': 'plane',
                'planeType': 'pointAndNormal',
                'point': p1,
                'normal': p2,
            }
        if shape == 'disk':
            return {
                'type': 'disk',
                'origin': p1,
                'normal': p2,
                'radius': self._item_value(geometry, 'radius', 1.0),
            }
        name = str(self._item_value(geometry, 'name', 'plate'))
        span = [float(value) for value in p2]
        # MEASURED on OpenFOAM 13, ``plate_searchableSurface.C:59-85``:
        # ``calcNormal`` demands "two positive and one zero entry" and calls
        # FatalError on a negative component or on a second zero one. A
        # FatalError inside a meshing run is a dead stage with the reason in a
        # log; said here, it is still something the user can fix.
        if sum(1 for value in span if value == 0.0) != 1 or min(span) < 0.0:
            self.warn(
                'geometry.plate.span_not_planar',
                f'the plate {name} has a span of {span}: OpenFOAM needs two '
                f'positive entries and exactly one zero one — the zero names '
                f'the direction the plate faces — and refuses to build the '
                f'surface otherwise',
                field_id='geometry', requested=span, severity='error')
        return {
            'type': 'plate',
            'origin': p1,
            'span': p2,
        }

    #: The four ``refinementRegions`` modes that ask a shell which side of
    #: itself a point is on. MEASURED on OpenFOAM 13,
    #: ``refinementRegions.C:54-70`` and ``:110-125``: each of the four tests
    #: ``hasVolumeType()`` and warns "Shell .. is not closed so testing for
    #: '<mode>' may fail" when the answer is no. ``distance`` is deliberately
    #: not in this list -- it only measures distance to the surface and works
    #: on an open one.
    _VOLUME_TYPE_MODES = ('inside', 'outside', 'insideSpan', 'outsideSpan')

    def _open_primitive_refinement_surfaces(self) -> dict:
        """``refinementSurfaces`` rows for the open shapes that asked for a side.

        A closed primitive earns a ``refinementRegions`` entry in any mode:
        snappy can ask it which side of itself a cell centre is on. A plane,
        disk or plate can only be asked how far away it is, so a *distance*
        region on one is perfectly good and is written like any other. It is
        the four volume-type modes that have nowhere to go, and the honest
        translation of "refine inside this plane" is "refine the cells this
        plane cuts" -- a ``refinementSurfaces`` row with a level.

        Doing that rather than writing the region anyway is the difference
        between the shape doing something and snappyHexMesh warning "Shell ..
        is not closed so testing for 'inside' may fail" into a log nobody
        reads and then refining on an answer nobody checked.

        The level is the group's ``volumeRefinementLevel``: it is the one
        level a volume row can express, and a surface refinement takes a
        minimum and a maximum, so it is written as both.
        """
        result: dict[str, dict] = {}
        bounding = self._bounding_hex6_key()
        for geometry_id, geometry in self._elements('geometry').items():
            if self._item_value(geometry, 'gType') != 'volume':
                continue
            shape = self._item_value(geometry, 'shape')
            if shape not in self._OPEN_PRIMITIVE_SHAPES:
                continue
            if bounding is not None and str(geometry_id) == str(bounding):
                continue  # DP-577: only a real hex6 is taken as the block
            group_id = self._item_value(geometry, 'castellationGroup')
            refinement = self._collection_item(
                'castellation/refinementVolumes', group_id)
            if refinement is None:
                continue
            mode = self._enum_value(self._item_value(
                refinement, 'mode', 'inside'))
            if mode not in self._VOLUME_TYPE_MODES:
                # A distance ramp needs no translation: it stayed in
                # ``refinementRegions`` where the user put it.
                continue
            name = str(self._item_value(
                geometry, 'name', f'volume_{geometry_id}'))
            try:
                level = int(self._item_value(
                    refinement, 'volumeRefinementLevel', 1))
            except (TypeError, ValueError):
                level = 1
            # Silence here would be the old failure mode: the user asks for
            # everything inside a shape, gets something else, and nothing
            # anywhere says the request was changed.
            self.warn(
                'refinement.open_primitive.mode_translated',
                f'{name} is a {shape}, which is an open surface: '
                f'snappyHexMesh cannot tell inside from outside for it, so '
                f'the "{mode}" refinement was written as a surface '
                f'refinement of the cells the shape cuts, at level {level}. '
                f'Use mode distance for a band around it, or a closed shape '
                f'to refine a volume',
                field_id='castellation/refinementVolumes',
                requested=mode, applied='surface refinement',
                severity='warning')
            result[name] = {'level': [level, level]}
        return result

    def _registered_surface_names(self) -> dict:
        """Configuration surface name -> the key ``geometry{}`` registers (R139).

        An imported body is registered in ``geometry{}`` under the staged file
        it was published as -- ``surface_<prepared uuid>`` -- and never under
        the display name the GUI shows. The bridge between the two stores is
        the one ``_group_geometry_bindings`` already uses: the prepared group
        names match the configuration surface names.
        """
        result: dict[str, str] = {}
        for surface in self.surfaces:
            registered = surface['name']
            for group in self._surface_groups(surface):
                for key in ('display_name', 'solver_name', 'source_region'):
                    value = str(group.get(key) or '').strip()
                    if value:
                        result.setdefault(value.casefold(), registered)
        return result

    def _registered_volume_name(self, geometry_id, name, registered,
                                volume=None):
        """The ``geometry{}`` key a volume refinement has to be written under (R139).

        Primitive volumes -- searchableBox, sphere, cylinder -- are registered
        under their display name, so that name is the answer. An imported body
        is not registered at all: only the tri-surfaces published from it are,
        under ``surface_<prepared uuid>``. Resolve through the surfaces that
        belong to this volume, and answer ``None`` when nothing in
        ``geometry{}`` can be named -- an unresolvable region must not be
        written, because snappyHexMesh drops unmatched entries with a warning
        in a log nobody reads and meshes on as though the run were clean.

        DP-476. That resolution used to go by *name*: the display name of a
        child surface row, looked up against the display names of the
        published groups. Those are two stores, and the GUI lets the user
        rename in one of them. MEASURED on the campaign's own cases, where
        every surface is renamed ``<model>_wall<N>`` on the Geometry page:

        * ``annulus`` -- volume ``annulus`` (body ``d01828bf``), child surface
          renamed ``annulus_wall1``, published group still ``annulus``. No
          name matched, the level-2 inside region was dropped, and the GUI
          reported "1 volume(s) at level 2, inside" applied and verified with
          no gap filed.
        * ``baffled_chamber`` -- worse than dropped. The upstream volume
          resolved to nothing, and the downstream volume matched the one row
          the rename had missed, the interface
          ``baffled_chamber_downstream_to_baffled_chamber_upstream``, so its
          distance ramp was written around the *baffle* rather than around
          the body that asked for it. That entry was the only
          ``refinementRegions`` row in the case and it looked like a working
          one.

        The bridge that does not depend on a name is already in both stores:
        the volume row carries ``geometryId``, and the published surface
        carries the same id under ``geometry_id`` -- it is the id the
        ``geometry{}`` key is spelled out of (``surface_<geometryId>``). Ask
        that first, and keep the name walk after it for the rows that predate
        the id.
        """
        if name in registered:
            return name
        body = str(self._item_value(volume, 'geometryId', '') or '').strip()
        if body:
            by_body = {surface['name'] for surface in self.surfaces
                       if str(surface.get('geometry_id') or '').strip() == body
                       and surface['name'] in registered}
            # One body publishes one staged surface. Two would be a key this
            # code cannot choose between, and guessing is what R139 was.
            if len(by_body) == 1:
                return next(iter(by_body))
        by_surface_name = self._registered_surface_names()
        candidates = set()
        for element in self._elements('geometry').values():
            if self._item_value(element, 'gType') != 'surface':
                continue
            volume = self._item_value(element, 'volume')
            if volume is None or str(volume) != str(geometry_id):
                continue
            child = str(self._item_value(element, 'name', '')).strip().casefold()
            if child in by_surface_name:
                candidates.add(by_surface_name[child])
        if len(candidates) == 1:
            return next(iter(candidates))
        return None

    def _refinement_region_rows(self, *, warn: bool = True):
        """Every volume refinement, resolved onto the ``geometry{}`` key it needs.

        Split out of ``_refinement_regions`` because ``surfaceFeaturesDict``
        has to know the same answer: a span region reads a closeness field that
        only ``surfaceFeatures`` can produce, and it is named after the
        ``geometry{}`` key, not after the display name the GUI shows.

        ``warn=False`` is the silent pass the feature dictionary takes, so an
        unresolved or duplicated region is reported once -- by the writer that
        actually drops it -- instead of twice.
        """
        result: list[tuple[str, str, object]] = []
        seen: set[str] = set()
        # R139. The keys of this block are looked up in ``geometry{}``;
        # anything else is silently discarded by the mesher.
        registered = set(self._geometry_dictionary())
        for geometry_id, geometry in self._elements('geometry').items():
            if self._item_value(geometry, 'gType') != 'volume':
                continue
            group_id = self._item_value(geometry, 'castellationGroup')
            refinement = self._collection_item(
                'castellation/refinementVolumes', group_id)
            if refinement is None:
                continue
            # Plan 31. A plane, disk or plate encloses nothing, so it cannot
            # answer "is this cell inside me?". MEASURED on OpenFOAM 13,
            # ``refinementRegions.C:54-70,110-125``: the four volume-type
            # modes warn "Shell .. is not closed so testing for 'inside' may
            # fail" and then refine whatever the uninitialised volume test
            # happens to say. ``mode distance`` asks nothing of the kind --
            # ``refinementRegions.C:145-190`` only ever measures distance to
            # the surface -- so a distance ramp on an open surface is exactly
            # as valid as one on a box and stays here. The other four go to
            # ``_open_primitive_refinement_surfaces`` instead.
            if (self._item_value(geometry, 'shape') in self._OPEN_PRIMITIVE_SHAPES
                    and self._enum_value(self._item_value(
                        refinement, 'mode', 'inside')) in self._VOLUME_TYPE_MODES):
                continue
            name = str(self._item_value(
                geometry, 'name', f'volume_{geometry_id}'))
            # R139. MEASURED on annulus.stl: the GUI accepted a group
            # `core_ref` (mode inside, level 1) on the volume `annulus`, the
            # dict got `refinementRegions { annulus { ... } }` while
            # `geometry{}` held `surface_0ff6ff63...`, and snappyHexMesh
            # answered "Not all entries in refinementRegions dictionary were
            # used ... 1(annulus)" -- in the log only. The GUI called the
            # stage a clean pass and marked Castellation done.
            key = self._registered_volume_name(
                geometry_id, name, registered, volume=geometry)
            if key is None:
                if warn:
                    self.warn(
                        'refinement.region.unresolved',
                        f'volume refinement on {name} was not written: nothing '
                        f'in the geometry block of snappyHexMeshDict '
                        f'corresponds to that volume, and snappyHexMesh '
                        f'discards refinement regions it cannot match',
                        field_id='castellation/refinementVolumes',
                        requested=name, severity='error')
                continue
            if key in seen:
                if warn:
                    self.warn(
                        'refinement.region.duplicate',
                        f'volume refinement on {name} was not written: '
                        f'{key} already carries one, and a dictionary holds '
                        f'one refinement region per geometry entry',
                        field_id='castellation/refinementVolumes',
                        requested=name, applied=key, severity='error')
                continue
            seen.add(key)
            result.append((key, name, refinement))
        return result

    #: The two modes ``refinementRegions.C`` sizes from a surface's local span
    #: rather than from a distance ramp.
    _SPAN_MODES = ('insideSpan', 'outsideSpan')

    def _volume_band_ramp(self, refinement, name: str,
                          mode: str) -> list[tuple[float, int]]:
        """DP-586: the ``castellation/volumeBands`` rows of one volume group.

        Field audit 0924 snappy-front D13. The ramp a workflow page can
        author: rows named by the group, in increasing distance, read only in
        ``distance`` mode. In any other mode Foundation 13 takes one level, so
        the rows are left out and said to be, rather than the first one
        standing in for the group's own level.
        """
        group = str(self._item_value(refinement, 'groupName', '') or '')
        if not group.strip():
            return []
        rows = sorted(
            (float(self._item_value(band, 'distance', 0.0)),
             int(self._item_value(band, 'level', 0)))
            for band in self._elements('castellation/volumeBands').values()
            if str(self._item_value(band, 'groupName', '')) == group)
        if rows and mode != 'distance':
            self.warn(
                'refinement.volume_bands.unused',
                f'volume refinement {name} is in {mode} mode, so the '
                f'{count_text(len(rows), "distance band")} it holds will not '
                f'be written; switch the group to distance mode to use them',
                field_id='meshing.castellation.volume_bands')
            return []
        return rows

    def _refinement_bands(self, refinement, name: str, mode: str) -> list[list]:
        """The ordered ``(distance level)`` pairs of one refinement region.

        C31-08. ``refinementRegions.C:145-190`` reads ``mode distance`` as a
        ``List<Tuple2<scalar, label>>`` and refines each band in turn, so one
        region can hold a wake at level 3 within 5 mm and level 1 within 50 mm.
        This writer only ever emitted ``levels ((distance level))`` -- one
        band -- so the ramp OpenFOAM's own tutorials use was unreachable from
        the GUI. The rows come from the ``bands`` list; a project saved before
        that list existed has none, and its single ``distance`` /
        ``volumeRefinementLevel`` pair is band one.

        The two ordering rules are Foundation 13's, not this program's: it
        raises ``FatalError`` -- "Refinement should be specified in order of
        increasing distance (and decreasing refinement level)" -- and a
        FatalError inside snappyHexMesh is a failed stage with the reason in a
        log, so the same rule is enforced here where the user can still fix it.
        """
        rows: list[tuple[float, int]] = []
        try:
            elements = refinement.elements('bands')
        except Exception:
            elements = {}
        def order(item):
            # ``IntKeyList`` hands back stringified integers, and "10" sorts
            # before "2" as text -- which would silently reorder a ramp.
            try:
                return (0, int(str(item)), '')
            except (TypeError, ValueError):
                return (1, 0, str(item))

        for key in sorted(elements, key=order):
            band = elements[key]
            rows.append((float(self._item_value(band, 'distance', 0.0)),
                         int(self._item_value(band, 'level', 0))))
        if not rows:
            rows = self._volume_band_ramp(refinement, name, mode)  # DP-586
        if not rows:
            rows = [(float(self._item_value(refinement, 'distance', 1.0)),
                     int(self._item_value(
                         refinement, 'volumeRefinementLevel', 1)))]
        if mode in self._SPAN_MODES and len(rows) > 1:
            raise ValueError(
                f'volume refinement {name} is in {mode} mode with '
                f'{len(rows)} refinement bands; Foundation 13 reads a single '
                f'"level (distance level)" pair for that mode and cannot use '
                f'a ramp — use mode distance, or keep one band')
        for index, (distance, level) in enumerate(rows):
            if not math.isfinite(distance) or distance <= 0:
                raise ValueError(
                    f'volume refinement distance for {name} must be positive '
                    f'(band {index + 1} asks for {distance})')
            if level < 0:
                raise ValueError(
                    f'volume refinement level for {name} must not be negative '
                    f'(band {index + 1} asks for {level})')
            if index == 0:
                continue
            previous_distance, previous_level = rows[index - 1]
            if distance <= previous_distance or level > previous_level:
                raise ValueError(
                    f'volume refinement bands for {name} must be given in '
                    f'order of increasing distance and non-increasing level; '
                    f'band {index} is ({previous_distance} {previous_level}) '
                    f'and band {index + 1} is ({distance} {level}). '
                    f'Foundation 13 stops the mesher on this exact condition')
        return [[distance, level] for distance, level in rows]

    def _refinement_regions(self) -> dict:
        result: dict[str, dict] = {}
        surface_names = {surface['name'] for surface in self.surfaces}
        for key, name, refinement in self._refinement_region_rows():
            mode = str(self._item_value(refinement, 'mode', 'inside'))
            entry: dict[str, object] = {'mode': mode}
            bands = self._refinement_bands(refinement, name, mode)
            if mode == 'distance':
                entry['levels'] = bands
            elif mode in self._SPAN_MODES:
                # C31-08. Two prerequisites, both of which OpenFOAM 13 turns
                # into a FatalError once the mesher is already running, and
                # both of which are decidable here.
                if key not in surface_names:
                    raise ValueError(
                        f'volume refinement {name} asks for {mode}, which '
                        f'Foundation 13 measures across a triangulated '
                        f'surface; {key} is a searchable primitive (a box, '
                        f'sphere or cylinder) and the mesher refuses it with '
                        f'"is not a triSurface as required by refinement '
                        f'modes insideSpan and outsideSpan". Draw the region '
                        f'as geometry, or use inside/outside/distance')
                cells = int(self._item_value(refinement, 'cellsAcrossSpan', 5))
                if cells < 1:
                    raise ValueError(
                        f'volume refinement {name} asks for {cells} cells '
                        f'across the span; Foundation 13 divides by that '
                        f'count, so it has to be at least one')
                # ``level`` is a single Tuple2 here, not a label: see
                # ``setAndCheckLevels`` in refinementRegions.C, which reads
                # ``Tuple2<scalar, label> distLevel(dict.lookup("level"))``
                # for these two modes only.
                entry['level'] = bands[0]
                entry['cellsAcrossSpan'] = cells
            else:
                # Foundation 13's native spelling for inside/outside.
                entry['level'] = bands[0][1]
            gap = self._item_element(refinement, 'gapRefinement')
            gap_direction = str(self._item_value(gap, 'direction', 'none'))
            increment = self._item_element(refinement, 'levelIncrement')
            increment_disabled = bool(
                self._item_value(increment, 'disabled', True))
            if gap_direction != 'none' or not increment_disabled:
                raise ValueError(
                    f'volume refinement {name} uses unsupported gap/anisotropic '
                    'controls; Foundation 13 supports inside, outside, distance, '
                    'insideSpan, outsideSpan, level/levels, and cellsAcrossSpan '
                    'only')
            result[key] = entry
        return result

    def _span_closeness_surfaces(self) -> dict[str, str]:
        """``geometry{}`` key -> the span mode that needs its closeness field.

        C31-08. ``refinementRegions.C:576-604`` opens
        ``<surface>.closeness.internalPointCloseness`` (or ``external`` for
        ``outsideSpan``) with ``IOobject::MUST_READ`` while it is *reading the
        dictionary*, so a span region without that file does not mesh badly --
        snappyHexMesh dies before it starts. Only ``surfaceFeatures`` writes
        it, and only when its dictionary carries a ``closeness`` sub-dictionary
        with ``pointCloseness on`` (``surfaceFeatures.C:439-540``). This is the
        list ``surfaceFeaturesDict`` has to switch that on for.
        """
        wanted: dict[str, str] = {}
        surface_names = {surface['name'] for surface in self.surfaces}
        for key, _name, refinement in self._refinement_region_rows(warn=False):
            mode = str(self._item_value(refinement, 'mode', 'inside'))
            if mode in self._SPAN_MODES and key in surface_names:
                wanted[key] = mode
        return wanted

    def _warn_local_cells_above_global(self, castellated: dict) -> None:
        """DP-585 (field audit 0924 snappy-front D12).

        ``maxLocalCells`` is the per-processor ceiling and ``maxGlobalCells``
        the whole-mesh one, so a local limit above the global one can never
        be the limit that stops refinement. OpenFOAM 13 accepts it without a
        word; the setting is still almost certainly a typo, so it is said
        here rather than refused.
        """
        local = castellated.get('maxLocalCells')
        total = castellated.get('maxGlobalCells')
        if isinstance(local, int) and isinstance(total, int) and local > total:
            self.warn(
                'castellation.max_local_cells.above_global',
                f'Max local cells ({local}) is above Max global cells '
                f'({total}). The local limit is per processor, so it can '
                f'never stop refinement before the global one does; lower it '
                f'or raise Max global cells.',
                field_id='meshing.castellation.max_local_cells',
                requested=local, applied=local)

    def snappy_hex_mesh_dict(self, *, castellation=True, snap=True, layers=True) -> str:
        buffer_layer_enabled = not bool(
            self._v('snap/bufferLayer/disabled', True))
        if buffer_layer_enabled:
            raise ValueError(
                'the legacy castellatedBufferLayer snap mode is not available '
                'in OpenFOAM Foundation 13; use boundary layers or disable the '
                'Snap buffer-layer control')
        castellated = {
            'maxLocalCells': self._int('castellation/maxLocalCells', 10000000),
            'maxGlobalCells': self._int('castellation/maxGlobalCells', 100000000),
            'minRefinementCells': self._int('castellation/minRefinementCells', 0),
            'maxLoadUnbalance': self._v('castellation/maxLoadUnbalance', 0.5),
            'nCellsBetweenLevels':
                self._int('castellation/nCellsBetweenLevels', 3),
            'features': self._feature_files(),
            'refinementSurfaces': self._refinement_surfaces(),
            'resolveFeatureAngle': self._v('castellation/resolveFeatureAngle', '30'),
            'refinementRegions': self._refinement_regions(),
            'allowFreeStandingZoneFaces': self._v(
                'castellation/allowFreeStandingZoneFaces', True),
        }
        self._warn_local_cells_above_global(castellated)  # DP-585
        # F-06. One seed is written singular, several are written as the
        # ``List<point>`` v13 reads; nothing is dropped and nothing is warned
        # about, because Foundation 13 keeps every region the list names.
        seeds = self._fluid_seeds()
        # F-18. ``refinementParameters.C`` reads ``insidePoints`` first, then
        # ``insidePoint``, then ``locationInMesh`` -- the last is the
        # backwards-compatible fallback the annotated dictionary no longer
        # spells. Both keys written here are the release's own names, so a
        # dictionary FoamMesh produces can be read against OpenFOAM 13's own
        # documentation instead of only against its compatibility table.
        if len(seeds) > 1:
            castellated['insidePoints'] = [list(point) for point in seeds]
        elif seeds:
            castellated['insidePoint'] = list(seeds[0])
        self._add_optional(castellated, 'castellation', (
            'gapLevelIncrement', 'planarAngle'))
        self._add_toggles(castellated, 'castellation', (
            'useTopologicalSnapDetection', 'handleSnapProblems',
            # C31-08. The companion switch to the span modes: it decides
            # whether the refinement a span asks for may reach past the span.
            'extendedRefinementSpan'))
        snap_controls = {
            'nSmoothPatch': self._v('snap/nSmoothPatch', '0'),
            'tolerance': self._v('snap/tolerance', '3'),
            'nSolveIter': self._v('snap/nSolveIter', '30'),
            'nRelaxIter': self._v('snap/nRelaxIter', '5'),
            # F-41. Two keys in snapControls, two switches here. They used to
            # be one enum written as its own complement, so "both on" -- what
            # OpenFOAM 13 wants for an STL whose feature edges were extracted
            # -- was unreachable, and so was "neither".
            'implicitFeatureSnap':
                bool(self._v('snap/implicitFeatureSnap', False)),
            'explicitFeatureSnap':
                bool(self._v('snap/explicitFeatureSnap', True)),
            'nFeatureSnapIter': self._v('snap/nFeatureSnapIter', '15'),
            'multiRegionFeatureSnap': self._v('snap/multiRegionFeatureSnap', False),
        }
        # C31-11. ``snapParameters.C:44-46`` reads this with
        # ``lookupOrDefault(..., true)``, so DEFAULT must leave it out rather
        # than write ``true``: an existing case has to produce the dictionary
        # it always did.
        self._add_toggles(snap_controls, 'snap', ('detectNearSurfacesSnap',))
        layer_surfaces = self._layer_surfaces()
        if layers and not layer_surfaces:
            # DP-112. MEASURED on all nine meshed snappy legs of the 1f2787eb
            # sweep: castellation, snap and layers reported the same cell
            # count, and the log said `No layers to generate ...`. The stage
            # ran, exited 0, and was ticked. The dictionary explains it --
            # `layers { }` is empty, so there is no patch for addLayers to
            # grow on. v13 treats that as a request for nothing rather than as
            # an error, which is correct of it; what was missing is anyone
            # saying so while the user is still looking at the case.
            self.warn(
                'layers.no_patch_selected',
                'the layers stage is enabled but no patch is configured to '
                'grow layers, so snappyHexMesh will report success without '
                'changing the mesh; assign a layer group to at least one '
                'surface, or turn the layers stage off',
                field_id='addLayers/layers',
                requested='add layers', applied='(no patch selected)')
        # A frozen patch is `nSurfaceLayers 0` and nothing else, so it carries
        # no thickness to read a global default from and none to disagree
        # about. Taking the defaults from the first entry regardless would
        # raise KeyError the moment a frozen group happened to sort first.
        growing = {name: values for name, values in layer_surfaces.items()
                   if 'firstLayerThickness' in values}
        if growing:
            first_name, first_layer = next(iter(growing.items()))
            default_first = first_layer['firstLayerThickness']
            default_ratio = first_layer['expansionRatio']
            default_minimum = first_layer['minThickness']
            # v13 takes the three thicknesses globally and lets a patch entry
            # override them. Whichever group happens to be first therefore sets
            # the dictionary-wide default for every patch that does not carry
            # its own -- so groups that disagree do not merge, one of them
            # simply wins. Silently, until WP2.2.
            for key, label in (('firstLayerThickness', 'first layer thickness'),
                               ('expansionRatio', 'expansion ratio'),
                               ('minThickness', 'minimum thickness')):
                disagreeing = sorted(
                    name for name, values in growing.items()
                    if values.get(key) != first_layer[key])
                if not disagreeing:
                    continue
                self.warn(
                    f'layer_global_{key}_substituted',
                    f'{len(disagreeing) + 1} layer groups request different '
                    f'{label} values; snappyHexMeshDict carries one global '
                    f'default, so the value from "{first_name}" was written '
                    f'for the dictionary and {", ".join(disagreeing)} '
                    f'{"differ" if len(disagreeing) > 1 else "differs"}',
                    field_id=f'addLayers/layers/{key}',
                    requested=sorted({
                        str(values.get(key)) for values in growing.values()}),
                    applied=first_layer[key])
        else:
            default_first = 0.25
            default_ratio = 1.2
            default_minimum = 0.1
        add_layers = {
            'layers': layer_surfaces,
            'relativeSizes': getattr(
                self, '_layer_relative_sizes', True),
            # v13 determines the layer specification from the two global
            # thickness keys. Per-patch values override the same pair.
            'firstLayerThickness': default_first,
            'expansionRatio': default_ratio,
            'minThickness': default_minimum,
            'featureAngle': self._v('addLayers/featureAngle', '60'),
            'slipFeatureAngle': self._v('addLayers/slipFeatureAngle', '30'),
            'nGrow': self._v('addLayers/nGrow', '0'),
            'nRelaxIter': self._v('addLayers/nRelaxIter', '10'),
            'maxFaceThicknessRatio':
                self._v('addLayers/maxFaceThicknessRatio', '0.5'),
            'nSmoothSurfaceNormals':
                self._v('addLayers/nSmoothSurfaceNormals', '1'),
            'nSmoothThickness': self._v('addLayers/nSmoothThickness', '10'),
            # F-18. Both the schema path and the written key are OpenFOAM 13's
            # own spelling now; the misspelt path is renamed on load by
            # ``migrateDocument``, so there is nothing left to fall back to.
            'minMedialAxisAngle': self._v('addLayers/minMedialAxisAngle', '90'),
            'maxThicknessToMedialRatio':
                self._v('addLayers/maxThicknessToMedialRatio', '0.3'),
            'nSmoothNormals': self._v('addLayers/nSmoothNormals', '3'),
            'nBufferCellsNoExtrude':
                self._v('addLayers/nBufferCellsNoExtrude', '0'),
            'nLayerIter': self._v('addLayers/nLayerIter', '50'),
            'nRelaxedIter': self._v('addLayers/nRelaxedIter', '20'),
            'meshShrinker': self._v('addLayers/meshShrinker', 'displacementMedialAxis'),
        }
        self._add_optional(add_layers, 'addLayers', (
            'nMedialAxisIter', 'nSmoothDisplacement'))
        self._add_toggles(add_layers, 'addLayers', (
            'detectExtrusionIsland', 'additionalReporting'))
        quality = self._quality_controls()
        d = {
            'castellatedMesh': castellation,
            'snap': snap,
            'addLayers': layers,
            # v13 requires a name-keyed entry with an explicit quoted ``file``;
            # the filename-as-key form is rejected ("keyword file is undefined").
            'geometry': self._geometry_dictionary(),
            'castellatedMeshControls': castellated,
            'snapControls': snap_controls,
            'addLayersControls': add_layers,
            'meshQualityControls': quality,
            'mergeTolerance': self._v('meshQuality/mergeTolerance', '1e-6'),
        }
        # C31-11. ``snappyHexMesh.C:715`` reads ``keepPatches`` off the top
        # level; with it on, a patch that ended the run with no faces survives
        # into ``constant/polyMesh/boundary`` instead of being deleted at
        # ``:1168``, ``:1214`` and ``:1271``.
        self._add_toggles(d, 'snappyAdvanced', ('keepPatches',))
        # Diagnostics are lists of words and there is no "none" spelling, so a
        # case with no flag on must not carry the keyword at all.
        for key, flags in (('writeFlags', WRITE_FLAGS),
                           ('debugFlags', DEBUG_FLAGS)):
            chosen = [flag for flag in flags
                      if self._bool(f'snappyAdvanced/{key}/{flag}')]
            if chosen:
                d[key] = chosen
        return format_dictionary_file('snappyHexMeshDict', d)

    def _bool(self, path) -> bool:
        value = self._v(path, False)
        if isinstance(value, str):
            return value.strip().lower() in ('true', 'yes', 'on', '1')
        return bool(value)

    def _add_optional(self, target: dict, prefix: str, keys) -> None:
        """Write each of *keys* only if the project actually set it.

        These are controls OpenFOAM already has a default for. Writing our own
        value into every case would change meshes that were tuned before the
        control existed, so an unset control leaves the dictionary alone.
        """
        for key in keys:
            value = self._v(f'{prefix}/{key}')
            if value is not None and str(value).strip() != '':
                target[key] = value

    def _add_toggles(self, target: dict, prefix: str, keys) -> None:
        """The same, for the three-state switches.

        ``BoolType`` cannot say "no opinion", so these are enums whose DEFAULT
        member means the key is not written.
        """
        for key in keys:
            value = self._enum_value(self._v(f'{prefix}/{key}', 'default'))
            if value in ('on', 'off'):
                target[key] = value == 'on'

    def decompose_par_dict(self, n: int | None = None,
                           method: str | None = None) -> str:
        """Plan 26 WP9.2. The method is a configured field, not a constant.

        Snappy's refinement is decomposition-sensitive, so this is a meshing
        input: `duct` moves +12.4% in cell count between serial and 16 ranks
        while a 609k-cell `annulus` moves -0.04% on the same ranks. The
        coefficients `hierarchical` and `simple` require are written by the
        same builder, because a dictionary naming one of them without its `n`
        vector is one `decomposePar` rejects.
        """
        from foammesh.openfoam.decomposition import (
            DecompositionError, DecompositionSettings, build,
        )

        if n is None:
            # Plan 31 CP-07 item 6. This used to fall back to
            # ``mesh/execution/maxCpuCores``, which is a *ceiling*, not a rank
            # count: a project whose limit was 8 and whose Parallel
            # Environment said 4 got ``numberOfSubdomains 8`` written into the
            # case while the launcher started ``mpirun -np 4``. The dictionary
            # and the run then disagreed about the one number they both name,
            # which is exactly what V16a exists to catch. Nothing knows the
            # effective ranks at generation time, so the answer is the honest
            # one -- serial -- and the rank count is written by the engine
            # seam at launch, from the allocation that actually ran.
            n = int(self.requested_ranks or 1)
        settings = DecompositionSettings.read(self.db)
        if method is not None:
            # `replace`, not a positional rebuild: the constraint and weight
            # settings are further down the field list and a positional call
            # silently dropped every one of them when a method was overridden.
            settings = _replace_dataclass(
                settings,
                method=str(getattr(method, 'value', method)).split('.')[-1])
        zones = self.face_zone_names()
        if settings.preserve_face_zones and not zones:
            self.warn(
                'decompose.preserve_face_zones.no_zones',
                'the decomposition was asked to keep faceZones whole and this '
                'case creates none, so no constraint is written',
                field_id='mesh/execution/preserveFaceZones',
                requested='preserveFaceZones', applied='none',
                severity='info')
        elif zones and not settings.preserve_face_zones and int(n) > 1:
            # The user cannot know to ask: the zones are created by the
            # castellation step from `cfdType`, not typed in anywhere, and a
            # zone cut across processors fails in the solver rather than here.
            self.warn(
                'decompose.preserve_face_zones.available',
                f'this case creates {count_text(len(zones), "faceZone")} '
                f'({", ".join(zones)}) and the decomposition may cut them '
                f'across processors; "Keep faceZones whole" on the Execution '
                f'page writes the preserveFaceZones constraint that stops it',
                field_id='mesh/execution/preserveFaceZones',
                requested='none', applied='none', severity='info')
        try:
            document = build(
                int(n), method=settings.method, order=settings.order,
                cells=settings.cells,
                constraints=settings.constraints(zones),
                weight_field=settings.weight_field)
        except DecompositionError as error:
            raise ValueError(str(error)) from error
        return format_dictionary_file('decomposeParDict', document)

    def face_zone_names(self) -> tuple[str, ...]:
        """Every faceZone ``castellatedMeshControls`` asks snappy to create.

        Plan 31 (parallel.decompose_extras). ``preserveFaceZones`` needs the
        names, and the *product* is the only party that knows them --
        ``_refinement_surfaces`` derives them from each group's ``cfdType``
        (``none`` and ``cellZone`` make an internal zone, ``interface`` makes
        a baffle or boundary one). Asking the user to type them would be
        asking them to guess.
        """
        names: list[str] = []
        try:
            surfaces = self._refinement_surfaces()
        except Exception:                                    # noqa: BLE001
            return ()
        for entry in surfaces.values():
            regions = entry.get('regions') if isinstance(entry, dict) else None
            for candidate in ([entry] + list((regions or {}).values())
                              if isinstance(regions, dict) else [entry]):
                if not isinstance(candidate, dict):
                    continue
                zone = candidate.get('faceZone')
                if zone and str(zone) not in names:
                    names.append(str(zone))
        return tuple(names)

    def control_dict(self) -> str:
        """Minimal utility case control required by all OpenFOAM executables."""
        return format_dictionary_file('controlDict', {
            'application': 'snappyHexMesh',
            'startFrom': 'startTime',
            'startTime': 0,
            'stopAt': 'endTime',
            'endTime': 1,
            'deltaT': 1,
            'writeControl': 'timeStep',
            'writeInterval': 1,
            'purgeWrite': 0,
            'writeFormat': 'ascii',
            'writePrecision': 8,
            'writeCompression': 'off',
            'timeFormat': 'general',
            'timePrecision': 6,
            'runTimeModifiable': True,
        })

    # assembly -------------------------------------------------------------
    def write_case(self, case_dir) -> dict:
        """Write the system/ dicts into *case_dir*; returns {name: path}."""
        case_dir = Path(case_dir)
        system = case_dir / 'system'
        system.mkdir(parents=True, exist_ok=True)
        written = {}
        dictionaries = {
            'blockMeshDict': self.block_mesh_dict(),
            'controlDict': self.control_dict(),
            'surfaceFeaturesDict': self.surface_features_dict(),
            'snappyHexMeshDict': self.snappy_hex_mesh_dict(),
            'decomposeParDict': self.decompose_par_dict(),
        }
        # Plan 31. Only when the project asked for user-defined checks. A file
        # nobody requested would change the manifest of every existing case,
        # and an unrequested file in system/ is a file someone has to explain.
        if self.wants_mesh_quality_dict():
            dictionaries['meshQualityDict'] = self.mesh_quality_dict()
        for name, text in dictionaries.items():
            p = system / name
            p.write_text(text, encoding='utf-8')
            written[name] = p
        return written

    def write_case_staged(self, case_dir, prepared_geometry=None) -> DictionaryManifest:
        """Generate dictionaries in staging and atomically promote each file.

        The manifest is written last, so its presence means every listed file
        reached ``system`` with the recorded content digest.
        """
        case_dir = Path(case_dir)
        surface_entries = self.stage_tri_surfaces(case_dir, prepared_geometry)
        self.write_group_manifest(case_dir, prepared_geometry)
        system = case_dir / 'system'
        system.mkdir(parents=True, exist_ok=True)
        staging = case_dir / f'.foammesh-dictionaries-{uuid4().hex}'
        try:
            generated = self.write_case(staging)
            staged_system = staging / 'system'
            entries = []
            for name in sorted(generated):
                source = staged_system / name
                content = source.read_bytes()
                destination = system / name
                os.replace(source, destination)
                entries.append({
                    'name': name,
                    'path': str(destination),
                    'sha256': hashlib.sha256(content).hexdigest(),
                    'bytes': len(content),
                })
            manifest = DictionaryManifest(
                tuple(entries), f'{self.target.flavor.value}-{self.target.version}',
                tuple(surface_entries),
                tuple(self._fluid_seed()) if self._fluid_seed() is not None else None,
                self._bbox_tuple(self._effective_bbox()),
                self._configuration_sha256(),
                {
                    _dictionary_stage(entry['name']): str(entry['sha256'])
                    for entry in entries
                },
                tuple(self.warnings))
            temporary = system / '.foammesh-dictionaries-manifest.tmp'
            temporary.write_text(
                json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + '\n',
                encoding='utf-8')
            os.replace(temporary, system / 'foammesh-dictionaries-manifest.json')
            return manifest
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def write_group_manifest(self, case_dir, prepared_geometry=None) -> Path:
        """Publish the case's groups, regions and interface pairs together.

        F-13. This is the writer the non-conformal reader was missing. The
        groups and regions are the prepared revision's, verbatim, so the
        ``patch_uuid`` the interface pair names resolves against the same
        record the facade validated it against; the pairs are the project's
        current ``interfacePairs`` rows. Written on every generation, so
        removing the last pair removes it from the manifest too.
        """
        prepared = (prepared_geometry if prepared_geometry is not None
                    else self.prepared_geometry)
        source = getattr(prepared, 'group_manifest', None) or {}
        reference = getattr(prepared, 'reference', None)
        document = {
            'schema_version': 2,
            'prepared_revision_id': (
                str(source.get('prepared_revision_id')
                    or getattr(reference, 'revision_id', '') or '')),
            'groups': [dict(group) for group in source.get('groups', ())],
            'regions': [dict(region) for region in source.get('regions', ())],
            'interface_pairs': self.interface_pairs(),
            # Plan 31 CP-07 item 3. The background block's faces are published
            # alongside the prepared groups, each saying whether a person
            # named it and what they meant it to be. Without this the reader
            # met them as patches nothing declared and called them fabricated,
            # which is true but useless: it cannot tell an unnamed side of a
            # derived box from a name that was invented in place of a user's
            # inlet.
            'background_boundaries': [
                dict(item) for item in self.background_boundaries()],
        }
        path = group_manifest_path(case_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, path)
        return path

    def stage_tri_surfaces(self, case_dir, prepared_geometry=None) -> list[dict]:
        """Atomically stage only immutable prepared tessellations for OpenFOAM."""
        if prepared_geometry is None:
            return []
        self.prepared_geometry = prepared_geometry
        tri_surface = Path(case_dir) / 'constant' / 'triSurface'
        tri_surface.mkdir(parents=True, exist_ok=True)
        prior_path = tri_surface / '.foammesh-surfaces.json'
        prior = {}
        if prior_path.is_file():
            try:
                prior = json.loads(prior_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                prior = {}
        records = []
        configured = []
        groups_by_geometry: dict[str, list[dict]] = {}
        for group in prepared_geometry.group_manifest.get('groups', ()):
            groups_by_geometry.setdefault(group['geometry_id'], []).append(
                dict(group))
        for source in prepared_geometry.manifest.get('sources', ()):
            source_name = source.get('surface_prepared_name') or source['prepared_name']
            source_hash = source.get('surface_sha256') or source['sha256']
            source_path = prepared_geometry.reference.root / 'sources' / source_name
            suffix = source_path.suffix.lower()
            if suffix not in {'.stl', '.obj', '.vtk', '.vtp'}:
                raise ValueError(
                    f'prepared surface representation is not supported by OpenFOAM: {suffix}')
            stable_name = f"surface_{source['geometry_id']}{suffix}"
            destination = tri_surface / stable_name
            temporary = destination.with_suffix(destination.suffix + '.tmp')
            shutil.copy2(source_path, temporary)
            copied_hash = _sha256_file(temporary)
            if copied_hash != source_hash:
                temporary.unlink(missing_ok=True)
                raise ValueError(f'prepared surface checksum mismatch: {source_name}')
            os.replace(temporary, destination)
            entry = {
                'geometry_id': source['geometry_id'],
                'name': f"surface_{source['geometry_id']}",
                'file': stable_name,
                'path': str(destination),
                'sha256': copied_hash,
                'bytes': destination.stat().st_size,
                'groups': tuple(
                    groups_by_geometry.get(source['geometry_id'], ())),
                'prepared_revision_id': prepared_geometry.reference.revision_id,
            }
            records.append(entry)
            configured.append({
                'name': entry['name'], 'file': stable_name,
                'groups': entry['groups'],
                'geometry_id': source['geometry_id']})
        owned_now = {item['file'] for item in records}
        for old in prior.get('surfaces', ()):
            old_name = old.get('file')
            if old_name and old_name not in owned_now:
                old_path = (tri_surface / old_name).resolve()
                if old_path.parent == tri_surface.resolve():
                    old_path.unlink(missing_ok=True)
        document = {
            'schema_version': 1,
            'prepared_revision_id': prepared_geometry.reference.revision_id,
            'prepared_fingerprint': prepared_geometry.reference.fingerprint,
            'surfaces': records,
        }
        temporary_manifest = prior_path.with_suffix('.tmp')
        temporary_manifest.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(temporary_manifest, prior_path)
        self.surfaces = tuple(configured)
        return records

    def load_case_context(self, case_dir) -> dict:
        """Restore bounds and stable surface/group mappings for regeneration."""
        case_dir = Path(case_dir)
        manifest_path = (
            case_dir / 'system' / 'foammesh-dictionaries-manifest.json')
        if not manifest_path.is_file():
            raise FileNotFoundError(
                'dictionary generation manifest is required; generate '
                'dictionaries before running a stage')
        document = json.loads(manifest_path.read_text(encoding='utf-8'))
        bbox = document.get('bbox')
        if not isinstance(bbox, list) or len(bbox) != 6:
            raise ValueError(
                'dictionary manifest has no reusable bounding-box contract; '
                'regenerate dictionaries once')
        from foammesh.core.geometry import BBox
        self.bbox = BBox(*(float(value) for value in bbox))
        fluid_seed = document.get('fluid_seed')
        self._restored_fluid_seed = (
            tuple(float(value) for value in fluid_seed)
            if isinstance(fluid_seed, list) and len(fluid_seed) == 3
            else None)
        surfaces_path = (
            case_dir / 'constant' / 'triSurface' /
            '.foammesh-surfaces.json')
        if surfaces_path.is_file():
            surfaces_document = json.loads(
                surfaces_path.read_text(encoding='utf-8'))
            self.surfaces = tuple({
                'name': item['name'],
                'file': item['file'],
                'groups': tuple(item.get('groups', ())),
                'geometry_id': item.get('geometry_id'),
            } for item in surfaces_document.get('surfaces', ()))
        return document

    @staticmethod
    def _require_current_staged_surfaces(case_dir) -> None:
        """Refuse to reuse tri-surfaces staged from a superseded revision.

        ``regenerate_stage`` restores the staged surface set from the
        tri-surface sidecar. Preparing geometry again mints a new revision
        without restaging, so without this check a stage rerun would mesh the
        previous geometry while reporting success.
        """
        case_dir = Path(case_dir)
        staged_path = (
            case_dir / 'constant' / 'triSurface' / '.foammesh-surfaces.json')
        current_path = (
            case_dir / 'foammesh' / 'geometry' / 'prepared' / 'current.json')
        if not staged_path.is_file() or not current_path.is_file():
            return
        try:
            staged = json.loads(staged_path.read_text(encoding='utf-8'))
            current = json.loads(current_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return
        staged_revision = staged.get('prepared_revision_id')
        current_revision = current.get('revision_id')
        if (staged_revision and current_revision
                and staged_revision != current_revision):
            raise ValueError(
                'staged surfaces come from prepared geometry revision '
                f'{staged_revision} but the case now uses {current_revision}; '
                'generate dictionaries again before running a stage')

    def regenerate_stage(self, case_dir, stage: str, *,
                         castellation: bool | None = None,
                         snap: bool | None = None,
                         layers: bool | None = None) -> DictionaryManifest:
        """Regenerate current-stage dictionaries without touching mesh output."""
        case_dir = Path(case_dir)
        previous = self.load_case_context(case_dir)
        self._require_current_staged_surfaces(case_dir)
        system = case_dir / 'system'
        renderers = {
            'blockMesh': {'blockMeshDict': self.block_mesh_dict()},
            'surfaceFeatures': {
                'surfaceFeaturesDict': self.surface_features_dict()},
            'castellation': {'snappyHexMeshDict': self.snappy_hex_mesh_dict(
                castellation=True, snap=False, layers=False)},
            'snappyHexMesh': {'snappyHexMeshDict': self.snappy_hex_mesh_dict(
                castellation=(
                    True if castellation is None else castellation),
                snap=True if snap is None else snap,
                layers=False if layers is None else layers)},
            'snap': {'snappyHexMeshDict': self.snappy_hex_mesh_dict(
                castellation=False, snap=True, layers=False)},
            'layers': {'snappyHexMeshDict': self.snappy_hex_mesh_dict(
                castellation=False, snap=False, layers=True)},
            'checkMesh': {},
        }
        if stage not in renderers:
            raise ValueError(f'unknown dictionary regeneration stage: {stage}')
        entries_by_name = {
            str(item['name']): dict(item)
            for item in previous.get('files', ())}
        for name, text in renderers[stage].items():
            destination = system / name
            temporary = destination.with_suffix(destination.suffix + '.tmp')
            temporary.write_text(text, encoding='utf-8')
            content = temporary.read_bytes()
            os.replace(temporary, destination)
            entries_by_name[name] = {
                'name': name,
                'path': str(destination),
                'sha256': hashlib.sha256(content).hexdigest(),
                'bytes': len(content),
            }
        entries = tuple(
            entries_by_name[name] for name in sorted(entries_by_name))
        manifest = DictionaryManifest(
            entries,
            f'{self.target.flavor.value}-{self.target.version}',
            tuple(previous.get('surfaces', ())),
            self._restored_fluid_seed,
            self._bbox_tuple(self._effective_bbox()),
            self._configuration_sha256(),
            {
                _dictionary_stage(item['name']): str(item['sha256'])
                for item in entries
            },
            tuple(self.warnings))
        temporary_manifest = (
            system / '.foammesh-dictionaries-manifest.tmp')
        temporary_manifest.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(
            temporary_manifest,
            system / 'foammesh-dictionaries-manifest.json')
        return manifest

    @staticmethod
    def snapshot_run_dictionaries(case_dir) -> Path:
        """Record the exact generated inputs used by the last accepted run."""
        case_dir = Path(case_dir)
        system = case_dir / 'system'
        manifest_path = system / 'foammesh-dictionaries-manifest.json'
        if not manifest_path.is_file():
            raise FileNotFoundError('dictionary manifest is unavailable')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        files = {}
        for item in manifest.get('files', ()):
            path = Path(item['path'])
            if not path.is_absolute():
                path = case_dir / path
            if path.is_file():
                files[item['name']] = path.read_text(encoding='utf-8')
        document = {
            'schema_version': 1,
            'configuration_sha256': manifest.get('configuration_sha256'),
            'stage_sha256': manifest.get('stage_sha256', {}),
            'files': files,
        }
        destination = (
            case_dir / 'foammesh' / 'dictionaries' / 'last-run.json')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, destination)
        return destination

    def stage_staleness(self, case_dir) -> dict[str, bool]:
        case_dir = Path(case_dir)
        state_path = (
            case_dir / 'foammesh' / 'dictionaries' / 'stage-runs.json')
        state = {}
        if state_path.is_file():
            state = json.loads(state_path.read_text(encoding='utf-8')).get(
                'stages', {})
        return {
            stage: (
                stage in state and
                state[stage].get('input_sha256') !=
                self.stage_input_sha256(stage))
            for stage in (
                'blockMesh', 'surfaceFeatures', 'castellation',
                'snap', 'layers', 'checkMesh')
        }

    def require_current_stage_dependencies(self, case_dir, stage: str) -> None:
        dependencies = {
            'surfaceFeatures': ('blockMesh',),
            'castellation': ('blockMesh', 'surfaceFeatures'),
            'snappyHexMesh': ('blockMesh', 'surfaceFeatures'),
            'snap': ('blockMesh', 'surfaceFeatures', 'castellation'),
            'layers': (
                'blockMesh', 'surfaceFeatures', 'castellation', 'snap'),
            'checkMesh': (),
            'blockMesh': (),
        }
        state_path = (
            Path(case_dir) / 'foammesh' / 'dictionaries' /
            'stage-runs.json')
        if not state_path.is_file():
            return
        state = json.loads(state_path.read_text(encoding='utf-8')).get(
            'stages', {})
        stale = [
            dependency for dependency in dependencies.get(stage, ())
            if dependency in state and
            state[dependency].get('input_sha256') !=
            self.stage_input_sha256(dependency)]
        if stale:
            raise ValueError(
                'upstream mesh stages are stale and must be rerun first: ' +
                ', '.join(stale))

    def mark_stage_run(self, case_dir, stage: str) -> Path:
        case_dir = Path(case_dir)
        destination = (
            case_dir / 'foammesh' / 'dictionaries' / 'stage-runs.json')
        document = {'schema_version': 1, 'stages': {}}
        if destination.is_file():
            document = json.loads(destination.read_text(encoding='utf-8'))
        document.setdefault('stages', {})[stage] = {
            'input_sha256': self.stage_input_sha256(stage),
            'configuration_sha256': self._configuration_sha256(),
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, destination)
        return destination

    #: The mesh stages in the order OpenFOAM applies them. Re-running one
    #: makes every later one meaningless, so a reset takes them with it.
    STAGE_ORDER = ('blockMesh', 'surfaceFeatures', 'castellation',
                   'snap', 'layers', 'checkMesh')

    def clear_stage_runs(self, case_dir, stage: str) -> tuple[str, ...]:
        """Forget that ``stage`` -- and everything after it -- ever ran.

        R85. ``mark_stage_run`` had no counterpart, so a recorded run was
        permanent. The Base Grid page decided whether to offer Generate or
        Reset from "does ``constant/polyMesh/boundary`` exist", which every
        snappy stage keeps true because they all overwrite that one directory
        in place. Generate therefore vanished after the first blockMesh and
        never came back, and the Reset offered instead cleared a numbered time
        directory that the snappy path never writes. MEASURED on venturi.stl:
        Reset unloaded the viewport mesh, left the task on the checkmark, and
        the button stayed Reset -- so a base grid could not be regenerated
        after changing its cell counts, and a case whose mesh had been damaged
        could not be rebuilt from inside the product.

        Returns the stages actually forgotten.
        """
        case_dir = Path(case_dir)
        if stage not in self.STAGE_ORDER:
            raise ValueError(f'unknown mesh stage: {stage}')
        destination = (
            case_dir / 'foammesh' / 'dictionaries' / 'stage-runs.json')
        if not destination.is_file():
            return ()
        document = json.loads(destination.read_text(encoding='utf-8'))
        stages = document.setdefault('stages', {})
        removed = tuple(
            name for name in self.STAGE_ORDER[self.STAGE_ORDER.index(stage):]
            if name in stages)
        for name in removed:
            stages.pop(name, None)
        if not removed:
            return ()
        temporary = destination.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, destination)
        return removed

    def mark_pipeline_run(self, case_dir) -> Path:
        result = None
        for stage in (
                'blockMesh', 'surfaceFeatures', 'castellation',
                'snap', 'layers', 'checkMesh'):
            result = self.mark_stage_run(case_dir, stage)
        return result

    @staticmethod
    def effective_dictionaries(case_dir, db=None) -> dict:
        """Return current generated text, last-run text, diffs, and warnings."""
        case_dir = Path(case_dir)
        manifest_path = (
            case_dir / 'system' / 'foammesh-dictionaries-manifest.json')
        if not manifest_path.is_file():
            raise FileNotFoundError(
                'generate dictionaries before opening the effective viewer')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        current = {}
        for item in manifest.get('files', ()):
            path = Path(item['path'])
            if not path.is_absolute():
                path = case_dir / path
            if path.is_file():
                current[item['name']] = path.read_text(encoding='utf-8')
        configuration_sha = manifest.get('configuration_sha256')
        if db is not None:
            from foammesh.core.geometry import BBox
            preview = CaseBuilder(db, BBox(0, 1, 0, 1, 0, 1))
            preview.load_case_context(case_dir)
            current.update({
                'blockMeshDict': preview.block_mesh_dict(),
                'controlDict': preview.control_dict(),
                'surfaceFeaturesDict': preview.surface_features_dict(),
                'snappyHexMeshDict': preview.snappy_hex_mesh_dict(),
                'decomposeParDict': preview.decompose_par_dict(),
            })
            configuration_sha = preview._configuration_sha256()
            stale_stages = preview.stage_staleness(case_dir)
            # The preview above regenerated every dictionary from the *current*
            # configuration, so its warnings describe what the user is looking
            # at. The manifest's describe whatever was generated last. Showing
            # the manifest's beside freshly regenerated text would attribute
            # old substitutions to new dictionaries -- and miss new ones.
            warnings = list(preview.warnings)
        else:
            stale_stages = {}
            warnings = list(manifest.get('warnings', []))
        snapshot_path = (
            case_dir / 'foammesh' / 'dictionaries' / 'last-run.json')
        previous = {}
        previous_sha = None
        if snapshot_path.is_file():
            snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
            previous = dict(snapshot.get('files', {}))
            previous_sha = snapshot.get('configuration_sha256')
        diffs = {}
        for name in sorted(set(current) | set(previous)):
            diff = ''.join(difflib.unified_diff(
                previous.get(name, '').splitlines(keepends=True),
                current.get(name, '').splitlines(keepends=True),
                fromfile=f'last-run/{name}',
                tofile=f'current/{name}'))
            diffs[name] = diff
        return {
            'schema_version': 1,
            'target': manifest.get('target'),
            'configuration_sha256': configuration_sha,
            'generated_configuration_sha256':
                manifest.get('configuration_sha256'),
            'last_run_configuration_sha256': previous_sha,
            'stale_since_last_run': (
                previous_sha is not None and
                previous_sha != configuration_sha),
            'stale_stages': stale_stages,
            'stage_sha256': manifest.get('stage_sha256', {}),
            'warnings': warnings,
            'source_links': snappy_source_links(),
            'current': current,
            'last_run': previous,
            'diffs': diffs,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _dictionary_stage(name: str) -> str:
    return {
        'blockMeshDict': 'blockMesh',
        'surfaceFeaturesDict': 'surfaceFeatures',
        'snappyHexMeshDict': 'snappyHexMesh',
        'decomposeParDict': 'decomposePar',
        'controlDict': 'control',
        'meshQualityDict': 'meshQuality',
    }.get(str(name), str(name))


def snappy_source_links() -> dict[str, tuple[str, ...]]:
    """Dictionary sections to their authoritative configuration fields."""
    return {
        'blockMeshDict/blocks': (
            'baseGrid/sizingMode', 'baseGrid/targetCellSize',
            'baseGrid/numCellsX', 'baseGrid/numCellsY',
            'baseGrid/numCellsZ', 'baseGrid/boundingHex6'),
        'surfaceFeaturesDict': (
            'castellation/refinementSurfaces/{id}/includedAngle',),
        'snappyHexMeshDict/castellatedMeshControls': (
            'castellation/*', 'geometry/{id}/castellationGroup',
            'geometry/{id}/cfdType', 'geometry/{id}/nonConformal',
            'geometry/{id}/interRegion'),
        'snappyHexMeshDict/snapControls': ('snap/*',),
        'snappyHexMeshDict/addLayersControls': (
            'addLayers/*', 'geometry/{id}/layerGroup',
            'geometry/{id}/slaveLayerGroup'),
        'snappyHexMeshDict/meshQualityControls': ('meshQuality/*',),
        'decomposeParDict': ('mesh/execution/*',),
    }
