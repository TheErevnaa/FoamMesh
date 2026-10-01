"""The farfield, drawn apart from the background block it sits in.

Plan 37 UF14. With a farfield the computational domain is two things: the
block ``blockMesh`` fills (``domain_box_actor``) and, inside it, the closed
box, sphere or cylinder snappy cuts it to (the ``far_field`` patch). Drawing
only the block would say the mesh is a box when it is a sphere; drawing only
the farfield would hide the block the seeds and the cell size live in. So
both are drawn, each in its own colour and each labelled.

The farfield is a faint translucent skin with its outline
(the tessellation's feature edges), in the theme's ``status.warning`` colour so it
never reads as the accent-tinted block, and a label anchored on its top
reading "Outer boundary (farfield) · far_field". Neither part can be picked:
a click must still reach the geometry inside.
"""
from __future__ import annotations

#: The skin is fainter than the block's walls: it is the thing looked through.
SURFACE_OPACITY = 0.06
EDGE_ANGLE = 30.0
FONT_SIZE = 12

_FALLBACK_COLOUR = '#d08c2b'
_FALLBACK_TEXT = '#e8ebef'
_FALLBACK_BACKGROUND = '#1f2328'


def _token(name: str, fallback: str) -> str:
    try:
        from foammesh.app import app

        tokens = app.themeManager.tokens if app.themeManager else None
        value = tokens.values.get(name) if tokens is not None else None
        if value:
            return str(value)
    except Exception:                                         # noqa: BLE001
        pass
    return fallback


def label_text(shape: str, engine: str = 'snappy') -> str:
    """What the label says: the role, the shape and the patch it becomes.

    The patch names are the engine's: snappy publishes one ``far_field``
    patch on every shape, Gmsh names the primitive's faces (the Geometry
    page draws the farfield for whichever engine the case has chosen).
    """
    from foammesh.core.mesh.farfield_spec import GMSH, ROLE_TITLE, primitives
    from foammesh.core.mesh.snappy_farfield import PATCH

    if str(engine or '').strip().lower() == GMSH:
        names = tuple(primitives().patch_names(shape))
        if len(names) == 1:
            return f'{ROLE_TITLE} · {shape} · patch {names[0]}'
        listed = (', '.join(names) if len(names) <= 3
                  else f'{names[0]} … {names[-1]}')
        return f'{ROLE_TITLE} · {shape} · patches {listed}'
    return f'{ROLE_TITLE} · {shape} · patch {PATCH}'


def label_anchor(primitive) -> tuple[float, float, float]:
    """The top of the farfield, above its middle, where the label sits."""
    from foammesh.core.mesh.snappy_farfield import primitive_bounds

    bounds = primitive_bounds(primitive)
    return ((bounds[0] + bounds[1]) / 2.0, (bounds[2] + bounds[3]) / 2.0,
            bounds[5])


def farfieldActor(primitive, colour: str | None = None):
    """A non-pickable ``vtkAssembly`` named ``farfield``: skin and outline.

    *primitive* is the resolved farfield (``Farfield.primitive``).
    """
    from vtkmodules.vtkFiltersCore import vtkFeatureEdges
    from vtkmodules.vtkRenderingCore import (
        vtkActor, vtkAssembly, vtkPolyDataMapper,
    )

    from foammesh.core.mesh.snappy_farfield import tessellate
    from foammesh.view.theming.vtk_theme import rgb

    tint = rgb(colour or _token('status.warning', _FALLBACK_COLOUR))
    surface = tessellate(primitive)

    mapper = vtkPolyDataMapper()
    mapper.SetInputData(surface)
    skin = vtkActor()
    skin.SetMapper(mapper)
    prop = skin.GetProperty()
    prop.SetColor(*tint)
    prop.SetOpacity(SURFACE_OPACITY)
    prop.SetLighting(False)
    skin.PickableOff()
    skin.SetObjectName('farfield:surface')

    edges = vtkFeatureEdges()
    edges.SetInputData(surface)
    edges.BoundaryEdgesOff()
    edges.NonManifoldEdgesOff()
    edges.ManifoldEdgesOff()
    edges.FeatureEdgesOn()
    edges.SetFeatureAngle(EDGE_ANGLE)
    edges.Update()
    edgeMapper = vtkPolyDataMapper()
    edgeMapper.SetInputData(edges.GetOutput())
    outline = vtkActor()
    outline.SetMapper(edgeMapper)
    prop = outline.GetProperty()
    prop.SetColor(*tint)
    prop.SetLineWidth(1.5)
    prop.SetLighting(False)
    outline.PickableOff()
    outline.SetObjectName('farfield:edges')

    assembly = vtkAssembly()
    assembly.AddPart(skin)
    assembly.AddPart(outline)
    assembly.SetObjectName('farfield')
    assembly.PickableOff()
    return assembly


def farfieldLabelActor(primitive, engine: str = 'snappy'):
    """The label, a 2-D text actor at a world anchor on top of the farfield.

    Drawn in the overlay pass, like the region labels (DP-925), so the skin
    it names does not hide it.
    """
    from vtkmodules.vtkRenderingCore import vtkTextActor

    from foammesh.view.theming.vtk_theme import rgb

    actor = vtkTextActor()
    actor.SetInput(label_text(primitive['shape'], engine))
    anchor = actor.GetPositionCoordinate()
    anchor.SetCoordinateSystemToWorld()
    anchor.SetValue(*label_anchor(primitive))
    actor.SetObjectName('farfieldLabel')
    prop = actor.GetTextProperty()
    text = rgb(_token('tooltip.foreground', _FALLBACK_TEXT))
    prop.SetFontSize(FONT_SIZE)
    prop.SetColor(*text)
    prop.SetBackgroundColor(*rgb(
        _token('tooltip.background', _FALLBACK_BACKGROUND)))
    prop.SetBackgroundOpacity(0.75)
    prop.SetFrame(True)
    prop.SetFrameColor(*text)
    prop.SetJustificationToCentered()
    prop.SetVerticalJustificationToBottom()
    actor.PickableOff()
    return actor
