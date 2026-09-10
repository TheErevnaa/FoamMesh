"""Published mesh-layout references; consumers never guess from directories."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


MESH_STATE_SCHEMA_VERSION = 1
_POLY_MESH_FILES = ('points', 'faces', 'owner', 'neighbour', 'boundary')


class MeshLayout(str, Enum):
    RECONSTRUCTED = 'reconstructed'
    DECOMPOSED_UNCOLLATED = 'decomposed_uncollated'
    DECOMPOSED_COLLATED = 'decomposed_collated'
    CANONICAL = 'canonical'


class MeshStateError(ValueError):
    pass


@dataclass(frozen=True)
class MeshArtifactRef:
    layout: MeshLayout
    engine_id: str
    run_id: str
    mesh_time: str
    relative_path: str
    fingerprint: str
    parts: int = 1
    runtime_fingerprint: str | None = None
    decomposition_fingerprint: str | None = None
    complete: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, 'layout', MeshLayout(self.layout))
        if self.parts < 1:
            raise MeshStateError('mesh parts must be positive')
        relative = PurePosixPath(str(self.relative_path).replace('\\', '/'))
        if relative.is_absolute() or '..' in relative.parts:
            raise MeshStateError('mesh relative_path must stay inside the case')
        object.__setattr__(self, 'relative_path', relative.as_posix())
        if len(self.fingerprint) != 64:
            raise MeshStateError('mesh fingerprint must be SHA-256')
        if self.layout in {
                MeshLayout.DECOMPOSED_UNCOLLATED,
                MeshLayout.DECOMPOSED_COLLATED} and self.parts < 2:
            raise MeshStateError('decomposed layout requires at least two parts')

    def to_dict(self) -> dict:
        return {
            'layout': self.layout.value,
            'engine_id': self.engine_id,
            'run_id': self.run_id,
            'mesh_time': self.mesh_time,
            'relative_path': self.relative_path,
            'fingerprint': self.fingerprint,
            'parts': self.parts,
            'runtime_fingerprint': self.runtime_fingerprint,
            'decomposition_fingerprint': self.decomposition_fingerprint,
            'complete': self.complete,
        }

    @classmethod
    def from_dict(cls, value: dict) -> 'MeshArtifactRef':
        return cls(
            MeshLayout(value['layout']), str(value['engine_id']),
            str(value['run_id']), str(value['mesh_time']),
            str(value['relative_path']), str(value['fingerprint']),
            int(value.get('parts', 1)), value.get('runtime_fingerprint'),
            value.get('decomposition_fingerprint'),
            bool(value.get('complete', False)))


class MeshStateStore:
    def __init__(self, case_path: str | Path):
        self.case_path = Path(case_path).resolve()
        self.path = self.case_path / 'foammesh' / 'mesh-state.json'

    def inspect(self, *, layout: MeshLayout, engine_id: str, run_id: str,
                mesh_time: str = 'constant', parts: int = 1,
                runtime_fingerprint: str | None = None,
                decomposition_fingerprint: str | None = None
                ) -> MeshArtifactRef:
        layout = MeshLayout(layout)
        if layout is MeshLayout.RECONSTRUCTED:
            relative = f'{mesh_time}/polyMesh'
            roots = [self.case_path / relative]
        elif layout is MeshLayout.DECOMPOSED_UNCOLLATED:
            if parts < 2:
                raise MeshStateError('decomposed mesh requires at least two ranks')
            relative = '.'
            roots = [
                self.case_path / f'processor{rank}' / mesh_time / 'polyMesh'
                for rank in range(parts)]
        elif layout is MeshLayout.DECOMPOSED_COLLATED:
            if parts < 2:
                raise MeshStateError('collated mesh requires at least two ranks')
            relative = 'processors'
            roots = [
                self.case_path / f'processors{parts}' / f'processor{rank}' /
                mesh_time / 'polyMesh'
                for rank in range(parts)]
        else:
            relative = f'foammesh/canonical/{run_id}'
            roots = [self.case_path / relative]
        fingerprint = _fingerprint_roots(self.case_path, roots, layout)
        return MeshArtifactRef(
            layout, engine_id, run_id, mesh_time, relative, fingerprint, parts,
            runtime_fingerprint, decomposition_fingerprint, True)

    def publish(self, reference: MeshArtifactRef) -> None:
        self.validate(reference)
        document = {
            'schema_version': MESH_STATE_SCHEMA_VERSION,
            'mesh': reference.to_dict(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, self.path)

    def load(self, *, validate: bool = True) -> MeshArtifactRef | None:
        if not self.path.is_file():
            return None
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise MeshStateError(f'invalid mesh-state file: {error}') from error
        if document.get('schema_version') != MESH_STATE_SCHEMA_VERSION:
            raise MeshStateError('unsupported mesh-state schema')
        reference = MeshArtifactRef.from_dict(document['mesh'])
        if validate:
            self.validate(reference)
        return reference

    def validate(self, reference: MeshArtifactRef) -> None:
        inspected = self.inspect(
            layout=reference.layout, engine_id=reference.engine_id,
            run_id=reference.run_id, mesh_time=reference.mesh_time,
            parts=reference.parts,
            runtime_fingerprint=reference.runtime_fingerprint,
            decomposition_fingerprint=reference.decomposition_fingerprint)
        if inspected.fingerprint != reference.fingerprint:
            raise MeshStateError('published mesh layout is missing, incomplete, or stale')


def _fingerprint_roots(case: Path, roots: list[Path], layout: MeshLayout) -> str:
    digest = hashlib.sha256()
    for root in roots:
        if layout is MeshLayout.CANONICAL:
            files = sorted(path for path in root.rglob('*') if path.is_file())
            if not files:
                raise MeshStateError(f'canonical artifact is empty: {root}')
        else:
            files = [root / name for name in _POLY_MESH_FILES]
            missing = [path.name for path in files if not path.is_file()]
            if missing:
                raise MeshStateError(
                    f'incomplete polyMesh {root}: missing {", ".join(missing)}')
        for path in files:
            relative = path.relative_to(case).as_posix()
            digest.update(relative.encode())
            digest.update(b'\0')
            digest.update(str(path.stat().st_size).encode())
            digest.update(b'\0')
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            digest.update(b'\0')
    return digest.hexdigest()
