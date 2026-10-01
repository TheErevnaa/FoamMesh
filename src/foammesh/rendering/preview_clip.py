#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Clip what is on screen in the renderer while a large section is dragged.

Plan 37 UF8. Above the section tool's live budget a clip is not re-cut on
every mouse move. The preview on screen -- the bounded boundary preview, a
geometry surface, the worker's last section -- is clipped by the mappers
instead: the GPU drops the fragments on the removed side, the data is not
touched, and nothing is read. It is a picture of where the plane is, never a
whole-cell result (the section tool says "Preview while moving"); the exact
cut is made when the handle is let go.
"""
from __future__ import annotations

from vtkmodules.vtkCommonDataModel import vtkPlane


def _mappers(props):
    for prop in props:
        getMapper = getattr(prop, 'GetMapper', None)
        mapper = getMapper() if getMapper is not None else None
        if mapper is not None and hasattr(mapper, 'AddClippingPlane'):
            yield prop, mapper


def _movePlane(held, plane) -> None:
    """Move a plane the mapper already holds onto ``plane``."""
    held.SetOrigin(plane.GetOrigin())
    held.SetNormal(plane.GetNormal())


def setPreviewClipping(props, planes) -> None:
    """Clip every prop in *props* by *planes* (kept half: the normal side,
    as a filter clip keeps it).

    2026-10-01. A mapper already clipped by as many planes has them moved
    in place: the plane collection, and so the mapper, stays unmodified, so
    the next frame only updates the clip uniforms. Replacing the planes on
    every move modified the mapper, which rebuilt its vertex buffers each
    frame (live, 5 M cells: 34-52 ms a moving frame against 10-16 ms idle).
    """
    for prop, mapper in _mappers(props):
        current = mapper.GetClippingPlanes()
        if (current is not None and planes
                and current.GetNumberOfItems() == len(planes)):
            for position, plane in enumerate(planes):
                _movePlane(current.GetItem(position), plane)
            continue
        mapper.RemoveAllClippingPlanes()
        for plane in planes:
            copy = vtkPlane()
            copy.SetOrigin(plane.GetOrigin())
            copy.SetNormal(plane.GetNormal())
            mapper.AddClippingPlane(copy)
        # vtkQuadricLODActor draws its own decimated copy on an interactive
        # frame (DP-814); a modified actor hands it the mapper's planes.
        prop.Modified()


def clearPreviewClipping(props) -> None:
    for prop, mapper in _mappers(props):
        if mapper.GetClippingPlanes() is not None \
                and mapper.GetClippingPlanes().GetNumberOfItems():
            mapper.RemoveAllClippingPlanes()
            prop.Modified()


def clippingPlaneCount(prop) -> int:
    getMapper = getattr(prop, 'GetMapper', None)
    mapper = getMapper() if getMapper is not None else None
    planes = mapper.GetClippingPlanes() if mapper is not None else None
    return planes.GetNumberOfItems() if planes is not None else 0
