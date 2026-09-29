#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""OpenCASCADE availability guard.

Keeps the base app installable/runnable without the heavy OCCT dependency; CAD
features check availability and fail with an actionable message if it's missing.
"""
from __future__ import annotations

import importlib.util
import os
import sys


_DLL_HANDLES = []
_DLL_PATHS = set()


def _prepare_dll_search() -> None:
    """Make conda OCCT DLLs visible under Python's restricted Windows loader."""
    if os.name != 'nt' or not hasattr(os, 'add_dll_directory'):
        return
    candidates = [os.path.join(sys.prefix, 'Library', 'bin')]
    for candidate in candidates:
        if os.path.isdir(candidate) and candidate not in _DLL_PATHS:
            try:
                _DLL_HANDLES.append(os.add_dll_directory(candidate))
                _DLL_PATHS.add(candidate)
            except OSError:
                pass

CAD_INSTALL_HINT = (
    'CAD import (STEP/IGES/BREP) requires OpenCASCADE. '
    'Install the supported binding with: '
    'conda install -c conda-forge pythonocc-core=7.8.1.1'
)


def is_available() -> bool:
    """True if pythonocc-core (OCC) is installed.

    Plan 35 CR7: the window never loads OCCT -- a worker reads every CAD
    file -- so outside a worker this asks only whether the package is there,
    and does not import it to find out. A worker, which is about to use it,
    loads it.
    """
    if os.environ.get('FOAMMESH_WORKER') != '1':
        return importlib.util.find_spec('OCC') is not None
    return _importable()


def _importable() -> bool:
    if importlib.util.find_spec('OCC') is None:
        return False
    _prepare_dll_search()
    try:
        from OCC.Core import BRep, STEPControl  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


def require() -> None:
    """Raise a clear error if OCCT is not installed.

    Called where OCCT is about to be used, so it loads it: an install that
    is present but broken fails here with the hint, not deeper in a reader.
    """
    if not _importable():
        raise RuntimeError(CAD_INSTALL_HINT)
