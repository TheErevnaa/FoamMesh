"""A read lease on ``constant/polyMesh`` (Plan 37 UF10a).

A worker reads a mesh file by file. If a run rewrites the mesh while that
happens, the arrays can mix two meshes: points from the old one, faces from
the new. Nothing in the arrays says so. The lease records every member's
size, modification time and file id before the read and checks them again
after it; any change is a typed ``stale_input`` refusal, never a result.

Two strengths:

* :meth:`ReadLease.take` + :meth:`ReadLease.verify` -- stat only, no extra
  I/O. It sees every overwrite that changes a size, an mtime or a file id
  (``os.replace`` of a new file). It cannot see a rewrite of identical size
  within the file system's timestamp resolution; nothing short of hashing can.
* :meth:`ReadLease.snapshot` -- copies the members into the worker's scratch
  directory, hashing as it copies, then checks the source stamps again. The
  read then runs on the copy, which nothing else writes, and the digest is
  the mesh's content identity. The user's mesh is only ever read.

The content digest is computed the way
:func:`foammesh.core.case.model.fingerprint_poly_mesh` computes a case's
``mesh_fingerprint``, over the same members, so a caller can compare what it
read with what the case metadata says it has.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil

from foammesh.core.mesh.poly_mesh_boundary import PolyMeshReadError, _member

#: The members :func:`fingerprint_poly_mesh` hashes, in its order.
FINGERPRINT_MEMBERS = ('boundary', 'faces', 'neighbour', 'owner', 'points')
_CHUNK = 1024 * 1024


class StaleInputError(PolyMeshReadError):
    """The mesh changed while it was being read: reason ``stale_input``."""

    def __init__(self, message: str, *, path: Path | None = None,
                 changed: tuple = ()):
        super().__init__('stale_input', message, path=path)
        self.changed = tuple(changed)


@dataclass(frozen=True)
class MemberStamp:
    """One member file as it stood when the lease was taken."""

    name: str          # the logical member, e.g. ``owner``
    file: str          # the file on disk, ``owner`` or ``owner.gz``
    size: int
    mtime_ns: int
    file_id: int

    def to_dict(self) -> dict:
        return {'name': self.name, 'file': self.file, 'size': self.size,
                'mtime_ns': self.mtime_ns, 'file_id': self.file_id}


def _stamp(mesh: Path, name: str) -> MemberStamp | None:
    path = _member(mesh, name)
    if path is None:
        return None
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return MemberStamp(name=name, file=path.name, size=stat.st_size,
                       mtime_ns=stat.st_mtime_ns, file_id=stat.st_ino)


@dataclass(frozen=True)
class ReadLease:
    """What a read is entitled to assume about the mesh directory."""

    mesh_dir: Path
    stamps: tuple[MemberStamp, ...]
    #: Names asked for that were absent when the lease was taken.
    absent: tuple[str, ...] = ()

    @classmethod
    def take(cls, mesh_dir: Path, names) -> 'ReadLease':
        mesh_dir = Path(mesh_dir)
        stamps, absent = [], []
        for name in names:
            stamp = _stamp(mesh_dir, name)
            if stamp is None:
                absent.append(name)
            else:
                stamps.append(stamp)
        return cls(mesh_dir=mesh_dir, stamps=tuple(stamps),
                   absent=tuple(absent))

    @property
    def revision(self) -> str:
        """A digest of every stamp: equal revisions, same files on disk."""
        digest = hashlib.sha256()
        for stamp in self.stamps:
            digest.update(f'{stamp.file}\0{stamp.size}\0{stamp.mtime_ns}\0'
                          f'{stamp.file_id}\0'.encode())
        for name in self.absent:
            digest.update(f'-{name}\0'.encode())
        return digest.hexdigest()

    @property
    def input_bytes(self) -> int:
        return sum(stamp.size for stamp in self.stamps)

    def changes(self) -> tuple[str, ...]:
        """Every member whose file differs now from the lease, as prose."""
        changed = []
        for stamp in self.stamps:
            now = _stamp(self.mesh_dir, stamp.name)
            if now is None:
                changed.append(f'{stamp.file} was removed')
            elif now.file != stamp.file:
                changed.append(f'{stamp.file} was replaced by {now.file}')
            elif now.size != stamp.size:
                changed.append(f'{stamp.file} changed size '
                               f'{stamp.size} -> {now.size} bytes')
            elif now.mtime_ns != stamp.mtime_ns:
                changed.append(f'{stamp.file} was rewritten (mtime moved)')
            elif now.file_id != stamp.file_id:
                changed.append(f'{stamp.file} was replaced (new file id)')
        for name in self.absent:
            if _member(self.mesh_dir, name) is not None:
                changed.append(f'{name} appeared')
        return tuple(changed)

    def verify(self) -> None:
        """Raise :class:`StaleInputError` if any member changed."""
        changed = self.changes()
        if changed:
            raise StaleInputError(
                f'{self.mesh_dir} changed while it was being read: '
                + '; '.join(changed) + '. Read it again once the writer '
                'has finished.', path=self.mesh_dir, changed=changed)

    def snapshot(self, scratch: Path) -> tuple[Path, str]:
        """Copy the leased members to ``scratch/constant/polyMesh``.

        Returns ``(the copy's polyMesh dir, content digest)``. The copy is
        verified against the lease before and after, so it is one mesh; the
        source is opened read-only.
        """
        self.verify()
        target = Path(scratch) / 'constant' / 'polyMesh'
        target.mkdir(parents=True, exist_ok=True)
        content = hashlib.sha256()
        order = {name: index for index, name in enumerate(FINGERPRINT_MEMBERS)}
        for stamp in sorted(self.stamps,
                            key=lambda item: order.get(item.name, len(order))):
            source = self.mesh_dir / stamp.file
            counted = stamp.name in order
            if counted:
                content.update(stamp.file.encode('utf-8') + b'\0'
                               + str(stamp.size).encode('ascii') + b'\0')
            copied = 0
            try:
                with open(source, 'rb') as reader, \
                        open(target / stamp.file, 'wb') as writer:
                    for chunk in iter(lambda: reader.read(_CHUNK), b''):
                        if counted:
                            content.update(chunk)
                        writer.write(chunk)
                        copied += len(chunk)
            except FileNotFoundError as error:
                raise StaleInputError(
                    f'{source} vanished while it was copied',
                    path=self.mesh_dir, changed=(stamp.file,)) from error
            if copied != stamp.size:
                raise StaleInputError(
                    f'{source} held {copied} bytes when copied, not the '
                    f'{stamp.size} it held when leased',
                    path=self.mesh_dir, changed=(stamp.file,))
        self.verify()
        return target, content.hexdigest()

    def to_dict(self) -> dict:
        return {'mesh_dir': str(self.mesh_dir), 'revision': self.revision,
                'members': [stamp.to_dict() for stamp in self.stamps],
                'absent': list(self.absent)}


def content_digest_of(mesh_dir: Path) -> str:
    """The ``fingerprint_poly_mesh`` digest of ``mesh_dir``'s members."""
    digest = hashlib.sha256()
    for name in FINGERPRINT_MEMBERS:
        path = _member(Path(mesh_dir), name)
        if path is None:
            continue
        digest.update(path.name.encode('utf-8'))
        digest.update(b'\0')
        digest.update(str(path.stat().st_size).encode('ascii'))
        digest.update(b'\0')
        with open(path, 'rb') as source:
            for chunk in iter(lambda: source.read(_CHUNK), b''):
                digest.update(chunk)
    return digest.hexdigest()


def discard_snapshot(scratch: Path) -> None:
    """Remove a snapshot this module made; never touches anything else."""
    target = Path(scratch) / 'constant' / 'polyMesh'
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    for parent in (target.parent, Path(scratch)):
        try:
            os.rmdir(parent)
        except OSError:
            break
