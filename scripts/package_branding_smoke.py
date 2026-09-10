#!/usr/bin/env python
"""Verify branding and legal payloads in a built FoamMesh one-folder bundle."""
from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_PAYLOADS = (
    'LICENSE',
    'NOTICE',
    'THIRD_PARTY.md',
    'docs/user_manual/getting_started.md',
    'resources/branding/foammesh.ico',
    'resources/branding/foammesh.icns',
    'resources/branding/foammesh_icon_256.png',
    'resources/branding/foammesh_watermark_light.png',
    'resources/branding/foammesh_watermark_dark.png',
    'resources/branding/manifest.json',
    'resources/branding/build_info.json',
)


def _windows_version_info(executable: Path) -> dict[str, str]:
    command = (
        "$item=(Get-Item -LiteralPath '" + str(executable).replace("'", "''") + "').VersionInfo; "
        "$item | Select-Object CompanyName,ProductName,FileDescription,FileVersion,"
        "ProductVersion,OriginalFilename | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ['powershell.exe', '-NoProfile', '-Command', command],
        check=True, capture_output=True, text=True, encoding='utf-8')
    return json.loads(completed.stdout)


def _windows_shell_icon(executable: Path) -> dict[str, object]:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QFileInfo
    from PySide6.QtWidgets import QApplication, QFileIconProvider

    application = QApplication.instance() or QApplication(sys.argv[:1])
    icon = QFileIconProvider().icon(QFileInfo(str(executable)))
    pixmaps = {}
    for size in (16, 32, 48, 64, 128, 256):
        pixmap = icon.pixmap(size, size)
        pixmaps[str(size)] = {
            'is_null': pixmap.isNull(),
            'width': pixmap.width(),
            'height': pixmap.height(),
        }
    return {
        'qt_platform': application.platformName(),
        'is_null': icon.isNull(),
        'requested_pixmaps': pixmaps,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle', nargs='?', default=ROOT / 'dist' / 'FoamMesh', type=Path)
    parser.add_argument('--report', type=Path,
                        default=ROOT / 'plans' / 'audits' / 'u6_windows_package_smoke.json')
    arguments = parser.parse_args()

    bundle = arguments.bundle.resolve()
    payload_root = bundle / '_internal'
    executable = bundle / ('FoamMesh.exe' if sys.platform == 'win32' else 'FoamMesh')
    payloads = {name: (payload_root / Path(name)).is_file() for name in REQUIRED_PAYLOADS}
    report: dict[str, object] = {
        'schema_version': 1,
        'host': {'system': platform.system(), 'release': platform.release()},
        'bundle': str(bundle),
        'executable': {
            'path': str(executable),
            'exists': executable.is_file(),
            'size_bytes': executable.stat().st_size if executable.is_file() else 0,
        },
        'required_payloads': payloads,
    }

    if sys.platform == 'win32' and executable.is_file():
        report['windows_version_info'] = _windows_version_info(executable)
        report['windows_shell_icon'] = _windows_shell_icon(executable)

    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')

    failures = [name for name, present in payloads.items() if not present]
    if not executable.is_file():
        failures.append('FoamMesh executable')
    if sys.platform == 'win32' and executable.is_file():
        version = report['windows_version_info']
        if version.get('CompanyName') != 'Erevnaa' or version.get('ProductName') != 'FoamMesh':
            failures.append('Windows version identity')
        shell_icon = report['windows_shell_icon']
        if shell_icon['is_null'] or any(
                item['is_null'] for item in shell_icon['requested_pixmaps'].values()):
            failures.append('Windows embedded shell icon')
    if failures:
        raise RuntimeError('Package branding smoke failed: ' + ', '.join(failures))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
