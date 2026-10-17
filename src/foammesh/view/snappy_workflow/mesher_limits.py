"""The limits snappyHexMesh holds the mesh to, edited on the stage that reads them.

Plan 40 UXD9 (#4 values, #5, N7). The twenty-nine ``meshQualityControls``
leaves were edited on Quality, a page that runs after the mesh is made:
changing one there said "re-runs the mesh from Snap", which is a page sending
the reader somewhere else to find out what the setting acts on. They live
where they act now:

* the strict limits and the merge tolerance on **Snap**, which is the first
  stage snappyHexMesh reverts a step against them;
* the relaxed limits on **Layers**, the only stage that reads the
  ``relaxed`` sub-dictionary.

Each field has exactly one editor in the whole workflow. Quality keeps
checkMesh's own report thresholds, which are a different group.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDoubleSpinBox, QLabel, QScrollArea

from foammesh.core.quality.policy import TIER_RELAXED, TIER_STRICT
from foammesh.view.quality_tier_note import TierNote
from foammesh.view.workflow_controls.field_group_page import FieldGroupPage


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


#: Every limit, strict and relaxed, plus ``mergeTolerance``, derived from the
#: one quality policy (Plan 30 WP-04): a leaf added there reaches the editors
#: and a leaf the schema lacks is an import-time error.
MESH_QUALITY_FIELDS = _mesh_quality_fields()
_RELAXED_MARK = '.relaxed.'

#: Edited on Snap.
STRICT_LIMIT_FIELDS = tuple(
    field_id for field_id in MESH_QUALITY_FIELDS
    if _RELAXED_MARK not in field_id)
#: Edited on Layers.
RELAXED_LIMIT_FIELDS = tuple(
    field_id for field_id in MESH_QUALITY_FIELDS
    if _RELAXED_MARK in field_id)

#: Plan 37 UF15. Said before the edit, not after it: on snappy a limit is a
#: meshing input, so changing one stales Snap onward and Proceed meshes again
#: from Snap.
RERUN_NOTE = 'Changing a limit re-runs the mesh from Snap'


class MesherLimitsGroup(FieldGroupPage):
    """A group of the ``meshQualityControls`` limits, as a panel in a page."""

    def __init__(self, facade_client, parent=None, *, engine_id='snappy',
                 relaxed=False):
        # `build()` runs inside the base constructor, so what the group is
        # must be known before it.
        self._engine_id = str(engine_id or '').strip().lower()
        self._rerunNote = None
        self.field_ids = (RELAXED_LIMIT_FIELDS if relaxed
                          else STRICT_LIMIT_FIELDS)
        self.token = 'workflow.layers' if relaxed else 'workflow.snap'
        self.heading = ('Relaxed mesher limits' if relaxed
                        else 'Mesher limits')
        self.purpose = (
            'The looser limits snappyHexMesh falls back to when layer '
            'addition cannot meet the strict ones. An empty box keeps the '
            'strict value. A change takes effect when Snap and Layers run '
            'again.' if relaxed else
            'The limits snappyHexMesh holds every Snap and Layers step to; '
            'it reverts a step that would breach one. A change takes effect '
            'when Snap and Layers run again; the mesh you have was made '
            'with the values set when it ran.')
        self.caveat = ''
        self.relaxed = bool(relaxed)
        super().__init__(facade_client, parent)

    def build(self) -> None:
        super().build()
        self._widenNumericEditors()
        self._addRerunNote()
        self._addTierNote()

    def _addTierNote(self) -> None:
        """Plan 40 UXD9 (N7). Name the tier this group's values belong to."""
        note = TierNote(TIER_RELAXED if self.relaxed else TIER_STRICT,
                        self._client, self._form)
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

    def _addRerunNote(self) -> None:
        if self._engine_id != 'snappy':
            self._rerunNote = None
            return
        note = QLabel(self.tr(RERUN_NOTE), self._form)
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
        """Give every limit room for the whole number it can hold (R102)."""
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


def fold_limits(page, layout, group: MesherLimitsGroup):
    """Plan 44 MF27: the limits behind one closed header on their page.

    The header carries the group's heading, so the box inside drops its own
    title rather than say it twice; the purpose is the header's tooltip.
    """
    from .sheet_folds import fold_behind, open_on_filter

    header = fold_behind(
        layout, group, group.tr(group.heading),
        'relaxedMesherLimitsFold' if group.relaxed else 'mesherLimitsFold',
        tooltip=group.tr(group.purpose))
    group._form.setTitle('')
    open_on_filter(page, header)
    return header
