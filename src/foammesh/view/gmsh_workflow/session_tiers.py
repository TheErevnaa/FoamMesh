"""Plan 41 GN18 (section 4.1): the tiers that work on the resident session.

Building geometry, picking entities, opening or merging a file and making a
structured grid all act on the Gmsh model the session holds, and the plan puts
each on the row it belongs to: the first three on Geometry, the grid on
Structure. They used to be four closed tiers at the foot of the Generate page.
The mounting code is here, once, so a row asks for the tier it owns and none
of the pages carries a copy.

A tier is a flat toggle header and the panel it opens, closed until asked for,
so the guided form of a row is as short as it was.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from foammesh.view.widgets.link_button import as_link


class SessionTiers:
    """Mixin for a :class:`GmshTaskPage` that hosts session tiers."""

    # -- the session and the viewport the tiers act on ----------------------- #

    def entityViewport(self):
        """The viewport widget the entities are drawn in, if there is one."""
        from foammesh.app import app

        window = getattr(app, 'window', None)
        display = getattr(window, 'displayControl', None)
        view = getattr(display, 'view', None)
        return view() if callable(view) else None

    def generateController(self):
        """The project's session controller, made when the first action runs."""
        from .compute_page import sharedGenerateController

        controller = getattr(self, '_generateController', None)
        shared = sharedGenerateController(self._client)
        if controller is shared:
            return controller
        # WALK-2. A new case got a new controller; this page follows it.
        self._generateController = shared
        # FIX-K (SG9). A page going away is not the case going away: an
        # engine switch rebuilds the Gmsh pages, and the session with its
        # model must outlive that. It ends with the case (or is replaced by
        # the next case's controller, above).
        client = self._client

        def release(*_args, controller=shared):
            from .compute_page import _same_case
            try:
                if _same_case(controller, client.case_path):
                    return
            except Exception:                            # noqa: BLE001
                pass
            controller.close()

        self.destroyed.connect(release)
        return shared

    # -- one tier ------------------------------------------------------------ #

    def addTier(self, layout, *, name: str, title: str, description: str,
                panel) -> QPushButton:
        """A closed tier: a flat toggle header over *panel*."""
        header = QPushButton(title, self)
        header.setObjectName(f'gmsh{name}Header')
        header.setCheckable(True)
        header.setFlat(True)
        header.setAccessibleDescription(description)
        panel.setVisible(False)
        header.toggled.connect(panel.setVisible)
        # Plan 44 MF20. A closed tier is a 20 px text link, not a row the
        # width of the sheet; opening it still shows its whole panel.
        as_link(header)
        layout.addWidget(header, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(panel)
        return header

    # -- the Geometry row ---------------------------------------------------- #

    def mountEntityTier(self, layout) -> None:
        """Pick on the model and name physical groups; then build and open."""
        from .entity_panel import GmshEntityPanel

        self._entityPanel = GmshEntityPanel(
            self.generateController, self.entityViewport, self,
            store_provider=lambda: self._client.case_path)
        # Plan 42 SG13. This is the one picker; every other page asks the
        # selection service for a PickRequest and this panel serves it.
        self._entityPanel.registerAsPicker()
        self._entityHeader = self.addTier(
            layout, name='Entity',
            title=self.tr('Pick entities and physical groups'),
            description=self.tr(
                'Pick points, curves, surfaces and volumes in the viewport, '
                'group them into named physical groups, and remove duplicate '
                'entities.'),
            panel=self._entityPanel)
        self.mountBuildTier(layout)
        self.mountImportTier(layout)

    def mountHealingTier(self, layout) -> None:
        """Plan 42 SG11 (D4). The 19 import and healing values.

        They describe how Gmsh's CAD importer reads the geometry, so they are
        asked on the first Gmsh row, after the engine is chosen; Preparation
        is the same page for both engines. The panel's edits commit with this
        page (`adoptPanel`).
        """
        from foammesh.view.workflow_controls.field_group_page import (
            GmshHealingPanel)

        self.healingPanel = self.adoptPanel(
            GmshHealingPanel(self._client, self))
        self._healingHeader = self.addTier(
            layout, name='Healing', title=self.tr('Import and healing'),
            description=self.tr(
                'How Gmsh reads the geometry when it meshes it: the '
                'tolerances, the sewing and the fixes its CAD importer '
                'applies.'),
            panel=self.healingPanel)

    def mountImportTier(self, layout) -> None:
        """Plan 41 GN5. Open or merge a file, reclassify."""
        from .import_panel import GmshImportPanel

        self._importPanel = GmshImportPanel(
            self.generateController, self._entityPanel, self)
        self._importHeader = self.addTier(
            layout, name='Import', title=self.tr('Open or merge a file'),
            description=self.tr(
                'Open or merge a Gmsh script, CAD, surface or mesh file; '
                'reclassify surfaces, create topology and geometry.'),
            panel=self._importPanel)

    def mountBuildTier(self, layout) -> None:
        """Plan 41 GN3. Build geometry on the Gmsh model."""
        from .geometry_build_panel import GmshGeometryBuildPanel

        self._buildPanel = GmshGeometryBuildPanel(
            self.generateController, self._entityPanel, self)
        self._buildHeader = self.addTier(
            layout, name='Build', title=self.tr('Build geometry'),
            description=self.tr(
                'Add parameters, points, curves, surfaces, volumes and '
                'primitives; translate, rotate, scale, mirror, extrude and '
                'revolve; fuse, intersect, cut and fragment; delete. '
                'Entities are picked in the viewport.'),
            panel=self._buildPanel)

    # -- the Structure row --------------------------------------------------- #

    def mountStructureTier(self, layout) -> None:
        """Plan 41 GN8. Transfinite surfaces and volumes with picked corners,
        automatic transfinite, the block grid preview and the Structured
        size grid.

        The corners are picked through the one Pick tool: the panel asks the
        selection service for a PickRequest (Plan 42 SG13), so the page holds
        no entity panel of its own.
        """
        from .shared_pick import SharedPick
        from .structure_panel import StructurePanel

        pick = SharedPick('structure', ordered=True, parent=self)
        self.structurePick = pick
        self.structurePanel = self.adoptPanel(StructurePanel(
            self._client, self.generateController, pick, self))
        self._structureHeader = self.addTier(
            layout, name='Structure', title=self.tr('Transfinite and blocks'),
            description=self.tr(
                'Make surfaces and volumes transfinite with corners picked '
                'in the viewport, set the triangle arrangement, let Gmsh '
                'find the blocks, preview the block grid, and write a '
                'Structured size grid.'),
            panel=self.structurePanel)
