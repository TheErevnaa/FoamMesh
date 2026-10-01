"""Snappy workflow page: ``snappy.surface_features``.

Plan 26 WP5.2. This node resolved to the **Castellation** widget, so clicking
"Surface Features & Refinement" showed a different task's page -- and because
it resolved to *a* widget the strict-GUI harness scored it as a visited pass.

The task is ``run_gated=True`` in the workflow descriptor and had no run
button anywhere: extraction happened implicitly inside
``_ensure_surface_features`` whenever something downstream needed it. A stage
that runs as a side effect of another stage cannot be inspected, and its one
governing control -- ``includedAngle`` -- had no visible effect at all.
"""
from __future__ import annotations

from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView, QFileDialog, QFormLayout, QGroupBox, QHeaderView,
    QLabel, QLineEdit, QStyle, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout,
)

from foammesh.view.workflow_controls.field_group_page import FieldGroupPage
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.facade_client import query, submit
from foammesh.view.theming.metrics import (
    CompactDoubleSpinBox, apply_form_metrics, unit_cell,
)

#: What ``surfaceFeatures`` extracts with when no group says otherwise
#: (``case_builder._surface_angle``, and the schema's own default).
DEFAULT_INCLUDED_ANGLE = 150.0

#: The angle is stored per refinement group, so this page edits that
#: collection rather than inventing a second home for the value.
REFINEMENT_COLLECTION = 'meshing.castellation.surface_refinements'

#: Plan 33 OF-04. What the included angle does, said once, on the control it
#: is about. It used to be the third sentence of a standing paragraph in the
#: result box and the whole of a second label under the spin box, so a reader
#: met it twice before meeting anything they could set.
#: Plan 37 UF19. The subset coordinates, shown only while their switch is on.
SUBSET_BOX_FIELDS = tuple(
    f'meshing.surface_features.subset_box_{end}.{axis}'
    for end in ('min', 'max') for axis in 'xyz')
SUBSET_PLANE_FIELDS = tuple(
    f'meshing.surface_features.subset_plane_{part}.{axis}'
    for part in ('point', 'normal') for axis in 'xyz')
ADD_FEATURES_FIELD = 'meshing.surface_features.add_features_file'

INCLUDED_ANGLE_HELP = (
    'surfaceFeatures keeps an edge whose two faces meet at less than this '
    'angle, so a low angle keeps almost every edge and a high one keeps only '
    'sharp creases.')


class SnappySurfaceFeaturesPage(EngineTaskPage):
    """Feature-edge extraction: its control, its run button, and its result."""

    engine_id = 'snappy'
    task_id_default = 'snappy.surface_features'
    #: Runs *this stage*, not the pipeline. Deliberately not ``run_all_task_id``:
    #: that button fires the engine's whole-pipeline operation, and a control
    #: labelled "re-extract feature edges" that silently re-meshed the case
    #: would be a worse defect than the implicit extraction it replaces.
    run_stage = 'surfaceFeatures'

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id or self.engine_id)

    def build_sections(self, layout) -> None:
        self._build_angle_section(layout)
        self._filters = self.adoptPanel(
            _FeatureFilterGroup(self._client, self))
        layout.addWidget(self._filters)
        self._diagnostics = self.adoptPanel(
            _FeatureDiagnosticsGroup(self._client, self))
        layout.addWidget(self._diagnostics)

        box = QGroupBox(self.tr('Extracted feature edges'), self)
        inner = QVBoxLayout(box)
        # Plan 33 OF-04. One line, and it reports rather than explains: the
        # four sentences that used to open this box said what the stage
        # writes and what the angle does, above a table that is empty until
        # the stage has run. What the angle does is on the angle now.
        self._note = QLabel('', box)
        self._note.setObjectName('featureEdgeResult')
        self._note.setWordWrap(True)
        # W-O1. Whether a run has happened is the state of the case, not a
        # setting, and the box it reports on is the thing it reports about.
        # The label carries the words for `stepHelpText` and for the tests
        # that read them; the box says them where a reader asks.
        self._note.setVisible(False)
        self._featureBox = box
        inner.addWidget(self._note)
        # Plan 37 UF19. OpenFOAM 13 extracts nothing from a box or plane that
        # misses the surface and exits 0 -- the one outcome a table of zeroes
        # does not explain. Shown only then.
        self._subsetNote = QLabel('', box)
        self._subsetNote.setObjectName('featureSubsetEmpty')
        self._subsetNote.setWordWrap(True)
        self._subsetNote.setVisible(False)
        inner.addWidget(self._subsetNote)

        # DP-512. The File column held `surface_<uuid>.eMesh`, a name the
        # user never chose and cannot act on; the user struck it out. The
        # table is Surface and Edges, and the file is the Surface tooltip.
        self._features = QTableWidget(0, 2, box)
        self._features.setHorizontalHeaderLabels(
            [self.tr('Surface'), self.tr('Edges')])
        self._features.setAccessibleName(
            self.tr('Feature edges extracted per surface'))
        self._features.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self._features.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        # R22/R80. Every column sized itself to its content, so a three-column
        # table about 250px wide inside the panel scrolled sideways with the
        # File column entirely off-screen behind the scrollbar -- on a
        # 1920-wide screen, beside 200px of empty panel. The two text columns
        # now share whatever width the panel has, and only the edge count,
        # which is narrow and has to stay readable, takes its content.
        header = self._features.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        inner.addWidget(self._features)
        layout.addWidget(box)

    def _build_angle_section(self, layout) -> None:
        """The one control that governs this stage (R19, R79, R135).

        MEASURED: both of the page's own sentences pointed at an included
        angle -- "Re-run after changing it to see the effect" and "Run this
        task to write an .eMesh per surface at the included angle above" --
        and there was no such control anywhere on the page: ``build_sections``
        built that label and a read-only table, nothing else. The task is
        declared with no ``fields=``, so the schema-driven form had nothing to
        render, and ``includedAngle`` was reachable only from a per-surface
        dialog on Castellation -- the *next* task. The angle is stored per
        refinement group, so this control writes it to every group rather than
        giving the value a second home that could disagree with the first.
        """
        box = QGroupBox(self.tr('Feature extraction'), self)
        form = QFormLayout(box)
        # Plan 33 section 6 check 4, W-O2. This was the one form on the page
        # built without the shared setters, so it carried the `QFormLayout`
        # default 9 px margin where the two panels below it carry 4 --
        # MEASURED, labels at x=31 above labels at x=26, on one column.
        apply_form_metrics(form)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._angle = CompactDoubleSpinBox(box)
        self._angle.setObjectName('includedAngle')
        self._angle.setRange(0.001, 179.999)
        self._angle.setDecimals(3)
        self._angle.setAccessibleName(self.tr('Feature included angle'))
        described = self.tr(INCLUDED_ANGLE_HELP)
        self._angle.setToolTip(described)
        self._angle.setAccessibleDescription(described)
        # Tracking every keystroke would write 1, then 15, then 150 to every
        # group on the way to typing one value.
        self._angle.setKeyboardTracking(False)
        self._angle.editingFinished.connect(self.apply_included_angle)
        # DP-164. ` deg` was a Qt suffix inside the box, so this page drew
        # `150.000 deg` next to four registry boxes reading `0` with their
        # unit in a column of its own. One rule now, for both.
        form.addRow(self.tr('Included angle'), unit_cell(self._angle, 'deg'))
        self._angleNote = QLabel('', box)
        self._angleNote.setObjectName('includedAngleNote')
        self._angleNote.setWordWrap(True)
        # W-O1. Off until `_populate_angle` finds a reason to draw it. The
        # reason a control is shut is said on the control; a disagreement
        # between groups is a warning about a value and stays on the form.
        self._angleNote.setVisible(False)
        form.addRow(self._angleNote)
        self._angleForm = form
        layout.addWidget(box)

    def aligned_forms(self):
        """Every form on this page is one column (DP-154).

        This used to return the two panels only, and said of the
        included-angle box that it "is a single row in a group of its own, at
        its own indent". W-O2 took that indent away -- an embedded panel sits
        at the page's own left margin now, not 18 px inside it -- and what
        was left was one field column at x=294 with a single row at x=121
        above it. A row at the page's indent is part of the page's form.
        """
        forms = [getattr(self, '_angleForm', None)]
        forms += [group.form_layout()
                  for group in (getattr(self, '_filters', None),
                                getattr(self, '_diagnostics', None))
                  if group is not None]
        return tuple(form for form in forms if form is not None)

    def refresh(self) -> None:
        super().refresh()
        if hasattr(self, '_angle'):
            self._populate_angle()
        for name in ('_filters', '_diagnostics'):
            group = getattr(self, name, None)
            if group is not None:
                # DP-339. A refresh keeps an uncommitted edit.
                group.reload(discard_pending=False)
        # After the panels reload, for the reason given on the QA page.
        self._align_field_columns()
        if hasattr(self, '_features'):
            self._populate_features()

    # -- the included angle ------------------------------------------------ #

    def refinement_groups(self) -> dict:
        """``{group id: stored fields}`` for the surface refinement groups."""
        try:
            configuration = self._client.configuration() or {}
        except Exception:                                    # noqa: BLE001
            return {}
        groups = (configuration.get('castellation') or {}).get(
            'refinementSurfaces') or {}
        return {str(key): dict(value) for key, value in groups.items()
                if isinstance(value, dict)}

    @staticmethod
    def _group_angle(group: dict) -> float:
        try:
            return float(group.get('includedAngle'))
        except (TypeError, ValueError):
            return DEFAULT_INCLUDED_ANGLE

    def _populate_angle(self) -> None:
        groups = self.refinement_groups()
        angles = sorted({self._group_angle(group) for group in groups.values()})
        # The widest configured angle keeps the most edges, so showing it is
        # the reading that cannot understate what extraction will produce.
        self._angle.blockSignals(True)
        self._angle.setValue(angles[-1] if angles else DEFAULT_INCLUDED_ANGLE)
        self._angle.blockSignals(False)
        self._angle.setEnabled(bool(groups))
        # Plan 33 OF-04. One sentence per branch, and each of them says what
        # is true of this case right now. The standing explanation that used
        # to be the third branch is `INCLUDED_ANGLE_HELP`, carried by the spin
        # box itself, so it is there for all three.
        if not groups:
            # Disabled and *explained*: this page already claimed a control it
            # did not have, and an unlabelled dead spin box says the same
            # thing over again.
            self._angleNote.setText(self.tr(
                'Stored per surface refinement group, and no group exists '
                'yet, so add one on Castellation to change it.'))
            # W-O1. Why this spin box is shut is the spin box's own
            # description, beside what the angle does, so the reader who
            # clicks the dead control is answered by it.
            described = ' '.join(
                (self.tr(INCLUDED_ANGLE_HELP), self._angleNote.text()))
            self._angle.setToolTip(described)
            self._angle.setAccessibleDescription(described)
        elif len(angles) > 1:
            self._angleNote.setText(self.tr(
                'The refinement groups do not agree (%s), and changing this '
                'writes one angle to every one of them.')
                % ', '.join(format(a, 'g') for a in angles))
        else:
            self._angleNote.setText('')
        if groups:
            described = self.tr(INCLUDED_ANGLE_HELP)
            self._angle.setToolTip(described)
            self._angle.setAccessibleDescription(described)
        # W-O1. Only the disagreement branch stands: the other two are the
        # state of the case, said on the control above.
        self._angleNote.setVisible(bool(groups) and len(angles) > 1)

    def apply_included_angle(self) -> None:
        """Write the angle to every refinement group, then re-read it."""
        value = float(self._angle.value())
        pending = [entity_id
                   for entity_id, group in self.refinement_groups().items()
                   if abs(self._group_angle(group) - value) >= 1e-9]

        # C31-12. Still one write per group, still strictly in order, and
        # still stopping at the first refusal -- but each write is scheduled
        # rather than run on the GUI thread, which for a case with a dozen
        # refinement groups was a dozen consecutive freezes. `write_next` is
        # the loop body and re-enters itself from the continuation of the
        # write it just made, so group N+1 is not sent before group N has
        # been answered.
        def write_next(index: int, changed: bool) -> None:
            if index >= len(pending):
                if changed:
                    self._populate_angle()
                return

            def written(result) -> None:
                if getattr(result, 'status', 'accepted') != 'accepted':
                    # Refusing in silence is the shape of defect this page
                    # exists to end, so the refusal is written where the
                    # control is.
                    self._angleNote.setText(
                        self.tr('The included angle was not saved: %s')
                        % (getattr(result, 'message', '')
                           or self.tr('the facade refused the change')))
                    # W-O1. A refusal is a validation error beside the input
                    # it concerns, which Plan 33 section 1 keeps on the form.
                    self._angleNote.setVisible(True)
                    return
                write_next(index + 1, True)

            submit(self._client, REFINEMENT_COLLECTION + '.patch',
                   {'entity_id': pending[index],
                    'fields': {'included_angle': value}}, then=written)

        write_next(0, False)

    # -- the result -------------------------------------------------------- #

    def _populate_features(self) -> None:
        """List what the last extraction produced, or say that none has run.

        Read through the facade like every other page. An empty table with no
        explanation would read as "extraction produced nothing", which is a
        different fact from "extraction has not run".
        """
        rows = ()
        try:
            payload = query(
                self._client, 'mesh.feature_edges',
                {'engine_id': self.engine_id}).payload
            rows = tuple(payload.get('surfaces') or ())
        except Exception:                                    # noqa: BLE001
            rows = ()
        self._features.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            # R21/R136. The Surface column read the staged file's stem, which
            # is the prepared-geometry uuid: the one row said
            # `surface_0ff6ff6343d247a39df11ad5dffa94ce` for the geometry the
            # rest of the app calls `annulus`, and with several surfaces the
            # ids are indistinguishable at a glance. The name is the whole
            # point of the column.
            path = str(entry.get('path') or '')
            values = (str(entry.get('display_name') or entry.get('name') or ''),
                      f'{int(entry.get("edges") or 0):,}')
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0 and path:
                    # R80/DP-512. The .eMesh this surface's edges were
                    # written to stays reachable for diagnosis, as the
                    # tooltip of the surface it belongs to.
                    item.setToolTip(self.tr('{0}\nEdges file: {1}').format(
                        value, path))
                self._features.setItem(row, column, item)
        self._features.setVisible(bool(rows))
        # F11. The label and the table read the same run: an empty table with
        # no line above it would say "extraction produced nothing", which is a
        # different fact from "extraction has not run".
        said = (self.tr('The last extraction kept these edges.') if rows
                else self.tr('No feature edges have been extracted yet.'))
        self._note.setText(said)
        self._populate_subset_note(rows)
        box = getattr(self, '_featureBox', None)
        if box is not None:
            box.setToolTip(said)
            box.setAccessibleDescription(said)


    def active_subset(self) -> str:
        """'box', 'plane', 'box and plane', or '' when nothing is subset."""
        ids = ('meshing.surface_features.subset_box',
               'meshing.surface_features.subset_plane')
        try:
            values = self._client.field_values(ids) or {}
        except Exception:                                    # noqa: BLE001
            return ''
        box = str(values.get(ids[0]) or 'none') != 'none'
        plane = str(values.get(ids[1])).strip().lower() in ('true', '1')
        return ' and '.join(name for name, on in
                            (('box', box), ('plane', plane)) if on)

    def _populate_subset_note(self, rows) -> None:
        empty = [str(entry.get('display_name') or entry.get('name') or '')
                 for entry in rows if not int(entry.get('edges') or 0)]
        subset = self.active_subset() if empty else ''
        if subset:
            self._subsetNote.setText(self.tr(
                'The feature subset %s kept no edges on %s. OpenFOAM does not '
                'report this; check that the coordinates are in the surface '
                'file\'s frame and actually meet the surface.')
                % (subset, ', '.join(empty)))
        else:
            self._subsetNote.setText('')
        self._subsetNote.setVisible(bool(subset))


class _FeatureFilterGroup(FieldGroupPage):
    """What the extraction keeps, beyond the included angle.

    Plan 31 (``surface_features.rest``). ``surfaceFeaturesDict`` carried two
    keys per surface -- the file and the angle -- and OpenFOAM 13 reads a
    dozen more. The filters are the ones with product value: a tessellation
    with open or non-manifold edges hands snappyHexMesh a feature set full of
    edges that are not features, and there was no way to say so from anywhere
    in this application.

    Every default is OpenFOAM 13's own and nothing is written unless it
    differs, so a case that never opens this panel gets the dictionary it
    always got.
    """

    token = 'workflow.surface_features'
    field_ids = (
        'meshing.surface_features.geometric_test_only',
        'meshing.surface_features.keep_non_manifold_edges',
        'meshing.surface_features.keep_open_edges',
        'meshing.surface_features.trim_min_length',
        'meshing.surface_features.trim_min_elements',
        # Plan 37 UF19. subsetFeatures box and plane, then addFeatures, in
        # the order OpenFOAM 13 applies them. The coordinates are rows only
        # while their switch is on, so the panel grows by three rows.
        'meshing.surface_features.subset_box',
        *SUBSET_BOX_FIELDS,
        'meshing.surface_features.subset_plane',
        *SUBSET_PLANE_FIELDS,
        ADD_FEATURES_FIELD,
    )
    heading = 'Which edges survive'
    purpose = (
        'The included angle above decides which edges are candidates; these '
        'decide which candidates are kept, and which are added from a file. '
        'They change the .eMesh, so the stage has to run again for them to '
        'take effect.')
    caveat = (
        'Keeping non-manifold and open edges is OpenFOAM\'s default and is '
        'right for a clean closed surface. Turn them off when the geometry '
        'is a tessellation with holes or T-junctions, where those edges are '
        'defects rather than features.')

    def build(self) -> None:
        super().build()
        editor = self._editors.get(ADD_FEATURES_FIELD)
        if editor is None:
            return
        # Plan 37 UF19. A path typed by hand is the usual way to name the
        # wrong file; the button fills the same field, and the field is still
        # what is saved. It sits inside the box as a trailing icon: a button
        # beside it took 75 px out of the box, which then ended short of
        # every other field in the column (DP-156).
        line = editor.editor
        action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon),
            self.tr('Choose the added feature file…'), line)
        action.triggered.connect(lambda: self._browse_added_features(editor))
        line.addAction(action, QLineEdit.ActionPosition.TrailingPosition)
        button = next(child for child in line.findChildren(QToolButton)
                      if child.defaultAction() is action)
        button.setObjectName('addFeaturesBrowse')
        button.setAccessibleName(self.tr('Choose the added feature file'))
        button.setToolTip(action.text())
        self._browse = button

    def _browse_added_features(self, editor) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self, self.tr('Added feature edges'), str(editor.value() or ''),
            self.tr('Extended feature edge mesh '
                    '(*.extendedFeatureEdgeMesh *.extendedFeatureEdgeMesh.gz)'
                    ';;All files (*)'))
        if path:
            editor.set_value(path)
            editor.valueChanged.emit(editor.field_id, editor.value())


class _FeatureDiagnosticsGroup(FieldGroupPage):
    """The fields and files surfaceFeatures can write about what it found.

    Plan 31. The closeness and proximity fields are how a user finds the
    narrow gaps a cell size will not resolve -- before meshing, rather than
    from a failed checkMesh afterwards -- and the OBJ and VTK writers are how
    an extraction gets attached to a support request.
    """

    token = 'workflow.surface_features'
    field_ids = (
        'meshing.surface_features.face_closeness',
        'meshing.surface_features.internal_angle_tolerance',
        'meshing.surface_features.external_angle_tolerance',
        'meshing.surface_features.feature_proximity',
        'meshing.surface_features.max_feature_proximity',
        'meshing.surface_features.write_obj',
        'meshing.surface_features.verbose_obj',
        'meshing.surface_features.write_vtk',
    )
    heading = 'Diagnostic fields and files'
    purpose = (
        'Extra output from the same run. None of it changes the feature '
        'edges; it describes them and the surface they came from.')
    caveat = (
        'Surface curvature is deliberately absent: on OpenFOAM 13 it was '
        'measured to crash surfaceFeatures outright, on a clean sphere, so '
        'there is nothing here to switch it on with.')
