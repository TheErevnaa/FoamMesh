#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Headless surface-geometry importers (STL, OBJ) returning VTK polydata."""

from .base import ImportedSurface, ImportResult, import_surface, SUPPORTED_SUFFIXES

__all__ = ['ImportedSurface', 'ImportResult', 'import_surface', 'SUPPORTED_SUFFIXES']
