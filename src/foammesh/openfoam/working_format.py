"""How the snappyHexMesh working case is written: the one place it is set.

Plan 50 IP15 (SN-3, decision D5; DP-6420). The working case was ASCII at
eight significant digits and is re-read between the three snappyHexMesh
calls, so every point was rounded twice before layers were added, and the
files were large and slow over the WSL mount. The user decided: binary, but
only once a measured comparison shows it is no worse than ASCII at precision
12; until then ASCII at precision 12.

:data:`WORKING_CASE_FORMAT` is that one setting. The lead switches the working
case to binary after the comparison by changing it to :data:`BINARY`;
nothing else needs to change. Every reader of the working case in this code
base reads all three values (see ``tests/unit/test_plan50_ip15_*``), and the
OpenFOAM export converts a binary mesh to ASCII before it writes the hand-off
manifest, so the exported files keep the layout they have today.

Nothing here imports Qt or OpenFOAM.
"""
from __future__ import annotations

from pathlib import Path

#: ``writeFormat binary``; ``writePrecision`` then only touches text parts.
BINARY = 'binary'
#: ``writeFormat ascii; writePrecision 12``.
ASCII_12 = 'ascii12'
#: ``writeFormat ascii; writePrecision 8`` -- the working case before IP15.
ASCII_8 = 'ascii8'

#: ``choice -> (writeFormat, writePrecision)``.
FORMATS = {
    BINARY: ('binary', 12),
    ASCII_12: ('ascii', 12),
    ASCII_8: ('ascii', 8),
}

#: THE setting. ASCII at precision 12 until the lead's measured comparison
#: (``scripts/compare_working_case_formats.py``) shows binary is no worse;
#: then :data:`BINARY`.
WORKING_CASE_FORMAT = ASCII_12


def control_entries(choice: str | None = None) -> dict:
    """The ``controlDict`` write entries for the working case.

    ``writeCompression`` stays off: v13 resets compressed binary to binary
    anyway, and gzip on ASCII is slower over the WSL mount, not faster.
    """
    choice = WORKING_CASE_FORMAT if choice is None else choice
    try:
        write_format, precision = FORMATS[choice]
    except KeyError:
        raise ValueError(
            f'unknown working-case format {choice!r}; one of '
            f'{", ".join(sorted(FORMATS))}') from None
    return {'writeFormat': write_format, 'writePrecision': precision,
            'writeCompression': 'off'}


def member_format(path: Path) -> str:
    """``format`` of one OpenFOAM file's FoamFile header (``ascii`` if none)."""
    import gzip
    import re

    path = Path(path)
    try:
        if path.suffix == '.gz':
            with gzip.open(path, 'rb') as stream:
                head = stream.read(4096)
        else:
            with open(path, 'rb') as stream:
                head = stream.read(4096)
    except (OSError, EOFError):
        return 'ascii'
    match = re.search(rb'FoamFile\s*\{(.*?)\}', head, re.DOTALL)
    if match is None:
        return 'ascii'
    found = re.search(rb'\bformat\s+(\w+)\s*;', match.group(1))
    return found.group(1).decode('ascii') if found else 'ascii'


def binary_members(poly_mesh: Path) -> list[Path]:
    """Every file under ``poly_mesh`` (sets included) written ``binary``.

    ``boundary`` is text even under a binary header, so the export would
    count it here too: that is intended, ``foamFormatConvert`` rewrites its
    header to ``ascii`` along with the lists.
    """
    poly_mesh = Path(poly_mesh)
    if not poly_mesh.is_dir():
        return []
    found = []
    for path in sorted(poly_mesh.rglob('*')):
        if path.is_file() and member_format(path) == 'binary':
            found.append(path)
    return found


def working_case_binary(case_root: Path) -> bool:
    """Whether the working case's mesh is, or will be written, binary."""
    import re

    case_root = Path(case_root)
    control = case_root / 'system' / 'controlDict'
    try:
        text = control.read_text(encoding='utf-8', errors='replace')
    except OSError:
        text = ''
    if re.search(r'^\s*writeFormat\s+binary\s*;', text, re.MULTILINE):
        return True
    roots = [case_root / 'constant', *sorted(case_root.glob('processor*'))]
    return any(binary_meshes(root) for root in roots if root.is_dir())


def _ascii_header(prefix: bytes) -> bytes:
    """``prefix`` (banner, FoamFile block, rule) with ``format ascii`` and no
    ``arch`` entry, every other byte kept."""
    import re

    match = re.search(rb'FoamFile\s*\{.*?\}', prefix, re.DOTALL)
    if match is None:
        return prefix
    block = re.sub(rb'(\bformat\s+)binary(\s*;)', rb'\1ascii\2', match.group())
    block = re.sub(rb'\n[ \t]*arch\s+"[^"]*"\s*;[ \t]*', b'', block)
    return prefix[:match.start()] + block + prefix[match.end():]


def _ascii_labels(values) -> bytes:
    """A label list's body as OpenFOAM 13 writes it in ASCII (observed, see
    ``tests/unit/plan37_uf10a_fixtures.py``): ``N{v}`` when uniform, one line
    up to ten entries, else one entry per line."""
    values = [int(value) for value in values]
    count = len(values)
    if count > 1 and all(value == values[0] for value in values):
        return f'\n{count}{{{values[0]}}}'.encode()
    if count <= 10:
        return f'\n{count}({" ".join(map(str, values))})'.encode()
    return (f'\n\n{count}\n(\n'.encode()
            + ''.join(f'{value}\n' for value in values).encode() + b')\n')


def rewrite_label_list_ascii(path: Path) -> bool:
    """Rewrite one binary ``labelList`` file as ASCII; False if it is not one.

    The banner, header and footer are the file's own bytes; only the
    ``format`` word and the list body change.
    """
    from foammesh.core.mesh import poly_mesh_binary as binary
    from foammesh.core.mesh.poly_mesh_boundary import PolyMeshReadError

    path = Path(path)
    if path.suffix == '.gz':
        return False
    try:
        header, raw, start = binary.split_member(path)
        if str(header.get('class', '')).strip() != 'labelList':
            return False
        stated, _scalar = binary.arch_widths(header, path)
        count_at = binary.skip_blank(raw, start)
        block, _count, width, end = binary.binary_list(
            raw, start, components=1,
            widths=(stated,) if stated else binary.LABEL_WIDTHS,
            hint=path.name,
            follows=lambda at, _w: binary.only_blank_after(raw, at))
        values = binary.labels_from(block, width)
    except (OSError, PolyMeshReadError):
        return False
    prefix = _ascii_header(raw[:count_at].rstrip(b' \t\r\n') + b'\n')
    text = prefix + _ascii_labels(values) + raw[end:]
    temporary = path.with_name(path.name + '.foammesh-ascii')
    temporary.write_bytes(text)
    temporary.replace(path)
    return True


def rewrite_boundary_header(path: Path) -> bool:
    """``boundary`` is text under a ``format binary`` header: fix the header."""
    path = Path(path)
    if member_format(path) != 'binary':
        return False
    raw = path.read_bytes()
    cut = raw.find(b'}')
    if cut < 0:
        return False
    fixed = _ascii_header(raw[:cut + 1]) + raw[cut + 1:]
    temporary = path.with_name(path.name + '.foammesh-ascii')
    temporary.write_bytes(fixed)
    temporary.replace(path)
    return True


#: Set files: written by checkMesh/topoSet, removed or never read by the
#: export, and not part of what FoamFlow imports.
_SET_CLASSES = ('cellSet', 'faceSet', 'pointSet')


def ascii_fixups(constant: Path) -> list[Path]:
    """Finish an export's conversion to ASCII; return what is still binary.

    Run after ``foamFormatConvert``: rewrites the header of a text
    ``boundary`` and any binary ``labelList`` it left, and lists every other
    binary file under ``constant`` (``sets`` folders aside).
    """
    import re

    left = []
    for folder in sorted(Path(constant).rglob('polyMesh')):
        for path in binary_members(folder):
            if 'sets' in path.relative_to(folder).parts:
                continue
            if path.name == 'boundary' and rewrite_boundary_header(path):
                continue
            if rewrite_label_list_ascii(path):
                continue
            try:
                head = path.read_bytes()[:4096]
            except OSError:
                head = b''
            found = re.search(rb'\bclass\s+(\w+)\s*;', head)
            if found and found.group(1).decode() in _SET_CLASSES:
                continue
            left.append(path)
    return left


def binary_meshes(root: Path) -> list[Path]:
    """Every ``polyMesh`` folder under ``root`` that holds a binary file."""
    root = Path(root)
    return [folder for folder in sorted(root.rglob('polyMesh'))
            if folder.is_dir() and binary_members(folder)]
