"""Immutable, fingerprinted snapshots of the snap-stage boundary.

Plan 23 §8.2. GF1 judges the mesh as it stood *after snapping and before layer
addition*, but the layers stage overwrites ``constant/polyMesh`` in place. Two
things follow.

**A snapshot has to exist at all.** ``CheckpointStore`` would serve, but it has
no production caller, so today there is nothing for GF1 to read.

**It has to be immutable.** ``CheckpointStore.save`` removes the destination
before copying, so a second snap -- or a crash midway through one -- destroys
the evidence a blocked run and its GF1 report both refer to. A checkpoint that
can be silently replaced cannot support a durable ``blocked`` state that
survives an application restart.

So each snapshot is published under its own content fingerprint and never
rewritten, with an atomically replaced ``current.json`` naming the newest:

    .foammesh/checkpoints/snap/<fingerprint>/polyMesh/
    .foammesh/checkpoints/snap/<fingerprint>/checkpoint.json
    .foammesh/checkpoints/snap/current.json

A later snap adds a sibling. The blocked-run manifest and the GF1 report keep
pointing at the exact fingerprint they were computed from, so what they mean
cannot change underneath them.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil

CHECKPOINT_SCHEMA_VERSION = 1

#: Files that define the boundary GF1 measures. ``neighbour`` is included so a
#: snapshot is a complete mesh rather than a surface, and the fingerprint is
#: taken over exactly these.
FINGERPRINTED = ('points', 'faces', 'owner', 'neighbour', 'boundary')


class SnapCheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class SnapCheckpoint:
    """One published snapshot."""

    root: Path
    fingerprint: str
    run_id: str
    stage: str = 'snap'

    @property
    def poly_mesh(self) -> Path:
        return self.root / 'polyMesh'

    @property
    def manifest_path(self) -> Path:
        return self.root / 'checkpoint.json'

    @property
    def exists(self) -> bool:
        return self.poly_mesh.is_dir() and self.manifest_path.is_file()

    def to_dict(self) -> dict:
        return {
            'schema_version': CHECKPOINT_SCHEMA_VERSION,
            'stage': self.stage, 'fingerprint': self.fingerprint,
            'run_id': self.run_id, 'poly_mesh': 'polyMesh',
        }


def poly_mesh_fingerprint(poly_mesh: str | Path) -> str:
    """Content digest of the mesh files that define the boundary.

    Deliberately not a whole-directory hash: OpenFOAM leaves ``sets/`` and
    other by-products next to the mesh, and a checkpoint should be identified
    by the mesh it holds rather than by incidental neighbours.
    """
    poly_mesh = Path(poly_mesh)
    digest = hashlib.sha256()
    for name in FINGERPRINTED:
        member = None
        for candidate in (poly_mesh / name, poly_mesh / f'{name}.gz'):
            if candidate.is_file():
                member = candidate
                break
        if member is None:
            raise SnapCheckpointError(
                f'{poly_mesh} is not a complete polyMesh: {name} is missing')
        digest.update(name.encode('ascii'))
        with member.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(block)
    return digest.hexdigest()


class SnapCheckpointStore:
    """Publish and resolve immutable snap snapshots for one case."""

    def __init__(self, case_path: str | Path, *, stage: str = 'snap'):
        self.case_path = Path(case_path).resolve()
        self.stage = stage
        self.root = self.case_path / '.foammesh' / 'checkpoints' / stage
        self.current_path = self.root / 'current.json'

    # -- publication ------------------------------------------------------- #

    def save(self, *, run_id: str = '',
             poly_mesh: str | Path | None = None) -> SnapCheckpoint:
        """Copy the live mesh into an immutable fingerprinted checkpoint.

        Re-saving identical content is a no-op that re-publishes the pointer,
        so a retried stage is cheap and cannot corrupt an existing snapshot.
        """
        source = Path(poly_mesh) if poly_mesh else (
            self.case_path / 'constant' / 'polyMesh')
        if not source.is_dir():
            raise SnapCheckpointError(f'no polyMesh to checkpoint at {source}')
        fingerprint = poly_mesh_fingerprint(source)
        destination = self.root / fingerprint
        checkpoint = SnapCheckpoint(destination, fingerprint, str(run_id),
                                    self.stage)

        if destination.exists():
            # Immutable by construction: identical content, so there is nothing
            # to rewrite. Only the pointer moves.
            if not checkpoint.exists:
                raise SnapCheckpointError(
                    f'existing checkpoint {fingerprint} is incomplete; remove '
                    f'{destination} before retrying')
            self._publish(checkpoint)
            return checkpoint

        self.root.mkdir(parents=True, exist_ok=True)
        staging = self.root / f'.{fingerprint}.tmp-{os.getpid()}'
        if staging.exists():
            shutil.rmtree(staging)
        try:
            staging.mkdir(parents=True)
            shutil.copytree(source, staging / 'polyMesh')
            copied = poly_mesh_fingerprint(staging / 'polyMesh')
            if copied != fingerprint:
                raise SnapCheckpointError(
                    'the mesh changed while it was being checkpointed')
            (staging / 'checkpoint.json').write_text(
                json.dumps(checkpoint.to_dict(), indent=2, sort_keys=True)
                + '\n', encoding='utf-8')
            os.replace(staging, destination)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise
        self._publish(checkpoint)
        return checkpoint

    def _publish(self, checkpoint: SnapCheckpoint) -> None:
        """Atomically name the newest checkpoint without touching the others."""
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.current_path.with_suffix('.json.tmp')
        temporary.write_text(
            json.dumps({
                'schema_version': CHECKPOINT_SCHEMA_VERSION,
                'stage': self.stage,
                'fingerprint': checkpoint.fingerprint,
                'run_id': checkpoint.run_id,
                'path': checkpoint.fingerprint,
            }, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(temporary, self.current_path)

    # -- resolution -------------------------------------------------------- #

    def current(self) -> SnapCheckpoint | None:
        """The newest published checkpoint, or ``None`` if there is none."""
        if not self.current_path.is_file():
            return None
        try:
            document = json.loads(self.current_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise SnapCheckpointError(
                f'snap checkpoint pointer is unreadable: {self.current_path}'
            ) from error
        if int(document.get('schema_version', 0)) != CHECKPOINT_SCHEMA_VERSION:
            raise SnapCheckpointError('unsupported snap checkpoint schema')
        fingerprint = str(document.get('fingerprint') or '')
        if not fingerprint:
            return None
        return self.resolve(fingerprint, run_id=str(document.get('run_id') or ''))

    def resolve(self, fingerprint: str, *, run_id: str = '') -> SnapCheckpoint:
        """Return a checkpoint by fingerprint, whatever has happened since.

        This is what makes a blocked run resumable: it names the exact snapshot
        it was blocked on, and a later snap cannot have changed it.
        """
        if not fingerprint or '/' in fingerprint or '\\' in fingerprint:
            raise SnapCheckpointError(
                f'invalid snap checkpoint fingerprint: {fingerprint!r}')
        checkpoint = SnapCheckpoint(
            self.root / fingerprint, fingerprint, run_id, self.stage)
        if not checkpoint.exists:
            raise SnapCheckpointError(
                f'snap checkpoint {fingerprint} is not available in '
                f'{self.root}')
        return checkpoint

    def verify(self, fingerprint: str) -> bool:
        """Whether a retained checkpoint still holds the content it claims."""
        try:
            checkpoint = self.resolve(fingerprint)
        except SnapCheckpointError:
            return False
        try:
            return poly_mesh_fingerprint(checkpoint.poly_mesh) == fingerprint
        except SnapCheckpointError:
            return False

    def list(self) -> tuple[str, ...]:
        if not self.root.is_dir():
            return ()
        return tuple(sorted(
            item.name for item in self.root.iterdir()
            if item.is_dir() and not item.name.startswith('.')))

    def prune(self, keep: tuple[str, ...]) -> int:
        """Remove checkpoints no retained run refers to.

        Pruning is separate from saving on purpose: a save must never be able
        to delete the evidence another run is blocked on.
        """
        retained = set(keep)
        current = None
        try:
            current = self.current()
        except SnapCheckpointError:
            pass
        if current is not None:
            retained.add(current.fingerprint)
        removed = 0
        for name in self.list():
            if name not in retained:
                shutil.rmtree(self.root / name, ignore_errors=True)
                removed += 1
        return removed
