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


@dataclass
class PartData:
    name: str = ''
    color: str = ''
    faces: list[FaceData] = field(default_factory=list)


def build_model_from_parts(parts: list[PartData], source_format: str,
                           unit: str = 'mm',
                           declared: str = '') -> CadModel:
    """Assemble a CadModel from extracted (name-bearing) parts. Pure / no OCCT."""
    bodies = []
    for i, part in enumerate(parts):
        faces = [
            CadFace(id=f'body{i}_face{j}', name=f.name, color=f.color,
                    source_ref={'body_index': i, 'face_index': j,
                                'xde_name': f.name or None})
            for j, f in enumerate(part.faces)
        ]
        bodies.append(CadBody(id=f'body{i}',
                              name=part.name or f'Body {i + 1}',
                              color=part.color, faces=faces))
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
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)

    for i in range(1, free.Length() + 1):
        label = free.Value(i)
        part = PartData(name=_label_name(label) or f'Part {i}',
                        color=_label_color(color_tool, label))
        shape = shape_tool.GetShape(label)
        builder.Add(compound, shape)

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
            part.faces.append(FaceData(name=fname or f'face{fi}', color=fcolor))
            fi += 1
            explorer.Next()
        parts.append(part)

    # R193. The shape is in the unit the reader was pinned to, not the one
    # the file declares -- OCCT has already done that conversion. Scaling
    # by the declaration afterwards either does it twice or, for the
    # metre-declaring STEP that is most of this corpus, not at all: a
    # 0.6 m tee arrived as a 600 m tee.
    model = build_model_from_parts(parts, fmt, unit=READER_UNIT,
                                   declared=declared_unit(path, fmt))
    return compound, model


def build_model(shape, source_format: str, unit_name: str | None = None,
                declared: str = '') -> CadModel:
    """Basic topology walk (no names) — fallback for BREP / when XDE fails. Requires OCCT."""
    require()
    from OCC.Core.TopExp import TopExp_Explorer
    from OCC.Core.TopAbs import TopAbs_SOLID, TopAbs_FACE

    parts: list[PartData] = []
    solid_exp = TopExp_Explorer(shape, TopAbs_SOLID)
    while solid_exp.More():
        part = PartData()
        face_exp = TopExp_Explorer(solid_exp.Current(), TopAbs_FACE)
        while face_exp.More():
            part.faces.append(FaceData())
            face_exp.Next()
        parts.append(part)
        solid_exp.Next()
    if not parts:
        part = PartData()
        face_exp = TopExp_Explorer(shape, TopAbs_FACE)
        while face_exp.More():
            part.faces.append(FaceData())
            face_exp.Next()
        parts.append(part)
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
