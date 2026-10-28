"""Job construction and the supervised launch of the Gmsh runner.

The runner is deliberately a subprocess. Gmsh aborts the process on some bad
geometry, and an abort inside the GUI would take the whole application with it.
Licensing does not require the separation (see plan 22 D1); crash isolation
does.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import json
import re
import uuid

from foammesh.core.engine.base import StageRun

from .manifest import RunBuilder, RunLayout
from .plan_derivation import (
    JobIntent, companion_export_formats, derive_from_native,
)

JOB_SCHEMA_VERSION = 1

#: C31-04. The version of the source-aware entity identity written into the
#: job and echoed back in the runner's per-source import receipt. Version 1 is
#: the source-local integer index that shipped before this: it is still
#: written, and still read by a runner that has nothing better, but it cannot
#: name an entity once a case holds more than one imported file.
ENTITY_SCHEMA_VERSION = 2


class ExecutionError(RuntimeError):
    def __init__(self, message='', *, details: dict | None = None):
        super().__init__(message)
        #: Plan 49 SR7. Structured refusal for the facade (``surface_intent_
        #: refused`` and its issues); empty for every other refusal.
        self.details = dict(details or {})


def assembly_layer_refusal(volumes: int) -> str:
    """Word for word what ``runner_v1`` says, said before the run starts.

    DP-52/DP-53. The runner is a standalone script executed inside WSL and
    cannot import this package, so the sentence necessarily exists twice;
    `test_the_seam_refuses_in_the_words_the_runner_would_have_used`
    reconstructs the runner's copy from its source and compares the two, so
    the pair cannot drift into two explanations of one refusal.
    """
    return (f'boundary layers are not supported on a {volumes}'
            '-volume assembly unless every patch they grow on bounds '
            'one and the same volume: the layer is carved out of a '
            'single core, and a selection spanning more than one '
            'leaves neither side closed. Name the patches of one '
            'volume, mesh this geometry without layers, or split it '
            'into one job per volume.')


def nothing_chosen_layer_refusal() -> str:
    """Layers on, the ticks are the answer, and nothing is ticked.

    Plan 37 F3d. ``runner_v1`` refuses this after WSL has booted and the
    geometry has been imported ("boundary layers are switched on and no
    surface was chosen to grow them"), and the Boundary layers page has said
    since Plan 33 section 1.1 that the run will be refused. Nothing it needs
    is only known inside the run, so it is refused at run start, in the same
    terms, before anything is written.
    """
    return ('boundary layers are switched on and no surface was chosen to '
            'grow them, so no layer could be grown and the mesh would have '
            'no near-wall resolution at all. Tick the surfaces a layer '
            'stands on, ask for every eligible wall, or turn boundary layers '
            'off.')


def prepared_volume_count(prepared_geometry) -> tuple[int, str]:
    """How many volumes the prepared revision recorded, and who counted them.

    The count is read rather than recomputed: `ensure_prepared` wrote it at
    prepare time from whichever route imported the geometry, and re-running
    the classifier here would put a cold read of a large STL in front of
    every run. ``unknown`` is returned for a revision prepared before this
    was recorded, and an unknown count refuses nothing.
    """
    topology = (prepared_manifest(prepared_geometry).get('preparation')
                or {}).get('topology') or {}
    source = str(topology.get('volumes_source') or 'unknown')
    try:
        volumes = int(topology.get('volumes') or 0)
    except (TypeError, ValueError):
        return 0, 'unknown'
    return volumes, source


def runner_path() -> Path:
    """Where the runner script lives on the host filesystem."""
    from resources import resource

    return Path(resource.file('gmsh/runner_v1.py')).resolve()


def _native_section(db) -> dict:
    """The ``gmsh`` section, with its periodic pairs read from the relations.

    DP-641 joined the Geometry page's cyclic pairs in here, the one reader
    every Gmsh plan, scope check and job goes through. Plan 49 SR5 (DP-5805)
    reads every translation or rotation pair from the relation store instead
    -- SR3 made each of them, from either page, a periodic relation -- and
    keeps only the affine rows of the section's own ``periodicPairs``, which
    have no relation (DP-5832). As copies: the project is not rewritten by
    reading it.
    """
    return _with_relation_periodic_pairs(_gmsh_section(db), db)


def _gmsh_section(db) -> dict:
    """Read the ``gmsh`` configuration section out of the project database.

    MEASURED: ``SimpleDB.getValue`` resolves leaf scalars only and raises
    ``LookupError`` for a section path, and SimpleDB has neither ``toDict`` nor
    ``asDict``. Reading the section through those accessors therefore returned
    ``{}`` for every real database, so a run started from the GUI discarded
    every Gmsh control the user had set and then failed deriving a size it
    could have read. ``data()`` is SimpleDB's own accessor for the content
    tree, so it is tried first and the rest are kept only as adapter fallbacks
    for the injected doubles used in unit tests.
    """
    if db is None:
        return {}
    content = getattr(db, 'data', None)
    if callable(content):
        try:
            section = (content() or {}).get('gmsh')
        except Exception:
            section = None
        if isinstance(section, dict):
            return section
    for accessor in ('getValue', 'get'):
        method = getattr(db, accessor, None)
        if method is None:
            continue
        try:
            value = method('gmsh')
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    snapshot = getattr(db, 'toDict', None) or getattr(db, 'asDict', None)
    if snapshot is not None:
        try:
            return dict((snapshot() or {}).get('gmsh') or {})
        except Exception:
            return {}
    return {}


_FACE_NAME = re.compile(r'^face(\d+)$')


def group_surface_indices(group) -> list[int]:
    """The imported surface indices one prepared group covers.

    WP-01 F-01. There is one producer of this ordering -- the geometry store,
    which records a ``source_ref`` per CAD face -- and this is the one reader
    of it. Nothing in the application ever wrote ``surface_indices`` or
    ``surface_index``, so a map that looked only for those keys was empty for
    every real prepared revision: MEASURED on ``duct.step``, six groups and
    ``scope_surface_map() == {}``, which silently scoped every Distance field,
    Threshold, curve control and periodic pair to nothing.

    Plan 28 names a CAD face's staged solid ``face<N>`` where ``N`` counts
    faces across the whole shape, which is the order Gmsh numbers surfaces in.
    ``face_index`` counts within one body and therefore repeats on a
    multi-body import, so the name wins where the store recorded one.
    """
    if not isinstance(group, dict):
        return []
    indices = group.get('surface_indices')
    if indices is None and group.get('surface_index') is not None:
        indices = [group['surface_index']]
    if indices:
        return [int(item) for item in indices]
    derived: list[int] = []
    for reference in group.get('source_refs') or ():
        if not isinstance(reference, dict):
            continue
        match = _FACE_NAME.match(str(reference.get('original_name') or ''))
        if match is not None:
            derived.append(int(match.group(1)))
            continue
        index = reference.get('face_index')
        if isinstance(index, int) and not isinstance(index, bool):
            derived.append(index)
            continue
        # DP-58. A tessellated file holding one `solid` block has no face to
        # index: the group is the whole file, and the store records
        # `face_index: null` beside the body it came from. Reading only the
        # two CAD keys made that group cover nothing, so it produced neither
        # an entity identity nor a legacy name.
        #
        # MEASURED across every prepared group manifest under `test_cases`: a
        # null face index appears on exactly one shape, 90 single-group STL
        # files, and on no STEP, IGES or OBJ source and no file that more than
        # one group names -- the multi-solid STL carries face indices 0 and 1.
        # The body index is the surface index there and nowhere else.
        body = reference.get('body_index')
        if isinstance(body, int) and not isinstance(body, bool):
            derived.append(body)
    return derived


def region_volume_indices(region) -> list[int]:
    """The imported volume indices one prepared region covers.

    Same fault as ``group_surface_indices``: the store writes ``solid_index``
    inside the region's ``source_ref`` and never a ``volume_indices`` key, so
    every volume control resolved to nothing.
    """
    if not isinstance(region, dict):
        return []
    indices = region.get('volume_indices')
    if indices is None and region.get('volume_index') is not None:
        indices = [region['volume_index']]
    if indices:
        return [int(item) for item in indices]
    derived: list[int] = []
    for reference in region.get('source_refs') or ():
        if not isinstance(reference, dict):
            continue
        for key in ('solid_index', 'body_index'):
            index = reference.get(key)
            if isinstance(index, int) and not isinstance(index, bool):
                derived.append(index)
                break
    return derived


def entity_id(source_id: str, kind: str, key) -> str:
    """The stable identity of one imported entity: which file, and which of it.

    C31-04. ``face0``, ``Part_1`` and ``volume0`` are labels, and the integer
    beside them is an index into *one* source file. MEASURED on a case holding
    ``duct.step`` and ``pipe.step`` prepared together: ``scope_surface_map``
    returned index ``0`` for the duct's ``face0`` and index ``0`` again for
    the pipe's, ``scope_volume_map`` returned volume index ``0`` for both
    bodies, and ``surface_names`` collapsed to six entries -- the pipe's three
    names overwriting the duct's first three -- for a model with nine
    surfaces. The runner then indexed those into one global tag list, so a
    control authored on the pipe was applied to the duct and nothing said so.

    The identity is the source's ``geometry_id`` (which the prepared revision
    already assigns and stages every file under), the kind of entity, and the
    entity's key *within that source* -- the same number as before, but no
    longer pretending to be global.
    """
    return f'{source_id}:{kind}:{key}'


def _source_of(record) -> str:
    """The ``geometry_id`` a prepared group or region belongs to."""
    if not isinstance(record, dict):
        return ''
    return str(record.get('geometry_id') or '').strip()


def group_surface_entities(group) -> list[str]:
    """The source-aware entity IDs one prepared group covers.

    Empty for a manifest with no ``geometry_id`` -- a hand-built test double,
    or a revision written before the prepared schema carried one -- which
    leaves the caller on the source-local integer path rather than inventing
    an identity that cannot be checked.
    """
    source_id = _source_of(group)
    if not source_id:
        return []
    return [entity_id(source_id, 'surface', index)
            for index in group_surface_indices(group)]


def region_volume_entities(region) -> list[str]:
    """The source-aware entity IDs one prepared region covers."""
    source_id = _source_of(region)
    if not source_id:
        return []
    return [entity_id(source_id, 'volume', index)
            for index in region_volume_indices(region)]


def shared_surface_aliases(prepared_geometry) -> dict:
    """Source id -> ``{second listing: first listing}`` of one shared face.

    DP-546. The CAD import walks each solid's faces in turn, so a face two
    solids share -- one ``TopoDS_Face`` in both shells, which is what a
    fragmented assembly is -- is listed twice: once per solid. Gmsh binds it
    once, at the first listing, and numbers every later face from there, so
    the second listing has no surface of its own. MEASURED on G6
    ``tee_with_plug.brep`` (audit 0924): eight listings, seven surfaces;
    ``body0_face2`` and ``body1_face4`` are the plug's end face seen from each
    body, and the positional join left ``body1_face4`` and its scope
    ``640cae55...`` pointing at a ninth surface that does not exist.

    The import marks such a listing as its own interface twin
    (``interface_patch_uuid == patch_uuid``: "one face, both solids"). Two
    such listings in one source are the same face when they measure the same
    area and border the same faces -- every edge of one is an edge of the
    other, so each lists the other as a neighbour. A listing that matches no
    earlier listing, or more than one, is left alone rather than guessed at.
    """
    manifest = _group_manifest(prepared_geometry)
    shared: dict[str, list[tuple]] = {}
    for group in manifest.get('groups') or ():
        if not isinstance(group, dict):
            continue
        uuid_ = str(group.get('patch_uuid') or '').strip()
        if not uuid_ or str(group.get('interface_patch_uuid') or '').strip() \
                != uuid_:
            continue
        indices = group_surface_indices(group)
        try:
            area = float(group.get('area'))
        except (TypeError, ValueError):
            continue
        if len(indices) != 1:
            continue
        rim = frozenset(str(item) for item in
                        group.get('adjacent_patch_uuids') or ()) | {uuid_}
        shared.setdefault(_source_of(group), []).append(
            (int(indices[0]), area, rim))
    aliases: dict[str, dict[int, int]] = {}
    for source, listings in shared.items():
        listings.sort(key=lambda item: item[0])
        for position, (index, area, rim) in enumerate(listings):
            partners = [earlier for earlier, other_area, other_rim
                        in listings[:position]
                        if other_rim == rim
                        and abs(other_area - area) <= 1e-9 * max(abs(area),
                                                                 1e-30)]
            if len(partners) == 1:
                first = aliases.get(source, {}).get(partners[0], partners[0])
                aliases.setdefault(source, {})[index] = first
    return aliases


def import_surface_position(aliases: dict, index: int) -> int:
    """Where listing *index* lands among the surfaces Gmsh imports, from 0.

    DP-546. A shared face's second listing is the first listing's surface,
    and every listing after a skipped one moves down by one.
    """
    first = int((aliases or {}).get(int(index), int(index)))
    return first - sum(1 for alias in (aliases or {}) if int(alias) < first)


def _with_relation_periodic_pairs(section: dict, db) -> dict:
    """*section* with ``periodicPairs`` = its affine rows + the valid periodic
    relations (``surface_roles.periodic_relation_rows``).

    A translation or rotation row of the section is not read: its relation
    is. A relation that is not valid is not lowered; ``check_job`` refuses
    the run with its name and reason.
    """
    from .surface_roles import periodic_relation_rows

    existing = section.get('periodicPairs') or {}
    lowered, _refused = periodic_relation_rows(db)
    if isinstance(existing, dict):
        items = list(existing.items())
    else:
        items = list(enumerate(existing))
    affine = [(key, row) for key, row in items if isinstance(row, dict)
              and str(row.get('transform') or '') == 'affine']
    if not lowered and len(affine) == len(items):
        return section
    section = dict(section)
    if isinstance(existing, dict) or not existing:
        merged = dict(affine)
        for row in lowered:
            merged[f'relation-{row.get("relationId") or row["name"]}'] = row
    else:
        merged = [row for _key, row in affine] + list(lowered)
    section['periodicPairs'] = merged
    return section


def job_interface_pairs(db, intent) -> list[dict]:
    """The ``interfacePairs`` a job carries: the Geometry page's rows and,
    Plan 49 SR5, the region-interface relations of the SR1 store (one pair
    per coupling; see ``surface_roles.relation_interface_pairs``)."""
    from .surface_roles import relation_interface_pairs
    rows = interface_pair_rows(db)
    block = getattr(intent, 'surface_intent', None) or {}
    if not block:
        return rows
    return relation_interface_pairs(block, rows)


def interface_pair_rows(db) -> list[dict]:
    """The enabled ``interfacePairs`` rows, in the job's shape.

    DP-548. The Geometry page's pair editor saves a row, the snappy route
    reads it (``CaseBuilder.interface_pairs``), and the Gmsh job never carried
    it: MEASURED on G6 ``tee_with_plug.brep`` (audit 0924), whose H5 held
    ``tee_plug_contact`` while ``job.json`` and ``result.json`` named no pair,
    so nothing could say whether the 36-face interface came from the pair or
    from the face the two solids share. The runner acts on none of these; it
    is given them so it can say, pair by pair, what the mesh did there.
    """
    rows = None
    content = getattr(db, 'data', None)
    if callable(content):
        try:
            rows = (content() or {}).get('interfacePairs')
        except Exception:
            rows = None
    if not isinstance(rows, dict):
        getter = getattr(db, 'getElements', None)
        if getter is None:
            return []
        try:
            rows = dict(getter('interfacePairs'))
        except Exception:
            return []

    def field(row, key, default=None):
        if isinstance(row, dict):
            value = row.get(key, default)
        else:
            try:
                value = row.value(key)
            except Exception:
                return default
        return getattr(value, 'value', value)

    pairs = []
    for key, row in sorted((rows or {}).items(), key=lambda item: str(item[0])):
        if not bool(field(row, 'enabled', True)):
            continue
        master = str(field(row, 'masterScopeToken', '') or '').strip()
        slave = str(field(row, 'slaveScopeToken', '') or '').strip()
        name = str(field(row, 'name', '') or '').strip()
        try:
            tolerance = float(field(row, 'matchTolerance', 1e-6))
        except (TypeError, ValueError):
            tolerance = 1e-6
        transform = str(field(row, 'transform', 'coincident') or '')
        entry = {
            'pairId': name or str(key),
            'name': name or str(key),
            'coupling': str(field(row, 'coupling', 'conformal') or ''),
            'transform': transform,
            'masterScope': master,
            'slaveScope': slave,
            'matchTolerance': tolerance,
        }
        if transform != 'coincident':
            # DP-641. The values a transformed pair is built from; dropped
            # here, the transform could not reach the job at all.
            def vector(stem, default):
                values = []
                for axis, fallback in zip('XYZ', default):
                    try:
                        values.append(float(field(row, f'{stem}{axis}',
                                                  fallback)))
                    except (TypeError, ValueError):
                        values.append(fallback)
                return values
            try:
                angle = float(field(row, 'rotationAngleDegrees', 0.0) or 0.0)
            except (TypeError, ValueError):
                angle = 0.0
            entry.update({
                'translation': vector('translation', (0.0, 0.0, 0.0)),
                'rotationCentre': vector('rotationCentre', (0.0, 0.0, 0.0)),
                'rotationAxis': vector('rotationAxis', (0.0, 0.0, 1.0)),
                'rotationAngleDegrees': angle})
        pairs.append(entry)
    return pairs


def seed_point(prepared_geometry):
    """The fluid seed the prepared revision recorded, in metres, or ``None``.

    Plan 30 WP-06a/b. The runner reads ``seedPoint`` to decide which shell of
    a tessellated import is the domain; the seed itself has been recorded on
    every prepared revision all along (from the region point the user placed,
    or from the import's own fluid-seed diagnosis) and simply never reached
    the job, so the runner fell back to alternating roles by nesting depth.
    """
    manifest = prepared_manifest(prepared_geometry)
    values = manifest.get('fluid_seed')
    if not values:
        return None
    try:
        seed = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    return seed if len(seed) == 3 else None


def shell_roles(prepared_geometry) -> dict:
    """Staged file stem -> the role its shell was declared to have.

    Only geometries that say something appear here: the runner infers a role
    from nesting for every shell it is told nothing about, and that inference
    is what a box with an obstacle in it needs. A geometry whose regions are
    all excluded is a hole, one whose regions are all solid is a body, and
    the shells of a file that holds several are numbered ``stem#1``, ``#2``
    -- which a whole-file declaration does not name, so the runner reports it
    as unmatched rather than silently applying it to one of them.
    """
    manifest = prepared_manifest(prepared_geometry)
    roles: dict[str, str] = {}
    for record in manifest.get('sources') or ():
        if not isinstance(record, dict):
            continue
        role = str(record.get('shell_role') or '').strip().lower()
        name = str(record.get('prepared_name') or '')
        if not role or not name:
            continue
        roles[Path(name).stem] = role
    return roles


def scope_surface_map(prepared_geometry) -> dict:
    """Prepared scope token -> imported surface indices.

    Gmsh numbers surfaces in import order, and the prepared geometry records
    the same order when it stages the CAD, so an index is a stable handle.
    A scope this cannot place is simply absent, and the runner reports it as
    unresolved rather than refining nothing and calling it done.
    """
    if prepared_geometry is None:
        return {}
    manifest = _group_manifest(prepared_geometry)
    groups = manifest.get('groups') or ()
    # DP-546. The index a group records is its listing in the face walk; the
    # index Gmsh gives it skips every second listing of a shared face.
    shared = shared_surface_aliases(prepared_geometry)
    mapping: dict[str, list[int]] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        token = str(group.get('patch_uuid') or '').strip()
        if not token:
            continue
        indices = group_surface_indices(group)
        if not indices:
            continue
        aliases = shared.get(_source_of(group)) or {}
        mapping[token] = [import_surface_position(aliases, index)
                          for index in indices]
    return mapping


def scope_volume_map(prepared_geometry) -> dict:
    """Prepared region token -> imported volume indices.

    Volume controls scope to regions, not surfaces, so they resolve against a
    separate map. Without it every volume control silently matches nothing.
    """
    if prepared_geometry is None:
        return {}
    manifest = _group_manifest(prepared_geometry)
    mapping: dict[str, list[int]] = {}
    for region in manifest.get('regions') or ():
        if not isinstance(region, dict):
            continue
        token = str(region.get('region_uuid') or '').strip()
        if not token:
            continue
        indices = region_volume_indices(region)
        if indices:
            mapping[token] = indices
    return mapping


def region_display_names(db) -> dict:
    """Prepared region UUID -> the name the project tree shows for that body.

    DP-419. A Gmsh cell zone was named from the prepared region record alone,
    which is written once at import from whatever the CAD file called its
    solid and is never rewritten. The snappy branch names the same thing from
    the *volume row in the tree*, which the user can rename and type a
    CellZone on. MEASURED on all four models this campaign meshed on both
    engines: Gmsh published `Part_1_solid_1_11ff99c5` where snappy published
    `coaxial_ducts_core`, for the same model and the same two regions. Two
    stores with nothing between them.

    The volume row now carries the region it stands for, so this is the link.
    A row with no `regionUuid` -- every row a project saved before DP-419, and
    every volume the tree composed rather than read -- simply is not here, and
    the region keeps the name it always had.
    """
    rows = None
    content = getattr(db, 'data', None)
    if callable(content):
        try:
            rows = (content() or {}).get('geometry')
        except Exception:
            rows = None
    if not isinstance(rows, dict):
        getter = getattr(db, 'getElements', None)
        if getter is None:
            return {}
        try:
            rows = dict(getter('geometry'))
        except Exception:
            return {}

    def field(row, key):
        if isinstance(row, dict):
            value = row.get(key)
        else:
            try:
                value = row.value(key)
            except Exception:
                return ''
        return str(getattr(value, 'value', value) or '').strip()

    names: dict[str, str] = {}
    for row in (rows or {}).values():
        if field(row, 'gType') != 'volume':
            continue
        token = field(row, 'regionUuid')
        label = field(row, 'name')
        if token and label:
            names[token] = label
    names.update(typed_region_names(db))
    return names


def typed_region_names(db) -> dict:
    """Prepared region UUID -> the name "how many regions?" applied to it.

    DP-865. On Gmsh a region is a solid (Plan 36 RP11), and applying one
    types the solid's volume control (``volumeType``) under the name the user
    gave it. The tree's volume row was the only name this module read, and a
    case built without the tree -- the facade, the CLI, the campaign --
    has no such row, so MEASURED on jacketed_pipe and shell_and_tube the
    zones published as ``Part_1_solid_1``/``_2`` while the regions applied
    were ``fluid``/``jacket`` and ``shell``/``tube``. A typed control is the
    region; its name is what the zone is called. An untyped control is a
    sizing control and names nothing.
    """
    getter = getattr(db, 'getElements', None)
    if getter is None:
        return {}
    try:
        rows = dict(getter('gmsh/volumeControls') or {})
    except Exception:  # noqa: BLE001 - a case without the list
        return {}
    from foammesh.core.mesh.cad_solids import typing_of

    names: dict[str, str] = {}
    for token, item in typing_of(rows).items():
        label = str(item.get('name') or '').strip()
        if token and label and item.get('type'):
            names[token] = label
    return names


def _patch_labels(manifest) -> set:
    """Every name the surfaces of this revision publish under.

    DP-419. A Gmsh model holds its physical surfaces and its physical volumes
    in one namespace, so a region cannot take a name a patch already has. The
    region's own ``solver_name`` was disambiguated against exactly this set
    when it was prepared; a name read off the tree has not been, so it is
    checked here before it is taken.
    """
    labels = set()
    for group in (manifest or {}).get('groups') or ():
        label = str((group or {}).get('solver_name')
                    or (group or {}).get('name') or '').strip()
        if label:
            labels.add(label)
    return labels


def _chosen_name(region, chosen: dict, taken: set | None = None) -> str:
    """What the region publishes under: the tree's word for it, or its own.

    DP-419. The tree's name is a display name -- the user may have typed a
    space into it -- so it makes the same trip through ``solver_stem`` that
    every patch name makes. It is taken only when that trip leaves a word: not
    empty, not one of the names OpenFOAM reserves, and not one a patch of the
    same revision already publishes under. Anything else and the region keeps
    the name it was prepared with, which is already safe on all three counts.
    """
    from foammesh.core.geometry.patches.ops import RESERVED_NAMES
    from foammesh.core.geometry.prepared import solver_stem

    own = str((region or {}).get('solver_name')
              or (region or {}).get('name') or '').strip()
    token = str((region or {}).get('region_uuid') or '').strip()
    label = str((chosen or {}).get(token) or '').strip() if token else ''
    if not label:
        return own
    stem = solver_stem(label)
    if not stem or stem in RESERVED_NAMES or stem in (taken or set()):
        return own
    return stem


def volume_names(prepared_geometry, chosen: dict | None = None) -> dict:
    """Imported volume tag (1-based, as a string) -> region name."""
    if prepared_geometry is None:
        return {}
    manifest = _group_manifest(prepared_geometry)
    taken = _patch_labels(manifest)
    names: dict[str, str] = {}
    for region in manifest.get('regions') or ():
        if not isinstance(region, dict):
            continue
        indices = region_volume_indices(region)
        label = _chosen_name(region, chosen, taken)
        if not label:
            continue
        for index in indices:
            names[str(int(index) + 1)] = label
    return names


def volume_types(prepared_geometry, types: dict | None,
                 chosen: dict | None = None) -> dict:
    """Published volume name -> ``{'type', 'region_type'}``, for the typed.

    Plan 36 RP11. *types* maps a prepared ``region_uuid`` to ``fluid`` or
    ``solid`` -- the Gmsh volume controls' ``volumeType``. The name is the one
    ``volume_names`` publishes the region under, so the publisher's region
    metadata (looked up by physical name) finds it. An untyped region is not
    here and publishes as it always did.
    """
    if prepared_geometry is None or not types:
        return {}
    manifest = _group_manifest(prepared_geometry)
    taken = _patch_labels(manifest)
    out: dict[str, dict] = {}
    for region in manifest.get('regions') or ():
        if not isinstance(region, dict):
            continue
        token = str(region.get('region_uuid') or '').strip()
        kind = str((types or {}).get(token) or '').strip()
        if not token or not kind:
            continue
        label = _chosen_name(region, chosen, taken)
        if label:
            out[label] = {'type': kind, 'region_type': kind}
    return out


def volume_fallback_names(prepared_geometry,
                          chosen: dict | None = None) -> dict:
    """Source ID -> what to call a volume of that source no region claimed.

    DP-423. An imported surface that closes into more than one volume gets
    one region record, not one per volume: the feature-angle split path
    writes ``regions = [{...'solid_index': 0}]`` at ``geometry/store.py:975``
    whatever the file turns out to hold. So only the first volume is ever
    named, and the runner called the rest after the Gmsh tag they happened to
    get. MEASURED on six meshes: ``box_with_cavity`` named 1 of 8 zones,
    ``venturi`` 1 of 8, ``heat_exchanger`` 1 of 11, ``manifold`` 1 of 12,
    ``centrifugal_impeller`` 1 of 49, ``finned_tube`` 1 of 52 -- 134 of 140
    zones carrying a name with no relation to the model.

    The volumes are not unattributed, though. The runner knows which file
    produced each one and in what order (``{source}:volume:{n}``), and the
    user named that file once. So an unclaimed volume is named after the
    region that file prepared, with its position appended -- and the first
    volume, which the region already names, is left exactly as it was.

    Only where the file prepared exactly one region. Where it prepared
    several there is no telling which of them an unclaimed volume belongs to,
    and naming it after whichever came first would be a guess presented as a
    fact. That is not a narrow escape: all six models above prepare one
    region per source, including the two-source ``centrifugal_impeller``.
    """
    manifest = _group_manifest(prepared_geometry)
    taken = _patch_labels(manifest)
    counted: dict[str, int] = {}
    labels: dict[str, str] = {}
    for region in manifest.get('regions') or ():
        if not isinstance(region, dict):
            continue
        source = _source_of(region)
        if not source:
            continue
        counted[source] = counted.get(source, 0) + 1
        labels[source] = _chosen_name(region, chosen, taken)
    return {source: label for source, label in labels.items()
            if counted.get(source) == 1 and label}


def scope_entity_map(prepared_geometry) -> dict:
    """Prepared scope token -> the source-aware entity IDs it covers.

    C31-04. This is the map the runner resolves against; ``scope_surface_map``
    and ``scope_volume_map`` remain beside it as the version-1 form, read only
    by a runner or a fixture that has no entity map, and correct only while a
    case holds a single imported file.
    """
    manifest = _group_manifest(prepared_geometry)
    surfaces: dict[str, list[str]] = {}
    for group in manifest.get('groups') or ():
        token = str((group or {}).get('patch_uuid') or '').strip()
        entities = group_surface_entities(group)
        if token and entities:
            surfaces[token] = entities
    volumes: dict[str, list[str]] = {}
    for region in manifest.get('regions') or ():
        token = str((region or {}).get('region_uuid') or '').strip()
        entities = region_volume_entities(region)
        if token and entities:
            volumes[token] = entities
    return {'surfaces': surfaces, 'volumes': volumes}


def entity_name_map(prepared_geometry, chosen: dict | None = None) -> dict:
    """Entity ID -> the solver name its group or region publishes under.

    The same collision as the scopes: ``surface_names`` keys on a global tag
    computed from a source-local index, so on the two-source duct/pipe case it
    lost three of the duct's six names to the pipe's three and never mentioned
    surfaces 7-9 at all.
    """
    manifest = _group_manifest(prepared_geometry)
    surfaces: dict[str, str] = {}
    for group in manifest.get('groups') or ():
        label = str((group or {}).get('solver_name')
                    or (group or {}).get('name') or '').strip()
        if not label:
            continue
        for identity in group_surface_entities(group):
            surfaces[identity] = label
    volumes: dict[str, str] = {}
    taken = set(surfaces.values())
    for region in manifest.get('regions') or ():
        label = _chosen_name(region, chosen, taken)
        if not label:
            continue
        for identity in region_volume_entities(region):
            volumes[identity] = label
    return {'surfaces': surfaces, 'volumes': volumes}


def source_identity_records(prepared_geometry) -> list[dict]:
    """One record per staged source, in the order the job lists its geometry.

    The runner receives a list of paths and has no way to tell which prepared
    source each one is; this is that join, and it is what turns "the surfaces
    this file added" into entity IDs the scopes are keyed on. ``confidence``
    is the weakest mapping confidence the prepared revision recorded for the
    source's own groups, carried so the receipt can say how much the mapping
    is worth rather than implying it is exact.
    """
    manifest = prepared_manifest(prepared_geometry)
    groups = _group_manifest(prepared_geometry).get('groups') or ()
    shared = shared_surface_aliases(prepared_geometry)
    confidence: dict[str, float] = {}
    counted: dict[str, int] = {}
    for group in groups:
        source_id = _source_of(group)
        if not source_id:
            continue
        value = float((group or {}).get('mapping_confidence', 1.0) or 0.0)
        confidence[source_id] = min(confidence.get(source_id, 1.0), value)
        counted[source_id] = counted.get(source_id, 0) + len(
            group_surface_indices(group))
    records = []
    for index, record in enumerate(manifest.get('sources') or ()):
        if not isinstance(record, dict):
            continue
        source_id = str(record.get('geometry_id') or '').strip()
        records.append({
            'index': index,
            'source_id': source_id,
            'prepared_name': str(record.get('prepared_name') or ''),
            'display_name': str(record.get('display_name') or ''),
            'source_format': str(record.get('source_format') or ''),
            'declared_surfaces': counted.get(source_id, 0),
            'mapping_confidence': confidence.get(source_id, 1.0),
            # DP-546. Listings of a face two solids share, second -> first:
            # Gmsh imports that face once, so the runner must not spend an
            # imported surface on the second listing.
            'shared_surfaces': {
                str(alias): int(first) for alias, first in sorted(
                    (shared.get(source_id) or {}).items())},
        })
    return records


def _group_manifest(prepared_geometry) -> dict:
    """The prepared group manifest, however the caller carries it.

    The facade passes ``prepared.reference`` -- a ``PreparedGeometryRef``,
    which holds ``group_manifest_path`` and *not* the manifest itself. Reading
    only the in-memory attribute therefore found nothing on the single code
    path that matters, so every derived name came back empty and every Gmsh
    patch published as ``face_<tag>``. MEASURED on the finned heat sink: 132 of
    132 patches unrated for fidelity.
    """
    if isinstance(prepared_geometry, dict):
        # R60/R152. `geometry.prepared.current` hands the GUI plain JSON, not
        # a `PreparedGeometryRef`, so a page asking which patch is surface 2
        # got `{}` from every getattr below and had nothing to show but the
        # bare index the dialog already displayed.
        manifest = prepared_geometry.get('group_manifest')
        if isinstance(manifest, dict) and manifest:
            return manifest
        if 'groups' in prepared_geometry or 'regions' in prepared_geometry:
            return prepared_geometry
        return {}
    manifest = getattr(prepared_geometry, 'group_manifest', None)
    if manifest:
        return manifest
    path = getattr(prepared_geometry, 'group_manifest_path', None)
    if path is None:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def surface_tag_offsets(prepared_geometry) -> dict:
    """Source id -> how many surfaces were imported before that source.

    DP-470. `group_surface_indices` counts within one file, and the two
    readers of it below turned a source-local index into a Gmsh tag by adding
    one -- which is right for a single source and wrong for every source after
    the first. Two single-solid STLs both yield index 0, both claim tag 1, and
    the DP-58 rule then drops the tag rather than let one name the other's
    surface. Correct, and it means no multi-source model can offer a boundary
    to size at all: MEASURED on `tube_bundle`, whose prepared geometry holds
    two named groups and answered zero names.

    What was missing when DP-58 was written is now on the record. C31-04 added
    the source table, and each entry carries `declared_surfaces` and the
    `index` the job lists its geometry in -- which is the same order Gmsh
    imports in, and so is the offset. `tube_bundle` declares one surface and
    `tube_bundle_farfield` declares one, so their tags are 1 and 2 rather than
    1 and 1.

    Empty when the table cannot carry the arithmetic -- a source declaring no
    surfaces leaves every source after it unplaceable -- and an empty answer
    puts both readers back on the source-local path, where the DP-58 rule
    still protects them.
    """
    records = source_identity_records(prepared_geometry)
    if not records:
        return {}
    offsets: dict[str, int] = {}
    running = 0
    for record in sorted(records, key=lambda item: int(item.get('index') or 0)):
        source = str(record.get('source_id') or '').strip()
        declared = int(record.get('declared_surfaces') or 0)
        if not source or declared < 1:
            return {}
        offsets[source] = running
        # DP-546. What the file adds to the model, not what it lists.
        running += declared - len(record.get('shared_surfaces') or {})
    return offsets


def _global_surface_rows(prepared_geometry) -> list:
    """One `(token, label, source, tags)` per prepared group.

    The single place the source-local index is turned into a Gmsh tag, so the
    map from tag to name and the map from tag back to boundary cannot disagree
    -- and they must not, because a row is authored by tag and stamped with
    the boundary that tag stands for at that moment.

    `group_surface_indices` is the reader of the store's ordering. Without the
    `source_refs` fallback it holds, the lookup found nothing for every real
    manifest, the runner fell back to `face_<tag>`, and every published patch
    failed the identity join -- so no Gmsh mesh could carry a per-section
    fidelity verdict (Plan 23 §4). MEASURED on the heat sink: 132/132 unrated.
    """
    manifest = _group_manifest(prepared_geometry)
    offsets = surface_tag_offsets(prepared_geometry)
    shared = shared_surface_aliases(prepared_geometry)
    rows = []
    for group in manifest.get('groups') or ():
        if not isinstance(group, dict):
            continue
        source = _source_of(group)
        base = offsets.get(source, 0) if offsets else 0
        aliases = shared.get(source) or {}
        tags = [import_surface_position(aliases, index) + 1 + base
                for index in group_surface_indices(group)]
        rows.append((str(group.get('patch_uuid') or '').strip(),
                     str(group.get('solver_name') or group.get('name')
                         or '').strip(),
                     source, tags))
    return rows


def surface_categories(prepared_geometry) -> dict:
    """Solver patch name -> the prepared boundary category it publishes with.

    DP-867. The publish step types a patch from this category (a group with
    none is a wall); the runner, which reads "every eligible wall" off the
    surface names alone, read an importer name such as ``face0`` as
    unclassified -- so a STEP nobody had named had no eligible wall and grew
    no layer, although every one of its faces publishes as ``wall``.
    """
    if prepared_geometry is None:
        return {}
    categories: dict[str, str] = {}
    for group in _group_manifest(prepared_geometry).get('groups') or ():
        if not isinstance(group, dict):
            continue
        name = str(group.get('solver_name') or group.get('name') or '').strip()
        if name:
            categories[name] = str(group.get('category') or 'wall').strip()
    return categories


#: Plan 49 SR5. The roles whose surface is no wall a layer may grow on.
def role_surface_categories(categories: dict, surface_intent) -> dict:
    """*categories* with each role surface given the category it is.

    Plan 49 SR5. "Every eligible wall" is read off ``surfaceCategories``;
    an internal face, a baffle or a region interface is no wall to grow a
    layer on, and a surface authored as a symmetry plane is a symmetry plane
    whatever its prepared category says.
    """
    result = dict(categories or {})
    for surface in (surface_intent or {}).get('surfaces') or ():
        if not isinstance(surface, dict) \
                or surface.get('state', 'valid') != 'valid':
            continue
        name = str(surface.get('name') or '')
        if not name:
            continue
        # Plan 49 SR7 (DP-5789). One rule with the snappy layers page.
        from foammesh.core.geometry.boundary_roles import role_category
        category = role_category(result.get(name, ''), surface.get('role'),
                                 surface.get('meshPatchType'))
        if category:
            result[name] = category
    return result


def surface_names(prepared_geometry) -> dict:
    """Imported surface index (as a string tag) -> solver patch name."""
    if prepared_geometry is None:
        return {}
    names: dict[str, str] = {}
    # DP-58. This map is keyed by the *global* Gmsh surface tag, and the
    # body-index fallback in `group_surface_indices` counts within one file:
    # two single-solid STLs both yield index 0, so both claim tag 1 and one
    # names the other's surface. That is the collapse `entity_id` above
    # already records for a two-STEP case, and the fallback would recreate it
    # for tessellated imports -- MEASURED, 12 prepared manifests across the
    # six farfield models.
    #
    # DP-470. `_global_surface_rows` applies the per-source offset when the
    # source table can carry it, so those two STLs now claim 1 and 2 and
    # neither is dropped. The guard below stays for the manifests where it
    # cannot -- a source declaring no surfaces leaves the ones after it
    # unplaceable -- and there a tag two sources claim is still dropped
    # rather than guessed.
    owner: dict[str, str] = {}
    poisoned: set[str] = set()
    for _token, label, source, tags in _global_surface_rows(prepared_geometry):
        if not label:
            continue
        for tag in tags:
            key = str(tag)
            if key in poisoned:
                continue
            if key in owner and owner[key] != source:
                poisoned.add(key)
                names.pop(key, None)
                continue
            if key in names:
                # DP-546. Both listings of a shared face land on one tag; the
                # surface publishes under the first, as the runner names it.
                continue
            owner[key] = source
            names[key] = label
    return names


def build_job(intent: JobIntent, *, geometry, outputs: dict,
              prepared_geometry=None, run_id: str = '',
              profile=None, surface_output=None,
              region_names: dict | None = None,
              interface_pairs=(), execution: dict | None = None) -> dict:
    """Assemble the immutable job the runner consumes.

    Paths are written in the runtime's own namespace when a WSL profile is
    given, so the runner never has to translate anything itself.

    WP-01 F-35. ``geometry`` is a *list* of every staged source in prepared
    manifest order. The runner has always accepted a list and imported each
    entry, but the job only ever carried ``sources[0]``, so a case assembled
    from more than one imported file meshed the first file and silently
    dropped the rest. A single path is still accepted here and written as a
    one-entry list, because the job schema now has exactly one shape.
    """
    def translate(path):
        return (profile.translate_host_path(path) if profile is not None
                else str(Path(path)))

    sources = ([geometry] if isinstance(geometry, (str, Path))
               else list(geometry))
    if not sources:
        raise ExecutionError('a Gmsh job needs at least one geometry source')

    return {
        'schema_version': JOB_SCHEMA_VERSION,
        'engine_id': 'gmsh',
        'units': 'm',
        'run_id': run_id,
        'job_digest': intent.digest,
        'geometry': [translate(item) for item in sources],
        'intent': intent.to_dict(),
        # C31-04. The source-aware identity, and the source table the runner
        # needs to build it: which staged file is which prepared source, in
        # the order `geometry` above lists them. The four version-1 maps below
        # stay for a runner that predates this and for a single-source case,
        # where a source-local index and a global one are the same number.
        'entitySchemaVersion': ENTITY_SCHEMA_VERSION,
        'sources': source_identity_records(prepared_geometry),
        'scopeEntities': scope_entity_map(prepared_geometry),
        'entityNames': entity_name_map(prepared_geometry, region_names),
        'scopeSurfaces': scope_surface_map(prepared_geometry),
        'scopeVolumes': scope_volume_map(prepared_geometry),
        'surfaceNames': surface_names(prepared_geometry),
        # DP-867. The category each patch publishes with, so "every eligible
        # wall" is judged in the runner by what the patch *is*, not by what
        # an importer name like `face0` fails to say.
        'surfaceCategories': role_surface_categories(
            surface_categories(prepared_geometry),
            getattr(intent, 'surface_intent', None)),
        'volumeNames': volume_names(prepared_geometry, region_names),
        # DP-423. What to call a volume the maps above do not name, per
        # source, so an artifact that closed into eight volumes publishes
        # eight zones of its own name rather than seven Gmsh tag numbers.
        'volumeFallback': volume_fallback_names(prepared_geometry,
                                                region_names),
        # WP-06a's shell topology, wired: the seed decides which shell is the
        # domain and a declared role overrides what nesting would infer.
        # Absent means "infer", which is what every job carried until now.
        'seedPoint': seed_point(prepared_geometry),
        'shellRoles': shell_roles(prepared_geometry),
        # DP-548. Every enabled interface pair the user authored, so the
        # result can say what became of each one.
        'interfacePairs': [dict(pair) for pair in interface_pairs or ()],
        'output': {key: translate(value) for key, value in outputs.items()},
        # DP-133. Where to leave the surface pass of a 3D run. Separate from
        # `output` because it is not an export: nothing asked for it, it is
        # not offered as a format, and the round-trip identity check that
        # guards an export does not apply to a mesh that is deliberately
        # half-built. A runner that meshes a section writes nothing here --
        # for a 2D case the surface pass *is* the mesh.
        'surfaceOutput': translate(surface_output) if surface_output else '',
        # DP-678. The CPU cap this run was given and the threads it used.
        # The runner reads threads from `intent.parallel`; this is the record
        # of where that number came from, so a case says what it was capped at.
        'execution': dict(execution or {}),
    }


def _size_preset(db):
    """Plan 40 UXA1: the Mesh size choice, or None when it cannot be read."""
    try:
        return db.getValue('mesh/sizePreset')
    except Exception:                                       # noqa: BLE001
        return None


def _with_automatic_field(intent, db, case_path, profile):
    from dataclasses import replace

    from foammesh.core.sizing import background

    if not background.is_on(db):
        return intent
    try:
        block = background.block(
            case_path, True, background.state_for(db, case_path),
            profile.translate_host_path if profile is not None else None)
    except background.AutomaticFieldRefusal as error:
        raise ExecutionError(str(error)) from None
    return replace(intent, automatic_field=block)


def derive_page_intent(db, bbox, case_path, *, prepared_geometry=None,
                       profile=None, run_id: str = '') -> JobIntent:
    """The intent every Gmsh execution path starts from (Plan 48 GM3).

    ``write_job`` (the batch run) and the resident session's page controls
    both call this, so a control on a page has one meaning whichever way the
    mesh is made. Nothing here depends on which path is asking.
    """
    from foammesh.core.engine.registry import configured_target_solver

    intent = derive_from_native(
        _native_section(db), bbox=bbox,
        target_solver=configured_target_solver(db),
        size_preset=_size_preset(db),
        # Plan 33 W-G1. Per-surface size rows name a prepared boundary; which
        # Gmsh tag that is belongs to the revision being written, not to the
        # saved row.
        prepared_geometry=prepared_geometry,
        metadata={'engine_id': 'gmsh', 'run_id': run_id})
    # Plan 49 SR5 (DP-5783). The surface roles and relations of the patches
    # this revision prepared, in the intent so the digest covers them.
    from .surface_roles import lowered_surface_intent
    block = lowered_surface_intent(db, prepared_geometry)
    if block:
        intent = replace(intent, surface_intent=block)
    # Plan 41 GN25f. The automatic size field as the background field: a
    # field that is switched on and cannot be used refuses here, with the
    # reason, rather than meshing without it.
    return _with_automatic_field(intent, db, case_path, profile)


def gate_target(intent) -> str:
    """The target a Gmsh job's outputs are judged for (Plan 49 SR7)."""
    export = getattr(intent, 'export', None)
    if getattr(export, 'writes_su2', False):
        return 'su2'
    if getattr(export, 'publishes_poly_mesh', False):
        return 'openfoam'
    return 'native'


def surface_intent_gate(db, intent, prepared_geometry, geometry,
                        case_path=None) -> None:
    """Raise :class:`ExecutionError` when the surface intent blocks a run."""
    from foammesh.core.engine import surface_capabilities
    from .surface_roles import _prepared_mapping
    try:
        surface_capabilities.gate(
            db, case_path, 'gmsh', gate_target(intent), stage='run',
            prepared=_prepared_mapping(prepared_geometry),
            geometry=[str(item) for item in geometry or ()],
            dimensionality=getattr(getattr(intent, 'dimensionality', None),
                                   'mode', None))
    except surface_capabilities.SurfaceIntentRefused as refusal:
        raise ExecutionError(str(refusal),
                             details=refusal.details()) from refusal


def check_job(db, intent, prepared_geometry, geometry,
              case_path=None) -> None:
    """Refuse, before anything is written or imported, a job the runner
    would refuse (Plan 48 RR). ``write_job`` and ``session_job`` call it."""
    from foammesh.core.engine.registry import configured_target_solver

    # Plan 49 SR7. The surface intent first, judged by the one capability
    # table the surface dialog and the export read (plan D7: an unresolved
    # surface or relation, an unmeasured or failed periodic pair, a role or
    # type this target cannot carry). The checks below stay as the route's
    # own second line.
    # DP-6084: the case path lets the gate read the geometry store, which
    # says which typed solid each body a region interface names is.
    surface_intent_gate(db, intent, prepared_geometry, geometry, case_path)

    # DP-52/DP-53. The runner refuses this, and it refuses it after WSL has
    # booted and the geometry has been imported -- MEASURED at 71 s on
    # `annulus_shell`, and graded against the geometry rather than against
    # the request. Everything that grading needs is already on the prepared
    # revision, so the same refusal is made for free, here, before anything
    # is written.
    #
    # DP-123. Including the case the R118 wording of this comment claimed
    # and the code did not do: asking for the whole boundary of an assembly
    # is a selection that spans volumes, and naming every patch is asking
    # for the whole boundary. MEASURED on `two_solid_block`, whose two
    # ticked patches passed the old "is a patch named" test and were then
    # refused 76 s into the run by the runner, which grades which volume
    # each named patch bounds. Anything this cannot place is still left to
    # the runner rather than guessed at.
    refusal = layer_selection_refusal(
        prepared_geometry, intent.layers.enabled, intent.layers.patches,
        intent.layers.patch_mode)
    if refusal:
        raise ExecutionError(refusal)
    # Plan 42 SG22 Q2. A pair was authored before the engine was chosen with
    # an intent; if Gmsh cannot honour it, the run is refused here with the
    # reason rather than meshed as the other coupling.
    from foammesh.core.engine.interface_intent import intent_refusal

    pairs = job_interface_pairs(db, intent)
    refusal = intent_refusal('gmsh', configured_target_solver(db), pairs)
    if not refusal:
        # Plan 49 SR5 (DP-5805). Every periodic relation, measured again where
        # its verdict is stale; one that is not valid is refused by name.
        from .surface_roles import (
            affine_periodic_refusal, periodic_relation_rows)
        _lowered, refused = periodic_relation_rows(db, prepared_geometry)
        refusal = '; '.join(refused)
        if not refusal:
            export = getattr(intent, 'export', None)
            refusal = affine_periodic_refusal(
                _gmsh_section(db),
                publishes_poly_mesh=bool(getattr(
                    export, 'publishes_poly_mesh', False)),
                writes_su2=bool(getattr(export, 'writes_su2', False)))
    if not refusal:
        # Plan 49 SR5. A role the Gmsh route cannot build for this target.
        from .surface_roles import surface_intent_refusal
        refusal = surface_intent_refusal(
            getattr(intent, 'surface_intent', None) or {},
            target_solver=configured_target_solver(db),
            geometry=[str(item) for item in geometry],
            dimensionality_mode=getattr(
                getattr(intent, 'dimensionality', None), 'mode', 'three_d'))
    if not refusal and any(str(pair.get('coupling')) == 'non_conformal'
                           for pair in pairs):
        # The two bodies of a pair are kept apart by not fusing the copies of
        # their shared CAD face. A surface mesh has no such copies to keep
        # apart (its coincident triangles are one merged triangulation), so
        # the intent cannot be honoured and is refused rather than dropped.
        tessellated = [Path(str(item)).name for item in geometry
                       if Path(str(item)).suffix.lower()
                       in ('.stl', '.obj', '.ply')]
        if tessellated:
            refusal = (
                'a non-conformal interface pair is met on the Gmsh route by '
                'meshing its two solids apart, and ' + ', '.join(tessellated)
                + ' ' + ('is a surface mesh' if len(tessellated) == 1
                         else 'are surface meshes') + ', which has no solids '
                'to keep apart. Make the pair conformal, or import the solid '
                'model.')
    if refusal:
        raise ExecutionError(refusal)


def write_job(db, bbox, case_path, *, prepared_geometry=None, profile=None,
              run_id: str = '', formats=('msh',)) -> dict:
    """Derive and persist one run's job. Returns the created run manifest."""
    case_path = Path(case_path)
    geometry = prepared_geometry_paths(prepared_geometry, case_path)
    if not geometry:
        raise ExecutionError(
            'Gmsh meshes the prepared CAD directly, so a prepared geometry '
            'revision is required before a job can be written')

    # Plan 48 GM3. The one derivation: the step-by-step session reads its page
    # controls from the same function, so the two paths start from one intent.
    intent = derive_page_intent(
        db, bbox, case_path, prepared_geometry=prepared_geometry,
        profile=profile, run_id=run_id)
    # Plan 48 RR (DP-4009). The refusals below are shared with the resident
    # session's job (``session_job``): one set of checks for both paths.
    check_job(db, intent, prepared_geometry, geometry, case_path)
    if intent.export.mesh_format == 'su2' and 'su2' not in formats:
        # A second-order mesh only survives in Gmsh's own SU2 file, so the run
        # has to be given somewhere to write it.
        formats = (*formats, 'su2')
    # Plan 31 FC-A, row `export-formats-unexposed`. A companion format is an
    # addition and never a replacement: the MSH is what the polyMesh
    # publisher, the element census and the geometry-fidelity check all read,
    # so a run asked for a MED alone would finish having written nothing this
    # application can open. What may be asked for is checked against what was
    # measured to round-trip, rather than passed to a writer whose output
    # nothing here could re-identify.
    companion_export_formats(formats)
    if 'msh' not in formats:
        formats = ('msh', *formats)
    run_id = run_id or f'gmsh-{uuid.uuid4().hex[:16]}'
    builder = RunBuilder(case_path)
    layout = RunLayout(builder.root(run_id))
    job = build_job(
        intent, geometry=geometry, outputs=layout.outputs(formats),
        prepared_geometry=prepared_geometry, run_id=run_id, profile=profile,
        surface_output=layout.surface,
        # DP-419. What the user called each body, where the tree knows.
        region_names=region_display_names(db),
        interface_pairs=job_interface_pairs(db, intent),
        execution=_execution_record(db, intent.parallel.threads))
    # Plan 48 GM5 (DP-4165). What the window's Geometry builder made, so a
    # batch run rebuilds it on the runner's `geometry` stage.
    steps = geometry_build_steps(case_path)
    warnings = list(intent.warnings)
    if steps:
        job['geometry_build'] = steps
        with_build_roles(job, case_path)
        note = build_replaced_layer_note(case_path, intent)
        if note:
            warnings.append(note)
    try:
        script = runner_path()
    except Exception:
        script = None
    manifest = builder.create(run_id, job=job, profile=profile,
                              runner_path=script)
    return {
        'run_id': run_id,
        'job': job,
        'manifest': manifest.document,
        'job_path': str(layout.job),
        'run_path': str(layout.root),
        'warnings': warnings,
    }


#: The intent sections the shared stage functions read (Plan 48 RR). A change
#: to one of them changes what a stage did to the model, and a stage cannot be
#: taken back, so the session starts the model again (as for an entity control).
STAGE_INTENT_KEYS = ('healing', 'farfield', 'layers', 'periodic',
                     'curveControls', 'volumeControls', 'structuring',
                     'dimensionality', 'sizeFields', 'geometry')
#: The job keys that name what a stage acts on, rather than say how.
STAGE_JOB_KEYS = ('entitySchemaVersion', 'sources', 'scopeEntities',
                  'entityNames', 'scopeSurfaces', 'scopeVolumes',
                  'surfaceNames', 'surfaceCategories', 'volumeNames',
                  'volumeFallback', 'seedPoint', 'shellRoles',
                  'interfacePairs')


def stage_inputs(job: dict) -> dict:
    """What of *job* the shared stages read, for comparing two jobs.

    The recombination stage reads ``algorithms.recombine`` and the code; the
    point-size stage ``sizing.fromPoints``. Everything else in the intent is
    set on the model by the session at any time and needs no new import.
    """
    intent = job.get('intent') or {}
    algorithms = intent.get('algorithms') or {}
    sizing = intent.get('sizing') or {}
    picked = {key: intent.get(key) for key in STAGE_INTENT_KEYS}
    picked['algorithms'] = {
        'recombine': algorithms.get('recombine'),
        'recombinationCode': algorithms.get('recombinationCode')}
    picked['sizing'] = {'fromPoints': sizing.get('fromPoints')}
    return {'intent': picked,
            'job': {key: job.get(key) for key in STAGE_JOB_KEYS}}


def session_job(db, bbox, case_path, *, prepared_geometry=None,
                profile=None, intent=None) -> dict:
    """The job the resident session's stages read (Plan 48 RR, DP-4009).

    The very job ``write_job`` would write for a batch run of the same
    project -- the same intent, scope maps, names and pairs, built by the
    same ``build_job`` after the same refusals -- but not persisted: no run
    directory, no manifest, no output paths. The stage functions of
    ``resources/gmsh/stages`` read it in the session exactly as they read it
    in the runner, which is what makes a stage mean one thing on both paths.
    """
    case_path = Path(case_path)
    geometry = prepared_geometry_paths(prepared_geometry, case_path)
    if not geometry:
        raise ExecutionError(
            'Gmsh meshes the prepared CAD directly, so a prepared geometry '
            'revision is required before a job can be written')
    if intent is None:
        intent = derive_page_intent(
            db, bbox, case_path, prepared_geometry=prepared_geometry,
            profile=profile)
    check_job(db, intent, prepared_geometry, geometry, case_path)
    job = build_job(
        intent, geometry=geometry, outputs={},
        prepared_geometry=prepared_geometry, profile=profile,
        region_names=region_display_names(db),
        interface_pairs=job_interface_pairs(db, intent),
        execution=_execution_record(db, intent.parallel.threads))
    return with_build_roles(job, case_path)


def with_build_roles(job: dict, case_path) -> dict:
    """DP-5717. *job* with the categories of the patches a geometry-build
    preset declared (front/back ``empty``) in ``surfaceCategories``, so
    "every eligible wall" of a built model never grows a layer on them, and
    the built surfaces' names in ``builtSurfaceNames`` (DP-5725)."""
    from . import geometry_build

    declared = geometry_build.role_categories(
        geometry_build.load_roles(case_path))
    if declared:
        job['surfaceCategories'] = {
            **dict(job.get('surfaceCategories') or {}), **declared}
    # DP-5725: the built surfaces as the window names them, so a rename in
    # the physical-group table reaches the run (it names a built model from
    # the build's own groups otherwise)
    built = geometry_build.built_surface_names(case_path)
    if built and geometry_build_steps(case_path):
        job['builtSurfaceNames'] = built
    return job


def build_replaced_layer_note(case_path, intent) -> str:
    """DP-5716. The note for ticked layer walls the geometry build took
    away, or ``''``. The run drops them (``stages.build_follow``); it never
    grows them on a new surface that took the old tag."""
    from . import geometry_build

    layers = getattr(intent, 'layers', None)
    if layers is None or not getattr(layers, 'enabled', False):
        return ''
    return geometry_build.replaced_layer_note(
        list(getattr(layers, 'patches', ()) or ()),
        geometry_build.load_replaced(case_path))


def geometry_build_steps(case_path) -> list:
    """The Geometry-module operations of the project, as the job carries them.

    ``[{'kind': <session.build kind>, 'params': {...}}, ...]`` read from the
    session op log (the ``session.build`` entries, kernel inside ``params``
    when it is not OpenCASCADE). Empty without a log or without builds.
    The copies a re-import made again (marked ``again``, DP-5738) are not
    steps of their own.
    """
    from .session import read_op_log

    steps = []
    for entry in read_op_log(case_path):
        if entry.get('op') != 'session.build' or 'again' in entry:
            continue
        args = entry.get('args') or []
        if not args:
            continue
        steps.append({'kind': str(args[0]),
                      'params': dict(entry.get('kwargs') or {})})
    return steps


def _execution_record(db, threads: int) -> dict:
    """DP-678. The execution cap read off ``db`` and the threads the job got."""
    from foammesh.core.engine.gmsh import execution_policy
    from foammesh.core.execution.resources import execution_record

    return execution_record(execution_policy(db), effective=threads,
                            unit='threads')


def prepared_manifest(prepared_geometry) -> dict:
    """The prepared-geometry manifest, however the caller carries it.

    A ``PreparedGeometryRef`` holds only ``manifest_path``; the GUI is handed
    the manifest itself as plain JSON. Both have to reach the same reader, or
    the job that the facade writes and the job the pages describe disagree.
    """
    if prepared_geometry is None:
        return {}
    if isinstance(prepared_geometry, dict):
        manifest = prepared_geometry.get('manifest')
        if isinstance(manifest, dict) and manifest:
            return manifest
        return prepared_geometry if 'sources' in prepared_geometry else {}
    manifest = getattr(prepared_geometry, 'manifest', None)
    if isinstance(manifest, dict) and manifest:
        return manifest
    path = getattr(prepared_geometry, 'manifest_path', None)
    if path is None:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def prepared_source_names(prepared_geometry) -> tuple[str, ...]:
    """The staged file name of every prepared source, in manifest order."""
    manifest = prepared_manifest(prepared_geometry)
    return tuple(
        str(record['prepared_name'])
        for record in (manifest.get('sources') or ())
        if isinstance(record, dict) and record.get('prepared_name'))


def measured_faces(manifest) -> dict:
    """Patch name -> what the CAD import measured about that face.

    DP-92, and DP-123 which moved it here. Three answers, keyed by the name
    the user sees: `planar`, the `adjacent` patches it shares an edge with,
    and `interface`, the name the mesher will know this face by where the
    file carries an interface as two coincident faces. Empty for a revision
    written before the import measured them, and for the STL route.
    """
    groups = [group for group in (manifest or {}).get('groups') or ()
              if isinstance(group, dict)]
    names, orders = {}, {}
    for group in groups:
        uuid = str(group.get('patch_uuid') or '').strip()
        name = str(group.get('solver_name') or group.get('name') or '').strip()
        if uuid and name:
            names[uuid] = name
            orders[uuid] = group.get('face_order')

    def keeper(uuid, twin):
        """Healing merges a coincident pair and keeps the earlier face."""
        here, there = orders.get(uuid), orders.get(twin)
        if here is None or there is None:
            return uuid
        return uuid if int(here) <= int(there) else twin

    measured = {}
    for group in groups:
        uuid = str(group.get('patch_uuid') or '').strip()
        name = names.get(uuid)
        if not name or group.get('planar') is None:
            continue
        twin = str(group.get('interface_patch_uuid') or '').strip()
        measured[name] = {
            'planar': bool(group.get('planar')),
            'adjacent': [names[str(other)] for other in
                         group.get('adjacent_patch_uuids') or ()
                         if str(other) in names],
            'interface': (names.get(keeper(uuid, twin), name)
                          if twin in names or twin == uuid else ''),
        }
    return measured


def canonical_patch_names(manifest) -> dict:
    """Patch name -> the name the mesher will know it by."""
    return {name: (fact['interface'] or name)
            for name, fact in measured_faces(manifest).items()}


def prepared_regions(manifest) -> list:
    """``(volume label, [patch names])`` for each prepared region.

    DP-88. The runner grades a layer selection by the volume its bases bound
    and refuses one that spans two, and the membership it grades against is
    already on the prepared revision: every region lists the patch uuids of
    its own boundary. MEASURED on `annulus_shell.step`: region `Part 1 solid
    1` owns annulus_shell_wall1/2/3 and `Part 1 solid 2` owns wall4/5/6/7.

    DP-123. The wrapped-STL route lists no boundary uuids at all -- MEASURED
    on `two_solid_block`, where both regions carry `boundary_patch_uuids:
    []` -- so a region with none falls back to the `geometry_id` its patches
    were imported under. That link is only unambiguous while one geometry
    yields one region, so a geometry_id claimed by two regions is left
    ungraded rather than guessed at.
    """
    manifest = manifest or {}
    names, by_geometry = {}, {}
    for group in manifest.get('groups') or ():
        if not isinstance(group, dict):
            continue
        uuid = str(group.get('patch_uuid') or '').strip()
        name = str(group.get('solver_name') or group.get('name') or '').strip()
        if uuid and name:
            names[uuid] = name
        geometry_id = str(group.get('geometry_id') or '').strip()
        if geometry_id and name:
            by_geometry.setdefault(geometry_id, []).append(name)
    canonical = canonical_patch_names(manifest)
    canonical = {name: canonical.get(name, name) for name in names.values()}
    claims: dict = {}
    for region in manifest.get('regions') or ():
        if isinstance(region, dict):
            claims.setdefault(
                str(region.get('geometry_id') or '').strip(), []).append(region)
    regions = []
    for region in manifest.get('regions') or ():
        if not isinstance(region, dict):
            continue
        label = str(region.get('display_name')
                    or region.get('solver_name') or '').strip()
        # DP-92. The merged-away half of an interface is listed here by its
        # own uuid, so without this the volume it bounds looks as though it
        # does not bound the face the mesher actually has.
        members, held = [], set()
        for uuid in region.get('boundary_patch_uuids') or ():
            name = canonical.get(names.get(str(uuid)))
            if name and name not in held:
                held.add(name)
                members.append(name)
        geometry_id = str(region.get('geometry_id') or '').strip()
        if not members and geometry_id and len(claims.get(geometry_id, ())) == 1:
            for name in by_geometry.get(geometry_id, ()):
                name = canonical.get(name, name)
                if name and name not in held:
                    held.add(name)
                    members.append(name)
        if label and members:
            regions.append((label, members))
    return regions


def regions_by_patch(regions) -> dict:
    """Patch name -> the set of volumes that patch bounds."""
    owners: dict = {}
    for label, members in regions:
        for name in members:
            owners.setdefault(name, set()).add(label)
    return owners


def grade_layer_selection(selected, owners) -> tuple:
    """Which volume a selection bounds, by the rule the runner applies.

    DP-88. `volume_the_layer_grows_into` intersects the volumes each base
    bounds: one volume left means a core can be carved, more than one means
    every base is a shared interface and nothing says which side the layer is
    for, none means the selection spans volumes.
    """
    known = [name for name in selected if name in owners]
    if not known:
        return 'ungraded', []
    common = set(owners[known[0]])
    for name in known[1:]:
        common &= owners[name]
    if len(common) == 1:
        return 'single', sorted(common)
    if common:
        return 'shared', sorted(common)
    return 'spanning', []


#: The two states :func:`grade_layer_selection` returns for a selection the
#: runner will not grow layers on.
REFUSED_LAYER_GRADES = ('shared', 'spanning')


def shells_by_source(topology) -> dict:
    """Source name -> ``(domain shells, void shells)`` the import classified.

    DP-123. `preparation.topology` names every closed shell the prepared
    revision found and suffixes `#N` when one source file contributed more
    than one of them. That suffix is the only record anywhere on the revision
    that a patch covers more than one shell: the group manifest lists one
    patch and one region per *file*, so on `two_solid_block` it reads as one
    patch on one volume while the file itself holds a box inside a box.
    """
    counts: dict = {}
    for key, index in (('domains', 0), ('voids', 1)):
        for shell in topology.get(key) or ():
            base = str(shell).split('#')[0].strip()
            if not base:
                continue
            tally = counts.setdefault(base, [0, 0])
            tally[index] += 1
    return {name: (tally[0], tally[1]) for name, tally in counts.items()}


def sources_that_span_volumes(prepared_geometry) -> frozenset:
    """The prepared patch names whose faces cannot all bound one volume.

    A void is a hole in whichever domain encloses it, so a source that
    contributed a domain shell *and* anything else names faces of that domain
    and of the domain it sits inside. MEASURED on `two_solid_block`, whose
    single `two_solid_block.stl` yields `two_solid_block#1` -- a void in the
    farfield -- and `two_solid_block#2`, a domain of its own: the runner reads
    surfaces 3-18 as one patch, intersects the volumes they bound to nothing
    and refuses. The group manifest cannot say this, and the patch/volume
    grading below cannot either: one patch has one entry in `owners`, and one
    entry can never intersect to nothing.

    A source of nothing but voids may still be several holes in one domain,
    which the topology does not record either way, so it is left to the
    runner rather than refused on a guess.
    """
    topology = (prepared_manifest(prepared_geometry).get('preparation')
                or {}).get('topology') or {}
    return frozenset(
        name for name, (domains, voids) in shells_by_source(topology).items()
        if domains and domains + voids > 1)


def patches_by_source(prepared_geometry) -> dict:
    """Source display name -> the prepared patch names imported from it.

    DP-365. :func:`sources_that_span_volumes` names *sources*; a layer
    selection names *patches*, and the pre-flight compared the two with a set
    intersection. Where a source yields exactly one surface group the two
    strings coincide -- `two_solid_block` is both the file and its only patch
    -- so DP-123's own fixture matched and the check read as working. A source
    that yields several groups gets them suffixed, `two_cubes_one_file_wall1`
    and `_wall2`, and no patch name has equalled a source name since.

    The link that does hold is the one the prepared revision writes down: each
    group carries the ``geometry_id`` it was imported under, and each entry in
    ``sources`` carries that id and the file's display name.
    """
    manifest = prepared_manifest(prepared_geometry)
    named_by_geometry = {}
    for source in manifest.get('sources') or ():
        if not isinstance(source, dict):
            continue
        geometry_id = str(source.get('geometry_id') or '').strip()
        name = str(source.get('display_name') or '').strip()
        if geometry_id and name:
            named_by_geometry[geometry_id] = name
    held: dict = {}
    for group in (_group_manifest(prepared_geometry) or {}).get('groups') or ():
        if not isinstance(group, dict):
            continue
        name = str(group.get('solver_name') or group.get('name') or '').strip()
        source = named_by_geometry.get(
            str(group.get('geometry_id') or '').strip())
        if name and source:
            held.setdefault(source, set()).add(name)
    return held


def selection_covers_a_spanning_source(prepared_geometry, named) -> bool:
    """Whether *named* holds every patch of a source that spans volumes.

    DP-365. The topology records how many shells a file contributed but not
    which of the file's patches sits on which shell, so a selection naming
    *some* of them cannot be graded here and is left to the runner -- guessing
    would refuse a mesh that works. A selection naming *all* of them needs no
    guess: the patches between them cover every triangle the file contributed,
    those triangles lie on more than one shell, and a layer grown on them is
    therefore grown on more than one volume.
    """
    spanning = sources_that_span_volumes(prepared_geometry)
    if not spanning:
        return False
    for source, patches in patches_by_source(prepared_geometry).items():
        if source in spanning and patches and patches <= named:
            return True
    return False


def layer_selection_refusal(prepared_geometry, enabled, patches,
                            patch_mode=None) -> str:
    """The sentence the run refuses a layer selection with, or ``''``.

    DP-123. The pre-flight used to read "a patch is named" as "graded", so
    `two_solid_block` passed it with both of its patches ticked and was
    refused 76 s later by the runner, which had graded *which volume each
    named patch bounds*. Everything that grading needs is on the prepared
    revision, so it is done here instead -- once, for the page that shows the
    selection, the button that starts the run, and the job writer.

    Unknown refuses nothing, in both of its forms: a revision that never
    counted its volumes, and one that records no membership for the patches
    named. Guessing either way would refuse a mesh that works.

    Plan 37 F3d. ``patch_mode`` is the choice the selection is read with.
    ``selected`` with nothing named grows on nothing, which the runner
    refuses on any geometry; ``None`` (not known) and an unset mode, which
    the migration reads as every eligible wall when nothing is named, add
    no refusal.
    """
    if not enabled:
        return ''
    volumes, counted_by = prepared_volume_count(prepared_geometry)
    if counted_by == 'unknown' or volumes <= 1:
        return (nothing_chosen_layer_refusal()
                if _grows_on_nothing(patch_mode, patches) else '')
    if not patches:
        return assembly_layer_refusal(volumes)
    named = {str(name).strip() for name in patches}
    if named & sources_that_span_volumes(prepared_geometry):
        return assembly_layer_refusal(volumes)
    # DP-365. The same question asked of the patches a source actually owns,
    # for the imports whose patch names are not the source's own name.
    if selection_covers_a_spanning_source(prepared_geometry, named):
        return assembly_layer_refusal(volumes)
    owners = regions_by_patch(prepared_regions(_group_manifest(prepared_geometry)))
    state, _common = grade_layer_selection(patches, owners)
    return assembly_layer_refusal(volumes) if state in REFUSED_LAYER_GRADES else ''


def _grows_on_nothing(patch_mode, patches) -> bool:
    """The ticks are the answer and nothing is ticked (Plan 37 F3d)."""
    from .layer_targets import MODE_SELECTED, normalise_mode

    if patch_mode is None:
        return False
    named = [str(name).strip() for name in (patches or ())]
    return (normalise_mode(patch_mode) == MODE_SELECTED
            and not any(named))


def prepared_geometry_paths(prepared_geometry, case_path: Path) -> tuple[Path, ...]:
    """Every staged source Gmsh will import, in prepared manifest order.

    WP-01 F-35. The prepared revision stages one file per imported geometry
    under ``<revision>/sources``; the job used to carry only the first of
    them, so an assembly imported as several files meshed as one. The single
    ``geometry_path`` on the reference (which is ``sources[0]``) stays as the
    fallback for a caller that has no manifest to read, such as a test double.
    """
    if prepared_geometry is None:
        return ()
    root = getattr(prepared_geometry, 'root', None)
    names = prepared_source_names(prepared_geometry)
    if root and names:
        staged = [Path(root) / 'sources' / name for name in names]
        if all(path.is_file() for path in staged):
            return tuple(staged)
    single = _prepared_geometry_path(prepared_geometry, case_path)
    return (single,) if single is not None else ()


def _prepared_geometry_path(prepared_geometry, case_path: Path):
    """The staged CAD file Gmsh will import."""
    if prepared_geometry is None:
        return None
    for attribute in ('cad_path', 'geometry_path', 'path'):
        value = getattr(prepared_geometry, attribute, None)
        if value:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = case_path / candidate
            if candidate.is_file():
                return candidate
    return None


def stage_run(engine, stage: str, *, db, case_path: Path, executable: str,
              profile=None, run_id: str = '') -> StageRun:
    """Prepare the supervised launch for one engine stage."""
    definition = engine.validate(stage)
    case_path = Path(case_path)
    if stage != 'compute':
        # publish and checkMesh are driven by the facade, not by argv here.
        return StageRun(definition, (), case_path, None, ())
    if profile is None:
        raise ExecutionError(
            'no qualified Gmsh runtime is configured; Gmsh runs inside a WSL '
            'distribution and none was found')
    run_id = run_id or _latest_run_id(case_path)
    if not run_id:
        raise ExecutionError(
            'no Gmsh job has been written for this case; derive a plan first')
    layout = RunLayout(RunBuilder(case_path).root(run_id))
    if not layout.job.is_file():
        raise ExecutionError(f'run {run_id} has no job file')
    command = profile.runner_argv(runner_path(), layout.job, cwd=case_path)
    return StageRun(
        definition, command.argv, case_path, None,
        tuple(layout.outputs(('msh',)).values()))


def _latest_run_id(case_path: Path) -> str:
    root = Path(case_path) / 'foammesh' / 'runs'
    if not root.is_dir():
        return ''
    candidates = sorted(
        (item for item in root.iterdir() if (item / 'job.json').is_file()),
        key=lambda item: item.stat().st_mtime)
    return candidates[-1].name if candidates else ''


# --------------------------------------------------------------------------- #
# Plan 48 GM3. One execution path: the page controls as option rows
# --------------------------------------------------------------------------- #
#
# The batch runner (``runner_v1.py``) applies the page controls of a job as
# Gmsh options in a fixed order, and records each one in its ledger. The
# resident session used to get only the Options panel text, so "Generate step
# by step" meshed with Gmsh's own defaults wherever the page had a value. The
# functions below turn the same intent dict into the same option rows, with
# the same ledger controls, requested values and notes; the session applies
# them with ``option.setNumber`` and reads each back. The runner cannot import
# this module (it runs inside WSL with its resource folder only), so
# ``tests/plan48/gm3`` runs both against a recording Gmsh and compares them.

#: Mirrors of the runner's qualification tables (``runner_v1.py``).
RUNNER_QUALITY_MEASURE = {'sicn': 0, 'sige': 1, 'gamma': 2, 'disto': 3}
RUNNER_TESTED_RECOMBINERS = frozenset({0, 1, 2, 3, 4})
RUNNER_TESTED_SUBDIVISIONS = frozenset({0, 1, 2, 3})
RUNNER_BARYCENTRIC_SUBDIVISION = 3
RUNNER_ALGORITHM_RETRIES = 10

#: Suffixes the runner reads as a surface mesh (no CAD importer involved).
TESSELLATED_SUFFIXES = ('.stl', '.obj', '.ply')


@dataclass(frozen=True)
class PageOption:
    """One step of the page-control pass.

    ``name`` is the Gmsh option set (``None``: a ledger line only, nothing is
    set). ``value`` is what is written, ``requested`` what the ledger says was
    asked for (the value written unless a clamp changed it), ``effective`` the
    ledger's literal answer for a row that sets nothing, and ``readback``
    makes the pass read the option and record it without writing.
    """

    control: str
    name: str | None = None
    value: object = None
    storage: str = 'number'
    requested: object = None
    effective: object = None
    applied: bool = True
    note: str = ''
    readback: bool = False
    warning: str = ''

    def to_dict(self) -> dict:
        return {'control': self.control, 'name': self.name,
                'value': self.value, 'storage': self.storage,
                'requested': self.requested, 'effective': self.effective,
                'applied': self.applied, 'note': self.note,
                'readback': self.readback}


def sources_are_tessellated(paths) -> bool:
    """True when the job's sources are surface meshes, as the runner reads it."""
    names = [str(item).lower() for item in paths or ()]
    return any(name.endswith(TESSELLATED_SUFFIXES) for name in names)


def importer_option_rows(intent: dict, *, tessellated: bool = False
                         ) -> tuple[PageOption, ...]:
    """The options that configure the importer, applied BEFORE the import.

    The order and the controls are those of ``GmshRun.import_geometry``:
    target unit, tolerance, the OCC switches, the scaling pin, the boolean
    tolerance. A tessellated source skips the ones the CAD importer owns and
    the ledger says so, as the runner does.
    """
    healing = (intent or {}).get('healing') or {}
    rows: list[PageOption] = [PageOption(
        'geometry.unit', 'Geometry.OCCTargetUnit',
        str(((intent or {}).get('geometry') or {}).get('targetUnit') or 'M'),
        storage='string')]
    value = float(healing.get('importTolerance', 1e-6) or 1e-6)
    if not tessellated:
        rows.append(PageOption('gmsh/healing/importTolerance',
                               'Geometry.Tolerance', value))
    else:
        rows.append(PageOption(
            'gmsh/healing/importTolerance', 'Geometry.Tolerance', None,
            requested=value, applied=False, readback=True,
            note='tessellated import: this tolerance configures the OCC '
                 'importer, and there is no CAD here for it to import'))
    skipped = []
    for option, key, default in (
            ('Geometry.OCCSewFaces', 'sewFaces', False),
            ('Geometry.OCCFixDegenerated', 'fixDegenerated', False),
            ('Geometry.OCCMakeSolids', 'makeSolids', False),
            ('Geometry.OCCFixSmallEdges', 'fixSmallEdges', False),
            ('Geometry.OCCFixSmallFaces', 'fixSmallFaces', False),
            ('Geometry.OCCAutoFix', 'autoFix', True),
            ('Geometry.OCCBooleanSimplify', 'unionUnify', True),
            ('Geometry.OCCImportLabels', 'importLabels', True),
            ('Geometry.OCCParallel', 'occParallel', False)):
        requested = healing.get(key, default)
        requested = (option_code(requested) if key == 'importLabels'
                     else bool(requested))
        control = f'gmsh/healing/{key}'
        if not tessellated:
            rows.append(PageOption(control, option, int(requested)))
            continue
        rows.append(PageOption(
            control, None, requested=requested, effective=None,
            applied=False,
            note='tessellated import: this configures the CAD importer, and '
                 'a surface is read without it'))
        if requested != default:
            skipped.append(key)
    if skipped:
        rows.append(PageOption(
            '', None, requested=None, applied=False,
            warning='these import settings were not applied because they '
                    'configure the CAD importer and this import is a surface '
                    f'(STL/OBJ/PLY): {", ".join(skipped)}.'))
    scaling = float(healing.get('importScaling', 1.0) or 1.0)
    if not tessellated:
        rows.append(PageOption('gmsh/healing/importScaling',
                               'Geometry.OCCScaling', 1.0))
    if scaling != 1.0:
        rows.append(PageOption(
            'gmsh/healing/importScaling', None, requested=scaling,
            effective=1.0, applied=False,
            note='pinned at 1: the unit is set at import, and a second '
                 'factor would size this mesh apart from everything else',
            warning=f'the import scaling of {scaling:g} was not applied: the '
                    'geometry is already in metres from its import unit.'))
    elif tessellated:
        rows.append(PageOption(
            'gmsh/healing/importScaling', None, requested=scaling,
            effective=None, applied=False,
            note='tessellated import: read without the CAD importer'))
    rows.append(PageOption(
        'gmsh/healing/booleanTolerance', 'Geometry.ToleranceBoolean',
        float(healing.get('booleanTolerance', 0.0) or 0.0)))
    return tuple(rows)


def option_code(value) -> int:
    """A coded choice as Gmsh takes it: a job holds the number (Plan 48
    DP-2952), a job written before holds ``True`` / ``False`` (1 / 0). The
    runner's ``option_code`` reads it the same way."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(bool(value))


def max_retries_of(algorithms: dict) -> int:
    """``Mesh.MaxRetries`` for a job (Plan 48 DP-2952): the page's number, or
    -- a job written before it -- 10 with the fallback on, 0 with it off."""
    retries = (algorithms or {}).get('maxRetries')
    if retries is None:
        fallback = bool((algorithms or {}).get('fallback', True))
        return RUNNER_ALGORITHM_RETRIES if fallback else 0
    return int(retries)


def thread_option_rows(parallel: dict) -> tuple[PageOption, ...]:
    """The thread options, in the runner's order (Plan 48 DP-2952).

    ``General.NumThreads`` takes Threads. Each stage takes its own count when
    one is set and Threads when it is automatic (0), the volume stage only
    what its algorithm can use. The curve stage is left at Gmsh's own 0 (=
    Threads) unless a count is set or the run is reproducible (DP-4605).
    """
    parallel = parallel or {}
    rows: list[PageOption] = []
    threads = int(parallel.get('threads', 1) or 1)
    asked_volume = int(parallel.get('volumeThreads') or 0) or threads
    volume_threads = int(parallel.get('effectiveVolumeThreads', asked_volume)
                         or asked_volume)
    rows.append(PageOption('gmsh/parallel/threads', 'General.NumThreads',
                           threads))
    rows.append(PageOption(
        'gmsh/parallel/threads.3d', 'Mesh.MaxNumThreads3D', volume_threads,
        note='' if volume_threads == asked_volume else (
            f'{asked_volume} requested; the chosen volume algorithm is '
            'single-threaded, so the request applies to surface meshing '
            'only')))
    rows.append(PageOption(
        'gmsh/parallel/threads.2d', 'Mesh.MaxNumThreads2D',
        int(parallel.get('surfaceThreads') or 0) or threads))
    if parallel.get('reproducible'):
        # Plan 48 ST (DP-4605): as the runner does, 1D on one thread too.
        rows.append(PageOption('gmsh/parallel/threads.1d',
                               'Mesh.MaxNumThreads1D', 1))
    elif int(parallel.get('curveThreads') or 0):
        rows.append(PageOption('gmsh/parallel/threads.1d',
                               'Mesh.MaxNumThreads1D',
                               int(parallel['curveThreads'])))
    return tuple(rows)


def page_option_rows(intent: dict) -> tuple[PageOption, ...]:
    """The page controls as the runner writes them, in the runner's order.

    Threads, global sizing, algorithms, optimisation, recombination, the
    structuring option and the element order. Nothing here reads the
    geometry; marking each surface for recombination stays with the session
    (``GenerateController.apply_page_controls``).
    """
    intent = intent or {}
    sizing = intent.get('sizing') or {}
    algorithms = intent.get('algorithms') or {}
    quality = intent.get('quality') or {}
    export = intent.get('export') or {}
    parallel = intent.get('parallel') or {}
    rows: list[PageOption] = []

    def number(control, name, value):
        rows.append(PageOption(control, name, value))

    for row in thread_option_rows(parallel):
        rows.append(row)

    number('gmsh/globalSizing/targetSize', 'Mesh.MeshSizeMax',
           sizing.get('targetSize'))
    number('gmsh/globalSizing/minimumSize', 'Mesh.MeshSizeMin',
           sizing.get('minimumSize'))
    number('gmsh/globalSizing/sizeFactor', 'Mesh.MeshSizeFactor',
           sizing.get('sizeFactor', 1.0))
    number('gmsh/globalSizing/fromCurvature', 'Mesh.MeshSizeFromCurvature',
           sizing.get('fromCurvature', 12))
    number('gmsh/globalSizing/fromPoints', 'Mesh.MeshSizeFromPoints',
           int(bool(sizing.get('fromPoints', True))))
    number('gmsh/globalSizing/extendFromBoundary',
           'Mesh.MeshSizeExtendFromBoundary',
           option_code(sizing.get('extendFromBoundary', True)))
    number('gmsh/globalSizing/minimumCirclePoints', 'Mesh.MinCircleNodes',
           int(sizing.get('minimumCirclePoints', 7) or 7))
    number('gmsh/globalSizing/minimumCurvePoints', 'Mesh.MinCurveNodes',
           int(sizing.get('minimumCurvePoints', 3) or 3))
    number('gmsh/algorithms/surface', 'Mesh.Algorithm',
           algorithms.get('surfaceCode', 6))
    number('gmsh/algorithms/volume', 'Mesh.Algorithm3D',
           algorithms.get('volumeCode', 1))
    fallback = bool(algorithms.get('fallback', True))
    number('gmsh/algorithms/algorithmFallback',
           'Mesh.AlgorithmSwitchOnFailure', int(fallback))
    number('gmsh/algorithms/maxRetries', 'Mesh.MaxRetries',
           max_retries_of(algorithms))

    subdivision = int(algorithms.get('subdivisionAlgorithm', 0) or 0)
    if subdivision not in RUNNER_TESTED_SUBDIVISIONS:
        rows.append(PageOption(
            'gmsh/algorithms/cellShape', None, requested=subdivision,
            effective=0, applied=False,
            note='Gmsh subdivision algorithms are 0-3',
            warning=f'subdivision algorithm {subdivision} was asked for and '
                    'is not one Gmsh has (0-3), so the run meshed '
                    'tetrahedra. Choose a hexahedral cell shape for '
                    'hexahedra'))
        subdivision = 0
    number('gmsh/algorithms/cellShape', 'Mesh.SubdivisionAlgorithm',
           subdivision)
    if sizing.get('barycentricRefinement'):
        if subdivision:
            rows.append(PageOption(
                'gmsh/globalSizing/barycentricRefinement', None,
                requested=True, effective=False, applied=False,
                note='barycentric refinement and a hexahedral cell shape are '
                     'the same Gmsh setting, so only one of them can hold; '
                     'the mesh was built as hexahedra, because refining '
                     'barycentrically would have produced four tetrahedra '
                     'per cell instead. Refine with a size field to keep '
                     'the hexahedra'))
        else:
            number('gmsh/globalSizing/barycentricRefinement',
                   'Mesh.SubdivisionAlgorithm',
                   RUNNER_BARYCENTRIC_SUBDIVISION)

    optimize = bool(quality.get('optimize', True))
    number('gmsh/optimization/optimize', 'Mesh.Optimize', int(optimize))
    netgen = bool(quality.get('netgen', True))
    if netgen and not optimize:
        rows.append(PageOption(
            'gmsh/optimization/netgen', 'Mesh.OptimizeNetgen', 0.0,
            requested=1, applied=False,
            note='Optimize is off, so the Netgen optimiser was not run'))
    else:
        number('gmsh/optimization/netgen', 'Mesh.OptimizeNetgen', int(netgen))
    number('gmsh/optimization/smoothing', 'Mesh.Smoothing',
           int(quality.get('smoothing', 1) or 0))
    number('gmsh/optimization/optimizeThreshold', 'Mesh.OptimizeThreshold',
           float(quality.get('optimizeThreshold', 0.3)))
    number('gmsh/optimization/qualityType', 'Mesh.QualityType',
           RUNNER_QUALITY_MEASURE.get(quality.get('qualityType', 'sicn'), 0))
    random_factor = quality.get('randomFactor')
    if random_factor is None:
        random_factor = algorithms.get('randomFactor')
    if random_factor is not None:
        number('gmsh/default/randomFactor', 'Mesh.RandomFactor',
               float(random_factor))
    else:
        rows.append(PageOption(
            'gmsh/default/randomFactor', 'Mesh.RandomFactor', None,
            readback=True,
            note='Gmsh default (Mesh.RandomFactor), not set by FoamMesh'))

    if algorithms.get('recombine'):
        number('gmsh/algorithms/recombine', 'Mesh.RecombineAll', 1)
        recombiner = int(algorithms.get('recombinationCode', 1) or 0)
        if recombiner not in RUNNER_TESTED_RECOMBINERS:
            rows.append(PageOption(
                'gmsh/algorithms/recombinationAlgorithm', None,
                requested=recombiner, effective=1, applied=False,
                note='qualified values are '
                     + ', '.join(str(item) for item
                                 in sorted(RUNNER_TESTED_RECOMBINERS)),
                warning=f'recombination algorithm {recombiner} was asked '
                        'for and is not one this product has qualified; '
                        'blossom was used instead'))
            recombiner = 1
        number('gmsh/algorithms/recombinationAlgorithm',
               'Mesh.RecombinationAlgorithm', recombiner)
    if (intent.get('structuring') or {}).get('transfiniteTri'):
        number('gmsh/structuring/transfiniteTri', 'Mesh.TransfiniteTri', 1)

    order = int(export.get('elementOrder', 1) or 1)
    high = int(quality.get('highOrderOptimize', 0) or 0)
    if order < 2:
        route = {'openfoam': 'OpenFOAM', 'su2': 'SU2'}.get(
            str(export.get('targetSolver') or '').lower())
        rows.append(PageOption(
            'gmsh/output/elementOrder', None, requested=order, effective=1,
            note='first order; the default' + (
                f' and the only order the {route} export can read'
                if route else '')))
        rows.append(PageOption(
            'gmsh/optimization/highOrderOptimize', None, requested=high,
            effective=0,
            note='not set: the high-order optimiser only acts on an order-2 '
                 'mesh'))
    else:
        number('gmsh/output/elementOrder', 'Mesh.ElementOrder', order)
        number('gmsh/output/secondOrderIncomplete',
               'Mesh.SecondOrderIncomplete',
               int(bool(export.get('secondOrderIncomplete', False))))
        number('gmsh/output/secondOrderLinear', 'Mesh.SecondOrderLinear',
               int(bool(export.get('secondOrderLinear', False))))
        number('gmsh/optimization/highOrderOptimize',
               'Mesh.HighOrderOptimize', high)
    return tuple(rows)


def ledger_matched(requested, effective, matched=None):
    """The runner ledger's comparison rule (``Ledger.record``)."""
    import math

    if matched is not None:
        return bool(matched)
    if (isinstance(requested, (int, float))
            and isinstance(effective, (int, float))):
        return math.isclose(float(requested), float(effective),
                            rel_tol=1e-9, abs_tol=1e-12)
    if requested is not None and effective is not None:
        return str(requested) == str(effective)
    return None


def ledger_entry(control, requested, effective, *, applied=True, note='',
                 matched=None) -> dict:
    """One ledger line in the shape the runner writes."""
    return {'control': control, 'requested': requested,
            'effective': effective, 'applied': bool(applied),
            'matched': ledger_matched(requested, effective, matched),
            'note': note}


def ledger_difference(session_entries, run_entries, *,
                      controls=None) -> list[dict]:
    """Where the session's ledger and a batch run's disagree.

    Compared by control, over the controls both have (or over *controls*):
    the requested value, the effective value and whether it applied. An empty
    list is a session that did what the batch run did.
    """
    def by_control(entries):
        table = {}
        for entry in entries or ():
            table.setdefault(entry.get('control'), []).append(entry)
        return table

    left, right = by_control(session_entries), by_control(run_entries)
    wanted = (list(controls) if controls is not None
              else sorted(set(left) & set(right), key=str))
    differences = []
    for control in wanted:
        one, other = left.get(control), right.get(control)
        if one is None or other is None:
            differences.append({'control': control, 'session': one,
                                'run': other,
                                'why': 'only one path recorded it'})
            continue
        keys = ('requested', 'effective', 'applied')
        if ([[e.get(k) for k in keys] for e in one]
                != [[e.get(k) for k in keys] for e in other]):
            differences.append({'control': control, 'session': one,
                                'run': other, 'why': 'values differ'})
    return differences
