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
RUNTIME_HOOK = SPEC_DIR / 'pyi_rth_foammesh_diagnostics.py'
SRC = ROOT / 'src'

sys.path.insert(0, str(SPEC_DIR))
import version_resource  # noqa: E402  (packaging/version_resource.py)

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


def crash_resilience_modules():
    """Plan 35 CR10: the modules `FoamMesh.exe --crash-helper/--worker` run.

    `main.py` reaches them only through an argv dispatch, and the runtime hook
    imports `native_capture` by name, so static analysis may not see any of
    them. They land with CR0/CR2; until then a missing one is reported here
    and skipped, so the spec still builds. A release build sets
    FOAMMESH_RELEASE_BUILD=1, and then a missing one stops the build: a 1.1.x
    installer without the crash helper would collect no evidence (§7).
    """
    wanted = {
        'foammesh.support.crash_helper': SRC / 'foammesh' / 'support' / 'crash_helper.py',
        'foammesh.support.native_capture': SRC / 'foammesh' / 'support' / 'native_capture.py',
    }
    found = [name for name, path in wanted.items() if path.is_file()]
    missing = [name for name, path in wanted.items() if not path.is_file()]
    workers = SRC / 'foammesh' / 'workers'
    if (workers / '__init__.py').is_file():
        found.append('foammesh.workers')
        found.extend(f'foammesh.workers.{module.stem}'
                     for module in sorted(workers.glob('*.py'))
                     if module.stem != '__init__')
    else:
        missing.append('foammesh.workers')
    if missing:
        message = ('foammesh.spec: crash-resilience modules not in the tree, '
                   'so not collected: ' + ', '.join(missing))
        if os.environ.get('FOAMMESH_RELEASE_BUILD') == '1':
            raise SystemExit(message + ' (FOAMMESH_RELEASE_BUILD=1)')
        print('WARNING: ' + message, file=sys.stderr)
    return found


hiddenimports += crash_resilience_modules()

# The exe's version resource is the committed template plus the PySide6 and
# VTK versions of this build (packaging/version_resource.py).
BUILD_VERSION_INFO = (version_resource.write(VERSION_INFO, Path(workpath))
                      if sys.platform == 'win32' else None)

a = Analysis(
    [str(ROOT / 'src' / 'foammesh' / 'main.py')],
    pathex=[str(ROOT / 'src'), str(ROOT / 'vendor')],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # Runs before PyInstaller's own hooks and before main.py (Plan 35 CR10).
    runtime_hooks=[str(RUNTIME_HOOK)],
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
    # Plan 35 CR10 (F14): no UPX anywhere. UPX-packed Qt/VTK DLLs draw
    # antivirus interference and odd loader faults, and a packed module makes
    # a minidump harder to symbolise.
    upx=False,
    console=False,
    icon=str(ICON_ICO) if sys.platform == 'win32' else None,
    version=str(BUILD_VERSION_INFO) if sys.platform == 'win32' else None,
)
bundle = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False,
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
            'CFBundleShortVersionString': '1.1.0',
            'CFBundleVersion': '1.1.0',
            'NSHighResolutionCapable': True,
        },
    )
