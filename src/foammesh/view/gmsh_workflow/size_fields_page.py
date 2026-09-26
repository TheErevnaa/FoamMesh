"""Gmsh workflow page: gmsh.size_fields."""
from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QVBoxLayout, QWidget

from foammesh.view.facade_client import query
from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import GmshTaskPage


class GmshSizeFieldsPage(GmshTaskPage):
    """Where the mesh is finer than the global size, in three sections.

    Plan 33 FIELD-07/CURVE-06. The product asked that one question on two
    steps and three tables: `Size fields` held the spatial fields and the
    per-surface sizes, `Curve controls` was a step of its own holding one
    table that is empty on almost every case. A reader looking for "make it
    finer at the inlet" had to know which of the two steps the control they
    wanted had been filed under, and the journey spent a press on a page
    with nothing on it.

    The three sections are here instead, in the order a case is usually
    built: the boundaries that carry their own target size, the spatial
    fields, and the edge controls. The stored collections are untouched and
    the two engine tasks stay two tasks -- the one press settles them in
    order -- so nothing about the job document moves with the page.
    """

    task_id_default = 'gmsh.size_fields'

    #: DP-560 (0924 rerun). MEASURED at the 420 px settings column: the
    #: eight columns asked 542 px of a 302 px table and scrolled 240 px
    #: sideways. The table is the review -- what a field is called, whether
    #: it is on, what kind it is, what it reaches and the size it asks for
    #: there -- and the outside size, the expression and the priority (which
    #: only sorts the rows, FIELD-05) are in the editor the row opens.
    #: DP-569 (0924 rerun follow-up). MEASURED at the 360 px settings column
    #: a window under 1600 px wide gets: those five asked 354 px of a 314 px
    #: table and scrolled 40 px sideways. The switch goes to the editor, as
    #: it did on the volume table (DP-560), and the name takes the room left
    #: over, so a field is read by what it is, what it reaches and its size.
    COLUMNS = ('name', 'field_type', 'scope_token', 'size_inside')

    #: Plan 29 WP8. A per-surface size asks for two numbers, not eight, so it
    #: gets its own table rather than more mostly-empty columns on the one
    #: above. Plan 33 FIELD-07: the blend distance and the priority are in
    #: the editor, not the table -- the table is the review, and the review
    #: is which boundary carries which size.
    SURFACE_COLUMNS = ('name', 'surface_id', 'target_size', 'enabled')

    #: Plan 33 CURVE-06. The same four questions of an edge control: what it
    #: is called, what it reaches, what it does there and whether it is on.
    #: The rest of the twelve fields are in the editor the row opens.
    #: DP-569 (0924 rerun follow-up). MEASURED at the 360 px settings
    #: column: the stretched `Enabled` fell to 51 px of the 73 its heading
    #: needs and read `Enable`. The switch is in the editor, as it is for the
    #: spatial fields above, and the name takes the room left over.
    CURVE_COLUMNS = ('name', 'scope_token', 'mode', 'segments')

    #: Plan 33 FIELD-07. `Surface id` is the number the runner writes in the
    #: job document and the run log; it is not what the user picked, and it
    #: is not what they can recognise a row by. The boundary name is, and
    #: the page already reads the join. The number stays in the tooltip.
    SURFACE_HEADINGS = {'surface_id': 'Boundary'}

    #: Plan 33 CURVE-05. `Geometry scope` over a column of face groups says
    #: nothing about what one control reaches, and the dialog offered a
    #: single picker where a reader expected to pick edges. A control takes
    #: every boundary curve of the faces in the group it names; the heading
    #: names the group and the help says what is taken.
    CURVE_HEADINGS = {'scope_token': 'Face group'}

    #: Plan 33 VOLUME-04. `transfiniteTri` decides how a three-sided face is
    #: filled, which is an edge decision, and it was drawn under a heading
    #: about volumes behind a folded box. It is rendered here instead; the
    #: volume step answers `renders_field` False for it, so there is still
    #: one editor for the one field.
    extra_field_ids = ('gmsh.volume_controls.transfinite_tri',)

    #: Plan 32 check 12. MEASURED on the page widget before this moved: 220
    #: words across six labels with no input visible at all, because the
    #: paragraphs were laid out above two tables that start empty. The words
    #: are not gone -- they are what the `?` beside the heading says, which
    #: is where section 4.5 puts longer reasoning, examples and native
    #: option names.
    HELP_DETAIL = (
        'A distance field needs a surface scope; box, ball, '
        'cylinder and frustum use their own coordinates — a box is its '
        'two opposite corners, a frustum its start point, axis and a '
        'radius at each end; math_eval takes '
        'an expression in x, y and z and ignores the size numbers. An '
        'expression is checked and costed against the geometry before a '
        'run is accepted. '
        'Restrict holds one size inside the scoped entities and nowhere '
        'else; curvature refines where the scoped surfaces bend. Each row '
        'shows only the parameters its own type uses. Surface sizing is '
        'the short way to say "finer here": pick a boundary and a '
        'target size, and the mesh ramps back to the global size over the '
        'blend distance. Leave the blend distance at 0 to ramp over one '
        'global cell. Each row becomes a distance field, so it is combined '
        'with the fields above the same way. An edge control takes every '
        'boundary curve of the faces in the group it names: transfinite '
        'mode fixes the node count along each of those curves, size mode '
        'sets a local element size on them instead.')

    #: Plan 33 FIELD-05. The page used to state one of these as a fact --
    #: "Fields combine as a minimum: at any point the finest requested size
    #: wins" -- while the combiner is a setting on the global sizing step
    #: with two values. A case set to Max read a page that told it the
    #: opposite of what the run would do.
    OVERLAP_HELP = {
        'min': 'Where two of them cover the same place the finest '
               'requested size wins: the fields are combined with Min on '
               'the global sizing step.',
        'max': 'Where two of them cover the same place the coarsest '
               'requested size wins: the fields are combined with Max on '
               'the global sizing step.',
    }

    #: Plan 33 FIELD-05 second half. Priority is `field.order`: it sorts the
    #: rows in the job document, highest first, and nothing reads it after
    #: that. It was described as though it settled overlaps, which is the
    #: combiner's job.
    PRIORITY_HELP = ('Priority sorts the rows, highest first. It does not '
                     'decide which size wins where two of them meet.')

    #: What was appended to the step description last time round, so a
    #: refresh that reads a different combiner replaces the sentence rather
    #: than adding a second one.
    _appendedDetail = ''

    #: Set while one table's selection is being reflected in the others.
    _selecting = False

    def build_sections(self, layout) -> None:
        self.surface_panel = ChildControlPanel(
            self._client, 'gmsh.surface_sizes.controls',
            self.tr('Surface sizing'), columns=self.SURFACE_COLUMNS,
            parent=self, choices={'surface_id': self.surfaceChoices()},
            headings=self.SURFACE_HEADINGS)
        layout.addWidget(self.surface_panel)

        self.panel = ChildControlPanel(
            self._client, 'gmsh.size_fields.controls',
            self.tr('Spatial size fields'), columns=self.COLUMNS,
            parent=self, stretch='name')
        layout.addWidget(self.panel)
        layout.addWidget(self._buildEdgeSection())

        for panel in self.scopePanels():
            panel.childrenChanged.connect(self.refresh)
            panel.selectedSurfaceChanged.connect(
                lambda reference, source=panel: self._scopeSelected(source))

    def _buildEdgeSection(self) -> QWidget:
        """The edge table and the one edge switch, in one section.

        VOLUME-04. The switch is a field of the volume task and the table is
        a collection of its own, so nothing but this widget joins them; they
        are joined because they answer the same question -- what happens
        along an edge, and what fills the face it bounds.
        """
        section = QWidget(self)
        section.setObjectName('edgeCurveSection')
        column = QVBoxLayout(section)
        column.setContentsMargins(0, 0, 0, 0)
        self.curve_panel = ChildControlPanel(
            self._client, 'gmsh.curve_controls.controls',
            self.tr('Edge/curve controls'), columns=self.CURVE_COLUMNS,
            parent=section, headings=self.CURVE_HEADINGS, stretch='name')
        column.addWidget(self.curve_panel)
        self._edgeForm = QFormLayout()
        host = QWidget(section)
        host.setLayout(self._edgeForm)
        column.addWidget(host)
        return section

    def scopePanels(self) -> tuple:
        """The three tables, in the order they are painted."""
        return tuple(
            panel for panel in
            (getattr(self, name, None)
             for name in ('surface_panel', 'panel', 'curve_panel'))
            if panel is not None)

    def field_form(self, field_id, classification):
        """Draw the edge switch beside the edge table, not under Advanced.

        The default routing reads the classification, and `transfiniteTri`
        is a native runner control, so it would land in the folded box at
        the foot of the page -- away from the only other controls on the
        page that are about edges.
        """
        if field_id in self.extra_field_ids:
            return getattr(self, '_edgeForm', None)
        return None

    def field_forms(self):
        form = getattr(self, '_edgeForm', None)
        return (form,) if form is not None else ()

    # -- one highlight at a time -------------------------------------------- #

    def _scopeSelected(self, source) -> None:
        """CURVE-05. The row that is selected is the row being painted.

        Each table highlights the geometry of its selected row, and three
        tables holding three selections mean the viewport shows whichever
        row was clicked last while the other two still look selected. The
        other tables let go instead, so what is highlighted and what is
        selected are the same thing.
        """
        if self._selecting:
            return
        self._selecting = True
        try:
            for panel in self.scopePanels():
                if panel is not source:
                    panel.table.clearSelection()
        finally:
            self._selecting = False

    # -- what the page says about itself ------------------------------------ #

    def fieldCombiner(self) -> str:
        """How the run combines two fields over the same place."""
        field_id = 'gmsh.global_sizing.field_combiner'
        try:
            values = self._client.field_values((field_id,))
        except Exception:                        # noqa: BLE001 - advisory only
            values = {}
        value = values.get(field_id)
        value = str(getattr(value, 'value', value) or '').strip().lower()
        return value if value in self.OVERLAP_HELP else 'min'

    def overlapHelp(self) -> str:
        """The two sentences that say what happens where fields overlap."""
        return ' '.join((self.tr(self.OVERLAP_HELP[self.fieldCombiner()]),
                         self.tr(self.PRIORITY_HELP)))

    def helpDetail(self) -> str:
        return ' '.join((self.tr(self.HELP_DETAIL), self.overlapHelp()))

    def showEvent(self, event):
        """Offer the boundary list again every time the page is opened.

        DP-471, and DP-103 one page over. `refresh` runs when the page is
        built, when an edit lands and on a revert, and none of those is the
        moment the geometry is prepared. The page is built with the workflow,
        before any geometry exists, so `surfaceChoices()` is empty then --
        which is exactly when `set_choices` leaves the plain numeric editor
        in place instead of a combo box with nothing in it. Nothing asks
        again, so the editor stays a spin box for the life of the case.

        MEASURED on the `two_cubes_one_file` gmsh leg of 22 September 2026,
        with the DP-470 reader: the editor was a `CompactSpinBox`, "built
        while the choices were empty and not re-offered since", while the
        same page answered `2 choice(s) from 2 prepared surface name(s)`
        when asked in that frame. The data was there and the widget was the
        one built before it arrived.

        This is the half of the empty picker that DP-470 did not reach.
        DP-470 was about *which names exist* on a multi-source model; this is
        about *when the page asks for them*, and it holds on single-source
        models where the names were never in doubt. Until it is repaired the
        DP-470 fix cannot show up on screen, because the widget that would
        display it was replaced by a spin box before the names landed.
        """
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        super().refresh()
        # FIELD-02. The surface picker is built from the prepared revision,
        # not from the catalogue, so it is the one list the panel cannot
        # rebuild for itself when the geometry is prepared again.
        self.surface_panel.set_choices('surface_id', self.surfaceChoices())
        self._sayHowFieldsCombine()
        self._moveProseBehindHelp()

    def _sayHowFieldsCombine(self) -> None:
        """FIELD-05. The overlap rule, as the case is actually set."""
        overlap = self.overlapHelp()
        self.panel.setToolTip(overlap)
        self.panel.table.setToolTip(overlap)
        self.panel.setAccessibleDescription(overlap)

    def _moveProseBehindHelp(self) -> None:
        """Say the rest of it through the control DP-230 already built.

        `_description` is where a task page authors what the step is for, and
        `refresh()` rewrites it from the descriptor on every pass, so the
        page's own paragraph is appended after that and the help is re-read
        from the label rather than set beside it. The two therefore stay the
        same two strings, which is what the DP-230 gate asserts.
        """
        described = self._description.text().strip()
        if self._appendedDetail:
            described = described.replace(self._appendedDetail, '').strip()
        detail = self.helpDetail()
        described = (described + ' ' + detail).strip()
        self._appendedDetail = detail
        self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())

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
        """`Boundary` options as `(label, value, status, enabled)`.

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
            # FIELD-07. The name is what the user gave the boundary and what
            # every other page calls it. The number is what the runner and
            # the job file record, and a reader following a warning about
            # "surface 2" still needs it, so it is the status the picker
            # shows beside the name and the tooltip the table cell carries.
            options.append((
                name if name else
                str(self.tr('Unnamed surface %d')) % tag,
                tag,
                str(self.tr('Surface %d in the prepared geometry')) % tag,
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
