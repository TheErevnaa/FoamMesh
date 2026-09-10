# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform one-folder PyInstaller definition for FoamMesh."""
import sys
from pathlib import Path

SPEC_LOCATION = Path(SPECPATH).resolve()
SPEC_DIR = SPEC_LOCATION if SPEC_LOCATION.is_dir() else SPEC_LOCATION.parent
ROOT = SPEC_DIR.parent
ICON_ICO = ROOT / 'src' / 'resources' / 'branding' / 'foammesh.ico'
ICON_ICNS = ROOT / 'src' / 'resources' / 'branding' / 'foammesh.icns'
VERSION_INFO = ROOT / 'packaging' / 'windows' / 'version_info.txt'

BYTECODE_SUFFIXES = frozenset({'.pyc', '.pyo'})


def tree(source, prefix):
    """Every file under `source`, as PyInstaller (file, destination-dir) pairs.

    PyInstaller's (directory, name) form copies a directory wholesale, which is
    the wrong shape for these two trees. `src/resources` is an importable
    package, so merely starting the app -- or running any script with
    PYTHONPATH=src -- leaves a `__pycache__` beside its source, and the next
    build shipped it: two builds of identical source produced 5,496 and 5,498
    entries, differing only by the .pyc files under `resources/`. That made the
    contents of a release depend on whether anyone had run the app before
    packaging. Listing the files ourselves is what keeps byte-compiled
    droppings out, and it covers `docs/` too in case anything importable ever
    lands there.
    """
    collected = []
    for path in sorted(source.rglob('*')):
        if not path.is_file():
            continue
        if '__pycache__' in path.parts or path.suffix in BYTECODE_SUFFIXES:
            continue
        collected.append((str(path), str(Path(prefix) / path.parent.relative_to(source))))
    return collected


datas = [
    *tree(ROOT / 'src' / 'resources', 'resources'),
    *tree(ROOT / 'docs', 'docs'),
    (str(ROOT / 'LICENSE'), '.'),
    (str(ROOT / 'NOTICE'), '.'),
    (str(ROOT / 'THIRD_PARTY.md'), '.'),
]
hiddenimports = [
    'resource_rc', 'PySide6.QtSvg',
    # Engine packages use lazy module exports so PyInstaller cannot discover
    # the registry reached by frozen package qualification and fresh cases.
    'foammesh.core.engine.registry',
    # VTK creates these backends through factories, so static analysis cannot
    # see them even though the application imports their public wrappers.
    'vtkmodules.vtkRenderingOpenGL2', 'vtkmodules.vtkInteractionStyle',
]

a = Analysis(
    [str(ROOT / 'src' / 'foammesh' / 'main.py')],
    pathex=[str(ROOT / 'src'), str(ROOT / 'vendor')],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'IPython', 'pytest', 'sphinx', 'torch'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='FoamMesh',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=str(ICON_ICO) if sys.platform == 'win32' else None,
    version=str(VERSION_INFO) if sys.platform == 'win32' else None,
)
bundle = COLLECT(exe, a.binaries, a.datas, strip=False, upx=True,
                 upx_exclude=[], name='FoamMesh')
if sys.platform == 'darwin':
    app = BUNDLE(
        bundle,
        name='FoamMesh.app',
        icon=str(ICON_ICNS),
        bundle_identifier='com.erevnaa.foammesh',
        info_plist={
            'CFBundleDisplayName': 'FoamMesh',
            'CFBundleName': 'FoamMesh',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'NSHighResolutionCapable': True,
        },
    )
