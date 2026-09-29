#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CAD import (STEP/IGES/BREP) via OpenCASCADE.

For STEP/IGES this uses the **XDE** readers (``STEPCAFControl_Reader`` /
``IGESCAFControl_Reader``) so the real assembly/part/face **names and colors** in
the CAD file are preserved — not fabricated. BREP carries no such metadata, so it
falls back to a basic topology walk with generic names.

OCCT use is lazy; without the ``[cad]`` extra these raise a clear install hint.
The model-assembly step (``build_model_from_parts``) is pure and unit-tested, so
the name-preservation logic is verified even without OCCT installed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path

from .availability import require
from .formats import detect_format
from .model import CadModel, CadBody, CadFace
from .units import READER_UNIT, declared_unit, map_cad_unit

logger = logging.getLogger(__name__)


def _pin_reader_unit() -> None:
    """Ask OCCT for the unit we are about to say the shape is in (R193).

    The XSTEP readers convert every length into ``xstep.cascade.unit``. Left
    alone that is millimetres, but it is a process-wide static that anything
    sharing the interpreter can change -- Gmsh writes it from
    ``Geometry.OCCTargetUnit``, which the mesh runner sets to ``M``. An import
    that inherits whatever was set last is an import whose scale depends on
    what ran before it, so state it every time.

    Plan 31 CP-04. MEASURED: ``SetCVal`` is a silent no-op until an XSTEP
    controller exists. In a fresh interpreter it returns ``False`` and
    ``CVal('xstep.cascade.unit')`` reads back empty; constructing a reader
    afterwards initialises the static to the reader's own default. So this
    pinned nothing on the first import of a process and only ever agreed with
    the default by luck -- which is the coincidence the paragraph above says
    it exists to remove. After initialising the controllers, ``SetCVal``
    returns ``True`` and ``CVal`` reads back ``MM``.
    """
    try:
        from OCC.Core.Interface import Interface_Static
        from OCC.Core.STEPControl import STEPControl_Controller
        from OCC.Core.IGESControl import IGESControl_Controller

        # Idempotent: OCCT keeps the controller already registered for a norm.
        STEPControl_Controller.Init()
        IGESControl_Controller.Init()
        if not Interface_Static.SetCVal('xstep.cascade.unit',
                                        READER_UNIT.upper()):
            logger.debug('OCCT refused the cascade unit pin')
    except Exception as error:  # noqa: BLE001 - OCCT raises many kinds
        logger.debug('could not pin the OCCT cascade unit: %s', error)


# --- pure, testable model assembly ----------------------------------------

@dataclass
class FaceData:
    name: str = ''
    color: str = ''
    # DP-92. What the OCCT face answers about itself, carried so the layer
    # page can tell a selection that meshes from one that cannot. Left unset
    # by the pure assembly path and wherever OCCT could not be asked.
    planar: bool | None = None
    area: float | None = None
    #: Position in the flat face walk over the whole document.
    order: int = -1
    #: Flat-walk positions of the faces this one shares an edge with.
    neighbours: tuple[int, ...] = ()
    #: Flat-walk position of the coincident face in another solid, or -1.
    twin: int = -1


@dataclass
class PartData:
    name: str = ''
    color: str = ''
    faces: list[FaceData] = field(default_factory=list)
    #: DP-900. Whether the part is (or holds) a closed solid. ``None`` where
    #: nobody asked OCCT, which every reader treats as "not measured".
    solid: bool | None = None


def build_model_from_parts(parts: list[PartData], source_format: str,
                           unit: str = 'mm',
                           declared: str = '') -> CadModel:
    """Assemble a CadModel from extracted (name-bearing) parts. Pure / no OCCT."""
    bodies = []
    for i, part in enumerate(parts):
        faces = [
            CadFace(id=f'body{i}_face{j}', name=f.name, color=f.color,
                    source_ref={'body_index': i, 'face_index': j,
                                'xde_name': f.name or None},
                    planar=f.planar, area=f.area, face_order=f.order)
            for j, f in enumerate(part.faces)
        ]
        bodies.append(CadBody(id=f'body{i}',
                              name=part.name or f'Body {i + 1}',
                              color=part.color, faces=faces,
                              solid=part.solid))
    # DP-92. The adjacency and the interface twin are answers about the whole
    # document, and the walk that measured them numbered the faces before
    # they were cut into bodies. Resolving them here, once every id exists,
    # is what lets the layer page read them by patch rather than by position.
    by_order = {f.order: face
                for part, body in zip(parts, bodies)
                for f, face in zip(part.faces, body.faces) if f.order >= 0}
    for part, body in zip(parts, bodies):
        for source, face in zip(part.faces, body.faces):
            face.adjacent_ids = tuple(
                by_order[index].id for index in source.neighbours
                if index in by_order)
            twin = by_order.get(source.twin)
            face.interface_id = twin.id if twin is not None else ''
    return CadModel(source_format=source_format, unit=map_cad_unit(unit),
                    declared_unit=declared or '', bodies=bodies)


# --- OCCT helpers (lazy) --------------------------------------------------

def _color_to_hex(qcolor) -> str:
    r = int(round(qcolor.Red() * 255))
    g = int(round(qcolor.Green() * 255))
    b = int(round(qcolor.Blue() * 255))
    return f'#{r:02x}{g:02x}{b:02x}'


def _label_name(label) -> str:
    from OCC.Core.TDataStd import TDataStd_Name
    attr = TDataStd_Name()
    try:
        if label.FindAttribute(TDataStd_Name.GetID(), attr):
            return str(attr.Get().ToExtString())
    except Exception as error:  # noqa: BLE001 - a label without the attribute is not an error
        logger.debug('CAD label attribute unavailable: %s', error)
    return ''


def _label_color(color_tool, label) -> str:
    try:
        from OCC.Core.Quantity import Quantity_Color
        from OCC.Core.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_ColorGen
        c = Quantity_Color()
        for ctype in (XCAFDoc_ColorSurf, XCAFDoc_ColorGen):
            if color_tool.GetColor(label, ctype, c):
                return _color_to_hex(c)
    except Exception as error:  # noqa: BLE001 - a label without the attribute is not an error
        logger.debug('CAD label attribute unavailable: %s', error)
    return ''


# --- readers --------------------------------------------------------------

def read_shape(path):
    """Read a CAD file into a plain OCCT TopoDS_Shape (geometry only). Requires OCCT."""
    require()
    path = Path(path)
    fmt = detect_format(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if fmt == 'step':
        from OCC.Core.STEPControl import STEPControl_Reader
        from OCC.Core.IFSelect import IFSelect_RetDone
        _pin_reader_unit()
        reader = STEPControl_Reader()
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            raise ValueError(f'OCCT could not read STEP file: {path}')
        if reader.TransferRoots() <= 0:
            raise ValueError(f'STEP file contains no transferable roots: {path}')
        shape = reader.OneShape()
        if shape.IsNull():
            raise ValueError(f'STEP transfer produced a null shape: {path}')
        return shape
    if fmt == 'iges':
        from OCC.Core.IGESControl import IGESControl_Reader
        from OCC.Core.IFSelect import IFSelect_RetDone
        _pin_reader_unit()
        reader = IGESControl_Reader()
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            raise ValueError(f'OCCT could not read IGES file: {path}')
        if reader.TransferRoots() <= 0:
            raise ValueError(f'IGES file contains no transferable roots: {path}')
        shape = reader.OneShape()
        if shape.IsNull():
            raise ValueError(f'IGES transfer produced a null shape: {path}')
        return shape
    from OCC.Core.BRepTools import breptools
    from OCC.Core.TopoDS import TopoDS_Shape
    from OCC.Core.BRep import BRep_Builder
    shape = TopoDS_Shape(); builder = BRep_Builder()
    if not breptools.Read(shape, str(path), builder) or shape.IsNull():
        raise ValueError(f'OCCT could not read BREP file: {path}')
    return shape


def read_with_xde(path, fmt: str):
    """Read STEP/IGES with XDE -> (shape, CadModel) preserving names/colors. Requires OCCT."""
    require()
    from OCC.Core.TDocStd import TDocStd_Document
    from OCC.Core.XCAFApp import XCAFApp_Application
    from OCC.Core.XCAFDoc import XCAFDoc_DocumentTool
    from OCC.Core.TDF import TDF_LabelSequence
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_FACE
    from OCC.Core.TopoDS import TopoDS_Compound
    from OCC.Core.BRep import BRep_Builder

    app = XCAFApp_Application.GetApplication()
    doc = TDocStd_Document('MDTV-XCAF')
    app.NewDocument('MDTV-XCAF', doc)

    _pin_reader_unit()
    if fmt == 'step':
        from OCC.Core.STEPCAFControl import STEPCAFControl_Reader
        reader = STEPCAFControl_Reader()
    else:
        from OCC.Core.IGESCAFControl import IGESCAFControl_Reader
        reader = IGESCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    from OCC.Core.IFSelect import IFSelect_RetDone
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise ValueError(f'OCCT XDE reader could not read {path}')
    if not reader.Transfer(doc):
        raise ValueError(f'OCCT XDE reader could not transfer {path}')

    shape_tool = XCAFDoc_DocumentTool.ShapeTool(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool(doc.Main())

    free = TDF_LabelSequence()
    shape_tool.GetFreeShapes(free)

    parts: list[PartData] = []
    # DP-92. One numbering across every free shape, because the questions
    # _measure_faces answers -- who shares an edge with whom, which two faces
    # are one interface written twice -- are about the document, not a part.
    walked: list = []
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)

    for i in range(1, free.Length() + 1):
        label = free.Value(i)
        base_name = _label_name(label) or f'Part {i}'
        base_color = _label_color(color_tool, label)
        shape = shape_tool.GetShape(label)
        builder.Add(compound, shape)

        # The flat walk is the authority. `TopExp_Explorer` visits a shared
        # face once, and `store._cad_solid_names` aligns the model against
        # the tessellation by position in exactly this sequence, so the
        # solids below are only allowed to say where to cut it.
        ordered = []
        explorer = TopExp_Explorer(shape, TopAbs_FACE)
        fi = 0
        while explorer.More():
            face = explorer.Current()
            fname = fcolor = ''
            try:
                from OCC.Core.TDF import TDF_Label
                face_label = TDF_Label()
                if shape_tool.FindSubShape(label, face, face_label):
                    fname = _label_name(face_label)
                    fcolor = _label_color(color_tool, face_label)
            except Exception as error:  # noqa: BLE001 - OCCT raises many kinds
                logger.debug('CAD face %d has no XDE label: %s', fi, error)
            # The generic name keeps counting across the whole free shape:
            # it becomes the patch name, and restarting it inside each solid
            # would put two `face0` patches in one file.
            data = FaceData(name=fname or f'face{fi}', color=fcolor,
                            order=len(walked))
            walked.append((face, data))
            ordered.append((face, data))
            fi += 1
            explorer.Next()

        parts.extend(_split_into_solids(
            shape, ordered, shape_tool, color_tool, label,
            base_name, base_color))

    _measure_faces(compound, walked)

    # R193. The shape is in the unit the reader was pinned to, not the one
    # the file declares -- OCCT has already done that conversion. Scaling
    # by the declaration afterwards either does it twice or, for the
    # metre-declaring STEP that is most of this corpus, not at all: a
    # 0.6 m tee arrived as a 600 m tee.
    model = build_model_from_parts(parts, fmt, unit=READER_UNIT,
                                   declared=declared_unit(path, fmt))
    return compound, model


def _measure_faces(shape, walked) -> None:
    """Fill in what each OCCT face knows about itself. DP-92.

    *walked* is ``[(TopoDS_Face, FaceData), ...]`` in flat-walk order over the
    whole document, which is the order the mesher reads the file in.

    Three answers, and each one is a question the boundary-layer page could
    not previously ask. Whether a face is flat, because a patch left without a
    layer has its opening closed with a plane ([DP-90]). Which faces it shares
    an edge with, because two patches whose layers grow in opposite directions
    may not share one ([DP-91]). And whether it is one half of an interface --
    two coincident faces, one per solid, which the STEP for a conjugate
    assembly carries and which healing merges into the single face the mesher
    sees. MEASURED on `annulus_shell.step`: seven faces, two of them the bore
    cylinder written twice, and the merge keeps the one the walk saw first.

    Advisory throughout. A document OCCT will not answer for keeps the
    unmeasured `None`, and every reader treats that as "nobody asked".
    """
    if not walked:
        return
    try:
        from OCC.Core.BRepAdaptor import BRepAdaptor_Surface
        from OCC.Core.BRepGProp import brepgprop
        from OCC.Core.GProp import GProp_GProps
        from OCC.Core.GeomAbs import GeomAbs_Plane
        from OCC.Core.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SOLID
        from OCC.Core.TopExp import TopExp_Explorer, topexp
        from OCC.Core.TopTools import TopTools_IndexedMapOfShape
    except Exception as error:  # noqa: BLE001 - OCCT raises many kinds
        logger.debug('OCCT cannot be asked about the faces: %s', error)
        return

    every_edge = TopTools_IndexedMapOfShape()
    topexp.MapShapes(shape, TopAbs_EDGE, every_edge)

    solids = []
    explorer = TopExp_Explorer(shape, TopAbs_SOLID)
    while explorer.More():
        owned = TopTools_IndexedMapOfShape()
        topexp.MapShapes(explorer.Current(), TopAbs_FACE, owned)
        solids.append(owned)
        explorer.Next()

    sharing, coincident = {}, {}
    for index, (face, data) in enumerate(walked):
        try:
            surface = BRepAdaptor_Surface(face)
            properties = GProp_GProps()
            brepgprop.SurfaceProperties(face, properties)
            centre = properties.CentreOfMass()
            data.planar = surface.GetType() == GeomAbs_Plane
            data.area = float(properties.Mass())
        except Exception as error:  # noqa: BLE001 - OCCT raises many kinds
            logger.debug('OCCT could not measure face %d: %s', index, error)
            continue
        rim = TopTools_IndexedMapOfShape()
        topexp.MapShapes(face, TopAbs_EDGE, rim)
        for position in range(1, rim.Size() + 1):
            number = every_edge.FindIndex(rim.FindKey(position))
            if number:
                sharing.setdefault(number, []).append(index)
        home = tuple(number for number, solid in enumerate(solids)
                     if solid.Contains(face))
        # Rounded, because two copies of one cylinder written independently
        # into a STEP agree to the last bit they were both written with and
        # not beyond it.
        coincident.setdefault(
            (round(data.area, 9), round(centre.X(), 9),
             round(centre.Y(), 9), round(centre.Z(), 9)), []).append(
                 (index, home))

    for index, (_face, data) in enumerate(walked):
        neighbours = {other for members in sharing.values()
                      if index in members for other in members} - {index}
        data.neighbours = tuple(sorted(neighbours))

    for members in coincident.values():
        for index, home in members:
            if len(home) > 1:
                # One face, both solids: the interface is already single and
                # it is its own canonical name.
                walked[index][1].twin = index
                continue
            partners = [other for other, elsewhere in members
                        if other != index and elsewhere and elsewhere != home]
            if partners:
                walked[index][1].twin = min(partners)

def _split_into_solids(shape, ordered, shape_tool, color_tool, label,
                       base_name: str, base_color: str) -> list:
    """Cut one free shape's face walk into one part per closed solid.

    *ordered* is ``[(TopoDS_Face, FaceData), ...]`` in flat-walk order. The
    return is that same list, partitioned, with nothing added, dropped or
    reordered. A free shape holding no solid, or exactly one, produces the
    single part it always did.
    """
    from OCC.Core.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCC.Core.TopExp import TopExp_Explorer, topexp
    from OCC.Core.TopTools import TopTools_IndexedMapOfShape

    solids = []
    solid_exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while solid_exp.More():
        faces = TopTools_IndexedMapOfShape()
        topexp.MapShapes(solid_exp.Current(), TopAbs_FACE, faces)
        solids.append((solid_exp.Current(), faces))
        solid_exp.Next()

    if len(solids) < 2:
        # DP-900. A free shape with no solid in it -- a lone face, an open
        # shell -- is a part, but not a volume.
        return [PartData(name=base_name, color=base_color,
                         faces=[data for _face, data in ordered],
                         solid=bool(solids))]

    buckets: list[list] = [[] for _ in solids]
    loose: list = []
    for face, data in ordered:
        for index, (_solid, faces) in enumerate(solids):
            if faces.Contains(face):
                buckets[index].append(data)
                break
        else:
            # A face under no solid is not dropped, and not silently attached
            # to a solid it does not belong to.
            loose.append(data)

    parts = []
    for index, bucket in enumerate(buckets):
        name = color = ''
        try:
            from OCC.Core.TDF import TDF_Label
            solid_label = TDF_Label()
            if shape_tool.FindSubShape(label, solids[index][0], solid_label):
                name = _label_name(solid_label)
                color = _label_color(color_tool, solid_label)
        except Exception as error:  # noqa: BLE001 - OCCT raises many kinds
            logger.debug('CAD solid %d has no XDE label: %s', index, error)
        parts.append(PartData(name=name or f'{base_name} solid {index + 1}',
                              color=color or base_color, faces=bucket,
                              solid=True))
    if loose:
        parts.append(PartData(name=f'{base_name} loose faces',
                              color=base_color, faces=loose, solid=False))
    return parts


def build_model(shape, source_format: str, unit_name: str | None = None,
                declared: str = '') -> CadModel:
    """Basic topology walk (no names) — fallback for BREP / when XDE fails. Requires OCCT."""
    require()
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_SOLID, TopAbs_FACE

    parts: list[PartData] = []
    walked: list = []
    solid_exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while solid_exp.More():
        part = PartData(solid=True)
        face_exp = TopExp_Explorer(solid_exp.Current(), TopAbs_FACE)
        while face_exp.More():
            data = FaceData(order=len(walked))
            walked.append((face_exp.Current(), data))
            part.faces.append(data)
            face_exp.Next()
        parts.append(part)
        solid_exp.Next()
    if not parts:
        part = PartData(solid=False)
        face_exp = TopExp_Explorer(shape, TopAbs_FACE)
        while face_exp.More():
            data = FaceData(order=len(walked))
            walked.append((face_exp.Current(), data))
            part.faces.append(data)
            face_exp.Next()
        parts.append(part)
    _measure_faces(shape, walked)
    return build_model_from_parts(parts, source_format,
                                  unit=map_cad_unit(unit_name),
                                  declared=declared)


def read_cad(path, unit_name: str | None = None):
    """High-level read -> (shape, CadModel). STEP/IGES use XDE (names preserved);
    BREP uses the basic walk. Requires OCCT ([cad] extra)."""
    fmt = detect_format(path)            # validates suffix before requiring OCCT
    if fmt in ('step', 'iges'):
        try:
            shape, model = read_with_xde(path, fmt)
            if not shape.IsNull() and model.n_faces:
                return shape, model
        except Exception as error:  # noqa: BLE001 - OCCT raises many kinds
            logger.warning(
                'XDE read of %s failed (%s); names and colours will be generic',
                path, error)
    shape = read_shape(path)
    if fmt in ('step', 'iges'):
        # R193 on the fallback path too: read_shape pinned the reader, so
        # the shape is in READER_UNIT. unit_name is the unit the user
        # declared in the dialog, which only BREP needs -- it is the one
        # format that cannot state its own.
        return shape, build_model(shape, fmt, READER_UNIT,
                                  declared=declared_unit(path, fmt))
    return shape, build_model(shape, fmt, unit_name)
