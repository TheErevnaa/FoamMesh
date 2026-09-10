"""Machine-readable PC7/PC8 evidence with fail-closed completion rules."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class MatrixCase:
    case_id: str
    phase: str
    description: str
    capabilities: tuple[str, ...] = ()
    required: bool = True


MATRIX_CASES = (
    MatrixCase('live.blockmesh', 'live', 'blockMesh authored vertical slice', ('blockMesh',)),
    MatrixCase('live.snappy.stages', 'live', 'every authored snappyHexMesh stage', ('snappyHexMesh',)),
    MatrixCase('live.checkmesh.matrix', 'live', 'checkMesh pass/warning/failure/truncation/writeSets', ('checkMesh',)),
    MatrixCase('live.transform.matrix', 'live', 'scale/translate/rotate/pivot and rollback', ('transformPoints',)),
    MatrixCase('live.converter.matrix', 'live', 'installed converter success/malformed/cancel/restore'),
    MatrixCase('live.export.matrix', 'live', 'native/VTK/Gmsh/CGNS/Fluent/format round trips'),
    MatrixCase('live.repair.extrusion', 'live', 'repair and extrusion success/failure/cancel/restore'),
    MatrixCase('live.process_tree_cancel', 'live', 'native process-tree cancellation without retry'),
    MatrixCase('gui.startup', 'gui', 'clean regeneration and measured main-window-first startup'),
    MatrixCase('gui.lifecycle', 'gui', 'create/open/recent/save/copy/close raw and authored cases'),
    MatrixCase('gui.mesh_journey', 'gui', 'geometry through mesh info/QA/repair/restore/export'),
    MatrixCase('gui.desktop_api_sync', 'gui', 'desktop API synchronization and dirty conflict'),
    MatrixCase('gui.job_close_recovery', 'gui', 'active-job close/cancel/recovery'),
    MatrixCase('visual.theme_dpi', 'visual', 'System/Light/Dark at 100/150/200 percent'),
    MatrixCase('a11y.keyboard_focus', 'visual', 'keyboard, focus, names, shortcuts, screen reader'),
    MatrixCase('legal.branding', 'package', 'legal payload and no unintended legacy branding'),
    MatrixCase('package.desktop', 'package', 'packaged desktop startup and icon/watermark'),
    MatrixCase('package.headless', 'package', 'packaged CLI/headless/API smoke'),
    MatrixCase(
        'package.schema_fresh', 'package',
        'fresh-project schema creation and unsupported-version rejection'),
    MatrixCase('release.clean_checkout', 'release', 'candidate rebuilt and tested from clean checkout'),
    MatrixCase('release.reproducible', 'release', 'regenerated inputs and repeatable package hashes'),
)


def _version(package: str):
    try:
        import importlib.metadata as metadata
        return metadata.version(package)
    except Exception:
        return None


def _git(*args: str) -> str | None:
    try:
        return subprocess.run(
            ['git', *args], check=True, capture_output=True, text=True,
            encoding='utf-8').stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def collect_environment(root: Path) -> dict:
    usage = shutil.disk_usage(root)
    return {
        'captured_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'os': platform.platform(), 'system': platform.system(),
        'release': platform.release(), 'machine': platform.machine(),
        'processor': platform.processor(), 'logical_cpu_count': os.cpu_count(),
        'python': sys.version, 'python_executable': sys.executable,
        'qt': _version('PySide6'), 'vtk': _version('vtk'),
        'qasync': _version('qasync'), 'fastapi': _version('fastapi'),
        'display': os.environ.get('DISPLAY'),
        'qt_platform': os.environ.get('QT_QPA_PLATFORM'),
        'filesystem_root': str(root.resolve()), 'filesystem_drive': root.resolve().drive,
        'disk_total_bytes': usage.total, 'disk_free_bytes': usage.free,
        'git_commit': _git('rev-parse', 'HEAD'),
        'git_status_porcelain': _git('status', '--porcelain'),
    }


@dataclass
class ReleaseEvidence:
    root: Path
    results: dict[str, dict] = field(default_factory=dict)
    defects: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)

    def record(self, case_id: str, status: str, *, detail: str = '', evidence=None) -> None:
        if case_id not in {case.case_id for case in MATRIX_CASES}:
            raise KeyError(f'unknown release matrix case: {case_id}')
        if status not in {'pass', 'fail', 'unavailable', 'not_run', 'scope_decision'}:
            raise ValueError(f'invalid release evidence status: {status}')
        self.results[case_id] = {'status': status, 'detail': detail, 'evidence': evidence or {}}

    def add_artifact(self, path: Path, kind: str) -> None:
        path = Path(path)
        digest = None
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.artifacts.append({'kind': kind, 'path': str(path), 'sha256': digest})

    def document(self) -> dict:
        cases = []
        for spec in MATRIX_CASES:
            result = self.results.get(spec.case_id, {
                'status': 'not_run', 'detail': 'matrix case was not executed', 'evidence': {}})
            cases.append({**asdict(spec), **result})
        blocking = [case['case_id'] for case in cases
                    if case['required'] and case['status'] not in {'pass', 'scope_decision'}]
        high_defects = [item for item in self.defects
                        if item.get('severity') in {'critical', 'high'}
                        and item.get('disposition') != 'fixed']
        return {
            'schema_version': 1, 'environment': collect_environment(self.root),
            'cases': cases, 'artifacts': self.artifacts, 'defects': self.defects,
            'summary': {'pass': sum(c['status'] == 'pass' for c in cases),
                        'fail': sum(c['status'] == 'fail' for c in cases),
                        'unavailable': sum(c['status'] == 'unavailable' for c in cases),
                        'not_run': sum(c['status'] == 'not_run' for c in cases),
                        'blocking': blocking, 'high_defects': high_defects,
                        'release_ready': not blocking and not high_defects},
        }

    def write(self, destination: Path) -> dict:
        document = self.document()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        return document
