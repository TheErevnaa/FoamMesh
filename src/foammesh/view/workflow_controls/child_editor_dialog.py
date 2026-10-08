"""A proper editor for one child item, instead of a column of stacked rows.

The repeatable controls -- interface pairs, size fields, periodic pairs -- carry
between eight and twenty-two fields each. Rendered as one label-and-editor row
per field down the navigation column, a geometry interface pair became a
seventeen-row ladder that pushed the geometry tree it belongs to out of view,
and the tree is the thing a user is actually looking at on that page.

So the list stays in the panel, where it is small and scannable, and the fields
move into this dialog, which has the width to lay them out as a form.

**Vector components are one row, not three.** ``translation_x``,
``translation_y`` and ``translation_z`` are one quantity; stacking them as three
separate rows triples the height and reads as three unrelated settings. They are
detected from the field names rather than listed per collection, so a schema
that adds a vector gets the treatment without anyone remembering to ask for it.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QBoxLayout, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel,
    QScrollArea, QVBoxLayout, QWidget,
)

from foammesh.core.gmsh import fields as _gmsh_fields
from foammesh.db.configurations_schema import (
    GmshPeriodicTransform, InterfaceTransform, ThicknessModel,
)


#: ``translation_x`` / ``centre.x`` -- both spellings are in the schema.
_COMPONENT = re.compile(r'^(?P<base>.+?)[._](?P<axis>[xyz])$')

#: Past this many rows the form is taller than a modest screen, so it scrolls
#: rather than growing a dialog that cannot be fully seen.
_SCROLL_AFTER_ROWS = 12


#: Plan 30 WP12 (F-43). The size-field branches come from the Gmsh control
#: register, so the form and the runner cannot disagree about which parameter
#: belongs to which field type.
_SIZE_FIELD_TYPES = tuple(_gmsh_fields.FIELD_PARAMETERS)
_size_field_keys = _gmsh_fields.editor_keys_for_field_type


#: ``collection id -> (controlling field, {value: fields that apply})``.
#:
#: An interface pair offers a translation *and* a rotation centre *and* a
#: rotation axis *and* an angle, all at once, whatever kind of interface it is.
#: On a coincident pair none of them mean anything, and on a translational one
#: the four rotation rows are noise a user has to know to ignore. Worse, they
#: are not merely useless: a rotation angle left over from an earlier edit is
#: stored and read back, so the form cannot be trusted to say what the pair
#: actually is.
#:
#: A field named in no branch is always shown -- name, enabled, the scopes and
#: the match tolerance apply to every pair.
RELEVANCE: dict = {
    'geometry.interface_pairs': ('transform', {
        # Faces already touch; there is no transform to describe.
        InterfaceTransform.COINCIDENT.value: frozenset(),
        InterfaceTransform.TRANSLATIONAL.value: frozenset({
            'translation_x', 'translation_y', 'translation_z'}),
        InterfaceTransform.ROTATIONAL.value: frozenset({
            'rotation_centre_x', 'rotation_centre_y', 'rotation_centre_z',
            'rotation_axis_x', 'rotation_axis_y', 'rotation_axis_z',
            'rotation_angle_degrees'}),
    }),
    # R153. With Mode set to `Size`, Segments, Law and Coefficient stayed
    # enabled and editable although only Local Size is read, so the dialog
    # accepted three values it would silently discard and the saved control
    # did not do what the form said it would.
    'gmsh.curve_controls.controls': ('mode', {
        # The control is off; nothing below it applies.
        'none': frozenset(),
        # Plan 31 CP-08 adds the grading direction, which is only a
        # transfinite idea, and moves the structured-surface request
        # here for the same reason Coefficient is here: in Size mode
        # nothing reads it.
        # Plan 31 FC-C adds the corner points, which are read only when a
        # structured surface is asked for -- and only matter on a face with
        # more than four candidate corners.
        'transfinite': frozenset({'segments', 'law', 'coefficient',
                                  'reverse_grading',
                                  'transfinite_surface', 'corner_points'}),
        'size': frozenset({'local_size'}),
    }),
    # Plan 30 WP12 (F-43). Which parameters a size field actually uses is
    # settled by the control register, which is pinned against the runner's
    # own branches; the dialog only renders what it says. Built from the
    # register rather than typed out, so a new field type cannot arrive with
    # a stale list here.
    'gmsh.size_fields.controls': ('field_type', {
        kind: _size_field_keys(kind) for kind in _SIZE_FIELD_TYPES}),
    # C31-11. ``surfaceZonesInfo.C:70-82`` only reads ``insidePoint`` when
    # ``mode`` is ``insidePoint``; on the other three modes the three
    # coordinate boxes are a value the mesher will never look at, which is
    # exactly the kind of field that gets filled in and then believed.
    'meshing.castellation.surface_refinements': ('zone_mode', {
        'inside': frozenset(),
        'outside': frozenset(),
        'insidePoint': frozenset({
            'zone_inside_point.x', 'zone_inside_point.y',
            'zone_inside_point.z'}),
        'none': frozenset(),
    }),
    # C31-11. A layer group either follows its geometry binding or matches
    # patch names with an expression. Showing the expression box on a group
    # that is bound to geometry invites someone to fill it in and expect it
    # to be read.
    'meshing.layers.groups': ('patch_selector', {
        'geometry': frozenset(),
        'pattern': frozenset({'patch_pattern'}),
    }),
    # The same idea, in the Gmsh schema's dotted spelling -- and in the Gmsh
    # schema's own two words. PERIODIC-01: this branch map was copied from the
    # geometry pair above and kept that field's three values, which a Gmsh
    # periodic pair has never been able to hold. Every lookup missed, every
    # miss took the "unrecognised value" path below, and that path shows the
    # union of all branches: a translation pair offered a rotation centre, a
    # rotation axis and an angle, and a rotation pair offered a translation.
    # The values are read off the enum the field is typed by now, so a
    # renamed member cannot leave a rule behind that quietly matches nothing.
    'gmsh.periodic_pairs.controls': ('transform', {
        GmshPeriodicTransform.TRANSLATION.value: frozenset({
            'translation.x', 'translation.y', 'translation.z'}),
        GmshPeriodicTransform.ROTATION.value: frozenset({
            'rotation_centre.x', 'rotation_centre.y', 'rotation_centre.z',
            'rotation_axis.x', 'rotation_axis.y', 'rotation_axis.z',
            'rotation_angle_degrees'}),
    }),
}


#: DP-596 (field audit 0924 snappy-back D4). Rules a collection needs *beside*
#: its ``RELEVANCE`` entry, because one row can be decided by more than one
#: field. A layer group's pattern box follows its patch selector, and its
#: thickness boxes follow its thickness model: the writer
#: (``CaseBuilder._canonical_layer_values``) reads exactly two of the four
#: stack values per model, so the other two were boxes a user filled in and
#: the mesh never saw. A row is shown only when every rule naming it agrees.
ALSO_RELEVANT: dict = {
    'meshing.layers.groups': (('thickness_model', {
        ThicknessModel.FIRST_AND_OVERALL.value: frozenset({
            'first_layer_thickness', 'thickness'}),
        ThicknessModel.FIRST_AND_EXPANSION.value: frozenset({
            'first_layer_thickness', 'expansion_ratio'}),
        ThicknessModel.FINAL_AND_OVERALL.value: frozenset({
            'final_layer_thickness', 'thickness'}),
        ThicknessModel.FINAL_AND_EXPANSION.value: frozenset({
            'final_layer_thickness', 'expansion_ratio'}),
        ThicknessModel.OVERALL_AND_EXPANSION.value: frozenset({
            'thickness', 'expansion_ratio'}),
        ThicknessModel.FIRST_AND_RELATIVE_FINAL.value: frozenset({
            'first_layer_thickness', 'final_layer_thickness'}),
    }),),
}


def relevance_rules(collection_id: str) -> tuple:
    """Every ``(controlling field, branches)`` rule *collection_id* has."""
    rules = []
    if collection_id in RELEVANCE:
        rules.append(RELEVANCE[collection_id])
    rules.extend(ALSO_RELEVANT.get(collection_id, ()))
    return tuple(rules)


def _as_rules(relevance) -> tuple:
    """One rule, or a sequence of them, as a tuple of rules."""
    if not relevance:
        return ()
    if len(relevance) == 2 and isinstance(relevance[0], str):
        return (tuple(relevance),)
    return tuple(relevance)


def check_relevance(relevance: dict) -> None:
    """Refuse a rule written against a value the field cannot hold.

    PERIODIC-01. A branch keyed by a word the controlling field has never
    offered is not a rule with no effect: it is a rule that never matches, and
    a value that matches nothing falls through to the union of every branch --
    so the one collection whose map was copied from another schema showed
    every conditional row on every pair, which is the arrangement this whole
    table exists to prevent. It is silent, it survives a rename, and it looks
    exactly like a table that works.

    So the map is checked against the registry the editors are built from: the
    branches of a rule must be the controlling field's own values, and a
    branch may only name fields that collection has. Checked at import, on the
    module the dialog lives in, so a rule written against the wrong spelling
    cannot reach a user.
    """
    from foammesh.core.facade.field_adapters import EntityAdapter
    from foammesh.core.facade.fields import REGISTRY

    for collection_id, (controlling, branches) in dict(relevance).items():
        collection = REGISTRY.collections.get(collection_id)
        if collection is None:
            raise ValueError(
                f'{collection_id} has a relevance rule and no collection')
        fields = EntityAdapter(collection).fields
        descriptor = getattr(fields.get(controlling), 'descriptor', None)
        if descriptor is None:
            raise ValueError(
                f'{collection_id} is branched on {controlling!r}, which is '
                f'not one of its fields')
        offered = frozenset(descriptor.enum or ())
        if not offered:
            raise ValueError(
                f'{collection_id}.{controlling} holds no fixed set of '
                f'values, so it cannot decide which rows apply')
        written = frozenset(str(value) for value in branches)
        if written != offered:
            raise ValueError(
                f'{collection_id} branches on {sorted(written)} and '
                f'{controlling} holds {sorted(offered)}')
        for value, keys in branches.items():
            unknown = sorted(str(key) for key in keys if key not in fields)
            if unknown:
                raise ValueError(
                    f'{collection_id} shows {unknown} for {value!r} and has '
                    f'no such field')


check_relevance(RELEVANCE)
for _collection, _rules in ALSO_RELEVANT.items():
    for _rule in _rules:
        check_relevance({_collection: _rule})


def component_groups(keys) -> list:
    """``keys`` in order, with x/y/z runs collapsed into one entry.

    Returns a list of ``(base, [keys])``: a scalar field is a group of one, a
    vector a group of two or three. Only a *complete* run counts -- a lone
    ``radius_x`` stays a scalar rather than being drawn as a crippled vector.
    """
    groups: list = []
    pending: dict = {}
    for key in keys:
        match = _COMPONENT.match(str(key))
        if match is None:
            pending.clear()
            groups.append((str(key), [key]))
            continue
        base = match.group('base')
        if base in pending and pending[base] is groups[-1][1]:
            pending[base].append(key)
            continue
        pending.clear()
        bucket = [key]
        pending[base] = bucket
        groups.append((base, bucket))
    return [(base, list(bucket)) for base, bucket in groups]


def humanise(name: str) -> str:
    """``rotation_centre`` -> ``Rotation centre``. The fallback only."""
    text = str(name).replace('_', ' ').replace('.', ' ').strip()
    return text[:1].upper() + text[1:] if text else text


def vector_title(base: str, editor) -> str:
    """The quantity's name, taken from the schema rather than reinvented.

    A component is titled ``Translation X``; dropping the axis gives the
    schema's own wording and casing for the group, so the vector rows read the
    same as the scalar rows beside them instead of announcing themselves as
    something the GUI made up.
    """
    title = str(getattr(editor.descriptor, 'title', '') or '').strip()
    if len(title) > 2 and title[-1].upper() in 'XYZ' and title[-2] in ' _.':
        return title[:-2].strip()
    return humanise(base)


class ChildEditorDialog(QDialog):
    """Modal form over one child item's editors.

    The editors belong to the panel and are only *shown* here: the panel reads
    their values after the dialog closes, so a headless caller can set them and
    commit without a dialog ever existing.

    Plan 36 RP2. Modal is the default and every panel but one keeps it. A
    panel whose row is placed in the viewport shows the same form docked in
    its own column (`showDocked`) or as a tool window (`showFloating`), so
    the viewport stays live while the form is open.
    """

    def __init__(self, title: str, editors: dict, parent=None,
                 relevance=None, annotations=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName('childEditorDialog')
        self.setModal(True)
        self._editors = editors
        # DP-596: one rule or several, held as a tuple of rules.
        self._relevance = _as_rules(relevance)
        self._annotations = dict(annotations or {})
        self._form = None
        self._rowKeys: list = []
        #: DP-922. The x/y/z row of each vector, stacked when docked.
        self._vectorRows: list = []

        outer = QVBoxLayout(self)
        form_host = QWidget(self)
        form = QFormLayout(form_host)
        self._form = form
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)

        rows = 0
        for base, keys in component_groups(editors):
            if len(keys) > 1:
                form.addRow(vector_title(base, editors[keys[0]]),
                            self._vector(keys))
            else:
                editor = editors[keys[0]]
                # The editor's own label carries the schema's title and its
                # tooltip; reusing it keeps documentation attached to the field.
                form.addRow(editor.label, self._scalar(editor))
            self._rowKeys.append(frozenset(keys))
            rows += 1
            # C31-11. A note that belongs to one field is placed under that
            # field, and carries the same visibility: a preview of what a
            # pattern matches is meaningless on a row that is hidden because
            # the item does not match by pattern at all.
            for key in keys:
                widget = self._annotations.get(key)
                if widget is None:
                    continue
                widget.setParent(form_host)
                # DP-1251. `releaseEditors` hides a note when the panel drops
                # this dialog (every band-picker rebuild does), and a panel
                # with no relevance rule never shows a row again -- so the
                # second form a band panel opened had lost its notes. Shown
                # here; `applyRelevance` below still hides a row that does
                # not apply.
                widget.setVisible(True)
                form.addRow('', widget)
                self._rowKeys.append(frozenset(keys))
                rows += 1

        if rows > _SCROLL_AFTER_ROWS:
            area = QScrollArea(self)
            area.setWidgetResizable(True)
            area.setWidget(form_host)
            area.setFrameShape(QScrollArea.Shape.NoFrame)
            # Scroll vertically, never horizontally. Left to itself the area
            # settles narrower than the form and puts a horizontal scrollbar
            # under it, so reading one row means dragging -- which is a worse
            # form than the tall one this replaced.
            area.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            area.setMinimumWidth(
                form_host.sizeHint().width()
                + area.verticalScrollBar().sizeHint().width()
                + 2 * area.frameWidth())
            outer.addWidget(area, 1)
        else:
            outer.addWidget(form_host, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        # DP-1190. Set while the step this row belongs to is locked.
        self._lockedNoteText = ''
        outer.addWidget(self.buttons)

        self._connectRelevance()

    def setLockedNote(self, note: str) -> None:
        """DP-1190. Say the step is locked and take OK away (or give it back).

        A run that finishes while this form is open locks the step under it;
        OK would send an edit the facade refuses. Cancel stays. The note is
        OK's tooltip and the panel's answer to any press that gets through.
        """
        note = str(note or '')
        self._lockedNoteText = note
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok is not None:
            ok.setEnabled(not note)
            ok.setToolTip(note)
            ok.setAccessibleDescription(note)

    def lockedNote(self) -> str:
        return self._lockedNoteText

    def accept(self) -> None:
        # DP-1190. A locked step's form only closes by Cancel, whatever
        # pressed OK (Enter included).
        if self._lockedNoteText:
            return
        super().accept()

    # -- Plan 36 RP2: shown without blocking the viewport ------------------ #

    def showDocked(self, host) -> None:
        """Show the form inside *host*, a widget with a layout, not modal.

        DP-922 (Plan 36 RP12 live pass). Docked, the form is as narrow as the
        settings column it sits in: each vector's X, Y and Z go one under
        the other, so three number boxes side by side no longer make the
        form -- and the column -- wider than the window gives it.
        """
        self.setModal(False)
        self.setCompact(True)
        if self.isWindow() or self.parent() is not host:
            self.setParent(host, Qt.WindowType.Widget)
            host.layout().addWidget(self)
        host.show()
        self.show()

    def showFloating(self, owner) -> None:
        """Show the form as a tool window over *owner*, not modal."""
        self.setModal(False)
        self.setCompact(False)
        if not self.isWindow() or self.parent() is not owner:
            self.setParent(owner, Qt.WindowType.Tool)
        self.show()
        self.raise_()

    def isDocked(self) -> bool:
        return not self.isWindow()

    def setCompact(self, compact: bool) -> None:
        """DP-922. Stack each vector's components (docked) or line them up."""
        direction = (QBoxLayout.Direction.TopToBottom if compact
                     else QBoxLayout.Direction.LeftToRight)
        for row in self._vectorRows:
            row.setDirection(direction)
        self._compact = bool(compact)

    def isCompact(self) -> bool:
        return bool(getattr(self, '_compact', False))

    # -- show only the fields the chosen kind actually has ------------------ #

    def _connectRelevance(self):
        if not self._relevance:
            return
        self._relevance = tuple(
            rule for rule in self._relevance if rule[0] in self._editors)
        self._relevanceSources = []
        for controlling, _branches in self._relevance:
            editor = self._editors[controlling]
            self._relevanceSources.append(editor)
            editor.valueChanged.connect(self._relevanceChanged)
        self.applyRelevance()

    def _relevanceChanged(self, *_args):
        self.applyRelevance()

    def releaseEditors(self, owner=None):
        """Hand the panel's widgets back before this dialog is destroyed.

        DP-495 (audit MA-07). The form lays out the panel's own editor widgets,
        so while it exists they are this dialog's children -- and deleting a
        dialog deletes its children. The panel drops the dialog whenever a
        scope picker is rebuilt, which is every time the geometry is prepared
        or discarded, and every editor it had *not* rebuilt went with it: the
        next Add wrote its default into a Name box that no longer existed and
        opened nothing, on both engine routes. The editors are the panel's and
        outlive any one opening, so each widget goes back to the editor that
        owns it, the annotations go back to their owner, and the relevance
        rule stops listening before the dialog it drives is gone.

        *owner* is where the annotation widgets wait for the next opening.
        """
        for source in getattr(self, '_relevanceSources', ()):
            try:
                source.valueChanged.disconnect(self._relevanceChanged)
            except (RuntimeError, TypeError):
                pass
        self._relevanceSources = []
        for editor in list(self._editors.values()):
            for widget in (editor.label, editor.editor, editor.unit_label):
                try:
                    if widget is not None and widget.parent() is not editor:
                        widget.setParent(editor)
                except RuntimeError:
                    # Already gone with an editor the panel replaced.
                    continue
        for widget in self._annotations.values():
            try:
                widget.setParent(owner)
                widget.hide()
            except RuntimeError:
                continue

    def applyRelevance(self):
        """Hide the rows this kind of item does not have.

        Called on open as well as on change, because the dialog is reused: a
        pair edited as rotational and then reopened on a translational one
        would otherwise still be showing the rotation rows.
        """
        if not self._relevance or self._form is None:
            return
        judged = []
        for controlling, branches in self._relevance:
            value = self._editors[controlling].value()
            value = getattr(value, 'value', value)
            applicable = branches.get(str(value))
            if applicable is None:
                # An unrecognised value is not a reason to hide fields: showing
                # too much is a nuisance, hiding a field someone needs is a wall.
                applicable = frozenset().union(*branches.values()) if branches \
                    else frozenset()
            conditional = frozenset().union(*branches.values()) if branches \
                else frozenset()
            judged.append((conditional, applicable))
        for row, keys in enumerate(self._rowKeys):
            # DP-596: a row stays only if every rule naming it keeps it.
            visible = all(not (keys & conditional) or bool(keys & applicable)
                          for conditional, applicable in judged)
            self._form.setRowVisible(row, visible)
        self.adjustSize()

    def _scalar(self, editor) -> QWidget:
        host = QWidget(self)
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(editor.editor, 1)
        if editor.descriptor.unit:
            row.addWidget(editor.unit_label)
        return host

    def _vector(self, keys) -> QWidget:
        host = QWidget(self)
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        unit = ''
        pair = None
        for key in keys:
            editor = self._editors[key]
            axis = _COMPONENT.match(str(key)).group('axis')
            # DP-922. Each component is a caption-and-box pair of its own, so
            # a docked form can stack the pairs (`setCompact`).
            pair = QWidget(host)
            pairRow = QHBoxLayout(pair)
            pairRow.setContentsMargins(0, 0, 0, 0)
            caption = QLabel(axis.upper(), pair)
            caption.setBuddy(editor.editor)
            # The component label is the only thing naming this box, so it is
            # what a screen reader must read out with the quantity.
            editor.editor.setAccessibleName(
                str(getattr(editor.descriptor, 'title', '') or '').strip()
                or f'{humanise(_COMPONENT.match(str(key)).group("base"))} '
                   f'{axis.upper()}')
            pairRow.addWidget(caption)
            pairRow.addWidget(editor.editor, 1)
            row.addWidget(pair, 1)
            unit = unit or (editor.descriptor.unit or '')
        if unit and pair is not None:
            pair.layout().addWidget(QLabel(unit, pair))
        self._vectorRows.append(row)
        return host
