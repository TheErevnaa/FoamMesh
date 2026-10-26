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

Plan 48 GM4 (output honesty).  The orders above are the measured limits of
each writer for a mesh of one kind.  What a writer does with a mesh of any
kind -- 2D or 3D, one element family or several, any order, partitioned or
not, periodic or not -- is the measured target matrix
(``plans/evidence/plan48/target-matrix.json``, written by
``plans/evidence/plan48/fragments/gm4/matrix``): every cell is SUPPORTED,
CONVERTED (only through a conversion the user chooses, validated on its own)
or REFUSED with its reason.  :func:`refusal` reads the matrix; the limits in
the :class:`Writer` table answer for a mesh the matrix has no cell for.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import option_catalogue as catalogue

TET, HEX, PRISM, PYRAMID = 'tetrahedron', 'hexahedron', 'prism', 'pyramid'
TRI, QUAD, LINE, POINT = 'triangle', 'quadrangle', 'line', 'point'
VOLUME_FAMILIES = (TET, HEX, PRISM, PYRAMID)
SURFACE_FAMILIES = (TRI, QUAD)

MESH, SURFACE, GEOMETRY, FOAM, FLUENT = (
    'mesh', 'surface', 'geometry', 'openfoam', 'fluent')


#: DP-1437 (user, 2026-10-23): the Medit (.mesh) export is retired. Gmsh
#: 4.15.2's Medit writer (``GModel::writeMESH``) reads the order of the first
#: face's ``triangles[0]`` and the first region's ``tetrahedra[0]`` without
#: looking whether they exist, so a boundary-layer (tet+prism) mesh segfaults
#: the session and some triangle/quadrangle surface meshes crash it too
#: (measured on the kept pipe mesh: exit 139, a file cut off after the
#: vertices).  FoamMesh no longer offers the writer anywhere; a project, job,
#: API call or options file that names it is refused with this sentence.
#: Reading .mesh files (Open / Merge) stays: only the writer crashes.
MEDIT_RETIRED = catalogue.MEDIT_RETIRED
#: Writer ids that were offered once and are refused now, with the sentence.
RETIRED_WRITERS = {'mesh': MEDIT_RETIRED, 'medit': MEDIT_RETIRED,
                   'meshb': MEDIT_RETIRED}
#: File suffixes only a retired writer writes.
RETIRED_SUFFIXES = {'.mesh': MEDIT_RETIRED, '.meshb': MEDIT_RETIRED}


class RetiredWriter(KeyError):
    """A writer id FoamMesh offered once and has retired; ``str()`` is the
    sentence the user reads."""

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ''


def retired(writer_id) -> str:
    """The retirement sentence for *writer_id*, or ``''``."""
    return RETIRED_WRITERS.get(str(writer_id or '').strip().lower(), '')


def retired_path(path) -> str:
    """The retirement sentence when *path* names a retired format, or ``''``."""
    text = str(path or '').lower()
    for suffix, sentence in RETIRED_SUFFIXES.items():
        if text.endswith(suffix):
            return sentence
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
           notes='Versions 1, 2.2, 3.0, 4.0 and 4.1, ASCII or binary where '
                 'Gmsh writes it; any order.'),
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
           notes='The polyMesh is built from a hidden MSH 2.2 copy of the '
                 'session mesh, written for this purpose only; the .msh the '
                 'user writes is never that copy. 2D meshes may be extruded.'),
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
        'Mesh.StlRemoveDuplicateTriangles is only a deprecated name of '
        'Mesh.StlRemoveBadTriangles, which acts when an STL is read; STL '
        'files are written as the mesh holds them',
}

#: Read-side options of F-16: they act when a file is read, so the Export page
#: applies them to the read-back it makes of what it wrote.
READ_OPTIONS = ('Mesh.ReadGroupsOfElements', 'Mesh.IgnorePeriodicity',
                'Mesh.IgnoreParametrization')


def writer(writer_id: str) -> Writer:
    sentence = retired(writer_id)
    if sentence:
        raise RetiredWriter(sentence)
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
    #: Plan 48 GM4. Entities that are the slave side of a periodic pair, and
    #: the pairs a publisher can name: ``{'slave', 'master', 'translation'}``
    #: by physical group name.
    periodic: int = 0
    periodic_pairs: tuple = ()
    #: ``(xmin, ymin, zmin, xmax, ymax, zmax)`` of the nodes, or ``()``.
    bounds: tuple = ()

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
            cad=(None if values.get('cad') is None else int(values['cad'])),
            periodic=int(values.get('periodic') or 0),
            periodic_pairs=tuple(dict(item) for item in
                                 values.get('periodic_pairs') or ()),
            bounds=tuple(float(v) for v in values.get('bounds') or ()))

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
            _hint: bool = True, convert=None, _matrix: bool = True) -> str:
    """Why *writer_id* cannot write this mesh, or ``''``.

    Asked before anything is written.  Each sentence names the writer, the
    property of the mesh it cannot carry and a way out that does not touch
    anything upstream: another format, or the Generate page's own action
    (Set order) when the user wants this format.

    Plan 48 GM4: ``convert`` is the conversion the user chose
    (:func:`conversions`); a cell the matrix marks CONVERTED is refused,
    naming the conversion, until it is chosen.
    """
    sentence = retired(writer_id)
    if sentence:
        return sentence
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
    if item.id == 'msh':
        reason = msh_option_refusal(options)
        if reason:
            return reason
    if item.id == 'diff':
        reason = diffpack_refusal(facts)
        if reason:
            return reason
    split = options.get('Mesh.PartitionSplitMeshFiles')
    if item.id == 'msh' and _truthy(split) and facts.partitions < 2:
        return ('one file per partition was asked for and the mesh is not '
                'partitioned: partition it on the Generate page first')
    if _matrix and item.kind in (MESH, SURFACE):
        verdict = _matrix_verdict(item, facts, options, convert, _hint)
        if verdict is not None:
            return verdict
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
        return matrix_refusal(item, facts, options, convert) if _matrix else ''
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
    return ''


# -- the .msh the user chose (Plan 48 GM4, D1) ---------------------------------- #

#: The versions the .msh writer offers: Mesh.MshFileVersion and the label. 3.0
#: and 4.0 were accepted by the run and hidden from this list until Plan 48.
MSH_VERSIONS = ((4.1, '4.1'), (4.0, '4.0'), (3.0, '3.0'), (2.2, '2.2'),
                (1.0, '1'))


def _number(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value) -> bool:
    return bool(_number(value))


@dataclass(frozen=True)
class MshChoice:
    """What the user chose for the ``.msh`` on Export, as Gmsh will write it.

    The user's file is written exactly like this and never silently replaced
    (D1).  Combinations Gmsh 4.15.2 does not honour -- it writes another
    version or an unreadable file without a word -- are refused with the
    measured reason (``fragments/gm4/matrix/msh-options-probe.json``).
    """

    version: float = 4.1
    binary: bool = False
    save_all: bool = False
    parametric: bool = False
    split: bool = False

    @classmethod
    def from_options(cls, options) -> 'MshChoice':
        values = dict(options or {})
        return cls(
            version=_number(values.get('Mesh.MshFileVersion'), 4.1),
            binary=_truthy(values.get('Mesh.Binary')),
            save_all=_truthy(values.get('Mesh.SaveAll')),
            parametric=_truthy(values.get('Mesh.SaveParametric')),
            split=_truthy(values.get('Mesh.PartitionSplitMeshFiles')))

    @property
    def label(self) -> str:
        name = dict(MSH_VERSIONS).get(self.version, f'{self.version:g}')
        return f'MSH {name}' + (' binary' if self.binary else ' ASCII')

    @property
    def row(self) -> str:
        """The row of the target matrix this choice reads."""
        return f'msh-{self.version:.1f}' + ('-binary' if self.binary else '')

    def refusal(self) -> str:
        if self.version == 1.0:
            if self.binary:
                return ('MSH 1 has no binary form: Gmsh 4.15.2 ignores the '
                        'version and writes a binary MSH 2.2 file instead '
                        '(measured). Write MSH 1 as ASCII, or choose MSH 2.2 '
                        'or 4.1 for binary.')
            if self.parametric:
                return ('Gmsh 4.15.2 writes MSH 1 with parametric '
                        'coordinates into a file it cannot read back '
                        '(measured: "Wrong node index"). Switch '
                        'SaveParametric off, or choose MSH 2.2 or newer.')
            if self.split:
                return ('Gmsh 4.15.2 writes the files of a partitioned mesh '
                        'as MSH 2.2 whatever version is chosen (measured), '
                        'so MSH 1 is not written one file per partition. '
                        'Choose MSH 2.2 or 4.1, or switch the split off.')
        if self.version == 4.0 and self.binary:
            return ('Gmsh 4.15.2 writes MSH 4.0 only as ASCII ("Can only '
                    'write MSH 4.0 format in ASCII mode"). Choose ASCII, or '
                    'MSH 4.1 for a binary file.')
        if self.version not in dict(MSH_VERSIONS):
            return (f'Gmsh 4.15.2 writes MSH versions '
                    f'{", ".join(name for _v, name in MSH_VERSIONS)}; '
                    f'{self.version:g} is not one of them.')
        return ''


def msh_option_refusal(options) -> str:
    """Why Gmsh would not honour these ``.msh`` options as chosen, or ``''``."""
    return MshChoice.from_options(options).refusal()


def msh_choice_drops(options) -> list:
    """What the chosen ``.msh`` options lose, beyond the matrix (measured).

    The matrix rows are measured with Gmsh's defaults; these are the extra
    losses of an option, from ``fragments/gm4/matrix/msh-options-probe.json``.
    """
    choice = MshChoice.from_options(options)
    drops = []
    if choice.save_all and choice.version <= 2.2:
        drops.append(f'{choice.label} with "Save all elements" is written '
                     'without the physical group names (kept in MSH 3.0 and '
                     'newer)')
    return drops


# -- the unit pins (Plan 48 GM4, D2) --------------------------------------------- #

PIN_UNIT, PIN_SCALING = 'Geometry.OCCTargetUnit', 'Geometry.OCCScaling'
#: What FoamMesh sets before every CAD import (``execution.importer_option_rows``).
PIN_DEFAULTS = {PIN_UNIT: 'M', PIN_SCALING: 1.0}
SET_BY_FOAMMESH = 'set by FoamMesh'
SET_BY_USER = 'set by you'
SET_BY_STARTUP = 'set by your start-up defaults'
#: Metres in one model unit, for the units OpenCASCADE's target unit names.
UNIT_METRES = {'M': 1.0, 'MM': 1e-3, 'CM': 1e-2, 'DM': 1e-1, 'KM': 1e3,
               'UM': 1e-6, 'IN': 0.0254, 'FT': 0.3048, 'YD': 0.9144,
               'MI': 1609.344}


def unit_pin_rows(overrides, startup=None) -> list:
    """The two pins as the Export page shows them.

    ``overrides`` is the project's stored ``{option: value}``; ``startup`` the
    start-up defaults.  A pin nobody changed reads *set by FoamMesh*; one in
    the project reads *set by you* and is unlocked.
    """
    overrides = dict(overrides or {})
    startup = dict(startup or {})
    rows = []
    for name in (PIN_UNIT, PIN_SCALING):
        default = PIN_DEFAULTS[name]
        if name in overrides:
            state, value = SET_BY_USER, overrides[name]
        elif name in startup and startup[name] not in ('', None):
            state, value = SET_BY_STARTUP, startup[name]
        else:
            state, value = SET_BY_FOAMMESH, default
        rows.append({'name': name, 'value': value, 'default': default,
                     'state': state, 'unlocked': state != SET_BY_FOAMMESH})
    return rows


def with_unit_pins(overrides, *, unlock: bool, unit=None, scaling=None) -> dict:
    """The stored options with the pins locked, or unlocked at a value.

    Locked removes both options, so FoamMesh's own pin applies again; unlocked
    stores both (the current value, or the default, or the one given), checked
    by the catalogue like any other option.
    """
    mapping = dict(overrides or {})
    if not unlock:
        mapping.pop(PIN_UNIT, None)
        mapping.pop(PIN_SCALING, None)
        return mapping
    unit_value = str(unit if unit is not None else mapping.get(
        PIN_UNIT, PIN_DEFAULTS[PIN_UNIT])).strip()
    scale_value = scaling if scaling is not None else mapping.get(
        PIN_SCALING, PIN_DEFAULTS[PIN_SCALING])
    if not unit_value:
        raise catalogue.OptionRefusal('the target unit cannot be empty')
    scale = catalogue.parse_value(PIN_SCALING, scale_value)
    if not float(scale) > 0:
        raise catalogue.OptionRefusal('the import scaling must be above zero')
    mapping[PIN_UNIT] = catalogue.parse_value(PIN_UNIT, unit_value)
    mapping[PIN_SCALING] = scale
    return mapping


def metres_per_unit(overrides, startup=None) -> float | None:
    """Metres in one unit of the model the pins make; ``None`` when unknown."""
    unit = str(unit_pin_rows(overrides, startup)[0]['value']).strip().upper()
    return UNIT_METRES.get(unit)


# -- the measured target matrix (Plan 48 GM4, §4.9.1) ---------------------------- #

SUPPORTED, CONVERTED, REFUSED = 'SUPPORTED', 'CONVERTED', 'REFUSED'
MATRIX_FILE = 'target-matrix.json'

#: The conversions a CONVERTED cell can name, each validated on its own.
CONVERSIONS = {
    'first_order_copy': (
        'Write a first-order copy',
        'The file holds the corner nodes of every element (order 1), made on '
        'a copy for this write. The mesh in the session and your .msh keep '
        'their order.'),
    'extrude_2d': (
        'Extrude the 2D mesh one cell',
        'A polyMesh is three-dimensional: the 2D mesh is extruded one cell '
        'thick between two empty patches. The 2D mesh itself is unchanged.'),
    'cyclic_patches': (
        'Publish the periodic pairs as cyclic patches',
        'Each periodic pair of the mesh becomes a matched pair of cyclic '
        'patches carrying the translation.'),
}

#: What the first-order copy leaves out (GM4.matrix_page).
MID_EDGE_DROP = 'the mid-edge nodes'

_matrix_cache: dict = {}


def matrix_paths() -> list:
    """Where the matrix is looked for: the shipped resource, then the evidence."""
    import os

    found = []
    override = os.environ.get('FOAMMESH_TARGET_MATRIX')
    if override is not None:
        return [Path(override)] if override else []
    try:
        from resources import resource
        found.append(Path(resource.file('gmsh/' + MATRIX_FILE)))
    except Exception:                                        # noqa: BLE001
        pass
    root = Path(__file__).resolve().parents[4]
    found.append(root / 'plans' / 'evidence' / 'plan48' / MATRIX_FILE)
    return found


def load_matrix(path=None, *, refresh: bool = False) -> dict:
    """The measured matrix, or ``{}`` when none is on disk."""
    key = str(path) if path else ''
    if not refresh and key in _matrix_cache:
        return _matrix_cache[key]
    document: dict = {}
    for candidate in ([Path(path)] if path else matrix_paths()):
        try:
            if candidate.is_file():
                document = json.loads(candidate.read_text(encoding='utf-8'))
                document['_path'] = str(candidate)
                break
        except (OSError, ValueError):
            continue
    _matrix_cache[key] = document
    return document


def clear_matrix_cache() -> None:
    _matrix_cache.clear()


def family_name(facts: MeshFacts) -> str:
    families = facts.volume_families() if facts.dimension >= 3 \
        else facts.surface_families()
    if not families:
        return ''
    return families[0] if len(families) == 1 else 'mixed'


def order_bucket(order: int) -> str:
    return '1' if order <= 1 else '2' if order == 2 else '3-10'


def mesh_class(facts: MeshFacts) -> dict | None:
    """The cell coordinates of this mesh in the matrix, or ``None``."""
    family = family_name(facts)
    if facts.dimension not in (2, 3) or not family:
        return None
    return {'dimension': facts.dimension, 'family': family,
            'order': order_bucket(facts.order),
            'partitioned': bool(facts.partitions),
            'periodic': bool(facts.periodic)}


def cell_key(cell_class: dict) -> str:
    return '|'.join((str(cell_class['dimension']), cell_class['family'],
                     cell_class['order'], str(int(cell_class['partitioned'])),
                     str(int(cell_class['periodic']))))


def row_id(item: Writer, options=None) -> str:
    return MshChoice.from_options(options).row if item.id == 'msh' else item.id


@dataclass(frozen=True)
class Cell:
    """One cell of the target matrix."""

    state: str
    reason: str = ''
    #: what the file loses on the way (stated to the user, never silent).
    drops: tuple = ()
    #: the conversions that must be chosen (CONVERTED only).
    conversions: tuple = ()
    evidence: str = ''


#: Text the measuring run printed that says nothing about the format: the WSL
#: launcher's warning, and the measuring command line with its paths.
_NOISE = re.compile(r"\s*Warning : Skipping unknown option '-noenv'\.?")
_COMMAND = re.compile(r"\s*Command '\[.*", re.DOTALL)
_PATH = re.compile(r"(?:/mnt/|/home/|/tmp/|[A-Za-z]:[\\/])[^\s'\",]*")
#: Signs that the measuring script itself failed (DP-5745), not the writer.
_MEASURING_FAILED = ('measure_case.py", line', 'Traceback (most recent call last)',
                     '[Errno ')


def measuring_failed(reason: str) -> bool:
    """True when a cell's reason is the measuring script's own failure, so
    the cell was never measured and the writer limits answer (DP-5745)."""
    return any(sign in reason for sign in _MEASURING_FAILED)


def plain_reason(reason: str) -> str:
    """A measured reason as the Export page shows it (DP-5745): without the
    launcher's warning, the measuring command line or any local path, each
    distinct part once."""
    parts = []
    for part in reason.split(' | '):
        part = _COMMAND.sub('', _NOISE.sub('', part))
        part = _PATH.sub('', part)
        part = re.sub(r'no census \(exit (-?\d+)\)', r'Gmsh stopped (exit \1)',
                      part)
        part = re.sub(r'^crash \(exit (-?\d+)\)',
                      r'Gmsh stopped with an error (exit \1)', part)
        part = ' '.join(part.split()).strip(' :;|')
        if part and part not in parts:
            parts.append(part)
    return '; '.join(parts)


def cell_for(item: Writer, facts: MeshFacts, options=None) -> Cell | None:
    """The measured cell for this writer and this mesh, or ``None``."""
    cell_class = mesh_class(facts)
    if cell_class is None:
        return None
    row = (load_matrix().get('rows') or {}).get(row_id(item, options))
    if not row:
        return None
    raw = (row.get('cells') or {}).get(cell_key(cell_class))
    if not raw or measuring_failed(str(raw.get('reason') or '')):
        return None
    return Cell(state=str(raw.get('state') or ''),
                reason=plain_reason(str(raw.get('reason') or '')),
                drops=tuple(raw.get('drops') or ()),
                conversions=tuple(raw.get('conversions') or ()),
                evidence=str(raw.get('evidence') or ''))


def _matrix_verdict(item: Writer, facts: MeshFacts, options, convert,
                    hint) -> str | None:
    """What the matrix says for a file writer: ``''`` (write it), a refusal, or
    ``None`` when it has no cell for this mesh (the writer limits answer).

    The writer limits keep their wording where they also refuse, so the
    sentence the user reads names the order the format carries.
    """
    cell = cell_for(item, facts, options)
    if cell is None:
        return None
    legacy = refusal(item.id, facts, options, hint, _matrix=False)
    if cell.state == SUPPORTED:
        # The matrix can only tighten a writer limit that was measured live
        # (an element-order ceiling, Plot3D's structured blocks, the session
        # holding a mesh read from a file): its read-back counts elements and
        # cannot show that the order or the blocks survived, so a limit the
        # writer rules state stays.
        return legacy
    if cell.state == REFUSED:
        return legacy or cell.reason or f'{item.label} cannot carry this mesh'
    chosen = {convert} if isinstance(convert, str) else set(convert or ())
    missing = [name for name in cell.conversions if name not in chosen]
    if not missing:
        return ''
    labels = ' and '.join(f'"{CONVERSIONS[name][0]}"' for name in missing)
    return (legacy or cell.reason or f'{item.label} cannot take this mesh as '
            'it is') + (f' Choose {labels} to write it; your mesh and your '
                        '.msh stay as they are.')


def matrix_refusal(item: Writer, facts: MeshFacts, options=None,
                   convert=None) -> str:
    """Why the matrix refuses this write, or ``''`` (SUPPORTED, or CONVERTED
    and the conversion chosen)."""
    cell = cell_for(item, facts, options)
    if cell is None or cell.state == SUPPORTED:
        return ''
    if cell.state == REFUSED:
        return cell.reason or f'{item.label} cannot carry this mesh'
    chosen = {convert} if isinstance(convert, str) else set(convert or ())
    missing = [name for name in cell.conversions if name not in chosen]
    if not missing:
        return ''
    labels = ' and '.join(f'"{CONVERSIONS[name][0]}"' for name in missing)
    return (cell.reason or f'{item.label} cannot take this mesh as it is') + \
        f' Choose {labels} to write it; your mesh and your .msh stay as they are.'


def conversions(item: Writer, facts: MeshFacts, options=None) -> list:
    """The conversions this write needs, ``[{id, label, description}]``."""
    cell = cell_for(item, facts, options)
    if cell is None or cell.state != CONVERTED:
        return []
    return [{'id': name, 'label': CONVERSIONS[name][0],
             'description': CONVERSIONS[name][1]} for name in cell.conversions]


def stated_drops(item: Writer, facts: MeshFacts, options=None) -> list:
    """What this write loses on a mesh of this kind, in sentences."""
    cell = cell_for(item, facts, options)
    drops = list(cell.drops) if cell is not None else []
    # DP-5729: a write that needs the first-order copy loses the mid-edge
    # nodes; the measured cells list only what the copy's write lost
    if (cell is not None and 'first_order_copy' in cell.conversions
            and not any('mid-edge' in drop for drop in drops)):
        drops.insert(0, MID_EDGE_DROP)
    if item.id == 'msh':
        drops += msh_choice_drops(options)
    return drops


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
