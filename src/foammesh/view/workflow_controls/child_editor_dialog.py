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
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QScrollArea,
    QVBoxLayout, QWidget,
)

from foammesh.core.gmsh import fields as _gmsh_fields


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
        'coincident': frozenset(),
        'translational': frozenset({
            'translation_x', 'translation_y', 'translation_z'}),
        'rotational': frozenset({
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
    # The same idea, in the Gmsh schema's dotted spelling.
    'gmsh.periodic_pairs.controls': ('transform', {
        'coincident': frozenset(),
        'translational': frozenset({
            'translation.x', 'translation.y', 'translation.z'}),
        'rotational': frozenset({
            'rotation_centre.x', 'rotation_centre.y', 'rotation_centre.z',
            'rotation_axis.x', 'rotation_axis.y', 'rotation_axis.z',
            'rotation_angle_degrees'}),
    }),
}


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
    """

    def __init__(self, title: str, editors: dict, parent=None,
                 relevance=None, annotations=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setObjectName('childEditorDialog')
        self.setModal(True)
        self._editors = editors
        self._relevance = relevance
        self._annotations = dict(annotations or {})
        self._form = None
        self._rowKeys: list = []

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
        outer.addWidget(self.buttons)

        self._connectRelevance()

    # -- show only the fields the chosen kind actually has ------------------ #

    def _connectRelevance(self):
        if not self._relevance:
            return
        controlling, _branches = self._relevance
        editor = self._editors.get(controlling)
        if editor is None:
            self._relevance = None
            return
        editor.valueChanged.connect(lambda *_args: self.applyRelevance())
        self.applyRelevance()

    def applyRelevance(self):
        """Hide the rows this kind of item does not have.

        Called on open as well as on change, because the dialog is reused: a
        pair edited as rotational and then reopened on a translational one
        would otherwise still be showing the rotation rows.
        """
        if not self._relevance or self._form is None:
            return
        controlling, branches = self._relevance
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
        for row, keys in enumerate(self._rowKeys):
            optional = keys & conditional
            self._form.setRowVisible(row, not optional or bool(keys & applicable))
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
        for key in keys:
            editor = self._editors[key]
            axis = _COMPONENT.match(str(key)).group('axis')
            caption = QLabel(axis.upper(), host)
            caption.setBuddy(editor.editor)
            # The component label is the only thing naming this box, so it is
            # what a screen reader must read out with the quantity.
            editor.editor.setAccessibleName(
                str(getattr(editor.descriptor, 'title', '') or '').strip()
                or f'{humanise(_COMPONENT.match(str(key)).group("base"))} '
                   f'{axis.upper()}')
            row.addWidget(caption)
            row.addWidget(editor.editor, 1)
            unit = unit or (editor.descriptor.unit or '')
        if unit:
            row.addWidget(QLabel(unit, host))
        return host
