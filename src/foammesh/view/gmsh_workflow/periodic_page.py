"""Gmsh workflow page: gmsh.periodic."""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from foammesh.view.widgets.empty_state import attach_empty_state

from .base import GmshTaskPage
from .periodic_arrow import PeriodicArrowView, scope_centre
from .periodic_pair_panel import GmshPeriodicPairPanel


class GmshPeriodicPage(GmshTaskPage):
    """Periodic pairs, published as matched OpenFOAM cyclic patches."""

    task_id_default = 'gmsh.periodic'

    #: DP-560 (0924 rerun). MEASURED at the 420 px settings column: seven
    #: columns scrolled 282 px sideways. A pair is read as which surface maps
    #: onto which and how; the switch, the rotation angle and the match
    #: tolerance are in the editor the row opens.
    COLUMNS = ('name', 'master_scope_token', 'slave_scope_token', 'transform')

    #: DP-560. The transform maps the source surface onto the target, as the
    #: note below says; `Master geometry scope` named the store, not that.
    #: Plan 40 UXB1 (#34): source and target, not master and slave -- the
    #: stored keys keep their names, the screen says what each side does.
    HEADINGS = {'master_scope_token': 'Source surface',
                'slave_scope_token': 'Target surface'}

    #: W-O1. What the transform column means, and what a pair pointing the
    #: wrong way does. It explains one column of the table below, so it is
    #: said on the table and in the step help rather than above them.
    TRANSFORM_NOTE = (
        'The transform maps the source surface onto the target. A pair '
        'pointing the wrong way is rejected by Gmsh with "no corresponding '
        'point", so check the direction if a run fails there.')

    def geometryPairLines(self) -> list[str]:
        """One line per cyclic pair the Geometry page defines (UXD9, #32)."""
        # Plan 49 SR3. The sides are named from the pair's periodic relation
        # (the surface names, following a rename), not by patch uuid.
        try:
            document = self._client.configuration()
        except Exception:                                    # noqa: BLE001
            return []
        node = document.get('interfacePairs') or {}
        labels = {}
        for relation in (document.get('surfaceRelations') or {}).values():
            if isinstance(relation, dict):
                labels[str(relation.get('legacySource') or '')] = (
                    relation.get('ownerLabel'), relation.get('neighbourLabel'))
        items = node.items() if isinstance(node, dict) else enumerate(node)
        lines = []
        for key, row in items:
            if not isinstance(row, dict) or str(
                    row.get('coupling') or '') != 'cyclic':
                continue
            state = '' if row.get('enabled', True) else ' (off)'
            owner, neighbour = labels.get(f'interfacePairs/{key}',
                                          (None, None))
            lines.append('%s: %s to %s%s' % (
                row.get('name') or 'pair',
                owner or row.get('masterScopeToken') or '?',
                neighbour or row.get('slaveScopeToken') or '?', state))
        return lines

    def openGeometry(self) -> bool:
        """Stand on the Geometry step, where those pairs are edited."""
        from foammesh.app import app
        from foammesh.db.configurations_schema import Step

        navigation = getattr(getattr(app, 'window', None),
                             '_navigationView', None)
        opener = getattr(navigation, 'openStep', None)
        return bool(opener(Step.GEOMETRY)) if opener is not None else False

    def build_sections(self, layout) -> None:
        # Plan 40 UXD9 (#32). Cyclic pairs are defined once, on Geometry, and
        # reach the run from there; this page lists them and links back, and
        # keeps its own table for the Gmsh-only transforms.
        row = QWidget(self)
        row.setObjectName('gmshPeriodicGeometryPairs')
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        self._geometryPairs = QLabel(row)
        self._geometryPairs.setObjectName('gmshPeriodicGeometryPairsList')
        self._geometryPairs.setWordWrap(True)
        box.addWidget(self._geometryPairs, 1)
        self._editOnGeometry = QPushButton(self.tr('Edit on geometry'), row)
        self._editOnGeometry.setObjectName('gmshPeriodicEditOnGeometry')
        self._editOnGeometry.setAccessibleDescription(self.tr(
            'Interface pairs are defined on the Geometry step.'))
        self._editOnGeometry.clicked.connect(self.openGeometry)
        box.addWidget(self._editOnGeometry, 0)
        layout.addWidget(row)
        self._showGeometryPairs()
        note = QLabel(self)
        note.setText(self.tr(self.TRANSFORM_NOTE))
        note.setObjectName('gmshPeriodicTransformNote')
        note.setWordWrap(True)
        note.setVisible(False)
        self._transformNote = note
        layout.addWidget(note)

        self.panel = GmshPeriodicPairPanel(
            self._client, 'gmsh.periodic_pairs.controls',
            self.tr('Periodic pairs'), columns=self.COLUMNS, parent=self,
            headings=self.HEADINGS, inline=True)
        self.panel.childrenChanged.connect(self.refresh_keeping_edits)
        self.panel.setToolTip(self.tr(self.TRANSFORM_NOTE))
        self.panel.setAccessibleDescription(self.tr(self.TRANSFORM_NOTE))
        layout.addWidget(self.panel)
        # Plan 44 MF20. No pairs is one line under the table's header.
        self.panelEmpty = attach_empty_state(
            self.panel.table, self.tr('No periodic pairs yet.'),
            'gmshPeriodicEmpty', cap_rows=8)

        # Plan 41 GN11 (M-33). The source -> target arrow of the selected pair.
        self.arrow = PeriodicArrowView(self)
        layout.addWidget(self.arrow)
        self.panel.table.itemSelectionChanged.connect(self.refreshArrow)

    def refreshArrow(self) -> None:
        row = None
        key = self.panel.selected_key()
        for item in self.panel.rows():
            if key is not None and item.get('__key__') == key:
                row = item
                break
        if row is None:
            self.arrow.showRow(None)
            return
        self.arrow.showRow(
            row, scope_centre(str(row.get('master_scope_token') or '')),
            scope_centre(str(row.get('slave_scope_token') or '')))

    def _showGeometryPairs(self) -> None:
        lines = self.geometryPairLines()
        self._geometryPairs.setText(
            self.tr('Pairs defined on geometry (used as they are):') + '\n'
            + '\n'.join(lines) if lines else
            self.tr('Pairs defined on Geometry: none. Cyclic pairs are '
                    'added there and used here as they are.'))

    def refresh(self) -> None:
        super().refresh()
        if hasattr(self, '_geometryPairs'):
            self._showGeometryPairs()
        self._moveProseBehindHelp()
        if hasattr(self, 'arrow'):
            self.refreshArrow()

    def _moveProseBehindHelp(self) -> None:
        """The transform sentence, said through the help control DP-230 built."""
        described = self._description.text().strip()
        if self.TRANSFORM_NOTE not in described:
            described = (described + ' ' + self.TRANSFORM_NOTE).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())
