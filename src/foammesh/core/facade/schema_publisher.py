"""Filesystem publisher for generated facade discovery artifacts."""
import json
from pathlib import Path


def publish_documents(out_dir: str | Path, artifacts: dict[str, dict]) -> dict:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, payload in artifacts.items():
        path = directory / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        written[name] = str(path)
    return written
