#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import QObject
from pathlib import Path
import shutil

from foammesh.db.project import Project
from foammesh.db.configurations import FILE_NAME as CONFIGURATIONS_FILE
from foammesh.core.case import (
    CaseKind,
    CaseMetadata,
    MeshOrigin,
    classify_case,
    fingerprint_poly_mesh,
    load_case_metadata,
    save_case_metadata,
)


class ProjectManager(QObject):
    def __init__(self, event_bus=None):
        super().__init__()
        self._eventBus = event_bus

    def createProject(self, path):
        path.mkdir()

        project = Project(path, event_bus=self._eventBus)
        project.new()

        return project

    def openProject(self, path):
        project = Project(path, event_bus=self._eventBus)
        project.open()

        return project

    def classifyCase(self, path):
        """Inspect a candidate case without creating files or acquiring locks."""
        return classify_case(path)

    def createInPlaceCase(self, path):
        """Create a FoamMesh sidecar in a new or empty native case directory.

        The caller is responsible for adding the minimum OpenFOAM skeleton.  It
        is intentionally separate from the authored ``createProject`` path.
        """
        path = Path(path)
        if path.exists():
            classification = classify_case(path)
            if classification.kind is not CaseKind.EMPTY:
                raise FileExistsError(f'new case directory is not empty: {path}')
        else:
            path.mkdir(parents=True)
        return self._initializeInPlace(path, CaseKind.EMPTY)

    def openInPlaceCase(self, path):
        """Open/adopt a case whose native OpenFOAM root is *path*.

        Raw meshes are adopted without copying or changing ``constant/polyMesh``.
        A sidecar is created only after classification succeeds.  Existing
        FoamMesh sidecars are opened in place.
        """
        path = Path(path)
        classification = classify_case(path)
        if classification.kind is CaseKind.FOAMMESH_CASE:
            if not (path / 'foammesh' / CONFIGURATIONS_FILE).is_file():
                raise ValueError('cannot open case in place: FoamMesh sidecar configuration is missing')
            project = Project(path, storage_path=path / 'foammesh', event_bus=self._eventBus)
            try:
                project.open()
                load_case_metadata(path)
                project.acceptExternalChanges()
            except Exception:
                project.close()
                raise
            return project
        if classification.kind not in {
                CaseKind.EMPTY, CaseKind.OPENFOAM_CASE, CaseKind.RAW_POLY_MESH_CASE}:
            details = '; '.join(classification.reasons) or classification.kind.value
            raise ValueError(f'cannot open case in place: {details}')
        return self._initializeInPlace(path, classification.kind, classification.poly_mesh_path)

    def _initializeInPlace(self, path, kind, poly_mesh_path=None):
        sidecar = path / 'foammesh'
        sidecar.mkdir(exist_ok=False)
        project = None
        try:
            project = Project(path, storage_path=sidecar, event_bus=self._eventBus)
            project.new()
            if kind is CaseKind.RAW_POLY_MESH_CASE:
                save_case_metadata(
                    path,
                    CaseMetadata.external_mesh(
                        fingerprint_poly_mesh(poly_mesh_path),
                        origin=MeshOrigin.OPENED_NATIVE,
                        provenance={'adopted_from': str(path.resolve())}))
            else:
                save_case_metadata(path, CaseMetadata())
            project.acceptExternalChanges()
            return project
        except Exception:
            # This sidecar was created by this call and contains only
            # FoamMesh-owned files.  Remove it on failure while leaving the
            # native case, including constant/polyMesh, untouched.
            if project is not None:
                project.close()
            shutil.rmtree(sidecar, ignore_errors=True)
            raise
