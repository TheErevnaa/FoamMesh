#!/usr/bin/env python
"""Capture release build identity for the packaged About diagnostics."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'src' / 'resources' / 'branding' / 'build_info.json'


def _commit() -> str:
    configured = os.environ.get('FOAMMESH_GIT_COMMIT') or os.environ.get('GIT_COMMIT')
    if configured:
        return configured
    try:
        return subprocess.run(
            ('git', 'rev-parse', '--short=12', 'HEAD'), cwd=ROOT,
            capture_output=True, text=True, check=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


def main() -> int:
    document = {
        'schema_version': 1,
        'build_id': os.environ.get('FOAMMESH_BUILD_ID', 'development'),
        'commit': _commit(),
        'generated_at': datetime.now(timezone.utc).isoformat(),
    }
    OUTPUT.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
