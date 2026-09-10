"""Shell-independent facade for inspection, reconciliation, and safe copies."""
from __future__ import annotations

from pathlib import Path

from .copying import copy_case_directory
from .model import (
    CaseKind, CaseMetadata, MeshOrigin, classify_case, fingerprint_poly_mesh,
    load_case_metadata, resolve_workflow, save_case_metadata,
)


class CaseService:
    def inspect(self, path: str | Path):
        return classify_case(path)

    def reconcile(self, path: str | Path):
        classification = classify_case(path)
        if classification.kind is CaseKind.INVALID:
            raise ValueError('; '.join(classification.reasons))
        fingerprint = (fingerprint_poly_mesh(classification.poly_mesh_path)
                       if classification.poly_mesh_path else None)
        metadata = (load_case_metadata(path)
                    if classification.kind is CaseKind.FOAMMESH_CASE else None)
        return resolve_workflow(metadata, fingerprint)

    def adopt_raw_mesh(self, path: str | Path):
        classification = classify_case(path)
        if classification.kind is not CaseKind.RAW_POLY_MESH_CASE:
            raise ValueError('only a complete raw polyMesh case can be adopted')
        metadata = CaseMetadata.external_mesh(
            fingerprint_poly_mesh(classification.poly_mesh_path),
            origin=MeshOrigin.OPENED_NATIVE,
            provenance={'adopted_from': str(Path(path).resolve())})
        save_case_metadata(path, metadata)
        return metadata

    def copy_project(self, source: str | Path, destination: str | Path):
        return copy_case_directory(source, destination)
