"""The Windows version resource, with the build's PySide6 and VTK versions in it.

Plan 35 CR10. A crash report is only as useful as the stack it can be read
against: a minidump from the other PC names DLLs, and symbolising those needs
the exact PySide6 and VTK the installer shipped. `version_info.txt` is the
committed template and carries the product version; the versions of the two
native stacks are only known on the build machine, so the spec renders a copy
of the template with them added and binds *that* copy to `FoamMesh.exe`.
Explorer > Properties > Details then shows them, and so does any tool that
reads the version resource of the exe a dump names.

The committed template is never rewritten, so a build leaves the tree clean
and the product version keeps its single set of sites.
"""
from __future__ import annotations

from importlib import metadata
from pathlib import Path

#: Where the added StringStructs go: the close of the one StringTable.
TABLE_CLOSE = '\n    ])]),'

#: Distribution name -> StringStruct key.
RECORDED = (('PySide6', 'PySide6Version'), ('vtk', 'VTKVersion'))


def runtime_versions() -> dict[str, str]:
    """The installed version of each recorded distribution, by resource key."""
    versions = {}
    for distribution, key in RECORDED:
        try:
            versions[key] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            versions[key] = 'not installed'
    return versions


def render(template: str, versions: dict[str, str]) -> str:
    """`template` with one StringStruct per entry of `versions` appended."""
    if template.count(TABLE_CLOSE) != 1:
        raise SystemExit(
            'packaging/version_resource.py: version_info.txt no longer has '
            'exactly one StringTable close to add the PySide6/VTK versions '
            'before; update TABLE_CLOSE to match it.')
    added = ''.join(
        f"\n      StringStruct({key!r}, {str(value)!r}),"
        for key, value in versions.items())
    return template.replace(TABLE_CLOSE, added + TABLE_CLOSE)


def write(template_path: Path, output_dir: Path) -> Path:
    """Render the template into `output_dir` and return the rendered file."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered = output_dir / 'version_info.generated.txt'
    text = Path(template_path).read_text(encoding='utf-8')
    rendered.write_text(render(text, runtime_versions()), encoding='utf-8')
    return rendered
