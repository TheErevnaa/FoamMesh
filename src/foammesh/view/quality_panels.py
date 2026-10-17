"""The panels of the one Quality page: checkMesh's settings and its outputs.

Plan 42 SG18. They lived in the snappy QA page module and the Gmsh Quality
page imported them from there; both engines mount them from here now.
"""
from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGroupBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout,
)

from foammesh.core.quality.policy import TIER_REPORT
from foammesh.view.quality_tier_note import TierNote
from foammesh.view.widgets.folded_note import FoldedNote
from foammesh.view.workflow_controls.field_group_page import FieldGroupPage

logger = logging.getLogger(__name__)

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

    def build(self) -> None:
        super().build()
        # Plan 40 UXD9 (N7). The report threshold is one of three values for
        # max non-orthogonality; say which, and where the other two are.
        note = TierNote(TIER_REPORT, self._client, self._form)
        self._form.layout().insertRow(0, note)
        self._tierNote = note

    def tier_note(self) -> str:
        note = getattr(self, '_tierNote', None)
        return note.text() if note is not None else ''

    def reload(self, *args, **kwargs) -> None:
        super().reload(*args, **kwargs)
        note = getattr(self, '_tierNote', None)
        if note is not None:
            note.refresh()


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
        # Plan 44 FIT (DP-2668): one line until opened.
        self._note = FoldedNote()
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
