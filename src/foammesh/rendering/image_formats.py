"""Picture formats beyond the six the snapshot already writes (Plan 48 GM10).

Gmsh saves a view as PNG, JPEG, GIF, BMP, PPM, YUV, XPM, PS, EPS, PDF, SVG,
LaTeX, PGF and TikZ, and as MPEG. The snapshot has PNG, JPEG, SVG, PDF, EPS
and PS (``core.gmsh.session_display.export_view``); this module adds the rest,
each with one named backend (``FORMATS``):

========  ===============================================================
GIF       Pillow, from the rendered RGBA buffer
PPM, BMP  the VTK writers ``vtkPNMWriter`` / ``vtkBMPWriter``
YUV       a small writer: planar YCbCr 4:2:0, the layout of Gmsh's own
XPM       a small writer (Pillow cannot write XPM), 256 colours
TEX       ``vtkGL2PSExporter`` in TeX mode: the *text* layer only, written
          with its picture (EPS) as a pair, as Gmsh's own ``.tex`` is
PGF, TikZ no VTK backend exists: ``svg_to_pgf`` / ``svg_to_tikz`` convert the
          SVG the vector export writes (candidate (b) of the plan)
MP4       ffmpeg, where it is present in the distro or on the host;
          refused with that reason otherwise
========  ===============================================================

Transparency: GIF and XPM keep a transparent pixel (one bit); PPM, BMP and
YUV have no alpha and are flattened onto the background colour.

The raster writers take a buffer (rows from the top, ``H x W x 3|4`` unsigned
bytes), so they need no window; the viewport hands one over from
``label_pass.capture``.
"""
from __future__ import annotations

import html
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class ImageFormatError(ValueError):
    """A picture that cannot be written, with the reason."""


@dataclass(frozen=True)
class ImageFormat:
    id: str
    label: str
    suffix: str
    kind: str          # raster | vector | text | movie
    backend: str
    alpha: str         # kept | flattened | none | text
    existing: bool = False
    note: str = ''


FORMATS = (
    ImageFormat('PNG', 'PNG', '.png', 'raster', 'label_pass', 'kept', True),
    ImageFormat('JPEG', 'JPEG', '.jpg', 'raster', 'label_pass', 'flattened',
                True),
    ImageFormat('SVG', 'SVG', '.svg', 'vector', 'vector_export', 'kept', True),
    ImageFormat('PDF', 'PDF', '.pdf', 'vector', 'vector_export', 'kept', True),
    ImageFormat('EPS', 'EPS', '.eps', 'vector', 'vector_export', 'kept', True),
    ImageFormat('PS', 'PostScript', '.ps', 'vector', 'vector_export', 'kept',
                True),
    ImageFormat('GIF', 'GIF', '.gif', 'raster', 'Pillow', 'kept',
                note='256 colours; a pixel is transparent or it is not'),
    ImageFormat('PPM', 'PPM', '.ppm', 'raster', 'vtkPNMWriter', 'flattened'),
    ImageFormat('BMP', 'BMP', '.bmp', 'raster', 'vtkBMPWriter', 'flattened'),
    ImageFormat('YUV', 'YUV (4:2:0 planar)', '.yuv', 'raster', 'numpy',
                'flattened',
                note='even width and height: an odd last row or column is '
                     'dropped, as Gmsh does'),
    ImageFormat('XPM', 'XPM', '.xpm', 'raster', 'numpy + Pillow', 'kept',
                note='256 colours; a pixel is transparent or it is not'),
    ImageFormat('TEX', 'LaTeX', '.tex', 'text', 'vtkGL2PSExporter (TeX)',
                'text',
                note='the text of the view only, written with the EPS it '
                     'goes with; \\includegraphics names the EPS'),
    ImageFormat('PGF', 'PGF', '.pgf', 'vector', 'svg_to_pgf', 'kept',
                note='plain PGF commands; needs \\usepackage{pgf}'),
    ImageFormat('TIKZ', 'TikZ', '.tikz', 'vector', 'svg_to_tikz', 'kept',
                note='a tikzpicture; needs \\usepackage{tikz}'),
    ImageFormat('MP4', 'MP4 movie', '.mp4', 'movie', 'ffmpeg', 'none',
                note='Record; needs ffmpeg'),
)
BY_ID = {item.id: item for item in FORMATS}
NEW_RASTER = ('GIF', 'PPM', 'BMP', 'YUV', 'XPM')
VECTOR_TEXT = ('PGF', 'TIKZ')


def format_for(name) -> ImageFormat:
    key = str(name).upper().lstrip('.')
    key = {'JPG': 'JPEG', 'TIKZ': 'TIKZ'}.get(key, key)
    try:
        return BY_ID[key]
    except KeyError:
        raise ImageFormatError(
            f'{name} is not a picture format; choose one of '
            + ', '.join(BY_ID)) from None


def listing() -> list:
    """The formats as plain rows (for the Save picture dialog and the API),
    with whether each can be written here and, if not, why."""
    rows = []
    for item in FORMATS:
        reason = refusal(item.id)
        rows.append({'id': item.id, 'label': item.label,
                     'suffix': item.suffix, 'kind': item.kind,
                     'backend': item.backend, 'alpha': item.alpha,
                     'existing': item.existing, 'note': item.note,
                     'available': not reason, 'reason': reason})
    return rows


def refusal(fmt) -> str:
    """Why *fmt* cannot be written on this machine, or ''."""
    item = format_for(fmt)
    if item.id == 'MP4':
        return movie_refusal()
    if item.id in ('PPM', 'BMP', 'TEX'):
        try:
            import vtk  # noqa: F401
        except ImportError:                              # pragma: no cover
            return f'{item.backend} needs VTK'
    if item.id == 'GIF':
        try:
            import PIL  # noqa: F401
        except ImportError:                              # pragma: no cover
            return 'GIF is written by Pillow, which is not installed'
    return ''


# --------------------------------------------------------------------------- #
# Raster formats from a buffer
# --------------------------------------------------------------------------- #

def _rgba(buffer) -> np.ndarray:
    array = np.asarray(buffer)
    if array.dtype != np.uint8:
        array = np.clip(array, 0, 255).astype(np.uint8)
    if array.ndim != 3 or array.shape[2] not in (3, 4):
        raise ImageFormatError('a picture buffer is H x W x 3 or H x W x 4 '
                               'unsigned bytes')
    if array.shape[0] < 1 or array.shape[1] < 1:
        raise ImageFormatError('the picture is empty')
    if array.shape[2] == 3:
        alpha = np.full(array.shape[:2] + (1,), 255, np.uint8)
        array = np.concatenate([array, alpha], axis=2)
    return array


def _background(background) -> np.ndarray:
    values = np.asarray(background, dtype=float).ravel()[:3]
    if values.size != 3:
        raise ImageFormatError('the background is three numbers')
    if values.max() <= 1.0:
        values = values * 255.0
    return np.clip(np.rint(values), 0, 255).astype(np.uint8)


def flatten(buffer, background=(255, 255, 255)) -> np.ndarray:
    """RGB with the alpha composited onto *background* (0-1 or 0-255)."""
    rgba = _rgba(buffer).astype(np.float32)
    alpha = rgba[..., 3:4] / 255.0
    base = _background(background).astype(np.float32)
    rgb = rgba[..., :3] * alpha + base * (1.0 - alpha)
    return np.clip(np.rint(rgb), 0, 255).astype(np.uint8)


def _vtk_image(rgb: np.ndarray):
    import vtk
    from vtkmodules.util import numpy_support

    height, width = rgb.shape[:2]
    image = vtk.vtkImageData()
    image.SetDimensions(width, height, 1)
    flat = np.ascontiguousarray(rgb[::-1]).reshape(-1, 3)   # VTK: bottom row first
    array = numpy_support.numpy_to_vtk(flat, deep=True,
                                       array_type=vtk.VTK_UNSIGNED_CHAR)
    array.SetNumberOfComponents(3)
    image.GetPointData().SetScalars(array)
    return image


def _write_vtk(rgb, path, writer) -> Path:
    writer.SetFileName(str(path))
    writer.SetInputData(_vtk_image(rgb))
    writer.Write()
    if not Path(path).is_file():
        raise ImageFormatError(f'{writer.GetClassName()} wrote nothing to '
                               f'{path}')
    return Path(path)


def write_ppm(buffer, path, background=(255, 255, 255)) -> Path:
    import vtk

    return _write_vtk(flatten(buffer, background), path, vtk.vtkPNMWriter())


def write_bmp(buffer, path, background=(255, 255, 255)) -> Path:
    import vtk

    return _write_vtk(flatten(buffer, background), path, vtk.vtkBMPWriter())


def _palette_image(rgba: np.ndarray):
    """A 256-colour Pillow image; index 255 is the transparent pixel (only
    when some pixel is more than half transparent)."""
    from PIL import Image

    alpha = rgba[..., 3]
    transparent = alpha < 128
    colours = 255 if transparent.any() else 256
    rgb = Image.fromarray(rgba[..., :3], 'RGB')
    indexed = rgb.convert('P', palette=Image.ADAPTIVE, colors=colours)
    if transparent.any():
        mask = Image.fromarray((transparent * 255).astype(np.uint8), 'L')
        indexed.paste(255, mask)
        return indexed, 255
    return indexed, None


def write_gif(buffer, path, background=None) -> Path:
    indexed, hole = _palette_image(_rgba(buffer))
    options = {} if hole is None else {'transparency': hole}
    indexed.save(str(path), format='GIF', **options)
    return Path(path)


def write_yuv(buffer, path, background=(255, 255, 255)) -> Path:
    """Planar YCbCr 4:2:0, the layout of Gmsh's ``create_yuv``: all the Y
    values, then Cb, then Cr, top row first, the chroma of each 2x2 block
    averaged. Gmsh's own weights are used."""
    rgb = flatten(buffer, background).astype(np.float32)
    height, width = (rgb.shape[0] // 2) * 2, (rgb.shape[1] // 2) * 2
    if not height or not width:
        raise ImageFormatError('a YUV picture needs at least 2 × 2 pixels')
    rgb = rgb[:height, :width]
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    luma = 0.299 * red + 0.587 * green + 0.114 * blue
    cb = -0.16874 * red - 0.33126 * green + 0.5 * blue
    cr = 0.5 * red - 0.41869 * green - 0.08131 * blue

    def block(plane):
        return (plane[0::2, 0::2] + plane[1::2, 0::2] + plane[0::2, 1::2]
                + plane[1::2, 1::2]) / 4.0

    Path(path).write_bytes(
        luma.astype(np.uint8).tobytes()
        + (block(cb) + 128).astype(np.uint8).tobytes()
        + (block(cr) + 128).astype(np.uint8).tobytes())
    return Path(path)


_XPM_KEYS = ('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ'
             '0123456789.#@%&*=-+;:,<>!~^()[]{}|?/_')


def write_xpm(buffer, path, background=None) -> Path:
    """XPM3: a palette of at most 256 colours and one string per row."""
    from PIL import Image  # noqa: F401

    indexed, hole = _palette_image(_rgba(buffer))
    index = np.asarray(indexed)
    palette = np.asarray(indexed.getpalette(), dtype=np.uint8).reshape(-1, 3)
    used = sorted(set(np.unique(index).tolist()))
    width_chars = 1 if len(used) <= len(_XPM_KEYS) else 2
    keys = {}
    for number, value in enumerate(used):
        if width_chars == 1:
            keys[value] = _XPM_KEYS[number]
        else:
            keys[value] = (_XPM_KEYS[number // len(_XPM_KEYS)]
                           + _XPM_KEYS[number % len(_XPM_KEYS)])
    lines = ['/* XPM */', 'static char *foammesh[] = {',
             '"%d %d %d %d",' % (index.shape[1], index.shape[0], len(used),
                                 width_chars)]
    for value in used:
        if hole is not None and value == hole:
            colour = 'None'
        else:
            red, green, blue = (int(c) for c in palette[value])
            colour = '#%02X%02X%02X' % (red, green, blue)
        lines.append('"%s c %s",' % (keys[value], colour))
    lookup = np.array([keys.get(i, '') for i in range(256)], dtype=object)
    rows = [''.join(lookup[row]) for row in index]
    lines.extend('"%s"%s' % (row, ',' if i < len(rows) - 1 else '')
                 for i, row in enumerate(rows))
    lines.append('};')
    Path(path).write_text('\n'.join(lines) + '\n', encoding='ascii')
    return Path(path)


RASTER_WRITERS = {'GIF': write_gif, 'PPM': write_ppm, 'BMP': write_bmp,
                  'YUV': write_yuv, 'XPM': write_xpm}


def write_raster(buffer, path, fmt, background=(255, 255, 255)) -> Path:
    """Write *buffer* in one of GIF, PPM, BMP, YUV, XPM."""
    item = format_for(fmt)
    writer = RASTER_WRITERS.get(item.id)
    if writer is None:
        raise ImageFormatError(
            f'{item.id} is not written from a buffer; the raster formats '
            'here are ' + ', '.join(RASTER_WRITERS))
    target = Path(path).with_suffix(item.suffix)
    return writer(buffer, target, background)


# --------------------------------------------------------------------------- #
# SVG -> TikZ / PGF
# --------------------------------------------------------------------------- #

_SVG = '{http://www.w3.org/2000/svg}'


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


def _colour(value):
    if not value or value == 'none':
        return None
    value = value.strip()
    match = re.fullmatch(r'#([0-9a-fA-F]{6})', value)
    if match:
        raw = match.group(1)
        return tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
    match = re.fullmatch(r'#([0-9a-fA-F]{3})', value)
    if match:
        return tuple(int(c * 2, 16) for c in match.group(1))
    named = {'black': (0, 0, 0), 'white': (255, 255, 255),
             'red': (255, 0, 0), 'green': (0, 128, 0), 'blue': (0, 0, 255)}
    if value.lower() in named:
        return named[value.lower()]
    raise ImageFormatError(f'colour {value!r} is not one the SVG export '
                           'writes')


_TEX_ESCAPES = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}',
                '$': r'\$', '&': r'\&', '#': r'\#', '^': r'\^{}',
                '_': r'\_', '%': r'\%', '~': r'\~{}'}


def tex_escape(text: str) -> str:
    return ''.join(_TEX_ESCAPES.get(ch, ch) for ch in text)


@dataclass
class _Drawing:
    width: float
    height: float
    background: tuple | None
    items: list      # tuples ('polygon'|'line'|'circle'|'text', ...)


def _parse_svg(svg_text: str) -> _Drawing:
    try:
        root = ET.fromstring(svg_text.encode('utf-8'))
    except ET.ParseError as error:
        raise ImageFormatError(f'the SVG cannot be read: {error}') from error
    if _local(root.tag) != 'svg':
        raise ImageFormatError('the file is not an SVG')
    box = (root.get('viewBox') or '').replace(',', ' ').split()
    if len(box) == 4:
        width, height = float(box[2]), float(box[3])
    else:
        width = float(re.sub(r'[a-z%]+$', '', root.get('width', '0')) or 0)
        height = float(re.sub(r'[a-z%]+$', '', root.get('height', '0')) or 0)
    if width <= 0 or height <= 0:
        raise ImageFormatError('the SVG has no size')
    items, background = [], None
    for node in root:
        kind = _local(node.tag)
        if kind == 'rect':
            background = _colour(node.get('fill'))
        elif kind == 'polygon':
            points = [tuple(float(v) for v in pair.split(','))
                      for pair in node.get('points', '').split()]
            items.append(('polygon', points, _colour(node.get('fill')),
                          _colour(node.get('stroke')),
                          float(node.get('stroke-width', 1.0))))
        elif kind == 'line':
            items.append(('line', [(float(node.get('x1')), float(node.get('y1'))),
                                   (float(node.get('x2')), float(node.get('y2')))],
                          _colour(node.get('stroke')),
                          float(node.get('stroke-width', 1.0))))
        elif kind == 'circle':
            items.append(('circle', (float(node.get('cx')), float(node.get('cy'))),
                          float(node.get('r')), _colour(node.get('fill'))))
        elif kind == 'text':
            items.append(('text', (float(node.get('x')), float(node.get('y'))),
                          html.unescape(node.text or ''),
                          _colour(node.get('fill')) or (0, 0, 0),
                          node.get('text-anchor', 'start'),
                          float(node.get('font-size', 10))))
    return _Drawing(width, height, background, items)


def _palette(drawing: _Drawing):
    names = {}

    def name(rgb):
        if rgb is None:
            return None
        if rgb not in names:
            names[rgb] = 'fm%d' % len(names)
        return names[rgb]

    for item in drawing.items:
        if item[0] == 'polygon':
            name(item[2]), name(item[3])
        elif item[0] == 'line':
            name(item[2])
        elif item[0] == 'circle':
            name(item[3])
        else:
            name(item[3])
    name(drawing.background)
    return names


def _definitions(names: dict) -> list:
    return ['\\definecolor{%s}{RGB}{%d,%d,%d}' % (label, *rgb)
            for rgb, label in names.items()]


def _flip(drawing, point):
    return point[0], drawing.height - point[1]


_TIKZ_ANCHOR = {'start': 'base west', 'middle': 'base', 'end': 'base east'}
_PGF_ALIGN = {'start': 'base,left', 'middle': 'base', 'end': 'base,right'}


def svg_to_tikz(svg_text: str, standalone: bool = False) -> str:
    """The picture in *svg_text* as a ``tikzpicture`` (1 SVG pixel = 1 pt,
    y up). Every polygon, line, dot and label of the SVG becomes one
    statement; labels stay text."""
    drawing = _parse_svg(svg_text)
    names = _palette(drawing)
    out = ['% FoamMesh picture: needs \\usepackage{tikz}']
    out += _definitions(names)
    out.append('\\begin{tikzpicture}[x=1pt,y=1pt]')
    if drawing.background is not None:
        out.append('\\fill[%s] (0,0) rectangle (%g,%g);'
                   % (names[drawing.background], drawing.width,
                      drawing.height))
    else:
        out.append('\\path[use as bounding box] (0,0) rectangle (%g,%g);'
                   % (drawing.width, drawing.height))
    for item in drawing.items:
        kind = item[0]
        if kind == 'polygon':
            _k, points, fill, edge, width = item
            path = ' -- '.join('(%.2f,%.2f)' % _flip(drawing, p)
                               for p in points) + ' -- cycle'
            options = ['line join=round', 'line width=%.2fpt' % width]
            if fill is not None:
                options.append('fill=' + names[fill])
            if edge is not None:
                options.append('draw=' + names[edge])
            command = '\\filldraw' if fill is not None and edge is not None \
                else '\\fill' if fill is not None else '\\draw'
            if command == '\\fill':
                options = [o for o in options if not o.startswith('line')]
            out.append('%s[%s] %s;' % (command, ','.join(options), path))
        elif kind == 'line':
            _k, points, colour, width = item
            out.append('\\draw[%s,line width=%.2fpt] (%.2f,%.2f) -- (%.2f,%.2f);'
                       % (names[colour], width, *_flip(drawing, points[0]),
                          *_flip(drawing, points[1])))
        elif kind == 'circle':
            _k, centre, radius, colour = item
            out.append('\\fill[%s] (%.2f,%.2f) circle (%.2fpt);'
                       % (names[colour], *_flip(drawing, centre), radius))
        else:
            _k, at, text, colour, anchor, size = item
            out.append(
                '\\node[anchor=%s,text=%s,inner sep=0pt,'
                'font=\\fontsize{%g}{%g}\\selectfont] at (%.2f,%.2f) {%s};'
                % (_TIKZ_ANCHOR.get(anchor, 'base west'), names[colour],
                   size, size * 1.2, *_flip(drawing, at), tex_escape(text)))
    out.append('\\end{tikzpicture}')
    return _wrap(out, standalone, 'tikz')


def svg_to_pgf(svg_text: str, standalone: bool = False) -> str:
    """The picture in *svg_text* as plain PGF commands (``pgfpicture``)."""
    drawing = _parse_svg(svg_text)
    names = _palette(drawing)
    out = ['% FoamMesh picture: needs \\usepackage{pgf}']
    out += _definitions(names)
    out.append('\\begin{pgfpicture}')
    out.append('\\pgfsetxvec{\\pgfpoint{1pt}{0pt}}')
    out.append('\\pgfsetyvec{\\pgfpoint{0pt}{1pt}}')
    out.append('\\pgfpathrectangle{\\pgfpointorigin}{\\pgfpoint{%gpt}{%gpt}}'
               % (drawing.width, drawing.height))
    out.append('\\pgfusepath{use as bounding box}')
    if drawing.background is not None:
        out.append('\\begin{pgfscope}\\pgfsetfillcolor{%s}'
                   '\\pgfpathrectangle{\\pgfpointorigin}'
                   '{\\pgfpoint{%gpt}{%gpt}}\\pgfusepath{fill}'
                   '\\end{pgfscope}'
                   % (names[drawing.background], drawing.width,
                      drawing.height))

    def point(p):
        x, y = _flip(drawing, p)
        return '\\pgfpoint{%.2fpt}{%.2fpt}' % (x, y)

    for item in drawing.items:
        kind = item[0]
        if kind == 'polygon':
            _k, points, fill, edge, width = item
            body = ['\\begin{pgfscope}', '\\pgfsetroundjoin',
                    '\\pgfsetlinewidth{%.2fpt}' % width]
            if fill is not None:
                body.append('\\pgfsetfillcolor{%s}' % names[fill])
            if edge is not None:
                body.append('\\pgfsetstrokecolor{%s}' % names[edge])
            body.append('\\pgfpathmoveto{%s}' % point(points[0]))
            body += ['\\pgfpathlineto{%s}' % point(p) for p in points[1:]]
            body.append('\\pgfpathclose')
            use = ','.join(([] if fill is None else ['fill'])
                           + ([] if edge is None else ['stroke'])) or 'stroke'
            body += ['\\pgfusepath{%s}' % use, '\\end{pgfscope}']
            out.append(''.join(body))
        elif kind == 'line':
            _k, points, colour, width = item
            out.append('\\begin{pgfscope}\\pgfsetlinewidth{%.2fpt}'
                       '\\pgfsetstrokecolor{%s}\\pgfpathmoveto{%s}'
                       '\\pgfpathlineto{%s}\\pgfusepath{stroke}'
                       '\\end{pgfscope}'
                       % (width, names[colour], point(points[0]),
                          point(points[1])))
        elif kind == 'circle':
            _k, centre, radius, colour = item
            out.append('\\begin{pgfscope}\\pgfsetfillcolor{%s}'
                       '\\pgfpathcircle{%s}{%.2fpt}\\pgfusepath{fill}'
                       '\\end{pgfscope}'
                       % (names[colour], point(centre), radius))
        else:
            _k, at, text, colour, anchor, size = item
            x, y = _flip(drawing, at)
            out.append('\\begin{pgfscope}\\color{%s}'
                       '\\pgftext[%s,x=%.2fpt,y=%.2fpt]'
                       '{\\fontsize{%g}{%g}\\selectfont %s}\\end{pgfscope}'
                       % (names[colour], _PGF_ALIGN.get(anchor, 'base,left'),
                          x, y, size, size * 1.2, tex_escape(text)))
    out.append('\\end{pgfpicture}')
    return _wrap(out, standalone, 'pgf')


def _wrap(lines: list, standalone: bool, package: str) -> str:
    if not standalone:
        return '\n'.join(lines) + '\n'
    return '\n'.join(['\\documentclass[border=2pt]{standalone}',
                      '\\usepackage{xcolor}', '\\usepackage{%s}' % package,
                      '\\begin{document}', *lines, '\\end{document}']) + '\n'


def write_vector_text(svg_path, path, fmt) -> Path:
    """Convert the SVG file at *svg_path* to ``.pgf`` or ``.tikz``."""
    item = format_for(fmt)
    if item.id not in VECTOR_TEXT:
        raise ImageFormatError(f'{item.id} is not PGF or TikZ')
    text = Path(svg_path).read_text(encoding='utf-8')
    body = svg_to_tikz(text) if item.id == 'TIKZ' else svg_to_pgf(text)
    target = Path(path).with_suffix(item.suffix)
    target.write_text(body, encoding='utf-8')
    return target


def write_scene(scene, path, fmt) -> Path:
    """PGF or TikZ straight from a vector-export ``Scene`` (the SVG it would
    write is the source, so the three agree)."""
    import tempfile

    from foammesh.core.gmsh import vector_export

    with tempfile.TemporaryDirectory() as folder:
        svg = vector_export.write_svg(scene, Path(folder) / 'scene.svg')
        return write_vector_text(svg, path, fmt)


# --------------------------------------------------------------------------- #
# LaTeX .tex: the text layer, from GL2PS
# --------------------------------------------------------------------------- #

def write_tex(render_window, path, companion='EPS') -> tuple:
    """Write ``<stem>.tex`` (the labels of the view, by ``vtkGL2PSExporter``
    in TeX mode) and the picture it goes with, ``<stem>.eps`` or ``.pdf``.

    GL2PS names the picture by the absolute path of the file it was asked to
    write; the path is cut to the bare stem so the pair works wherever it is
    copied. Returns ``(tex path, picture path)``.
    """
    import vtk

    target = Path(path).with_suffix('.tex')
    companion = str(companion).upper()
    if companion not in ('EPS', 'PDF'):
        raise ImageFormatError('a .tex goes with an EPS or a PDF')
    exporter = vtk.vtkGL2PSExporter()
    exporter.SetRenderWindow(render_window)
    exporter.SetFilePrefix(str(target.with_suffix('')))
    exporter.SetFileFormatToTeX()
    exporter.CompressOff()
    exporter.SetTextAsPath(0)
    exporter.Write()
    if not target.is_file():
        raise ImageFormatError('GL2PS wrote no .tex file')
    text = target.read_text(encoding='utf-8', errors='replace')
    text = re.sub(r'\\includegraphics(\[[^\]]*\])?\{[^}]*\}',
                  lambda m: '\\includegraphics%s{%s}' % (m.group(1) or '',
                                                         target.stem), text)
    target.write_text(text, encoding='utf-8')
    from foammesh.core.gmsh import session_display

    picture = session_display.export_view(
        render_window, target.with_suffix('.' + companion.lower()), companion)
    return target, Path(picture)


# --------------------------------------------------------------------------- #
# Movie
# --------------------------------------------------------------------------- #

def find_ffmpeg(distro: str = 'OpenFOAM13Runtime', runner=None):
    """``(argv prefix, where)`` for ffmpeg on the host or in the distro, or
    ``(None, reason)``. The prefix is a list: ``['ffmpeg']`` or
    ``['wsl.exe', '-d', distro, '--exec', 'ffmpeg']``."""
    found = shutil.which('ffmpeg')
    if found:
        return [found], 'host'
    wsl = shutil.which('wsl.exe')
    if wsl:
        run = runner or subprocess.run
        try:
            done = run([wsl, '-d', distro, '--', 'sh', '-c',
                        'command -v ffmpeg'], capture_output=True,
                       timeout=60)
        except (OSError, subprocess.SubprocessError):
            done = None
        if done is not None and done.returncode == 0 and done.stdout.strip():
            # --exec, not --: wsl.exe hands the words after -- to a shell,
            # which breaks on the brackets of the scale filter.
            return [wsl, '-d', distro, '--exec', 'ffmpeg'], distro
        return None, (f'ffmpeg is not installed on this machine or in the '
                      f'WSL distro {distro}')
    return None, 'ffmpeg is not installed on this machine'


def movie_refusal(distro: str = 'OpenFOAM13Runtime', runner=None) -> str:
    """Why MP4 cannot be recorded, or ''."""
    prefix, where = find_ffmpeg(distro, runner)
    if prefix is None:
        return f'MP4 needs ffmpeg: {where}. Record to PNG frames instead.'
    return ''


def _distro_path(path: Path) -> str:
    text = str(path.resolve()).replace('\\', '/')
    if len(text) > 1 and text[1] == ':':
        return '/mnt/' + text[0].lower() + text[2:]
    return text


def movie_command(prefix: list, frame_pattern: str, output, fps: float = 25.0,
                  quality: int = 18) -> list:
    """The ffmpeg command line for numbered frames (``frame_%06d.png``) to an
    H.264 MP4 with even sides (``yuv420p`` needs them)."""
    if not 1 <= float(fps) <= 240:
        raise ImageFormatError('the frame rate is between 1 and 240 per '
                               'second')
    if not 0 <= int(quality) <= 51:
        raise ImageFormatError('the quality (crf) is between 0 and 51')
    wsl = prefix[0].lower().endswith('wsl.exe')
    frames = _distro_path(Path(frame_pattern)) if wsl else str(frame_pattern)
    out = _distro_path(Path(output)) if wsl else str(output)
    return [*prefix, '-y', '-framerate', '%g' % float(fps), '-i', frames,
            '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-c:v', 'libx264',
            '-crf', str(int(quality)), '-pix_fmt', 'yuv420p', out]


def encode_mp4(frame_pattern, output, fps: float = 25.0, quality: int = 18,
               distro: str = 'OpenFOAM13Runtime', runner=None) -> Path:
    """Encode numbered frames to an MP4, or refuse with the reason."""
    prefix, where = find_ffmpeg(distro, runner)
    if prefix is None:
        raise ImageFormatError(
            f'MP4 needs ffmpeg: {where}. Record to PNG frames instead.')
    command = movie_command(prefix, frame_pattern, output, fps, quality)
    run = runner or subprocess.run
    done = run(command, capture_output=True, timeout=3600)
    if done.returncode != 0 or not Path(output).is_file():
        tail = (done.stderr or b'')[-400:].decode('utf-8', 'replace')
        raise ImageFormatError(f'ffmpeg failed ({done.returncode}): {tail}')
    return Path(output)
