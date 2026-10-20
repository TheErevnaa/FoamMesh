"""The viewport as a vector drawing: SVG, PDF, EPS and PostScript.

Plan 41 GN17 (F-17).  Gmsh's *File > Export* writes the model view as SVG, PDF,
EPS or PS through GL2PS.  VTK's own GL2PS exporter cannot do that for a 3D
scene: on this build it writes the whole picture as one embedded bitmap.  So
the picture is built here from the geometry on screen: every polygon, line,
point and label of the renderer's actors is projected through the camera,
sorted far to near (the painter's algorithm GL2PS also uses for its simple
sort) and written as paths, so the file stays sharp at any size.

Only numpy and VTK's data objects are used; no Qt, no render pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from foammesh.rendering import label_style


@dataclass
class Scene:
    """What is drawn, in screen coordinates (origin top-left, y down)."""

    width: int
    height: int
    background: tuple = (1.0, 1.0, 1.0)
    polygons: list = field(default_factory=list)   # (depth, pts(n,2), fill, edge, edge_w)
    lines: list = field(default_factory=list)      # (depth, pts(2,2), rgb, width)
    points: list = field(default_factory=list)     # (depth, xy, rgb, size)
    #: (depth, xy, text, rgb, anchor, size_px); anchor is 'middle', 'start' or 'end'.
    labels: list = field(default_factory=list)
    #: The label font key of ``foammesh.rendering.label_style`` (Plan 43 LY15).
    font: str = 'pretendard'
    #: SVG only: leave the background clear.
    transparent: bool = False

    def counts(self) -> dict:
        return {'polygons': len(self.polygons), 'lines': len(self.lines),
                'points': len(self.points), 'labels': len(self.labels)}


def _screen(matrix: np.ndarray, points: np.ndarray, width: int, height: int):
    homogeneous = np.concatenate([points, np.ones((len(points), 1))], axis=1)
    clip = homogeneous @ matrix.T
    w = np.where(np.abs(clip[:, 3]) > 1e-300, clip[:, 3], 1e-300)
    ndc = clip[:, :3] / w[:, None]
    x = (ndc[:, 0] + 1.0) * 0.5 * width
    y = (1.0 - (ndc[:, 1] + 1.0) * 0.5) * height
    return np.stack([x, y], axis=1), ndc[:, 2]


def _rgb(value) -> tuple:
    return tuple(float(c) for c in value)


def scene_of(renderer, width: int, height: int, *, font: str | None = None,
             hide=(), transparent: bool = False) -> Scene:
    """Project every visible actor of *renderer* into a :class:`Scene`.

    *font* is the label font key (default: the user's); *hide* are props left
    out of the drawing (an Include box that is off).
    """
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    camera = renderer.GetActiveCamera()
    matrix = np.array([[camera.GetCompositeProjectionTransformMatrix(
        width / float(height), -1, 1).GetElement(row, column)
        for column in range(4)] for row in range(4)])
    if font is None:
        font = label_style.load().font
    scene = Scene(width, height, _rgb(renderer.GetBackground()), font=font,
                  transparent=bool(transparent))
    props = renderer.GetViewProps()
    props.InitTraversal()
    for index in range(props.GetNumberOfItems()):
        prop = props.GetItemAsObject(index)
        if not prop.GetVisibility() or any(prop is other for other in hide):
            continue
        if prop.IsA('vtkScalarBarActor'):
            _scalar_bar(scene, renderer, prop)
            continue
        if prop.IsA('vtkTextActor'):
            _text_actor(scene, renderer, prop)
            continue
        if prop.IsA('vtkBillboardTextActor3D'):
            _billboard(scene, prop, matrix)
            continue
        mapper = prop.GetMapper() if hasattr(prop, 'GetMapper') else None
        if mapper is None:
            continue
        if mapper.IsA('vtkLabeledDataMapper'):
            _labels(scene, mapper, matrix)
            continue
        if prop.IsA('vtkActor2D'):
            # DP-2850. A 2-D overlay (the worst-cell markers, a rubber band)
            # is drawn in viewport pixels with a vtkProperty2D, which has no
            # representation or edges: projecting it through the camera
            # raised AttributeError and the SVG snapshot answered HTTP 500.
            if mapper.IsA('vtkPolyDataMapper2D'):
                _overlay(scene, renderer, prop, mapper, vtk_to_numpy)
            continue
        data = mapper.GetInput() if hasattr(mapper, 'GetInput') else None
        if not isinstance(data, vtkPolyData) or data.GetNumberOfPoints() == 0:
            continue
        _geometry(scene, prop, mapper, data, matrix, vtk_to_numpy)
    return scene


def _cell_blocks(cells, vtk_to_numpy):
    """A vtkCellArray as ``{width: (n, width) ids}`` plus the cell order."""
    if cells.GetNumberOfCells() == 0:
        return {}
    offsets = vtk_to_numpy(cells.GetOffsetsArray()).astype(np.int64)
    conn = vtk_to_numpy(cells.GetConnectivityArray()).astype(np.int64)
    sizes = np.diff(offsets)
    blocks = {}
    for width in np.unique(sizes):
        ids = np.nonzero(sizes == width)[0]
        gather = offsets[ids][:, None] + np.arange(width)[None, :]
        blocks[int(width)] = (ids, conn[gather])
    return blocks


def _geometry(scene: Scene, actor, mapper, data, matrix, vtk_to_numpy) -> None:
    prop = actor.GetProperty()
    points = vtk_to_numpy(data.GetPoints().GetData()).astype(np.float64)
    xy, depth = _screen(matrix, points, scene.width, scene.height)
    base = _rgb(prop.GetColor())
    colours = None
    scalars = data.GetCellData().GetScalars()
    if mapper.GetScalarVisibility() and scalars is not None and \
            scalars.GetNumberOfComponents() >= 3:
        colours = vtk_to_numpy(scalars)[:, :3].astype(float) / 255.0
    wireframe = prop.GetRepresentation() == 1
    edge = _rgb(prop.GetEdgeColor()) if prop.GetEdgeVisibility() else None
    width = float(prop.GetLineWidth())
    # polygons occupy cell ids after the verts and lines in a vtkPolyData
    first_poly = data.GetNumberOfVerts() + data.GetNumberOfLines()
    for size, (ids, block) in _cell_blocks(data.GetPolys(), vtk_to_numpy).items():
        for row, cell in zip(block, ids):
            fill = (tuple(colours[first_poly + cell]) if colours is not None
                    and first_poly + cell < len(colours) else base)
            corners = xy[row]
            if np.any(~np.isfinite(corners)):
                continue
            scene.polygons.append((float(depth[row].mean()), corners,
                                   None if wireframe else fill,
                                   fill if wireframe else edge, width))
    first_line = data.GetNumberOfVerts()
    for size, (ids, block) in _cell_blocks(data.GetLines(), vtk_to_numpy).items():
        for row, cell in zip(block, ids):
            colour = (tuple(colours[first_line + cell]) if colours is not None
                      and first_line + cell < len(colours) else base)
            for a, b in zip(row[:-1], row[1:]):
                scene.lines.append((float(depth[[a, b]].mean()),
                                    xy[[a, b]], colour, width))
    for size, (ids, block) in _cell_blocks(data.GetVerts(), vtk_to_numpy).items():
        for row in block:
            for node in row:
                scene.points.append((float(depth[node]), xy[node], base,
                                     float(prop.GetPointSize())))


def _overlay(scene: Scene, renderer, actor, mapper, vtk_to_numpy) -> None:
    """A 2-D polydata overlay, in front of the 3-D scene.

    Its points are viewport pixels (or pass through the mapper's transform
    coordinate). There is no alpha in the vector writers, so a translucent
    fill is drawn as its outline rather than hiding the model under it.
    """
    from vtkmodules.vtkCommonDataModel import vtkPolyData

    data = mapper.GetInput()
    if not isinstance(data, vtkPolyData) or data.GetNumberOfPoints() == 0:
        return
    points = vtk_to_numpy(data.GetPoints().GetData()).astype(np.float64)
    transform = mapper.GetTransformCoordinate()
    if transform is not None:
        xy = []
        for x, y, z in points:
            transform.SetValue(float(x), float(y), float(z))
            xy.append(transform.GetComputedDoubleViewportValue(renderer)[:2])
        xy = np.array(xy, dtype=np.float64).reshape(-1, 2)
    else:
        offset = actor.GetPositionCoordinate()             .GetComputedDoubleViewportValue(renderer)
        xy = points[:, :2] + np.array(offset[:2], dtype=np.float64)
    xy = np.stack([xy[:, 0], scene.height - xy[:, 1]], axis=1)
    prop = actor.GetProperty()
    colour = _rgb(prop.GetColor())
    width = float(prop.GetLineWidth())
    opaque = float(prop.GetOpacity()) >= 0.99
    for _size, (_ids, block) in _cell_blocks(data.GetPolys(),
                                             vtk_to_numpy).items():
        for row in block:
            corners = xy[row]
            if np.all(np.isfinite(corners)):
                scene.polygons.append((-2.0, corners,
                                       colour if opaque else None,
                                       None if opaque else colour, width))
    for _size, (_ids, block) in _cell_blocks(data.GetLines(),
                                             vtk_to_numpy).items():
        for row in block:
            for a, b in zip(row[:-1], row[1:]):
                pair = xy[[a, b]]
                if np.all(np.isfinite(pair)):
                    scene.lines.append((-2.0, pair, colour, width))
    for _size, (_ids, block) in _cell_blocks(data.GetVerts(),
                                             vtk_to_numpy).items():
        for row in block:
            for node in row:
                scene.points.append((-2.0, xy[node], colour,
                                     float(prop.GetPointSize())))


def _labels(scene: Scene, mapper, matrix) -> None:
    from vtkmodules.util.numpy_support import vtk_to_numpy

    data = mapper.GetInput()
    name = mapper.GetFieldDataName()
    array = data.GetPointData().GetAbstractArray(name) if name else None
    if array is None:
        return
    points = vtk_to_numpy(data.GetPoints().GetData()).astype(np.float64)
    xy, depth = _screen(matrix, points, scene.width, scene.height)
    style = mapper.GetLabelTextProperty()
    colour = _rgb(style.GetColor())
    for index in range(len(points)):
        if np.all(np.isfinite(xy[index])):
            scene.labels.append((float(depth[index]), xy[index],
                                 str(array.GetValue(index)), colour,
                                 'middle', float(style.GetFontSize())))


def _lines_at(scene: Scene, xy_bottom, text: str, style) -> None:
    """A (possibly multi-line) text, anchored by its text property."""
    size = float(style.GetFontSize())
    lines = str(text).split('\n')
    lead = size * 1.2
    horizontal = {0: 'start', 1: 'middle', 2: 'end'}.get(
        int(style.GetJustification()), 'start')
    vertical = int(style.GetVerticalJustification())      # 0 bottom 1 mid 2 top
    block = lead * len(lines)
    bottom = xy_bottom[1]
    if vertical == 1:
        bottom += block / 2.0
    elif vertical == 2:
        bottom += block
    colour = _rgb(style.GetColor())
    for number, line in enumerate(lines):
        baseline = bottom - 0.2 * size - (len(lines) - 1 - number) * lead
        scene.labels.append((-2.0, np.array([xy_bottom[0], baseline]), line,
                             colour, horizontal, size))


def _text_actor(scene: Scene, renderer, actor) -> None:
    text = actor.GetInput()
    if not text:
        return
    x, y = actor.GetPositionCoordinate().GetComputedDoubleViewportValue(renderer)
    _lines_at(scene, (x, scene.height - y), text, actor.GetTextProperty())


def _billboard(scene: Scene, actor, matrix) -> None:
    text = actor.GetInput()
    if not text:
        return
    xy, _depth = _screen(matrix, np.array([actor.GetPosition()], dtype=float),
                         scene.width, scene.height)
    if np.all(np.isfinite(xy[0])):
        _lines_at(scene, xy[0], text, actor.GetTextProperty())


def _scalar_bar(scene: Scene, renderer, bar) -> None:
    """The colour bar as strips and its title and tick labels as text."""
    lut = bar.GetLookupTable()
    if lut is None:
        return
    x1, y1 = bar.GetPositionCoordinate().GetComputedDoubleViewportValue(renderer)
    x2, y2 = bar.GetPosition2Coordinate().GetComputedDoubleViewportValue(renderer)
    if x2 < x1 or y2 < y1:                 # Position2 is relative to Position
        x2, y2 = x1 + x2, y1 + y2
    left, right = x1, x2
    top, bottom = scene.height - y2, scene.height - y1
    width, height = right - left, bottom - top
    if width <= 1 or height <= 1:
        return
    title_style, label_text = bar.GetTitleTextProperty(), bar.GetLabelTextProperty()
    title_size, label_size = (float(title_style.GetFontSize()),
                              float(label_text.GetFontSize()))
    title = bar.GetTitle() or ''
    head = title_size * 1.4 if title else 0.0
    if title:
        scene.labels.append((-2.0, np.array([left + width / 2.0,
                                             top + title_size]),
                             title, _rgb(title_style.GetColor()), 'middle',
                             title_size))
    low, high = lut.GetRange()
    vertical = int(bar.GetOrientation()) == 1
    ratio = max(min(float(bar.GetBarRatio()), 1.0), 0.05)
    steps = 64
    if vertical:
        bar_width = max(width * ratio, 6.0)
        bar_top = top + head + label_size / 2.0
        bar_bottom = bottom - label_size / 2.0
        slab = (bar_bottom - bar_top) / steps
        for step in range(steps):
            value = low + (high - low) * (step + 0.5) / steps
            colour = tuple(lut.GetColor(value))
            y_top = bar_bottom - (step + 1) * slab
            corners = np.array([[left, y_top], [left + bar_width, y_top],
                                [left + bar_width, y_top + slab + 0.3],
                                [left, y_top + slab + 0.3]])
            scene.polygons.append((-2.0, corners, colour, None, 0.0))
    else:
        bar_height = max(height * ratio, 6.0)
        bar_top = top + head
        bar_left, bar_right = left + label_size, right - label_size
        slab = (bar_right - bar_left) / steps
        for step in range(steps):
            value = low + (high - low) * (step + 0.5) / steps
            colour = tuple(lut.GetColor(value))
            x_left = bar_left + step * slab
            corners = np.array([[x_left, bar_top], [x_left + slab + 0.3, bar_top],
                                [x_left + slab + 0.3, bar_top + bar_height],
                                [x_left, bar_top + bar_height]])
            scene.polygons.append((-2.0, corners, colour, None, 0.0))
    count = max(int(bar.GetNumberOfLabels()), 2)
    fmt = bar.GetLabelFormat() or '%g'
    label_colour = _rgb(label_text.GetColor())
    for number in range(count):
        fraction = number / (count - 1)
        value = low + (high - low) * fraction
        try:
            text = fmt % value
        except (TypeError, ValueError):
            text = '%g' % value
        if vertical:
            point = np.array([left + bar_width + 4.0,
                              bar_bottom - fraction * (bar_bottom - bar_top)
                              + 0.35 * label_size])
            scene.labels.append((-2.0, point, text, label_colour, 'start',
                                 label_size))
        else:
            point = np.array([bar_left + fraction * (bar_right - bar_left),
                              bar_top + bar_height + label_size * 1.1])
            scene.labels.append((-2.0, point, text, label_colour, 'middle',
                                 label_size))


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #

def _ordered(scene: Scene):
    """Primitives far to near (larger NDC depth is farther)."""
    items = ([(d, 0, i) for i, (d, *_r) in enumerate(scene.polygons)]
             + [(d, 1, i) for i, (d, *_r) in enumerate(scene.lines)]
             + [(d, 2, i) for i, (d, *_r) in enumerate(scene.points)])
    items.sort(key=lambda item: (-item[0], item[1]))
    return items


def _label_order(scene: Scene):
    """Labels far to near, so the nearest text is written last."""
    return sorted(scene.labels, key=lambda label: -label[0])


def _ps_escape(text: str) -> str:
    return text.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def _hex(rgb) -> str:
    return '#%02x%02x%02x' % tuple(int(round(max(0, min(1, c)) * 255))
                                   for c in rgb)


def _escape(text: str) -> str:
    return (text.replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;'))


def write_svg(scene: Scene, path) -> Path:
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d">' % (scene.width, scene.height, scene.width,
                                     scene.height)]
    if not scene.transparent:
        out.append('<rect width="100%%" height="100%%" fill="%s"/>'
                   % _hex(scene.background))
    for _depth, kind, index in _ordered(scene):
        if kind == 0:
            _d, pts, fill, edge, width = scene.polygons[index]
            text = ' '.join('%.2f,%.2f' % tuple(p) for p in pts)
            out.append('<polygon points="%s" fill="%s" stroke="%s" '
                       'stroke-width="%.2f" stroke-linejoin="round"/>'
                       % (text, _hex(fill) if fill else 'none',
                          _hex(edge) if edge else (_hex(fill) if fill
                                                   else 'none'),
                          width if edge else 0.4))
        elif kind == 1:
            _d, pts, colour, width = scene.lines[index]
            out.append('<line x1="%.2f" y1="%.2f" x2="%.2f" y2="%.2f" '
                       'stroke="%s" stroke-width="%.2f"/>'
                       % (*pts[0], *pts[1], _hex(colour), width))
        else:
            _d, xy, colour, size = scene.points[index]
            out.append('<circle cx="%.2f" cy="%.2f" r="%.2f" fill="%s"/>'
                       % (*xy, size / 2.0, _hex(colour)))
    family = _escape(label_style.SVG_FAMILY.get(scene.font, 'Helvetica'))
    for _d, xy, text, colour, anchor, size in _label_order(scene):
        out.append('<text x="%.2f" y="%.2f" font-family="%s" '
                   'font-size="%.1f" text-anchor="%s" fill="%s">%s</text>'
                   % (*xy, family, size, anchor, _hex(colour), _escape(text)))
    out.append('</svg>')
    path = Path(path)
    path.write_text('\n'.join(out), encoding='utf-8')
    return path


def _ps_body(scene: Scene) -> list:
    """Page-space PostScript (y up) for the EPS and PS writers."""
    height = scene.height
    out = ['%g %g %g setrgbcolor 0 0 %d %d rectfill'
           % (*scene.background, scene.width, scene.height),
           '1 setlinejoin']
    for _depth, kind, index in _ordered(scene):
        if kind == 0:
            _d, pts, fill, edge, width = scene.polygons[index]
            path = 'newpath %.2f %.2f moveto ' % (pts[0][0], height - pts[0][1])
            path += ' '.join('%.2f %.2f lineto' % (p[0], height - p[1])
                             for p in pts[1:]) + ' closepath'
            if fill:
                out.append('%g %g %g setrgbcolor %s fill' % (*fill, path))
            colour = edge or fill
            if colour:
                out.append('%g %g %g setrgbcolor %.2f setlinewidth %s stroke'
                           % (*colour, width if edge else 0.4, path))
        elif kind == 1:
            _d, pts, colour, width = scene.lines[index]
            out.append('%g %g %g setrgbcolor %.2f setlinewidth newpath '
                       '%.2f %.2f moveto %.2f %.2f lineto stroke'
                       % (*colour, width, pts[0][0], height - pts[0][1],
                          pts[1][0], height - pts[1][1]))
        else:
            _d, xy, colour, size = scene.points[index]
            out.append('%g %g %g setrgbcolor newpath %.2f %.2f %.2f 0 360 '
                       'arc fill' % (*colour, xy[0], height - xy[1],
                                     size / 2.0))
    face = label_style.POSTSCRIPT_FONT.get(scene.font, 'Helvetica')
    for _d, xy, text, colour, anchor, size in _label_order(scene):
        safe = _ps_escape(text)
        shift = {'middle': '2 div neg', 'end': 'neg', 'start': '0 mul'}[anchor]
        out.append('/%s findfont %.1f scalefont setfont %g %g %g setrgbcolor '
                   '%.2f %.2f moveto (%s) dup stringwidth pop %s 0 rmoveto show'
                   % (face, size, *colour, xy[0], height - xy[1], safe, shift))
    return out


def write_eps(scene: Scene, path) -> Path:
    body = ['%!PS-Adobe-3.0 EPSF-3.0',
            '%%%%BoundingBox: 0 0 %d %d' % (scene.width, scene.height),
            '%%Creator: FoamMesh', '%%EndComments', *_ps_body(scene),
            'showpage', '%%EOF']
    path = Path(path)
    path.write_text('\n'.join(body) + '\n', encoding='latin-1', errors='replace')
    return path


def write_ps(scene: Scene, path) -> Path:
    body = ['%!PS-Adobe-3.0', '%%%%BoundingBox: 0 0 %d %d'
            % (scene.width, scene.height), '%%Creator: FoamMesh',
            '%%Pages: 1', '%%EndComments', '%%Page: 1 1', *_ps_body(scene),
            'showpage', '%%EOF']
    path = Path(path)
    path.write_text('\n'.join(body) + '\n', encoding='latin-1', errors='replace')
    return path


def write_pdf(scene: Scene, path) -> Path:
    """A one-page PDF whose content stream is the same path operators."""
    height = scene.height
    ops = ['%g %g %g rg 0 0 %d %d re f' % (*scene.background, scene.width,
                                           scene.height), '1 j']
    for _depth, kind, index in _ordered(scene):
        if kind == 0:
            _d, pts, fill, edge, width = scene.polygons[index]
            path_ops = '%.2f %.2f m ' % (pts[0][0], height - pts[0][1])
            path_ops += ' '.join('%.2f %.2f l' % (p[0], height - p[1])
                                 for p in pts[1:]) + ' h'
            if fill:
                ops.append('%g %g %g rg %s f' % (*fill, path_ops))
            colour = edge or fill
            if colour:
                ops.append('%g %g %g RG %.2f w %s S'
                           % (*colour, width if edge else 0.4, path_ops))
        elif kind == 1:
            _d, pts, colour, width = scene.lines[index]
            ops.append('%g %g %g RG %.2f w %.2f %.2f m %.2f %.2f l S'
                       % (*colour, width, pts[0][0], height - pts[0][1],
                          pts[1][0], height - pts[1][1]))
        else:
            _d, xy, colour, size = scene.points[index]
            r = size / 2.0
            ops.append('%g %g %g rg %.2f %.2f %.2f %.2f re f'
                       % (*colour, xy[0] - r, height - xy[1] - r, size, size))
    # Base-14 faces have a known average advance, so the anchor can be set.
    advance = {'Helvetica': 0.52, 'Times-Roman': 0.46, 'Courier': 0.6}
    face = label_style.POSTSCRIPT_FONT.get(scene.font, 'Helvetica')
    for _d, xy, text, colour, anchor, size in _label_order(scene):
        run = advance[face] * size * len(text)
        x = xy[0] - {'middle': run / 2.0, 'end': run, 'start': 0.0}[anchor]
        ops.append('BT /F1 %.1f Tf %g %g %g rg %.2f %.2f Td (%s) Tj ET'
                   % (size, *colour, x, height - xy[1], _ps_escape(text)))
    stream = '\n'.join(ops).encode('latin-1', 'replace')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents 4 0 R '
         '/Resources << /Font << /F1 5 0 R >> >> >>'
         % (scene.width, scene.height)).encode(),
        b'<< /Length %d >>\nstream\n' % len(stream) + stream
        + b'\nendstream',
        ('<< /Type /Font /Subtype /Type1 /BaseFont /%s >>' % face).encode(),
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number + body + b'\nendobj\n'
    start = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objects) + 1)
    for offset in offsets:
        out += b'%010d 00000 n \n' % offset
    out += (b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
            % (len(objects) + 1, start))
    path = Path(path)
    path.write_bytes(bytes(out))
    return path


WRITERS = {'SVG': ('.svg', write_svg), 'PDF': ('.pdf', write_pdf),
           'EPS': ('.eps', write_eps), 'PS': ('.ps', write_ps)}
