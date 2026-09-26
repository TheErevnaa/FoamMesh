"""Snappy workflow page: ``snappy.snap``.

Plan 30 WP-09 / F-17. Every key the legacy Designer page wrote --
``nSmoothPatch``, ``nSolveIter``, ``nRelaxIter``, ``nFeatureSnapIter``,
``implicitFeatureSnap``, ``explicitFeatureSnap``, ``multiRegionFeatureSnap``
and ``tolerance`` -- is declared on this task, so the form is the descriptor's.
The two feature-snap switches are the pair WP-11 (F-41) split out of the old
``featureSnapType`` enum: OpenFOAM 13 reads them independently and accepts both
on, which the enum could never express.

Plan 33 OF-06. The page used to be one paragraph, with all nine of those
settings folded behind the Advanced disclosure underneath it. Three of the
paragraph clauses said what the feature-snap switches are to each other, and
the form says that instead: the switches are in a group of their own, and
multi-region feature snapping -- which the explicit snapper alone reads --
is on the form only while explicit snapping is on.
"""
from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QGroupBox

from foammesh.view.theming.metrics import apply_form_metrics

from .base import SnappyTaskPage

#: The switch that decides whether multi-region feature snapping is read.
EXPLICIT_FEATURE_SNAP = 'meshing.snap.explicit_feature_snap'

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
    'meshing.snap.implicit_feature_snap',
    EXPLICIT_FEATURE_SNAP,
    MULTI_REGION_FEATURE_SNAP,
)


class SnappySnapPage(SnappyTaskPage):
    """Pull the castellated mesh onto the surface it was cut from."""

    task_id_default = 'snappy.snap'
    run_stage = 'snap'

    def build_sections(self, layout) -> None:
        self._snappingForm = QFormLayout()
        self._featureForm = QFormLayout()
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
            # DP-650. After `setLayout`, so the form has the box to watch:
            # without this call the two groups never had the wrap watcher
            # every other form has (FORM-04, DP-573), and a longer number --
            # the tolerance box at its DP-583 floor, `0.000000001` -- held
            # the Snapping group at 343 px in the 326 px compact column.
            apply_form_metrics(form)
            layout.insertWidget(index, box)

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
        editor = self._editors.get(EXPLICIT_FEATURE_SNAP)
        if editor is None:
            return False
        return bool(self._pending.get(EXPLICIT_FEATURE_SNAP, editor.value()))

    def syncFeatureSnapping(self) -> None:
        """Take multi-region feature snapping off the form when nothing reads.

        FIELD-02. ``multiRegionFeatureSnap`` is read inside the explicit
        feature snapper and nowhere else, so with explicit snapping off it is
        a control whose value cannot change the mesh. It is taken off the
        form rather than greyed, and its pending edit is dropped with it so a
        value nothing reads is not written back.
        """
        editor = self._editors.get(MULTI_REGION_FEATURE_SNAP)
        if editor is None:
            return
        applies = self._explicitSnapIsOn()
        reason = '' if applies else self.tr(
            'Read by explicit feature snapping, which is off.')
        editor.setApplicability(applies, reason)
        if applies:
            self._inactive_fields.pop(MULTI_REGION_FEATURE_SNAP, None)
        else:
            self._pending.pop(MULTI_REGION_FEATURE_SNAP, None)
            self._inactive_fields[MULTI_REGION_FEATURE_SNAP] = reason

    def reload_values(self) -> None:
        super().reload_values()
        self.syncFeatureSnapping()

    def _on_field_changed(self, field_id: str, value) -> None:
        super()._on_field_changed(field_id, value)
        if field_id == EXPLICIT_FEATURE_SNAP:
            self.syncFeatureSnapping()
