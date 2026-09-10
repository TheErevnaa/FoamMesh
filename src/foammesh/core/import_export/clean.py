"""Preview-first cleanup for disposable OpenFOAM case artifacts."""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CleanPreview:
    case_path: Path
    removable: tuple[Path, ...]
    decomposed_only: bool = False


class CaseCleanService:
    """Remove only generated processor/time artifacts; never touch sidecar or polyMesh."""

    def preview(self, case_path: str | Path) -> CleanPreview:
        case = Path(case_path)
        removable = []
        for item in case.iterdir():
            if item.name.startswith('processor') and item.is_dir():
                removable.append(item)
            elif item.name.replace('.', '', 1).isdigit() and item.name != '0' and item.is_dir():
                removable.append(item)
        has_processors = any(
            path.name.startswith('processor') for path in removable)
        has_root_mesh = (case / 'constant' / 'polyMesh').is_dir()
        return CleanPreview(
            case, tuple(sorted(removable)),
            decomposed_only=has_processors and not has_root_mesh)

    def clean(self, preview: CleanPreview, *, confirm_token: str | None = None) -> int:
        if preview.decomposed_only and confirm_token != preview.case_path.name:
            raise ValueError(
                'decomposed-only mesh cleanup requires the case name as confirm_token')
        for path in preview.removable:
            shutil.rmtree(path)
        return len(preview.removable)
