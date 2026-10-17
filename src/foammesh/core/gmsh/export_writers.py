"""Every file Gmsh 4.15.2 writes, and what each can carry (Plan 41 GN15).

The Export page of the Gmsh route offers this list.  Nothing here asks which
solver the mesh is for: a writer either carries the mesh in hand or it does
not, and when it does not the page says why, in the words of
:func:`refusal`, before the file is written.  Nothing upstream (the Method
page, the algorithm, the element order) is changed to fit a writer.

Every limit below was measured on the installed Gmsh 4.15.2 on 2026-10-09
(``plans/evidence/plan41/gn15/writer-probe.json``): a tetrahedral and a
hexahedral box meshed at order 1, 2 and 3, written in every extension Gmsh
accepts, and read back -- by Gmsh where Gmsh has a reader, otherwise by the
plain readers of :mod:`export_verify`.  The orders a format carries are the
orders whose node and element counts survive that read-back; a format whose
file kept the nodes but wrote the elements with a card that cannot hold them
(Abaqus at order 3 gets a 3-node shell card with ten nodes) is held to the
order whose cards are valid.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import option_catalogue as catalogue

TET, HEX, PRISM, PYRAMID = 'tetrahedron', 'hexahedron', 'prism', 'pyramid'
TRI, QUAD, LINE, POINT = 'triangle', 'quadrangle', 'line', 'point'
VOLUME_FAMILIES = (TET, HEX, PRISM, PYRAMID)
SURFACE_FAMILIES = (TRI, QUAD)

MESH, SURFACE, GEOMETRY, FOAM, FLUENT = (
    'mesh', 'surface', 'geometry', 'openfoam', 'fluent')


#: Plan 42 SG15 (WALK-2, FIX-K; closing round 2026-10-12). Gmsh 4.15.2's
#: Medit writer (``GModel::writeMESH``) stores edges, triangles, quadrangles,
#: tetrahedra and hexahedra only, and reads the order of the first face's
#: ``triangles[0]`` and the first region's ``tetrahedra[0]`` without looking
#: whether they exist.  A mesh whose lowest-numbered region holds prisms (any
#: boundary-layer mesh) therefore drops every prism and segfaults, taking the
#: Gmsh session with it (measured on the kept pipe mesh: exit 139, a file cut
#: off after the vertices).  A mesh of one volume family that Medit has,
#: tetrahedra or hexahedra, writes and reads back.
MEDIT_FAMILIES = (TET, HEX)


def medit_refusal(facts) -> str:
    """Why Gmsh's Medit writer cannot be run on this mesh, or ``''``."""
    volume = tuple(facts.volume_families())
    surface = tuple(facts.surface_families())
    if volume:
        wrong = [name for name in volume if name not in MEDIT_FAMILIES]
        if wrong or len(volume) > 1:
            return (
                "Gmsh 4.15.2's Medit writer has no "
                f"{' or '.join(wrong or volume)} elements: it writes tetrahedra "
                'and hexahedra, and crashes when the first region holds '
                'anything else (it ends the Gmsh session), so FoamMesh does '
                'not run it on this mesh; write MSH, VTK, MED or another '
                'format instead')
    elif len(surface) > 1:
        return ("Gmsh 4.15.2's Medit writer crashes on a surface mesh of "
                'triangles and quadrangles when its first face has no '
                'triangles (it ends the Gmsh session), so FoamMesh does not '
                'run it on this mesh; write MSH, VTK or another format '
                'instead')
    return ''


def diffpack_refusal(facts) -> str:
    """SG15A. Why Gmsh's Diffpack writer cannot carry this mesh, or ``''``.

    Measured on the kept pipe mesh read back by Gmsh 4.15.2: of 24,288 prisms
    and 42,256 tetrahedra the file holds the tetrahedra only (9,217 of 21,367
    nodes). Tetrahedral and hexahedral meshes write and read back whole.
    """
    gone = [name for name in facts.volume_families()
            if name in (PRISM, PYRAMID)]
    if gone:
        return ("Gmsh 4.15.2's Diffpack writer drops "
                f"{' and '.join(gone)} elements (the file would hold the "
                'rest of the mesh only); write MSH, VTK, MED or another '
                'format instead')
    return ''


#: What a geometry writer says when the session holds a mesh and no model.
NO_CAD = ('the open session holds a mesh read from a file and no CAD model, '
          'and this format writes the CAD model; import or build geometry '
          'first, or write a mesh format')


@dataclass(frozen=True)
class Writer:
    id: str
    label: str
    suffix: str
    kind: str = MESH
    #: highest order carried, for tetrahedra and for the other volume
    #: families (hexahedra were measured; prisms and pyramids follow them).
    tet_order: int = 3
    other_order: int = 3
    #: ``gmsh`` read back by Gmsh, ``text`` by export_verify, ``file`` only
    #: that the file exists and is not empty.
    verify: str = 'gmsh'
    #: Gmsh options (F-16) this writer reads.
    options: tuple = ()
    #: Extra files the writer creates beside the one named (suffix appended
    #: to the full name), when the named file itself is not created.
    companions: tuple = ()
    only_volume_families: tuple | None = None
    #: a surface writer that stores triangles only (quadrangles are written
    #: as nothing).
    triangles_only: bool = False
    unavailable: str = ''
    notes: str = ''

    def limit(self, family: str) -> int:
        return self.tet_order if family in (TET, TRI, LINE, POINT) \
            else self.other_order


_COMMON = ('Mesh.SaveAll', 'Mesh.ScalingFactor')
_MSH = _COMMON + (
    'Mesh.MshFileVersion', 'Mesh.Binary', 'Mesh.SaveParametric',
    'Mesh.SaveTopology', 'Mesh.SaveWithoutOrphans', 'Mesh.SaveElementTagType',
    'Mesh.PartitionSplitMeshFiles', 'Mesh.PartitionTopologyFile',
    'Mesh.PartitionCreateTopology', 'Mesh.PartitionCreatePhysicals',
    'Mesh.PartitionCreateGhostCells', 'Mesh.PartitionOldStyleMsh2',
    'Mesh.SaveGroupsOfNodes', 'Mesh.SaveGroupsOfElements')
_GROUPS = _COMMON + ('Mesh.SaveGroupsOfNodes', 'Mesh.SaveGroupsOfElements')

WRITERS: tuple[Writer, ...] = (
    Writer('msh', 'Gmsh mesh (.msh)', '.msh', options=_MSH,
           notes='Versions 1, 2.2 and 4.1, ASCII or binary; any order.'),
    Writer('unv', 'I-DEAS Universal (.unv)', '.unv', tet_order=2,
           other_order=1, options=_GROUPS + ('Mesh.UnvStrictFormat',)),
    Writer('vtk', 'VTK legacy (.vtk)', '.vtk', options=_COMMON + ('Mesh.Binary',)),
    Writer('med', 'MED (.med), SALOME and Code_Aster', '.med', tet_order=2,
           other_order=2,
           options=_GROUPS + ('Mesh.MedFileMinorVersion',
                              'Mesh.MedImportGroupsOfNodes')),
    Writer('cgns', 'CGNS (.cgns)', '.cgns',
           options=_COMMON + ('Mesh.CgnsExportCPEX0045',
                              'Mesh.CgnsExportStructured',
                              'Mesh.CgnsImportOrder',
                              'Mesh.CgnsImportIgnoreBC',
                              'Mesh.CgnsImportIgnoreSolution')),
    Writer('su2', 'SU2 (.su2)', '.su2', tet_order=2, other_order=2,
           verify='text', options=_COMMON,
           notes='Order 3 would be written as a Lagrange card (VTK type 71) '
                 'that SU2 has no element for.'),
    Writer('mesh', 'Medit (.mesh)', '.mesh', tet_order=3, other_order=1,
           options=_COMMON),
    Writer('bdf', 'Nastran bulk data (.bdf)', '.bdf', tet_order=2,
           other_order=1, options=_COMMON),
    Writer('nas', 'Nastran (.nas)', '.nas', tet_order=2, other_order=1,
           options=_COMMON),
    Writer('inp', 'Abaqus (.inp)', '.inp', tet_order=2, other_order=2,
           verify='text', options=_GROUPS),
    Writer('key', 'LS-DYNA keyword (.key)', '.key', tet_order=2,
           other_order=2, verify='text', options=_COMMON),
    Writer('rad', 'Radioss (.rad)', '.rad', tet_order=2, other_order=2,
           verify='text', options=_COMMON),
    Writer('neu', 'Gambit neutral (.neu)', '.neu', tet_order=2,
           other_order=2, verify='text', options=_COMMON),
    Writer('dat', 'Tochnog (.dat)', '.dat', tet_order=2, other_order=2,
           verify='text', options=_COMMON),
    Writer('diff', 'Diffpack (.diff)', '.diff', tet_order=2, other_order=2,
           options=_COMMON),
    Writer('ir3', 'IR3 (.ir3)', '.ir3', tet_order=2, other_order=2,
           verify='text', options=_COMMON),
    Writer('p3d', 'Plot3D (.p3d)', '.p3d', tet_order=1, other_order=1,
           verify='file', options=_COMMON, only_volume_families=(HEX,),
           notes='Structured blocks: Gmsh writes an empty file for any mesh '
                 'that is not hexahedral (measured).'),
    Writer('m', 'Matlab (.m)', '.m', verify='text', options=_COMMON),
    Writer('mail', 'MAIL (.mail)', '.mail', tet_order=1, other_order=1,
           verify='text', options=_COMMON,
           notes='Written above order 1 with every node, but the element '
                 'cards could not be read back, so order 1 only.'),
    Writer('celum', 'Celum (.celum_f and .celum_s)', '.celum', verify='file',
           companions=('_f', '_s'), options=_COMMON,
           notes='Gmsh writes two files, name.celum_f and name.celum_s, and '
                 'none called name.celum (measured).'),
    Writer('stl', 'STL surface (.stl)', '.stl', kind=SURFACE, verify='gmsh',
           options=('Mesh.Binary', 'Mesh.StlOneSolidPerSurface',
                    'Mesh.StlLinearDeflection', 'Mesh.StlAngularDeflection',
                    'Mesh.SaveAll', 'Mesh.ScalingFactor')),
    Writer('obj', 'Wavefront OBJ surface (.obj)', '.obj', kind=SURFACE,
           tet_order=1, other_order=1, options=_COMMON,
           notes='Order 2 and 3 triangles read back as no elements (measured).'),
    Writer('off', 'OFF surface (.off)', '.off', kind=SURFACE, options=_COMMON),
    Writer('ply2', 'PLY2 surface (.ply2)', '.ply2', kind=SURFACE,
           triangles_only=True,
           options=_COMMON + ('Mesh.Binary',)),
    Writer('wrl', 'VRML surface (.wrl)', '.wrl', kind=SURFACE, tet_order=1,
           other_order=1, options=_COMMON,
           notes='Order 2 and 3 triangles are subdivided on writing (measured).'),
    Writer('x3d', 'X3D surface (.x3d)', '.x3d', kind=SURFACE, verify='file',
           options=_COMMON),
    # Geometry
    Writer('brep', 'OpenCASCADE BREP (.brep)', '.brep', kind=GEOMETRY,
           verify='file', options=()),
    Writer('step', 'STEP (.step)', '.step', kind=GEOMETRY, verify='file',
           options=()),
    Writer('iges', 'IGES (.iges)', '.iges', kind=GEOMETRY, verify='file',
           options=()),
    Writer('xao', 'XAO (.xao)', '.xao', kind=GEOMETRY, verify='file',
           options=(),
           notes='Geometry with its physical groups, for SALOME.'),
    Writer('geo_unrolled', 'Unrolled geometry script (.geo_unrolled)',
           '.geo_unrolled', kind=GEOMETRY, verify='file', options=(),
           companions=('.xao',),
           notes='For a model built with OpenCASCADE Gmsh writes a short '
                 'script that merges the XAO it writes beside it.'),
    # Not Gmsh writers: FoamMesh's own, from the session mesh.
    Writer('openfoam', 'OpenFOAM case (polyMesh)', '', kind=FOAM,
           verify='file', options=('Mesh.SaveAll',),
           notes='The session mesh is written as MSH 4.1 and converted to a '
                 'polyMesh here; 2D meshes may be extruded.'),
    Writer('fluent', 'Fluent mesh (.msh)', '.msh', kind=FLUENT, verify='file',
           options=(),
           notes='Written from the OpenFOAM polyMesh with foamMeshToFluent.'),
)

BY_ID = {writer.id: writer for writer in WRITERS}

#: Options F-16 names that Gmsh 4.15.2 does not have (PROBED 2026-10-09:
#: ``gmsh.option.getNumber`` raises for each).
ABSENT_WRITER_OPTIONS = {
    'Mesh.MedSingleFloat':
        'Gmsh 4.15.2 has no Mesh.MedSingleFloat; MED files are always '
        'written in double precision',
    'Mesh.StlRemoveDuplicateTriangles':
        'Gmsh 4.15.2 has no Mesh.StlRemoveDuplicateTriangles; STL files '
        'are written as the mesh holds them',
}

#: Read-side options of F-16: they act when a file is read, so the Export page
#: applies them to the read-back it makes of what it wrote.
READ_OPTIONS = ('Mesh.ReadGroupsOfElements', 'Mesh.IgnorePeriodicity',
                'Mesh.IgnoreParametrization')


def writer(writer_id: str) -> Writer:
    try:
        return BY_ID[writer_id]
    except KeyError:
        raise KeyError(f'there is no export writer called {writer_id!r}') \
            from None


def by_kind(kind: str) -> tuple[Writer, ...]:
    return tuple(item for item in WRITERS if item.kind == kind)


def writer_options(writer_id: str) -> tuple:
    """The catalogue entries a writer reads, in the catalogue's own words."""
    rows = []
    for name in writer(writer_id).options + READ_OPTIONS:
        option = catalogue.get(name)
        if option is not None:
            rows.append(option)
    return tuple(rows)


def all_writer_option_names() -> tuple[str, ...]:
    names: dict[str, None] = {}
    for item in WRITERS:
        for name in item.options:
            names[name] = None
    for name in READ_OPTIONS:
        names[name] = None
    return tuple(names)


# -- the mesh in hand ---------------------------------------------------------- #

@dataclass(frozen=True)
class MeshFacts:
    """What the Export page needs to know about the session mesh."""

    nodes: int = 0
    order: int = 1
    families: tuple = ()
    dimension: int = 0
    partitions: int = 0
    physical_groups: int = 0
    entities: int = 0
    #: OpenCASCADE entities in the model; ``None`` when not known.
    cad: int | None = None

    @classmethod
    def from_dict(cls, values) -> 'MeshFacts':
        values = dict(values or {})
        return cls(
            nodes=int(values.get('nodes') or 0),
            order=int(values.get('order') or 1),
            families=tuple(values.get('families') or ()),
            dimension=int(values.get('dimension') or 0),
            partitions=int(values.get('partitions') or 0),
            physical_groups=int(values.get('physical_groups') or 0),
            entities=int(values.get('entities') or 0),
            cad=(None if values.get('cad') is None else int(values['cad'])))

    def volume_families(self) -> tuple:
        return tuple(name for name in self.families if name in VOLUME_FAMILIES)

    def surface_families(self) -> tuple:
        return tuple(name for name in self.families if name in SURFACE_FAMILIES)


def carriers(facts: MeshFacts) -> tuple:
    """The writers that carry this mesh (no refusal), by label."""
    return tuple(item for item in WRITERS
                 if item.kind in (MESH, SURFACE)
                 and not refusal(item.id, facts, _hint=False))


def refusal(writer_id: str, facts: MeshFacts, options=None,
            _hint: bool = True) -> str:
    """Why *writer_id* cannot write this mesh, or ``''``.

    Asked before anything is written.  Each sentence names the writer, the
    property of the mesh it cannot carry and a way out that does not touch
    anything upstream: another format, or the Generate page's own action
    (Set order) when the user wants this format.
    """
    item = writer(writer_id)
    options = dict(options or {})
    if item.unavailable:
        return item.unavailable
    if item.kind == GEOMETRY:
        if not facts.entities:
            return ('there is no model to write: import or build geometry '
                    'first')
        if facts.cad == 0:
            return NO_CAD
        return ''
    if not facts.nodes:
        return 'there is no mesh to write: generate one first'
    if item.id == 'diff':
        reason = diffpack_refusal(facts)
        if reason:
            return reason
    if item.id == 'mesh':
        reason = medit_refusal(facts)
        if reason:
            return reason
    if item.kind == SURFACE:
        if not facts.surface_families() and not facts.volume_families():
            return (f'{item.label} stores a surface and the mesh has no '
                    'surface elements')
        if item.triangles_only and QUAD in facts.surface_families():
            return (f'{item.label} stores triangles only and the mesh has '
                    'quadrangle faces (Gmsh writes none of them); choose '
                    'STL, which splits them, or recombine off on the '
                    'Generate page.')
        if facts.order > item.limit(TRI) and item.tet_order < 3:
            return (f'{item.label} carries order {item.limit(TRI)} for '
                    f'triangles, and this mesh is order {facts.order}. '
                    'Lower the order with Set order on the Generate page, '
                    'or choose another format.')
        return ''
    if item.only_volume_families is not None:
        volume = facts.volume_families()
        wrong = [name for name in volume if name not in item.only_volume_families]
        if wrong or not volume:
            what = ', '.join(wrong) if wrong else 'no volume elements'
            return (f'{item.label} stores structured blocks of '
                    f'{" and ".join(item.only_volume_families)} only, and the '
                    f'mesh has {what}; Gmsh writes an empty file for it')
        if item.id == 'p3d' and facts.cad == 0:
            # Plan 42 SG15 (closing round 2026-10-12, measured on Gmsh
            # 4.15.2): the Plot3D writer saves the model's structured
            # (transfinite) volumes and warns "No structured grids to save"
            # for anything else.  A mesh read from a file has no such record,
            # so a hexahedral .msh opened in the session writes an empty file
            # (a box generated and written in the same model writes 9062
            # bytes; the same box written to .msh, opened and written writes
            # 0).
            return (f'{item.label} is written only for structured blocks '
                    'Gmsh generated in the same model, and the open session '
                    'holds a mesh read from a file, which has none (Gmsh '
                    'writes an empty file); write MSH, VTK or another format '
                    'instead')
    if item.kind in (FOAM, FLUENT):
        if not facts.volume_families() and facts.dimension >= 3:
            return 'the mesh has no volume cells to write as a case'
        return ''
    families = facts.volume_families() or facts.surface_families()
    limit = min((item.limit(name) for name in families), default=3)
    if facts.order > limit:
        limited = [name for name in families if item.limit(name) < facts.order]
        others = [other.label for other in (carriers(facts) if _hint else ())
                  if other.id != item.id and other.kind == MESH][:4]
        hint = (f' Formats that carry order {facts.order}: '
                f'{", ".join(others)}.' if others else '')
        return (f'{item.label} carries order {limit} for '
                f'{" and ".join(limited or families)}, and this mesh is '
                f'order {facts.order}.{hint} Lower the order with Set order '
                'on the Generate page, or choose another format.')
    split = options.get('Mesh.PartitionSplitMeshFiles')
    if item.id == 'msh' and split and float(split) and facts.partitions < 2:
        return ('one file per partition was asked for and the mesh is not '
                'partitioned: partition it on the Generate page first')
    return ''


def option_values(writer_id: str, raw: dict | None) -> dict:
    """The values to hand Gmsh for the options of one export, each checked.

    Raises :class:`catalogue.OptionRefusal` for a value Gmsh would silently
    rewrite; names the writer does not read are refused too, so a value the
    user set never disappears on its way to Gmsh.
    """
    item = writer(writer_id)
    allowed = set(item.options) | set(READ_OPTIONS)
    values: dict = {}
    for name, value in dict(raw or {}).items():
        if name in ABSENT_WRITER_OPTIONS:
            raise catalogue.OptionRefusal(ABSENT_WRITER_OPTIONS[name])
        if name not in allowed:
            raise catalogue.OptionRefusal(
                f'{item.label} does not read {name}')
        values[name] = catalogue.parse_value(name, value)
    return values


def output_paths(writer_id: str, path) -> tuple:
    """Every file one write creates, given the name the user chose."""
    from pathlib import Path

    item = writer(writer_id)
    base = str(path)
    if item.id == 'celum':
        return tuple(base + suffix for suffix in item.companions)
    if item.id == 'geo_unrolled':
        return (base, base + '.xao')
    return (base,)


def compare(writer_id: str, expected: dict, found: dict) -> list:
    """Problems between the mesh and a written file read back.

    ``expected``: ``nodes`` and ``volume`` (Gmsh element type -> count) of
    what the writer was given.  ``found``: the census of the file.  Empty
    list means the counts agree.
    """
    item = writer(writer_id)
    problems = []
    if item.verify == 'file':
        return problems
    if item.kind == SURFACE:
        key = 'surface_triangles' if item.id == 'stl' else 'surface_elements'
        want = int(expected.get(key) or expected.get('surface_elements') or 0)
        got = int(found.get('surface_elements') or 0)
        if want and got != want:
            problems.append(f'{item.label}: {got} surface elements read '
                            f'back, {want} written')
        return problems
    nodes_got = found.get('nodes')
    if nodes_got is not None and nodes_got < int(expected.get('nodes') or 0):
        problems.append(f'{item.label}: {nodes_got} nodes read back, '
                        f'{expected.get("nodes")} written')
    if item.verify == 'gmsh':
        want = {int(k): int(v) for k, v in (expected.get('volume') or {}).items()}
        got = {int(k): int(v) for k, v in (found.get('by_type') or {}).items()
               if int(k) in want}
        if want != got:
            problems.append(f'{item.label}: element counts read back {got}, '
                            f'written {want}')
    elif found.get('elements') is not None and expected.get('cells') is not None:
        if int(found['elements']) != int(expected['cells']):
            problems.append(f'{item.label}: {found["elements"]} elements read '
                            f'back, {expected["cells"]} written')
    return problems
