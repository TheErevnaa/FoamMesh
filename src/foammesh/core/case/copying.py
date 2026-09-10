"""Verified copy primitives for native FoamMesh cases."""
from __future__ import annotations

import os
import shutil
import uuid
import hashlib
from dataclasses import dataclass
from pathlib import Path

#: Never copied, under any option. A lock names a process that does not hold
#: the copy, and a session file names a session that is not this one; carrying
#: either forward produces a case that lies about who has it open.
_LOCK_SIDECAR_ENTRIES = {
    'cache', 'case.lock', 'case.lock.info.json',
    'facade.session.lock', 'facade.session.lock.json', 'facade.session.json',
}

#: The event journal. Dropped from an ordinary "Save As", because that makes a
#: second case whose history is its own; carried when a case is being *moved*
#: rather than duplicated -- relocating a scratch case out of the temporary
#: directory is the same case arriving at its permanent address, and a case
#: that forgets everything it did on the way there has lost real provenance.
_HISTORY_SIDECAR_ENTRIES = {
    'event_journal.sqlite3', 'event_journal.sqlite3-wal',
    'event_journal.sqlite3-shm',
}

_TRANSIENT_SIDECAR_ENTRIES = _LOCK_SIDECAR_ENTRIES | _HISTORY_SIDECAR_ENTRIES


def _excluded(carry_history: bool) -> set:
    return (_LOCK_SIDECAR_ENTRIES if carry_history
            else _TRANSIENT_SIDECAR_ENTRIES)


@dataclass(frozen=True)
class CaseCopyResult:
    source: Path
    destination: Path
    excluded_cache: bool


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _validate_destination(source: Path, destination: Path):
    source_key = os.path.normcase(str(source)).casefold()
    destination_key = os.path.normcase(str(destination)).casefold()
    if source_key == destination_key or destination.is_relative_to(source):
        raise ValueError('destination must not be the source or a child of it')
    if destination.exists():
        raise FileExistsError(f'destination already exists: {destination}')


def _staging_path(destination: Path) -> Path:
    return destination.with_name(f'.{destination.name}.foammesh-staging-{uuid.uuid4().hex}')


def _commit_staging(staging: Path, destination: Path):
    try:
        os.replace(staging, destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _ensure_no_symlinks(source: Path):
    linked = next((item for item in source.rglob('*') if item.is_symlink()), None)
    if linked is not None:
        raise ValueError(f'case copy refuses symbolic links: {linked}')


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


#: Sidecars that record where the case's own files live. A case copy that
#: leaves these pointing at the old directory produces a case that opens,
#: looks complete, and fails on the first file it needs -- which is exactly
#: what relocating a scratch case did, because the temporary original is
#: deleted the moment the copy opens.
_REBASED_SUFFIXES = {'.json'}
_REBASE_SIZE_LIMIT = 8 * 1024 * 1024


def _path_spellings(path: Path, raw: str | Path) -> list:
    """Every way an absolute path to ``path`` can be spelled inside a file."""
    spellings = []
    for candidate in (str(path), str(Path(raw))):
        for text in (candidate, candidate.replace(chr(92), '/'),
                     candidate.replace(chr(92), chr(92) * 2)):
            if text and text not in spellings:
                spellings.append(text)
    return spellings


def _rebase_absolute_paths(source: Path, raw_source, staging: Path,
                           destination: Path, raw_destination):
    """Point the copy's sidecars at the copy instead of at the original.

    The geometry store, the prepared-geometry manifest and the readiness
    record all keep absolute paths to the STL revisions they describe. Copying
    them verbatim makes the new case reference files it does not own -- and
    when the source was a scratch directory that is about to be discarded, it
    references files that no longer exist at all.

    Rewriting is textual and prefix-anchored so that JSON escaping and forward
    or backward slashes are all handled without reformatting the documents.
    """
    replacements = list(zip(_path_spellings(source, raw_source),
                            _path_spellings(destination, raw_destination)))
    rewritten = []
    for item in staging.rglob('*'):
        if (not item.is_file() or item.suffix.lower() not in _REBASED_SUFFIXES
                or item.stat().st_size > _REBASE_SIZE_LIMIT):
            continue
        try:
            text = item.read_text(encoding='utf-8')
        except (UnicodeDecodeError, OSError):
            continue
        updated = text
        for before, after in replacements:
            if before != after:
                updated = updated.replace(before, after)
        if updated != text:
            item.write_text(updated, encoding='utf-8', newline='')
            rewritten.append(item.relative_to(staging))
    return rewritten


def _verify_copy(source: Path, staging: Path, excluded: set):
    source_files = {
        item.relative_to(source): item for item in source.rglob('*') if item.is_file()
        and not (
            (len(item.relative_to(source).parts) == 1 and
             item.relative_to(source).parts[0] in excluded)
            or (len(item.relative_to(source).parts) >= 2 and
                item.relative_to(source).parts[0] == 'foammesh' and
                item.relative_to(source).parts[1] in excluded))
    }
    copied_files = {item.relative_to(staging): item for item in staging.rglob('*') if item.is_file()}
    if set(source_files) != set(copied_files):
        raise OSError('case copy verification failed: file list differs')
    for relative, original in source_files.items():
        copied = copied_files[relative]
        if original.stat().st_size != copied.stat().st_size or _file_digest(original) != _file_digest(copied):
            raise OSError(f'case copy verification failed: {relative}')


def copy_case_directory(source: str | Path, destination: str | Path, *,
                        carry_history: bool = False) -> CaseCopyResult:
    """Create a verified portable copy without disposable cache or live locks.

    With ``carry_history`` the event journal travels too. Use it when the case
    is *moving* rather than being duplicated: a scratch case relocating out of
    the temporary directory is the same case reaching its permanent address,
    and it should arrive with the record of how it got there.

    The caller is responsible for checkpointing a live journal first --
    ``EventJournal.checkpoint()`` folds the write-ahead log back into the main
    database, without which the copy can land mid-transaction and read short.
    """
    source_path = _resolved(source)
    destination_path = _resolved(destination)
    if not source_path.is_dir():
        raise FileNotFoundError(f'case source does not exist: {source_path}')
    _validate_destination(source_path, destination_path)
    _ensure_no_symlinks(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    staging = _staging_path(destination_path)
    excluded = _excluded(carry_history)

    def ignore(directory, names):
        return (excluded.intersection(names)
                if Path(directory).resolve() == source_path or
                Path(directory).name == 'foammesh' else set())

    try:
        shutil.copytree(source_path, staging, ignore=ignore, copy_function=shutil.copy2)
        _verify_copy(source_path, staging, excluded)
        # After verification, never before: the check compares the copy with
        # the original byte for byte, and rebasing deliberately changes bytes.
        _rebase_absolute_paths(source_path, source, staging,
                               destination_path, destination)
        _commit_staging(staging, destination_path)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return CaseCopyResult(source_path, destination_path, excluded_cache=True)
