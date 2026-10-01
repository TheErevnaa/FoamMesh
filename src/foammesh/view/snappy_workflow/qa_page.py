"""Snappy workflow page: ``snappy.qa`` -- the values snappy meshes against.

Plan 26 WP5.1. This node resolved to the **Export** widget. Two consequences,
and the second is worse than the first: a user clicking the Quality row got the
export page, and the strict-GUI harness recorded the node as a visited pass
because it resolved to *a* widget, so an empty gap register was compatible with
the defect.

The values themselves were reachable only through the Mesh ▸ Parameters *menu*
dialog -- sixteen, not four: the whole ``meshQualityControls`` block plus
``mergeTolerance`` -- even though ``field_metadata`` declares
``ui_location = workflow.quality`` for a page that did not exist. The menu
dialog stays as a shortcut; this is the page that location always named.
"""
from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox, QGroupBox, QLabel, QListWidget, QListWidgetItem,
    QScrollArea, QVBoxLayout,
)

from foammesh.view.workflow_controls.checkmesh_findings import (
    CheckMeshFindings,
)
from foammesh.view.workflow_controls.task_page import EngineTaskPage
from foammesh.view.workflow_controls.field_group_page import FieldGroupPage

logger = logging.getLogger(__name__)

#: The values snappyHexMesh judges every candidate cell against, derived from
#: the one quality policy rather than listed again here.
#:
#: Plan 30 WP-04 (F-29). This was a hand-written tuple of twenty-seven
#: semantic ids, and the Mesh menu dialog held its own hand-written list of
#: the same record's leaves. Two lists, one record: nothing made them agree,
#: so the QA page and the summary page could show different numbers for the
#: same case (R191). Both are now built from
#: ``core/quality/policy.storage_paths()``, so a leaf added to the policy
#: reaches both editors and a leaf missing from the schema is an import-time
#: error rather than a silently absent control.
def _mesh_quality_fields() -> tuple[str, ...]:
    from foammesh.core.facade import FIELD_REGISTRY
    from foammesh.core.quality.policy import storage_paths

    ids = []
    for path in storage_paths():
        descriptor = FIELD_REGISTRY.by_storage_path(path)
        if descriptor is None:
            raise RuntimeError(
                f'mesh quality policy names {path}, which the configurations '
                'schema does not have')
        ids.append(descriptor.id)
    return tuple(ids)


MESH_QUALITY_FIELDS = _mesh_quality_fields()
_MESH_QUALITY_IDS = frozenset(MESH_QUALITY_FIELDS)

#: Plan 31 (``checkmesh.thresholds_and_region``, ``checkmesh.write_surfaces``).
#: checkMesh's own command line. Every one of these was a key in the
#: configuration with no control anywhere in the product: the thresholds the
#: verdict is computed against were OpenFOAM's defaults and unreachable, and
#: the two artefact switches were never passed at all.
CHECK_MESH_FIELDS = (
    'quality.check.non_orth_threshold',
    'quality.check.skew_threshold',
    'quality.check.user_defined_checks',
    'quality.check.skip_topology',
    'quality.check.write_surfaces',
    # Plan 37 UF18. The three switches every run carried, now the user's.
    'quality.check.all_topology',
    'quality.check.all_geometry',
    'quality.check.write_sets',
)


class SnappyQaPage(EngineTaskPage):
    """The QA task, with the mesh-quality limits it actually judges against."""

    engine_id = 'snappy'
    task_id_default = 'snappy.qa'
    #: The task is run-gated (D23): "Run this step" is checkMesh on the mesh
    #: in the case root, and the only thing that advances the row.
    run_stage = 'checkMesh'

    #: Plan 32 check 12. MEASURED on the page widget before this moved: 324
    #: words over 47 labels, of which 187 were the two panels' purpose and
    #: caveat paragraphs -- what the settings are for, which dictionary they
    #: reach and what re-running the check does. The page now opens on the
    #: verdict, the two headings and the thirty-two controls; the reasoning
    #: is one press away, where §4.5 puts it.
    HELP_DETAIL = (
        'How the finished mesh is checked: these change the verdict and the '
        'evidence, never the mesh. They are checkMesh\'s own command line, '
        'so re-running the check with a lower threshold re-judges the mesh '
        'you already have. Judging against the mesher\'s own limits writes '
        'system/meshQualityDict from the mesher limits below and needs the '
        'case dictionaries generated again before it takes effect. The '
        'failed sets are written into constant/polyMesh/sets, with the '
        'point sets under postProcessing/checkMesh; the problem faces reach '
        'postProcessing/checkMesh only as a surface, and those two are what '
        'the check highlights draw. '
        'Every candidate cell is judged against the quality limits. '
        'snappyHexMesh reverts a snap or a layer insertion that would breach '
        'one, so they decide what the mesher is allowed to produce rather '
        'than only what is reported afterwards. They are written into '
        'snappyHexMeshDict/meshQualityControls and are not the checkMesh '
        'reporting thresholds above.')

    def __init__(self, facade_client, parent=None, *, engine_id=None):
        super().__init__(facade_client, self.task_id_default, parent,
                         engine_id=engine_id or self.engine_id)

    def build_sections(self, layout) -> None:
        """Mount what checkMesh found, then the limits snappy meshes against."""
        self._findings = CheckMeshFindings(self._client, self)
        layout.addWidget(self._findings)
        self._check = self.adoptPanel(_CheckMeshGroup(self._client, self))
        layout.addWidget(self._check)
        # Plan 37 UF18: what the check wrote, drawn over the mesh on demand.
        self._highlights = CheckHighlightsPanel(self._client)
        layout.addWidget(self._highlights)
        self._quality = self.adoptPanel(
            _MeshQualityGroup(self._client, self, engine_id=self.engine_id))
        layout.addWidget(self._quality)

    def renders_field(self, field_id: str) -> bool:
        """The limits belong to the panel, not to the page's own form.

        DP-153. `snappy.qa` binds exactly ``MESH_QUALITY_FIELDS``, and
        `_MeshQualityGroup` renders exactly ``MESH_QUALITY_FIELDS``, so the
        page drew all twenty-seven twice -- once in Advanced, once in the
        panel -- with no link between the two copies. The panel is the
        surface that was designed for them: it carries the heading, the
        purpose, the caveat that says these are the mesher's own limits and
        not the checkMesh thresholds above, and the R102 widening that lets
        `Min Vol -100000000000.0000000` be read to its last digit.
        """
        return field_id not in _MESH_QUALITY_IDS

    def aligned_forms(self):
        """The two panels are one column: checkMesh's settings over the
        limits they may be compared against (DP-154)."""
        return tuple(group.form_layout()
                     for group in (getattr(self, '_check', None),
                                   getattr(self, '_quality', None))
                     if group is not None)

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()
        for name in ('_check', '_quality'):
            group = getattr(self, name, None)
            if group is not None:
                # DP-339. A refresh keeps an uncommitted edit.
                group.reload(discard_pending=False)
        # The panels have just rewritten their own labels -- CP-09 adds and
        # strips " (inactive)" on reload -- so the shared column is measured
        # again here rather than in `super().refresh()`.
        self._align_field_columns()

    def _moveProseBehindHelp(self) -> None:
        """Say the rest of it through the control DP-230 already built.

        `_description` is where a task page authors what the step is for, and
        `refresh()` rewrites it from the descriptor on every pass, so the two
        panels' paragraphs are appended after that and the help is re-read
        from the label rather than set beside it. The two therefore stay the
        same two strings, which is what the DP-230 gate asserts.
        """
        described = self._description.text().strip()
        if self.HELP_DETAIL not in described:
            described = (described + ' ' + self.HELP_DETAIL).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())

    def refresh_status(self) -> None:
        """Re-read the task's state *and* the report that produced it.

        R100: the run finished `warning`, and the finding behind it -- 1,451
        concave cells and one failed mesh check -- appeared on no surface at
        all. The status line moved and the page did not, so the only way to
        learn what the warning was about was to open the checkMesh log.
        """
        super().refresh_status()
        for name in ('_findings', '_highlights'):
            panel = getattr(self, name, None)
            if panel is not None:
                panel.refresh()


class _CheckMeshGroup(FieldGroupPage):
    """How checkMesh is run, as a panel above the limits it may compare with.

    Plan 31. Six keys reached the dictionary layer and no control: a user who
    wanted a stricter non-orthogonality report, or the offending faces written
    out as a surface they could open, had no way to ask for either. The
    defaults here are OpenFOAM 13's own, and a value left at its default
    writes no flag at all, so a case nobody has touched runs the command line
    it has always run.
    """

    token = 'workflow.quality'
    field_ids = CHECK_MESH_FIELDS
    #: DP-764. `Mesh check` over "Non-orthogonality reported above 70", and
    #: `Mesh quality limits` under it over "Max face non-orthogonality 65":
    #: two numbers for one quantity, and nothing on screen said the first is
    #: where checkMesh starts reporting a face and the second is what the
    #: mesher holds itself to (gui review 0925 workflow-pages #4).
    heading = 'checkMesh report thresholds'
    #: Plan 32 check 12. The paragraph that stood here and the caveat under
    #: it are `SnappyQaPage.HELP_DETAIL` now: they say what the settings are
    #: for and which dictionary they reach, which is reasoning rather than a
    #: blocker, and §4.5 puts reasoning behind the help control. What stays
    #: is one sentence, which SETUP-01 puts on the heading's tooltip and the
    #: accessible description rather than on screen.
    purpose = ('Where checkMesh starts reporting a face. Changing these '
               're-judges the mesh you have; it does not change the mesh.')
    caveat = ''


class _MeshQualityGroup(FieldGroupPage):
    """The ``meshQuality`` values, as a panel inside the QA page."""

    token = 'workflow.quality'
    field_ids = MESH_QUALITY_FIELDS
    #: DP-764. Named for the stages that read them (workflow-pages #4/#5):
    #: snappyHexMesh reverts a snap or layer step that would breach one, so
    #: they act before this page, not on it.
    heading = 'Mesher limits (Snap and Layers)'
    #: Plan 32 check 12, as above: the longer account is
    #: `SnappyQaPage.HELP_DETAIL`. This sentence is the one a reader editing
    #: a limit after the run needs, on the heading's tooltip (SETUP-01).
    purpose = ('The limits snappyHexMesh holds every Snap and Layers step '
               'to. A change takes effect when Snap and Layers run again; '
               'the mesh you have was made with the values set when it ran.')
    caveat = ''
    #: Plan 37 UF15. Said before the edit, not after it: on snappy a limit is
    #: a meshing input, so changing one stales Snap onward and Proceed meshes
    #: again from Snap. Gmsh does not mesh against these, so it is not said
    #: there.
    RERUN_NOTE = 'Changing a limit re-runs the mesh from Snap'

    def __init__(self, facade_client, parent=None, *, engine_id='snappy'):
        # `build()` runs inside the base constructor, so the engine is known
        # before it.
        self._engine_id = str(engine_id or '').strip().lower()
        self._rerunNote = None
        super().__init__(facade_client, parent)

    def build(self) -> None:
        super().build()
        self._widenNumericEditors()
        self._addRerunNote()

    def _addRerunNote(self) -> None:
        """Name the cost of an edit above the limits, on snappy only."""
        if self._engine_id != 'snappy':
            self._rerunNote = None
            return
        note = QLabel(self.tr(self.RERUN_NOTE), self._form)
        note.setObjectName('meshQualityRerunNote')
        note.setWordWrap(True)
        note.setProperty('foammeshStatus', 'info')
        self._form.layout().insertRow(0, note)
        self._rerunNote = note

    def rerun_note(self) -> str:
        """The note's words when it is shown, '' when it is not."""
        note = self._rerunNote
        return note.text() if note is not None else ''

    def _widenNumericEditors(self) -> None:
        """Give every limit room for the whole number it can hold.

        R102. `Min Vol -100000000000.0000000` was cut mid-digit at the right
        edge of the panel: the spin box is built with nine decimals and, where
        the schema leaves a side unbounded, a range of +/-1e12 -- so its widest
        text is twenty-four characters and nothing had reserved the width for
        them. A quality threshold the user cannot read in full is one they
        cannot check, and these particular numbers are the ones snappy reverts
        cells against.

        The width comes from the widest value the box can actually display,
        measured in its own font, and the group's scroll area is allowed a
        horizontal bar for the case where the panel is still narrower. It is
        the innermost scrollable thing here, which is the one C5 says may
        scroll.
        """
        for editor in self._editors.values():
            widget = editor.editor
            if not isinstance(widget, QDoubleSpinBox):
                continue
            widest = max(widget.textFromValue(widget.minimum()),
                         widget.textFromValue(widget.maximum()), key=len)
            widget.setMinimumWidth(
                widget.fontMetrics().horizontalAdvance(widest) + 48)
        scroll = self.findChild(QScrollArea)
        if scroll is not None:
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAsNeeded)


class CheckHighlightsPanel(QGroupBox):
    """The sets and surfaces the last checkMesh wrote, one toggle each.

    Plan 37 UF18. ``-writeSets`` and ``-writeSurfaces`` wrote files nobody
    in the product could see. Each output of the newest kept check is a row:
    ticking it asks the mesh worker for its geometry
    (``quality.check_highlights``) and draws it over the mesh -- point sets
    as points, face and cell sets as surfaces, each labelled with its check.
    A row whose output cannot be drawn (not written, not legacy VTK, over the
    size budget, or from a mesh that has since changed) is disabled and says
    why. Nothing is parsed in this process.

    *overlay* is what takes the actors -- ``DisplayControl`` in the window,
    with ``addOverlay`` / ``removeOverlay``; left out, the window's own is
    looked up when a row is first ticked.
    """

    def __init__(self, facade_client, parent=None, *, overlay=None):
        super().__init__(parent)
        self._client = facade_client
        self._overlay = overlay
        self._layer = None
        self._revision = ''
        self._rows: dict[str, dict] = {}
        self._filling = False
        # DP-1104: shown only where checkMesh is the check (not the SU2
        # route) and only once a check has written its outputs.
        self._routeAllowed = True
        self._hasCheck = False
        self.setObjectName('checkHighlights')
        self.setTitle(self.tr('Highlights from the last check'))
        self.setAccessibleName('Highlights from the last check')
        inner = QVBoxLayout(self)
        self._note = QLabel()
        self._note.setObjectName('checkHighlightsNote')
        self._note.setWordWrap(True)
        inner.addWidget(self._note)
        self._list = QListWidget()
        self._list.setObjectName('checkHighlightsList')
        self._list.setAccessibleName('Check outputs to draw')
        self._list.itemChanged.connect(self._onItemChanged)
        inner.addWidget(self._list)
        self.refresh()

    # -- what there is ------------------------------------------------------ #

    def manifest(self) -> dict:
        from foammesh.view.facade_client import query

        try:
            payload = query(self._client, 'quality.check_artifacts').payload
        except Exception:                                    # noqa: BLE001
            logger.debug('check outputs unavailable', exc_info=True)
            return {}
        return dict(payload or {})

    def refresh(self) -> None:
        from foammesh.core.quality.check_artifacts import highlight_rows

        manifest = self.manifest()
        revision = str(manifest.get('revision') or '')
        rows = highlight_rows(manifest)
        if revision != self._revision or manifest.get('stale'):
            # Another check, or the mesh moved under this one: nothing drawn
            # from the old one may stay on screen.
            self._clearLayer()
        self._revision = revision
        shown = set(self._layer.shown()) if self._layer is not None else set()
        self._rows = {row['name']: row for row in rows}
        self._filling = True
        try:
            self._list.clear()
            for row in rows:
                item = QListWidgetItem(row['label'] if row['enabled'] else
                                       f"{row['label']}\n{row['reason']}")
                item.setData(Qt.ItemDataRole.UserRole, row['name'])
                item.setToolTip(row['reason'] or row['note'] or row['label'])
                if row['enabled']:
                    item.setFlags(Qt.ItemFlag.ItemIsEnabled
                                  | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked
                                       if row['name'] in shown
                                       else Qt.CheckState.Unchecked)
                else:
                    item.setFlags(Qt.ItemFlag.NoItemFlags)
                self._list.addItem(item)
        finally:
            self._filling = False
        self._list.setVisible(bool(rows))
        self._note.setText(self._summary(manifest, rows))
        # DP-1104. Before any check the box held one standing sentence on
        # the default screen; it waits for something to show instead.
        self._hasCheck = bool(manifest)
        self.setVisible(self._routeAllowed and self._hasCheck)

    def setRouteAllowed(self, allowed: bool) -> None:
        """Whether the target's route checks with checkMesh at all."""
        self._routeAllowed = bool(allowed)
        self.setVisible(self._routeAllowed and self._hasCheck)

    def _summary(self, manifest: dict, rows: list[dict]) -> str:
        if not manifest:
            return self.tr('Run checkMesh with Write failed sets on to list '
                           'what it flags here.')
        if manifest.get('stale'):
            return self.tr('The mesh has changed since this check; run the '
                           'check again to draw what it finds now.')
        if not rows:
            return self.tr('The last check wrote no failed sets.')
        return self.tr('Tick an output to draw it over the mesh.')

    def rows(self) -> dict[str, dict]:
        return dict(self._rows)

    def item(self, name: str):
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.data(Qt.ItemDataRole.UserRole) == name:
                return item
        return None

    # -- drawing ------------------------------------------------------------ #

    def _target(self):
        if self._overlay is not None:
            return self._overlay
        target = getattr(self.window(), 'displayControl', None)
        if target is None:
            try:
                from foammesh.app import app
                target = getattr(getattr(app, 'window', None),
                                 'displayControl', None)
            except Exception:                                # noqa: BLE001
                target = None
        return target

    def _ensureLayer(self):
        if self._layer is None:
            target = self._target()
            if target is None or not hasattr(target, 'addOverlay'):
                return None
            from foammesh.rendering.check_highlights import (
                CheckHighlightLayer,
            )
            self._layer = CheckHighlightLayer(
                add=target.addOverlay, remove=target.removeOverlay,
                revision=self._revision)
        return self._layer

    def _clearLayer(self) -> None:
        if self._layer is not None:
            self._layer.clear()

    def _onItemChanged(self, item) -> None:
        if self._filling:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        wanted = item.checkState() == Qt.CheckState.Checked
        coroutine = self.setShown(name, wanted)
        try:
            asyncio.ensure_future(coroutine)
        except RuntimeError:
            coroutine.close()
            self._refuse(name, self.tr('no event loop is running'))

    async def setShown(self, name: str, wanted: bool) -> list[dict]:
        """Draw or take away one output; returns what was refused."""
        from foammesh.view.facade_client import query_async

        if not wanted:
            if self._layer is not None:
                self._layer.hide(name)
            return []
        layer = self._ensureLayer()
        if layer is None:
            return self._refuse(name, self.tr('there is no viewport to draw '
                                              'it in'))
        revision = self._revision
        try:
            result = await query_async(
                self._client, 'quality.check_highlights',
                {'names': [name], 'revision': revision})
        except Exception as error:                           # noqa: BLE001
            return self._refuse(name, str(error) or type(error).__name__)
        payload = dict(result.payload or {})
        if (str(payload.get('revision') or revision) != self._revision
                or not self._isChecked(name)):
            # A newer check landed, or the row was unticked, while the
            # worker read: what came back is no longer wanted.
            return []
        refused = layer.show(payload)
        for one in refused:
            self._refuse(one.get('name') or name,
                         one.get('message') or one.get('reason') or '')
        return refused

    def _isChecked(self, name: str) -> bool:
        item = self.item(name)
        return (item is not None
                and item.checkState() == Qt.CheckState.Checked)

    def _refuse(self, name: str, message: str) -> list[dict]:
        item = self.item(name)
        if item is not None:
            self._filling = True
            try:
                item.setCheckState(Qt.CheckState.Unchecked)
            finally:
                self._filling = False
        self._note.setText(self.tr('{0} was not drawn: {1}').format(
            name, message))
        return [{'name': name, 'message': message}]

    def clear(self) -> None:
        """Take every highlight off the viewport."""
        self._clearLayer()
        self._filling = True
        try:
            for index in range(self._list.count()):
                item = self._list.item(index)
                if item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                    item.setCheckState(Qt.CheckState.Unchecked)
        finally:
            self._filling = False
