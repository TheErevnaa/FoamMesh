"""Minimal PEP 517 backend for foammesh-envhooks (no build dependencies).

The wheel holds foammesh_envhooks.py and foammesh_envhooks.pth at the top of
site-packages; the .pth line imports the module at interpreter start.
"""
import base64
import hashlib
import io
import os
import tarfile
import tomllib
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "pyproject.toml"), "rb") as _fh:
    _PROJECT = tomllib.load(_fh)["project"]
# Pixi reinstalls a path dependency only when its version changes: bump
# [project].version in pyproject.toml after editing the hook.
NAME, VERSION = _PROJECT["name"], _PROJECT["version"]
DIST = NAME.replace("-", "_")
FILES = ["foammesh_envhooks.py", "foammesh_envhooks.pth"]
SDIST_FILES = FILES + ["pyproject.toml", "_build.py", "README.md"]
DIST_INFO = f"{DIST}-{VERSION}.dist-info"
TAG = "py3-none-any"
METADATA = (
    "Metadata-Version: 2.1\n"
    f"Name: {NAME}\n"
    f"Version: {VERSION}\n"
    f"Summary: {_PROJECT['description']}\n"
    f"Requires-Python: {_PROJECT['requires-python']}\n"
)
WHEEL = f"Wheel-Version: 1.0\nGenerator: foammesh-envhooks _build\nRoot-Is-Purelib: true\nTag: {TAG}\n"


def _read(name):
    with open(os.path.join(HERE, name), "rb") as fh:
        return fh.read()


def _record_line(arc, data):
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{arc},sha256={digest},{len(data)}"


def _write_metadata(directory):
    path = os.path.join(directory, DIST_INFO)
    os.makedirs(path, exist_ok=True)
    for fname, text in (("METADATA", METADATA), ("WHEEL", WHEEL)):
        with open(os.path.join(path, fname), "w", encoding="utf8", newline="\n") as fh:
            fh.write(text)
    return DIST_INFO


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_sdist(config_settings=None):
    return []


get_requires_for_build_editable = get_requires_for_build_wheel


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _write_metadata(metadata_directory)


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    # Always a real install (also when asked for an editable one): the .pth
    # must sit in site-packages either way.
    wheel_name = f"{DIST}-{VERSION}-{TAG}.whl"
    entries = [(name, _read(name)) for name in FILES]
    entries += [
        (f"{DIST_INFO}/METADATA", METADATA.encode()),
        (f"{DIST_INFO}/WHEEL", WHEEL.encode()),
    ]
    record = [_record_line(arc, data) for arc, data in entries] + [f"{DIST_INFO}/RECORD,,"]
    entries.append((f"{DIST_INFO}/RECORD", ("\n".join(record) + "\n").encode()))
    with zipfile.ZipFile(os.path.join(wheel_directory, wheel_name), "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in entries:
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)
    return wheel_name


build_editable = build_wheel


def build_sdist(sdist_directory, config_settings=None):
    base = f"{DIST}-{VERSION}"
    sdist_name = f"{base}.tar.gz"
    with tarfile.open(os.path.join(sdist_directory, sdist_name), "w:gz", format=tarfile.PAX_FORMAT) as tf:
        members = [(name, _read(name)) for name in SDIST_FILES if os.path.exists(os.path.join(HERE, name))]
        members.append(("PKG-INFO", METADATA.encode()))
        for name, data in members:
            info = tarfile.TarInfo(f"{base}/{name}")
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tf.addfile(info, io.BytesIO(data))
    return sdist_name
