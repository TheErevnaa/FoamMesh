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

from pathlib import Path

from PySide6.QtWidgets import (
    QAbstractItemView, QDoubleSpinBox, QFormLayout, QGroupBox, QHeaderView,
    QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from foammesh.view.workflow_controls.field_group_page import FieldGroupPage
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.facade_client import query, submit

#: What ``surfaceFeatures`` extracts with when no group says otherwise
#: (``case_builder._surface_angle``, and the schema's own default).
DEFAULT_INCLUDED_ANGLE = 150.0

#: The angle is stored per refinement group, so this page edits that
#: collection rather than inventing a second home for the value.
REFINEMENT_COLLECTION = 'meshing.castellation.surface_refinements'


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
        self._filters = _FeatureFilterGroup(self._client, self)
        layout.addWidget(self._filters)
        self._diagnostics = _FeatureDiagnosticsGroup(self._client, self)
        layout.addWidget(self._diagnostics)

        box = QGroupBox(self.tr('Extracted feature edges'), self)
        inner = QVBoxLayout(box)
        self._note = QLabel(self.tr(
            'surfaceFeatures writes an .eMesh per surface. The included angle '
            'decides which edges survive: a low angle keeps almost every '
            'edge, a high one keeps only sharp creases. Re-run after changing '
            'it to see the effect.'), box)
        # F11. The empty state was written into this label and never taken
        # back out, so "No feature edges have been extracted yet" stayed on
        # screen directly above the table listing the 84 edges that had just
        # been extracted. Keep the standing explanation to return to.
        self._noteText = self._note.text()
        self._note.setWordWrap(True)
        inner.addWidget(self._note)

        self._features = QTableWidget(0, 3, box)
        self._features.setHorizontalHeaderLabels(
            [self.tr('Surface'), self.tr('Edges'), self.tr('File')])
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
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
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
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self._angle = QDoubleSpinBox(box)
        self._angle.setObjectName('includedAngle')
        self._angle.setRange(0.001, 179.999)
        self._angle.setDecimals(3)
        self._angle.setSuffix(self.tr(' deg'))
        self._angle.setAccessibleName(self.tr('Feature included angle'))
        # Tracking every keystroke would write 1, then 15, then 150 to every
        # group on the way to typing one value.
        self._angle.setKeyboardTracking(False)
        self._angle.editingFinished.connect(self.apply_included_angle)
        form.addRow(self.tr('Included angle'), self._angle)
        self._angleNote = QLabel('', box)
        self._angleNote.setObjectName('includedAngleNote')
        self._angleNote.setWordWrap(True)
        form.addRow(self._angleNote)
        layout.addWidget(box)

    def refresh(self) -> None:
        super().refresh()
        if hasattr(self, '_angle'):
            self._populate_angle()
        for name in ('_filters', '_diagnostics'):
            group = getattr(self, name, None)
            if group is not None:
                group.reload()
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
        if not groups:
            # Disabled and *explained*: this page already claimed a control it
            # did not have, and an unlabelled dead spin box says the same
            # thing over again.
            self._angleNote.setText(self.tr(
                'The angle is stored per surface refinement group and no '
                'group exists yet, so extraction runs at the OpenFOAM '
                'default of %.0f degrees. Add a refinement group on '
                'Castellation to change it.') % DEFAULT_INCLUDED_ANGLE)
        elif len(angles) > 1:
            self._angleNote.setText(self.tr(
                'The %d refinement groups do not agree (%s). Changing this '
                'writes one angle to all of them.')
                % (len(groups), ', '.join(format(a, 'g') for a in angles)))
        else:
            self._angleNote.setText(self.tr(
                'surfaceFeatures keeps an edge whose two faces meet at less '
                'than this angle. Applies to all %d refinement groups; '
                're-run this step after changing it.') % len(groups))

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
            # point of the column; the File column still shows the id.
            path = str(entry.get('path') or '')
            values = (str(entry.get('display_name') or entry.get('name') or ''),
                      f'{int(entry.get("edges") or 0):,}',
                      # R80. This column held the .eMesh's absolute path,
                      # which is mostly the case directory the user just chose
                      # and was the width that pushed the column off screen.
                      # The full path stays reachable as the tooltip.
                      Path(path).name if path else '')
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 2 and path:
                    item.setToolTip(path)
                self._features.setItem(row, column, item)
        self._features.setVisible(bool(rows))
        if rows:
            # F11. Both the label and the table read the same list now.
            self._note.setText(self._noteText)
        else:
            self._note.setText(self.tr(
                'No feature edges have been extracted yet. Run this task to '
                'write an .eMesh per surface at the included angle above.'))


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
    )
    heading = 'Which edges survive'
    purpose = (
        'The included angle above decides which edges are candidates; these '
        'decide which candidates are kept. They change the .eMesh, so the '
        'stage has to run again for them to take effect.')
    caveat = (
        'Keeping non-manifold and open edges is OpenFOAM\'s default and is '
        'right for a clean closed surface. Turn them off when the geometry '
        'is a tessellation with holes or T-junctions, where those edges are '
        'defects rather than features.')


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
