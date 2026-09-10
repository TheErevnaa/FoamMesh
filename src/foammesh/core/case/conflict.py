"""External-change snapshots for safe case mutations and saves."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .model import fingerprint_poly_mesh


class CaseConflictError(RuntimeError):
    def __init__(self, changed: tuple[str, ...]):
        self.changed = changed
        super().__init__('case changed outside FoamMesh: ' + ', '.join(changed))


@dataclass(frozen=True)
class CaseExternalSnapshot:
    files: tuple[tuple[str, str], ...]
    mesh_digest: str | None
    surface_digest: str | None = None

    @classmethod
    def capture(cls, case_path: str | Path, storage_path: str | Path) -> 'CaseExternalSnapshot':
        case = Path(case_path)
        storage = Path(storage_path)
        selected = (
            storage / 'configurations.h5', storage / 'local.cfg',
            storage / 'foammesh_history.json', storage / 'foammesh_proposals.json',
            storage / 'artifact_history.json',
            case / 'foammesh' / 'project.json',
        )
        files = []
        for path in selected:
            relative = str(path.relative_to(case)) if path.is_relative_to(case) else str(path)
            if path.is_file():
                files.append((relative, _sha256(path)))
            else:
                files.append((relative, '<missing>'))
        mesh = case / 'constant' / 'polyMesh'
        try:
            mesh_digest = fingerprint_poly_mesh(mesh).digest
        except (OSError, ValueError):
            mesh_digest = None
        surface_digest = _directory_digest(
            case / 'constant' / 'triSurface',
            suffixes={'.stl', '.obj', '.vtk', '.vtp', '.emesh'})
        return cls(tuple(files), mesh_digest, surface_digest)

    def differences(self, current: 'CaseExternalSnapshot') -> tuple[str, ...]:
        changed = []
        old_files = dict(self.files)
        new_files = dict(current.files)
        for name in sorted(set(old_files) | set(new_files)):
            if old_files.get(name) != new_files.get(name):
                changed.append(name)
        if self.mesh_digest != current.mesh_digest:
            changed.append('constant/polyMesh')
        if self.surface_digest != current.surface_digest:
            changed.append('constant/triSurface')
        return tuple(changed)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_digest(path: Path, *, suffixes: set[str]) -> str | None:
    if not path.is_dir():
        return None
    digest = hashlib.sha256()
    found = False
    for item in sorted(candidate for candidate in path.rglob('*')
                       if candidate.is_file()
                       and candidate.suffix.lower() in suffixes):
        found = True
        digest.update(item.relative_to(path).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(_sha256(item).encode('ascii'))
        digest.update(b'\0')
    return digest.hexdigest() if found else None
