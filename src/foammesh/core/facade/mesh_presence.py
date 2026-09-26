#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Does this case hold a mesh, asked in the words the engine answers in.

DP-62 taught the export gate that a Gmsh run targeting SU2 leaves a mesh
behind without leaving a ``constant/polyMesh`` behind, and answered it
inside :meth:`DomainOperations._classify` as one extra payload key. Every
other reader of "is there a mesh" kept asking for the polyMesh alone, so
the quality surfaces went on reading a meshed SU2 case as an empty one.

This module is that one question, written once, so the window and the
facade payload cannot drift apart. It is deliberately small and free of
session state: it takes a path and an engine id and reads the disk.
"""
from __future__ import annotations

from foammesh.db.configurations_schema import MeshEngine


def native_mesh_artifact(case_path):
    """The mesh file an accepted run wrote, where no polyMesh exists.

    SU2 first, then the ``.msh``, because the SU2 artifact is the one the
    run was asked for; the ``.msh`` is there for every accepted run and is
    what the conversion formats are written from. Either is a mesh the
    user can be handed a file of.

    A run whose quality verdict was refused wrote no accepted artifact and
    is passed over here, because ``accepted_artifact`` reads the run
    manifest rather than the directory.

    Wrapped, because a classification that cannot answer must not stop a
    case from opening -- the caller reads a value, not an exception.
    """
    try:
        from foammesh.core.import_export import ImportExportService
    except Exception:                                         # noqa: BLE001
        return None
    for finder in (ImportExportService.native_su2_artifact,
                   ImportExportService.native_msh_artifact):
        try:
            row = finder(case_path)
        except Exception:                                     # noqa: BLE001
            continue
        path = str((row or {}).get('path') or '')
        if path:
            return path
    return None


def has_engine_mesh(case_path, engine: str = '') -> bool:
    """Whether this case holds a mesh the configured engine can have made.

    A polyMesh counts for everyone: it is what the OpenFOAM route
    publishes and what snappyHexMesh writes in place.

    A native artifact counts only for an engine that writes one. Gmsh
    does, and on the SU2 route it is the whole mesh. snappyHexMesh does
    not -- there is no run on which it leaves a ``.msh`` and no polyMesh
    -- so accepting one for snappy would be answering a question about
    one engine with evidence from another. An engine that has not been
    chosen yet is asked both ways, because the case may have been meshed
    before the setting was read.
    """
    try:
        from foammesh.core.case import classify_case
        if classify_case(case_path).has_mesh:
            return True
    except Exception:                                         # noqa: BLE001
        pass
    if str(engine or '').strip().lower() == MeshEngine.SNAPPY.value:
        return False
    return bool(native_mesh_artifact(case_path))
