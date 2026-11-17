"""Generate pip requirement files from pixi.lock, so they cannot drift from Pixi.

Usage (from the repository root, inside the Pixi env):
    pixi run gen-requirements                          # write the files
    python tools/deps/gen_requirements.py --check      # exit 1 if a file on disk differs

For each direct dependency in pixi.toml it takes the version that pixi.lock
resolved for environment "default" on win-64 and writes an exact `name==version`
pin under its PyPI name. Same scheme as FoamFlow's tools/deps/gen_requirements.py.

    requirements.txt        [dependencies] + [pypi-dependencies]: the app's
                            runtime set (pyproject.toml reads it as the
                            package's dependencies)
    requirements-test.txt   -r requirements.txt, plus the `api` and `test`
                            features (CI installs this one)
    constraints.txt         every pin above, plus the `package` feature
                            (pyinstaller), for `pip install -c constraints.txt`

python, libblas and the native libraries held at FoamFlow's builds (icu,
libaec, libdeflate) are skipped (pip cannot install them), and so is the local
foammesh-envhooks (the Pixi env's start-up ICU preload; pip installs and the
packaged exe get it from foammesh.support.win_icu instead).
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ENV, PLATFORM = "default", "win-64"
# icu, libaec, libdeflate: conda-only native libraries held at FoamFlow's builds.
SKIP = {"python", "libblas", "icu", "libaec", "libdeflate", "foammesh-envhooks"}
# conda-forge name -> PyPI name (names not listed are the same on both).
CONDA_TO_PYPI = {
    "matplotlib-base": "matplotlib",
    "pyyaml": "PyYAML",
}
RUNTIME = ["dependencies", "pypi-dependencies"]
API_TEST = [
    "feature.api.dependencies", "feature.api.pypi-dependencies",
    "feature.test.dependencies", "feature.test.pypi-dependencies",
]
PACKAGE = ["feature.package.dependencies", "feature.package.pypi-dependencies"]
OUTPUTS = {
    "requirements.txt": ("", RUNTIME),
    "requirements-test.txt": ("-r requirements.txt\n\n", API_TEST),
    "constraints.txt": ("", RUNTIME + API_TEST + PACKAGE),
}
HEADER = """\
# generated from pixi.lock by tools/deps/gen_requirements.py -- do not edit.
# Regenerate: pixi run gen-requirements   (check: ... gen_requirements.py --check)
# Pins = environment "{env}" on {platform} (Python {python}): the Pixi set, for
# installing FoamMesh with pip (CI, machines without Pixi). Pixi is the
# reference environment; these exact versions are what it tested.
"""
NOTES = {
    "requirements.txt": "",
    "requirements-test.txt": """\
# Test and transport dependencies are explicit so CI cannot silently skip
# contract coverage through pytest.importorskip.
""",
    "constraints.txt": """\
# Reproducible development, CI, packaging and runtime resolution.
""",
}


def _section(toml: dict, dotted: str) -> dict:
    node = toml
    for part in dotted.split("."):
        node = node.get(part, {})
    return node


def _conda_name_version(url: str) -> tuple[str, str]:
    fname = url.rsplit("/", 1)[-1]
    for ext in (".conda", ".tar.bz2"):
        if fname.endswith(ext):
            fname = fname[: -len(ext)]
    name, version, _build = fname.rsplit("-", 2)
    return name, version


def _norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def locked_versions(lock: dict) -> dict[str, str]:
    """Normalised package name -> version, for ENV on PLATFORM."""
    records = {}
    for rec in lock["packages"]:
        if "pypi" in rec:
            records[rec["pypi"]] = (rec["name"], str(rec.get("version", "")))
    out = {}
    for entry in lock["environments"][ENV]["packages"][PLATFORM]:
        if "conda" in entry:
            name, version = _conda_name_version(entry["conda"])
        else:
            name, version = records[entry["pypi"]]
        out[_norm(name)] = version
    return out


def render() -> dict[str, str]:
    toml = tomllib.loads((ROOT / "pixi.toml").read_text(encoding="utf8"))
    lock = yaml.safe_load((ROOT / "pixi.lock").read_text(encoding="utf8"))
    versions = locked_versions(lock)
    header = HEADER.format(env=ENV, platform=PLATFORM, python=versions["python"])
    files = {}
    for fname, (prefix, sections) in OUTPUTS.items():
        pins = set()
        for sec in sections:
            for name in _section(toml, sec):
                if name in SKIP:
                    continue
                key = _norm(name)
                if key not in versions:
                    sys.exit(f"{name} (from [{sec}]) is not in pixi.lock {ENV}/{PLATFORM}; run pixi install")
                pins.add(f"{CONDA_TO_PYPI.get(name, name)}=={versions[key]}")
        files[fname] = header + NOTES[fname] + prefix + "\n".join(sorted(pins, key=str.lower)) + "\n"
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if a file on disk differs")
    args = ap.parse_args()
    stale = []
    for fname, text in render().items():
        path = ROOT / fname
        if args.check:
            # Compare ignoring CR: git autocrlf may check the file out with CRLF.
            if not path.exists() or path.read_bytes().decode("utf8").replace("\r\n", "\n") != text:
                stale.append(fname)
        else:
            path.write_bytes(text.encode("utf8"))
            print(f"wrote {fname}")
    if stale:
        print("stale (run pixi run gen-requirements): " + ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
