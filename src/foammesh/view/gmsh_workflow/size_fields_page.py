"""Gmsh workflow page: gmsh.size_fields."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.facade_client import query
from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import GmshTaskPage


class GmshSizeFieldsPage(GmshTaskPage):
    """Spatial refinement rows: distance thresholds and analytic regions.

    Which parameters matter depends on the field type -- a ball ignores the
    distance ramp, a distance threshold ignores the radius -- so the table
    shows the type first and the editor shows only the parameters that type
    uses. Plan 30 WP12 (F-43): the page used to render all of them and say
    that hiding the rest was "the register's job, not the page's". It was, and
    the register did not say; ``core.gmsh.fields.FIELD_PARAMETERS`` now does,
    pinned against the runner's own branches, and the editor reads it. The
    page still decides nothing.
    """

    task_id_default = 'gmsh.size_fields'

    COLUMNS = ('name', 'enabled', 'field_type', 'scope_token',
               'size_inside', 'size_outside', 'expression', 'priority')

    #: Plan 29 WP8. A per-surface size asks for two numbers, not eight, so it
    #: gets its own table rather than more mostly-empty columns on the one
    #: above.
    SURFACE_COLUMNS = ('name', 'enabled', 'surface_id', 'target_size',
                       'blend_distance', 'priority')

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'Fields combine as a minimum: at any point the finest requested '
            'size wins. A distance field needs a surface scope; box, ball, '
            'cylinder and frustum use their own coordinates -- a box is its '
            'two opposite corners, a frustum its start point, axis and a '
            'radius at each end; math_eval takes '
            'an expression in x, y and z and ignores the size numbers. An '
            'expression is checked and costed against the geometry before a '
            'run is accepted. Choose Max below to coarsen instead: under Min '
            'a request for a larger cell is outvoted by every other field. '
            'Restrict holds one size inside the scoped entities and nowhere '
            'else; curvature refines where the scoped surfaces bend. Each row '
            'shows only the parameters its own type uses.'), self)
        note.setWordWrap(True)
        layout.addWidget(note)

        self.panel = ChildControlPanel(
            self._client, 'gmsh.size_fields.controls',
            self.tr('Size fields'), columns=self.COLUMNS, parent=self)
        self.panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.panel)

        surfaceNote = QLabel(self.tr(
            'Per-surface sizes are the short way to say "finer here": pick an '
            'imported surface and a target size, and the mesh ramps back to '
            'the global size over the blend distance. Leave the blend distance '
            'at 0 to ramp over one global cell. Each row becomes a distance '
            'field, so it joins the same minimum as the fields above.'), self)
        surfaceNote.setWordWrap(True)
        layout.addWidget(surfaceNote)

        self.surface_panel = ChildControlPanel(
            self._client, 'gmsh.surface_sizes.controls',
            self.tr('Per-surface sizes'), columns=self.SURFACE_COLUMNS,
            parent=self, choices={'surface_id': self.surfaceChoices()})
        self.surface_panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.surface_panel)

    # -- naming the surfaces the dialog asks for --------------------------- #

    def preparedSurfaceNames(self) -> dict:
        """Gmsh surface tag (as a string) -> the patch name the user gave it.

        R60/R152. The dialog asked for a bare integer `Surface Id` and the page
        could only explain that surfaces are "numbered from 1 in import
        order" -- an ordering the app stops showing the moment the user renames
        the boundaries the workflow told them to rename. The prepared geometry
        already carries the join, so the page reads it instead of asking the
        user to reconstruct it.
        """
        from foammesh.core.gmsh.execution import surface_names

        try:
            payload = query(
                self._client, 'geometry.prepared.current', {}).payload or {}
        except Exception:                        # noqa: BLE001 - advisory only
            return {}
        prepared = payload.get('prepared')
        if not isinstance(prepared, dict):
            return {}
        try:
            return surface_names(prepared)
        except Exception:                        # noqa: BLE001 - advisory only
            return {}

    def surfaceChoices(self) -> list:
        """`Surface Id` options as `(label, value, status, enabled)`.

        Empty when no geometry has been prepared, which leaves the plain
        numeric editor in place rather than a combo box with nothing in it.
        """
        names = self.preparedSurfaceNames()
        if not names:
            return []
        labelled = {int(tag): name for tag, name in names.items()}
        # R64. The run log reported seven imported surfaces on a case with five
        # named boundaries, so a surface the prepared geometry cannot name is
        # still addressable -- and, more importantly, a row that already refers
        # to one is not silently retargeted at the first named surface the
        # moment somebody opens the editor on it.
        for surface_id in self.storedSurfaceIds():
            labelled.setdefault(surface_id, '')
        options = []
        for tag in sorted(labelled):
            name = labelled[tag]
            # The number stays visible: it is what the runner and the job file
            # record, and a user reading a warning about "surface 2" has to be
            # able to find the row it means.
            options.append((
                f'{tag} — {name}' if name else
                str(self.tr('%d — unnamed in the prepared geometry')) % tag,
                tag,
                self.tr('Prepared') if name else self.tr('Not named'),
                True))
        return options

    def storedSurfaceIds(self) -> list:
        """Surface numbers the saved rows already refer to."""
        node = self._client.configuration() or {}
        for segment in ('gmsh', 'surfaceSizes'):
            node = node.get(segment) if isinstance(node, dict) else None
            if not node:
                return []
        rows = node.values() if isinstance(node, dict) else node
        found = []
        for row in rows:
            try:
                found.append(int(row.get('surfaceId')))
            except (AttributeError, TypeError, ValueError):
                continue
        return found
