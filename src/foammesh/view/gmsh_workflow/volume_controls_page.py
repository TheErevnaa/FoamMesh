"""Gmsh workflow page: gmsh.volume_controls."""
from __future__ import annotations

import dataclasses
import math

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import GmshTaskPage


class VolumeControlPanel(ChildControlPanel):
    """The volume-control table, whose size starts where the global one is.

    DP-502 (MA-05, G1 `jacketed_pipe`). The row's target size is optional in
    the schema, so it had no default, and a number editor shows no default as
    0 -- the one value the schema refuses. Add opened on 0, OK sent it, and
    the only answer was `entity value failed validation`, which names neither
    the field nor the rule. A new row now starts at the global target size,
    which is the size the volume would be meshed at anyway, and a size that
    is not a positive length is refused here, by name and range, with the
    form reopened on the field.
    """

    SIZE_KEY = 'target_size'
    GLOBAL_SIZE_FIELD = 'gmsh.global_sizing.target_size'

    def refreshChoices(self) -> None:
        super().refreshChoices()
        self._startAtGlobalSize()

    def globalTargetSize(self) -> float | None:
        """The global target size, or its default when none is stored."""
        size = None
        try:
            values = self._client.field_values((self.GLOBAL_SIZE_FIELD,))
            stored = values.get(self.GLOBAL_SIZE_FIELD)
            size = getattr(stored, 'value', stored)
        except (AttributeError, KeyError, LookupError, TypeError, ValueError):
            size = None
        if _positive(size) is None:
            try:
                size = self._client.descriptor(self.GLOBAL_SIZE_FIELD).default
            except (AttributeError, KeyError, LookupError, TypeError,
                    ValueError):
                size = None
        return _positive(size)

    def _startAtGlobalSize(self) -> None:
        descriptor = self._descriptors.get(self.SIZE_KEY)
        size = self.globalTargetSize()
        if descriptor is None or size is None or descriptor.default == size:
            return
        derived = dataclasses.replace(descriptor, default=size)
        self._descriptors[self.SIZE_KEY] = derived
        editor = self._editors.get(self.SIZE_KEY)
        if editor is not None:
            editor.descriptor = derived

    def sizeProblem(self, values: dict) -> str:
        """Why the row's target size cannot be sent, or '' when it can."""
        if self.SIZE_KEY not in values:
            return ''
        # DP-614: unset ("Auto") is a size the derivation accepts -- the
        # volume is meshed at the global size -- and it is where a new row
        # starts now that a new project's global size is itself Auto.
        if values.get(self.SIZE_KEY) is None:
            return ''
        if _positive(values.get(self.SIZE_KEY)) is not None:
            return ''
        hint = ''
        size = self.globalTargetSize()
        if size is not None:
            hint = self.tr(' The global target size is {0} m.').format(size)
        return self.tr(
            'Target size must be a positive length in metres (greater than '
            '0 m).') + hint

    def add_child(self) -> None:
        if self._refuseSize():
            if self.editor_dialog().exec() == QDialog.DialogCode.Accepted:
                self.add_child()
            return
        super().add_child()

    def apply_selected(self) -> None:
        if self.selected_key() is not None and self._refuseSize():
            if self.editor_dialog().exec() == QDialog.DialogCode.Accepted:
                self.apply_selected()
            return
        super().apply_selected()

    def _refuseSize(self) -> bool:
        problem = self.sizeProblem(self.child_values())
        if not problem:
            return False
        QMessageBox.warning(self, self.tr('Target size'), problem)
        editor = self._editors.get(self.SIZE_KEY)
        widget = getattr(editor, '_editor', None)
        if widget is not None:
            QTimer.singleShot(0, widget.setFocus)
        return True


def _app():
    from foammesh.app import app

    return app


def _geometryKey(case_path):
    """What the staged geometry is: re-measured when any artifact changes."""
    try:
        from foammesh.core.geometry import GeometryArtifactStore

        return tuple((str(entry.get('geometry_id')), str(entry.get('artifact')),
                      str(entry.get('cad_artifact') or ''))
                     for entry in GeometryArtifactStore(case_path).entries())
    except Exception:                            # noqa: BLE001 - no geometry
        return None


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


class GmshVolumeControlsPage(GmshTaskPage):
    """Per-volume sizing and inclusion, and where the mesh is structured.

    Plan 33 VOLUME-02/04/05. The step opened on a box headed `Guided` with
    one checkbox headed `Automatic` in it, and a folded box headed
    `Advanced` with one headed `Transfinite tri`. Two of those four words
    name the form, one names a Gmsh API call, and the fourth does not say
    what is automated. The box is headed by what it decides instead, the
    switch is named after the meshing it turns on, and the triangular
    switch is drawn on the sizing step beside the other edge controls.
    """

    task_id_default = 'gmsh.volume_controls'

    #: VOLUME-05. Per volume: whether it is meshed at all, how fine it is
    #: and whether it is structured. The structured switch stays: it is a
    #: property of the one volume, unlike the automatic request above it.
    #: DP-560 (0924 rerun). MEASURED at the 420 px settings column: seven
    #: columns scrolled 181 px sideways. Those three answers, and the volume
    #: they are about, are what the table holds; whether the control is on
    #: and its priority are in the editor the row opens.
    #: DP-569 (0924 rerun follow-up). MEASURED at the 360 px settings column
    #: a window under 1600 px wide gets: the stretched `Transfinite` fell to
    #: 43 px of the 93 its heading needs. The volume is what a row is read
    #: by, so it is the column that stretches, and the control's own name --
    #: a second label for the same volume -- is in the editor the row opens.
    COLUMNS = ('scope_token', 'included', 'target_size', 'transfinite')

    #: DP-560. A volume control reaches one volume, and `Geometry scope`
    #: took two lines' worth of width to say less than that.
    HEADINGS = {'scope_token': 'Volume'}

    #: VOLUME-04. Drawn on the sizing step, beside the edge controls: how a
    #: three-sided face is filled is an edge decision, and one field gets
    #: one editor (DP-153).
    HOSTED_FIELDS = ('gmsh.volume_controls.transfinite_tri',)

    #: W-O1. What the Included column does. It explains one column of the
    #: table below and names it, so it is said on the table and in the step
    #: help rather than standing above them.
    INCLUDED_NOTE = (
        'Each included volume publishes as its own cell zone. Clear Included '
        'to drop a volume from the mesh entirely.')

    def build_sections(self, layout) -> None:
        # VOLUME-02. The box the automatic switch sits in is headed by the
        # meshing it turns on, not by the half of the form it is in.
        self._guided.setTitle(self.tr('Structured meshing'))
        note = QLabel(self)
        note.setText(self.tr(self.INCLUDED_NOTE))
        note.setObjectName('gmshVolumeIncludedNote')
        note.setWordWrap(True)
        note.setVisible(False)
        self._includedNote = note
        layout.addWidget(note)

        self.panel = VolumeControlPanel(
            self._client, 'gmsh.volume_controls.controls',
            self.tr('Volume controls'), columns=self.COLUMNS, parent=self,
            headings=self.HEADINGS, stretch='scope_token')
        self.panel.childrenChanged.connect(self.refresh)
        self.panel.setToolTip(self.tr(self.INCLUDED_NOTE))
        self.panel.setAccessibleDescription(self.tr(self.INCLUDED_NOTE))
        layout.addWidget(self.panel)

        # Plan 36 RP11. On Gmsh every solid is a region: while this step is
        # up each is drawn as a translucent volume in its region colour, and
        # listed here with its type and volume. Read-only; typing a solid is
        # the Included column and `geometry.fluid_regions.apply`.
        solids = QLabel(self)
        solids.setObjectName('gmshSolidsSummary')
        solids.setWordWrap(True)
        solids.setVisible(False)
        self._solidsLabel = solids
        layout.addWidget(solids)
        self._solidVolumes = None

    def refresh(self) -> None:
        super().refresh()
        self._moveProseBehindHelp()
        self._retypeSolids()

    # -- Plan 36 RP11: the solids, drawn ------------------------------------ #

    def solidVolumes(self):
        """The solids' viewport volumes, made on first use."""
        if self._solidVolumes is None:
            from foammesh.rendering.solid_volumes import SolidVolumes

            window = getattr(_app(), 'window', None)
            display = getattr(window, 'displayControl', None)
            self._solidVolumes = SolidVolumes(display, self)
            self._solidVolumes.solidsReady.connect(self._solidsReady)
        return self._solidVolumes

    def _solidTyping(self) -> dict:
        from foammesh.core.mesh.cad_solids import typing_of

        try:
            db = self._client.session().state.db
            return typing_of(dict(db.getElements('gmsh/volumeControls') or {}))
        except Exception:                        # noqa: BLE001 - no case yet
            return {}

    def _retypeSolids(self) -> None:
        volumes = self._solidVolumes
        if volumes is None:
            return
        volumes.setTyping(self._solidTyping())
        self._listSolids()

    def _listSolids(self) -> None:
        volumes = self._solidVolumes
        lines = volumes.lines() if volumes is not None else []
        heading = self.tr('Solids (each is a region):')
        if lines and self._farfieldCuts(volumes):
            # DP-915: the far-field box cuts every solid out; they are the
            # obstacle, and the fluid is the space around them.
            heading = self.tr(
                'Solids (the far-field box cuts them out: they are the '
                'obstacle, and the fluid is the space around them):')
        self._solidsLabel.setText(
            heading + '\n' + '\n'.join(lines) if lines else '')
        self._solidsLabel.setVisible(bool(lines))

    def _farfieldCuts(self, volumes) -> bool:
        from foammesh.core.mesh.cad_solids import (
            CaseSolids, farfield_cuts, farfield_enabled,
        )

        try:
            db = self._client.session().state.db
            solids = list(volumes.solids())
        except Exception:                        # noqa: BLE001 - no case yet
            return False
        return farfield_cuts(CaseSolids(solids=solids), farfield_enabled(db))

    def _solidsReady(self) -> None:
        self._retypeSolids()

    def showSolids(self, shown: bool) -> None:
        """Draw (or put away) every solid of the case as its region."""
        if not shown:
            if self._solidVolumes is not None:
                self._solidVolumes.hide()
            return
        try:
            case_path = self._client.case_path
        except Exception:                        # noqa: BLE001 - no case yet
            return
        volumes = self.solidVolumes()
        volumes.setTyping(self._solidTyping())
        volumes.request(case_path, key=_geometryKey(case_path))
        volumes.show()
        self._listSolids()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            self.showSolids(True)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        if not event.spontaneous():
            self.showSolids(False)

    def _moveProseBehindHelp(self) -> None:
        """The Included sentence, through the help control DP-230 built."""
        described = self._description.text().strip()
        if self.INCLUDED_NOTE not in described:
            described = (described + ' ' + self.INCLUDED_NOTE).strip()
            self._description.setText(described)
        self._help.setDetail(described, self._prerequisites.text())

    def renders_field(self, field_id: str) -> bool:
        return field_id not in self.HOSTED_FIELDS
