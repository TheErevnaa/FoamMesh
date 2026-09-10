"""Deterministic case archives with a checksum manifest."""
from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArchiveResult:
    archive: Path
    files: int
    manifest: dict[str, str]


class CaseArchiveService:
    def create(self, case_path: str | Path, destination: str | Path) -> ArchiveResult:
        case = Path(case_path).resolve()
        archive = Path(destination).resolve()
        if not case.is_dir():
            raise FileNotFoundError(f'case does not exist: {case}')
        if archive.exists():
            raise FileExistsError(f'archive already exists: {archive}')
        archive.parent.mkdir(parents=True, exist_ok=True)
        files = sorted(path for path in case.rglob('*') if path.is_file() and not self._excluded(case, path))
        manifest = {str(path.relative_to(case)).replace('\\', '/'): self._sha256(path) for path in files}
        temporary = archive.with_suffix(f'{archive.suffix}.tmp')
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as output:
                for path in files:
                    output.write(path, str(path.relative_to(case)).replace('\\', '/'))
                output.writestr('foammesh-archive-manifest.json', json.dumps(manifest, indent=2, sort_keys=True) + '\n')
            temporary.replace(archive)
        finally:
            if temporary.exists():
                temporary.unlink()
        return ArchiveResult(archive, len(files), manifest)

    # Process-local runtime sidecar written by an open facade session. It is
    # not case content and may be exclusively locked, so it is never archived.
    _RUNTIME_SIDECAR = {
        'facade.session.lock', 'facade.session.lock.json', 'facade.session.json',
        'event_journal.sqlite3', 'event_journal.sqlite3-wal', 'event_journal.sqlite3-shm',
    }

    @classmethod
    def _excluded(cls, case: Path, path: Path) -> bool:
        relative = path.relative_to(case).parts
        if relative[:2] in {('foammesh', 'cache'), ('foammesh', 'recovery')}:
            return True
        return len(relative) == 2 and relative[0] == 'foammesh' and relative[1] in cls._RUNTIME_SIDECAR

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def verify(self, archive_path: str | Path) -> bool:
        """Verify every archived file against its bundled deterministic manifest."""
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read('foammesh-archive-manifest.json'))
                for name, expected in manifest.items():
                    if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                        return False
            return True
        except (KeyError, OSError, ValueError, zipfile.BadZipFile):
            return False
