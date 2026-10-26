"""The Gmsh tool dock: what the tools do, with no window of its own.

Plan 48 GM11b (section 4.10.3).  The dock is the rail group "Gmsh tools"
(Gmsh route only).  This module holds the tools' state and actions;
:mod:`foammesh.view.gmsh_tools.tools_panel` is the form, and
:mod:`foammesh.view.gmsh_tools.mount` hangs both on the window.

Every provider is a callable so the dock follows the model on screen instead
of keeping a copy: ``view()`` the rendering widget, ``controller()`` the
resident Gmsh session's controller (or ``None``), ``source()`` the entity
panel the Pick tool is registered with, ``display()`` the Gmsh display panel
and ``client()`` the project's field client.
"""
from __future__ import annotations

from foammesh.view.gmsh_tools import bounding_boxes
from foammesh.view.gmsh_tools import overlays
from foammesh.view.gmsh_tools import point_placement

#: The colour modes of ``Mesh.ColorCarousel`` (Gmsh): partition is the fourth.
PARTITION_COLOUR = 3
GHOST_OPTION = 'Mesh.PartitionCreateGhostCells'

#: What the dock offers, in the order of plan section 4.10.3.  ``area`` says
#: where it is, so a test can check nothing in the table is unreachable.
TOOLS = (
    ('entities', 'Entities', 'mounted'),
    ('visibility', 'Visibility and display', 'mounted'),
    ('element_pick', 'Element picking', 'linked'),
    ('bounding_boxes', 'Bounding boxes', 'dock'),
    ('sweep_preview', 'Sweep and layer preview', 'dock'),
    ('transfinite_preview', 'Transfinite preview', 'dock'),
    ('partition_colour', 'Partition colouring', 'dock'),
    ('ghost_cells', 'Ghost cells', 'dock'),
    ('plugin_views', 'Plugin views', 'mounted'),
    ('point_placement', 'Point placement', 'dock'),
)


class GmshToolDock:
    """The state and actions of the Gmsh tool dock."""

    def __init__(self, *, view, controller=lambda: None,
                 source=lambda: None, display=lambda: None,
                 client=lambda: None, elements=lambda: None):
        self.view = view
        self.elements = elements
        self.controller = controller
        self.source = source
        self.display = display
        self.client = client
        self.bounding = bounding_boxes.BoundingBoxLayer(
            view, self.polydata, self.hidden)
        self.placement = point_placement.PlacementTool(
            view, self._place_point, self._model_actors)
        self.sweep = overlays.SegmentLayer(
            view, overlays.SWEEP_NAME, overlays.SWEEP_COLOUR, 3.0)
        self.reclassify = overlays.SegmentLayer(
            view, overlays.RECLASSIFY_NAME, overlays.RECLASSIFY_COLOUR, 3.0)
        self.qualify = overlays.QualificationLayer(
            view, self.polydata, self.entity_map)
        #: Told after something the form shows changed.
        self.listeners: list = []
        self._connected: list = []
        self.status = ''

    # -- what is on screen ------------------------------------------------ #

    def _picker(self):
        source = self.source()
        if source is None:
            return None
        try:
            return source.picker()
        except Exception:                             # noqa: BLE001
            return None

    def entity_map(self):
        picker = self._picker()
        return picker.map if picker is not None else None

    def polydata(self):
        """The ``geometry.vtp`` data of the model on screen, or ``None``."""
        source = self.source()
        entity_view = getattr(source, '_view', None)
        return getattr(entity_view, '_polydata', None)

    def hidden(self) -> set:
        panel = self.display()
        if panel is None:
            return set()
        try:
            return set(panel.visibilityModel().hidden)
        except Exception:                             # noqa: BLE001
            return set()

    def _model_actors(self) -> list:
        entity_view = getattr(self.source(), '_view', None)
        body = getattr(entity_view, '_body', None)
        return [body] if body is not None else []

    def _say(self, text: str) -> None:
        self.status = text
        self.changed()

    def changed(self) -> None:
        for listener in list(self.listeners):
            listener()

    # -- bounding boxes (Alt+B, Alt+Shift+1) ------------------------------ #

    def toggle_bounding_boxes(self) -> bool:
        shown = self.bounding.toggle()
        self._say('Bounding boxes: %s (%d entities).' % (
            'shown' if shown else 'hidden', len(self.bounding.boxes())))
        return shown

    def set_bounding_boxes(self, shown: bool) -> bool:
        shown = self.bounding.setShown(shown)
        self._say('Bounding boxes: %s (%d entities).' % (
            'shown' if shown else 'hidden', len(self.bounding.boxes())))
        return shown

    def reset_to_visible(self) -> tuple | None:
        bounds = self.bounding.resetToVisible()
        self._say('The view fits the visible entities.' if bounds else
                  'There are no visible entities to fit.')
        return bounds

    # -- point placement --------------------------------------------------- #

    def _place_point(self, values: dict) -> dict:
        controller = self.controller()
        if controller is None:
            raise ValueError('open a project first')
        result = controller.build('point', dict(values))
        refresh = getattr(self, 'after_build', None)
        if callable(refresh):
            refresh()
        return result if isinstance(result, dict) else {'result': result}

    def start_placement(self) -> bool:
        started = self.placement.start()
        self.changed()
        return started

    def stop_placement(self) -> None:
        self.placement.stop()
        self.changed()

    # -- partition colouring and ghost cells ------------------------------- #

    def _colour_combo(self):
        """The Colour by control of the Elements panel (the one control)."""
        panel = self.elements()
        return getattr(panel, '_colour', None)

    def set_partition_colour(self, on: bool) -> bool:
        """Colour the mesh by partition through the Elements panel's Colour
        by control (``Mesh.ColorCarousel`` 3); that control offers a colouring
        only when the mesh has more than one value of it."""
        combo = self._colour_combo()
        if combo is None:
            self._say('The Elements panel is not open on this case.')
            return False
        want = PARTITION_COLOUR if on else 1
        index = combo.findData(want)
        if index < 0:
            self._say('This mesh has not been partitioned: Partition is '
                      'offered after the mesh is partitioned.' if on else
                      'Colour by entity is not offered for this mesh.')
            return False
        combo.setCurrentIndex(index)
        self._say('Mesh colour: %s.' % ('by partition' if on else
                                        'by elementary entity'))
        return True

    def partition_colour(self) -> bool:
        combo = self._colour_combo()
        return combo is not None and combo.currentData() == PARTITION_COLOUR

    def set_ghost_cells(self, on: bool) -> bool:
        """Create ghost cells when partitioning (``Mesh.PartitionCreateGhostCells``)."""
        client = self.client()
        if client is None:
            self._say('Open a project first.')
            return False
        from foammesh.core.gmsh.option_catalogue import (
            OVERRIDES_FIELD, overrides_from_text, overrides_to_text)
        from foammesh.view import facade_client

        values = client.field_values((OVERRIDES_FIELD,))
        raw = values.get(OVERRIDES_FIELD)
        stored = overrides_from_text(getattr(raw, 'value', raw))
        if on:
            stored[GHOST_OPTION] = 1
        else:
            stored.pop(GHOST_OPTION, None)
        facade_client.submit(
            client, 'configuration.patch',
            {'patch': {OVERRIDES_FIELD: overrides_to_text(stored)}})
        self._say('Ghost cells: %s.' % ('on' if on else 'off'))
        return True

    def ghost_cells(self) -> bool:
        client = self.client()
        if client is None:
            return False
        try:
            from foammesh.core.gmsh.option_catalogue import (
                OVERRIDES_FIELD, overrides_from_text)
            raw = client.field_values((OVERRIDES_FIELD,)).get(OVERRIDES_FIELD)
            stored = overrides_from_text(getattr(raw, 'value', raw))
            return bool(int(float(stored.get(GHOST_OPTION, 0))))
        except Exception:                             # noqa: BLE001
            return False

    # -- the previews other panels compute --------------------------------- #

    def connect_previews(self, *panels) -> int:
        """Connect the panels that preview something to the viewport layers.

        Each panel is connected once; the number newly connected is returned.
        """
        count = 0
        for panel in panels:
            if panel is None or any(panel is held for held in self._connected):
                continue
            if hasattr(panel, 'sweepPreview'):
                panel.sweepPreview.connect(self.show_sweep)
                count += 1
            if hasattr(panel, 'qualificationChanged'):
                panel.qualificationChanged.connect(self.show_qualification)
                count += 1
            self._connected.append(panel)
        return count

    def show_sweep(self, segments) -> int:
        shown = self.sweep.show(segments)
        self._say('Sweep preview: %d segments.' % shown)
        return shown

    def show_qualification(self, colours) -> dict:
        painted = self.qualify.show(colours)
        self._say('Transfinite preview: %d entities painted.' % len(painted))
        return painted

    def show_reclassify_edges(self, result) -> int:
        """Draw the edges ``session.reclassify_edges`` returned."""
        edges = (result or {}).get('edges') or []
        shown = self.reclassify.show(edges)
        self._say('Reclassify 2D would cut along %d edges%s.' % (
            (result or {}).get('count', shown),
            ' (the first %d are drawn)' % shown
            if (result or {}).get('truncated') else ''))
        return shown

    def clear_previews(self) -> None:
        self.sweep.clear()
        self.qualify.clear()
        self.reclassify.clear()
        self._say('Previews cleared.')

    def state(self) -> dict:
        """What the form and the API show of the dock."""
        return {
            'bounding_boxes': {'shown': self.bounding.shown(),
                               'count': len(self.bounding.boxes())},
            'placement': {'active': self.placement.active(),
                          **self.placement.placement.state(),
                          'placed': len(self.placement.placed())},
            'partition_colour': self.partition_colour(),
            'ghost_cells': self.ghost_cells(),
            'sweep_segments': self.sweep.count,
            'transfinite_painted': len(self.qualify.painted),
            'reclassify_edges': self.reclassify.count,
            'status': self.status or self.placement.status(),
        }
