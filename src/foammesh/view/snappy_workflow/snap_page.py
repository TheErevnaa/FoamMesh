"""Snappy workflow page: ``snappy.snap``.

Plan 30 WP-09 / F-17. Every key the legacy Designer page wrote --
``nSmoothPatch``, ``nSolveIter``, ``nRelaxIter``, ``nFeatureSnapIter``,
``implicitFeatureSnap``, ``explicitFeatureSnap``, ``multiRegionFeatureSnap``
and ``tolerance`` -- is declared on this task, so the form is the descriptor's.
DP-5712 (engineer report 17). WP-11 (F-41) had split the feature-snap choice
into two independent switches, and a person could tick both. It is one choice
again -- Explicit, Implicit or None -- and the writer turns it into the two
``snapControls`` switches with at most one of them true.

Plan 33 OF-06. The page used to be one paragraph, with all nine of those
settings folded behind the Advanced disclosure underneath it. Three of the
paragraph clauses said what the feature-snap switches are to each other, and
the form says that instead: the choice is in a group of its own, and
multi-region feature snapping -- which the explicit snapper alone reads, where
regions or zones meet -- is on the form only with explicit snapping on a case
with more than one region or zone (DP-5712).
"""
from __future__ import annotations

from PySide6.QtWidgets import QGroupBox

from foammesh.view.theming.sheet_form import SheetForm

from .base import SnappyTaskPage
from .mesher_limits import MesherLimitsGroup, fold_limits

#: The choice that decides whether multi-region feature snapping is read.
FEATURE_SNAPPING = 'meshing.snap.feature_snapping'

#: Read by the explicit snapper only (``snappyHexMeshDict`` snapControls).
MULTI_REGION_FEATURE_SNAP = 'meshing.snap.multi_region_feature_snap'

#: Field ids drawn in each of the two groups, in the order they are read.
_SNAPPING = (
    'meshing.snap.smooth_patch_iterations',
    'meshing.snap.n_solve_iter',
    'meshing.snap.n_relax_iter',
    'meshing.snap.tolerance',
    'meshing.snap.detect_near_surfaces_snap',
)
_FEATURE_SNAPPING = (
    'meshing.snap.n_feature_snap_iter',
    FEATURE_SNAPPING,
    MULTI_REGION_FEATURE_SNAP,
)


class SnappySnapPage(SnappyTaskPage):
    """Pull the castellated mesh onto the surface it was cut from."""

    task_id_default = 'snappy.snap'
    run_stage = 'snap'

    def build_sections(self, layout) -> None:
        # Plan 44 MF14. The sheet's grid, not QFormLayout: label | editor on
        # one row at every width, and one label column shared with the rest
        # of the page (`_align_field_columns`).
        self._snappingForm = SheetForm()
        self._featureForm = SheetForm()
        # In front of everything the base page put in the body: the two
        # groups are the page, and the guided box, the Advanced disclosure
        # and the details link read after them.
        for index, (title, name, form) in enumerate((
                (self.tr('Snapping'), 'snapIterationBox', self._snappingForm),
                (self.tr('Feature snapping'), 'snapFeatureBox',
                 self._featureForm))):
            box = QGroupBox(title, self)
            box.setObjectName(name)
            box.setLayout(form)
            # DP-650. The two groups never had the wrap watcher every other
            # form has, and a longer number held the Snapping group at 343 px
            # in the 326 px compact column. A SheetForm never wraps a label
            # above its editor and its editor column shrinks, so the
            # condition DP-650 guarded is gone by construction.
            form.setContentsMargins(0, 0, 0, 0)
            layout.insertWidget(index, box)
        # Plan 40 UXD9 (#4, #5). The limits a snap step is reverted against
        # are edited where the step runs, not on the page after the mesh.
        self._limits = self.adoptPanel(
            MesherLimitsGroup(self._client, self, engine_id=self.engine_id))
        layout.insertWidget(2, self._limits)
        # Plan 44 MF27. The limits are a group a reader opens on purpose:
        # 297 px of them took Snap 190 px past the 300 px sheet. They sit
        # behind one closed header now, the essentials stay on the sheet.
        self._limitsFold = fold_limits(self, layout, self._limits)

    # -- where each field lands -------------------------------------------- #

    def field_form(self, field_id: str, classification):
        if field_id in _SNAPPING:
            return getattr(self, '_snappingForm', None)
        if field_id in _FEATURE_SNAPPING:
            return getattr(self, '_featureForm', None)
        return None

    def field_forms(self):
        return tuple(
            form for form in (getattr(self, '_snappingForm', None),
                              getattr(self, '_featureForm', None))
            if form is not None)

    # -- the one setting that is read only sometimes ------------------------ #

    def _explicitSnapIsOn(self) -> bool:
        from foammesh.db.configurations_schema import featureSnapSwitches

        editor = self._editors.get(FEATURE_SNAPPING)
        if editor is None:
            return False
        choice = self._pending.get(FEATURE_SNAPPING, editor.value())
        return featureSnapSwitches(choice)[1]

    def _isMultiRegionCase(self) -> bool:
        from foammesh.db.configurations_schema import isMultiRegionCase

        try:
            return isMultiRegionCase(self._client.checkout())
        except Exception:  # noqa: BLE001 - no case open yet
            return False

    def syncFeatureSnapping(self) -> None:
        """Take multi-region feature snapping off the form when nothing reads.

        FIELD-02. ``multiRegionFeatureSnap`` is read inside the explicit
        feature snapper, where points sit on more than one region, and
        nowhere else. With another choice, or on a case with one region and
        no zone, it is a control whose value cannot change the mesh (DP-5712:
        the writer writes it false there). It is taken off the form rather
        than greyed, as every inapplicable input is, and its pending edit is
        dropped with it so a value nothing reads is not written back.
        """
        editor = self._editors.get(MULTI_REGION_FEATURE_SNAP)
        if editor is None:
            return
        if not self._explicitSnapIsOn():
            reason = self.tr(
                'Read by explicit feature snapping, which is not the choice.')
        elif not self._isMultiRegionCase():
            reason = self.tr(
                'Read where regions or zones meet; this case has one region '
                'and no zone.')
        else:
            reason = ''
        applies = not reason
        editor.setApplicability(applies, reason)
        if applies:
            self._inactive_fields.pop(MULTI_REGION_FEATURE_SNAP, None)
        else:
            self._pending.pop(MULTI_REGION_FEATURE_SNAP, None)
            self._inactive_fields[MULTI_REGION_FEATURE_SNAP] = reason

    def aligned_forms(self):
        """The limits panel reads as part of this page's one column (DP-154)."""
        limits = getattr(self, '_limits', None)
        return (limits.form_layout(),) if limits is not None else ()

    def reload_values(self) -> None:
        super().reload_values()
        self.syncFeatureSnapping()
        limits = getattr(self, '_limits', None)
        if limits is not None:
            # DP-339. A refresh keeps an uncommitted edit.
            limits.reload(discard_pending=False)
            # DP-156. The panel's reload aligns its own unit column alone;
            # the page puts it back in the page's one column after it.
            self._align_field_columns()

    def _on_field_changed(self, field_id: str, value) -> None:
        super()._on_field_changed(field_id, value)
        if field_id == FEATURE_SNAPPING:
            self.syncFeatureSnapping()
