#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Per-stage mesh checkpoints.

Snapshot/restore ``constant/polyMesh`` so a workflow stage can be rolled back
without redoing everything upstream. Pure filesystem operations (headless).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Checkpoint:
    stage: str
    path: Path

    @property
    def exists(self) -> bool:
        return self.path.is_dir()


class CheckpointStore:
    """Stores polyMesh snapshots under ``<project>/.foammesh/checkpoints/<stage>``."""

    def __init__(self, project_dir, poly_mesh_subpath: str = 'constant/polyMesh'):
        self.project_dir = Path(project_dir)
        self.poly_mesh_subpath = poly_mesh_subpath
        self.root = self.project_dir / '.foammesh' / 'checkpoints'

    def _dest(self, stage: str) -> Path:
        return self.root / stage / 'polyMesh'

    def _source(self) -> Path:
        return self.project_dir / self.poly_mesh_subpath

    def save(self, stage: str) -> Checkpoint:
        src = self._source()
        if not src.is_dir():
            raise FileNotFoundError(f'no polyMesh to checkpoint at {src}')
        dest = self._dest(stage)
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        return Checkpoint(stage, dest)

    def restore(self, stage: str) -> bool:
        dest = self._dest(stage)
        if not dest.is_dir():
            return False
        target = self._source()
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(dest, target)
        return True

    def has(self, stage: str) -> bool:
        return self._dest(stage).is_dir()

    def list(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir())

    def prune(self, keep: list[str]) -> int:
        removed = 0
        for name in self.list():
            if name not in keep:
                shutil.rmtree(self.root / name)
                removed += 1
        return removed
