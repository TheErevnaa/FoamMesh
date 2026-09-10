"""The Add/Edit Volume form for OpenFOAM's three open searchable surfaces.

Plan 31. ``VolumeDialog`` carried a Designer stack with one page per shape,
and every shape it had was a closed volume. OpenFOAM 13 also offers ``plane``,
``disk`` and ``plate`` -- an infinite plane, a circular disc and an
axis-aligned rectangle -- and none of them could be reached from anywhere in
this program, so refining along a shear layer, across a fan disc or over a
splitter plate meant leaving the app and authoring an STL for a shape OpenFOAM
already knows analytically.

The page is built in code rather than added to ``volume_dialog.ui`` for one
practical reason: the generated ``*_ui.py`` modules are not in the repository
and are regenerated per checkout, so a Designer row is a row that can be
missing in a fresh clone while the Python that reads it is not. Building it
here also lets one widget serve all three shapes, which is what they are: two
vectors, and for the disk a radius.

Field names follow OpenFOAM 13's own keys, because that is what the writer
emits and what the user will read in the dictionary:
``plane`` takes ``point`` and ``normal`` (``plane.C:146``), ``disk`` takes
``origin``, ``normal`` and ``radius`` (``disk_searchableSurface.C:179-181``),
``plate`` takes ``origin`` and ``span`` (``plate_searchableSurface.C:257``).
"""
from __future__ import annotations

from PySide6.QtWidgets import (QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QVBoxLayout, QWidget)

from foammesh.db.configurations_schema import Shape


#: Shape -> (page title, first vector label, second vector label, has radius).
#: The labels are OpenFOAM's own words for the two vectors, so what the form
#: asks for and what the dictionary says are the same thing.
OPEN_SURFACE_FORMS = {
    Shape.PLANE: ('Plane', 'Point', 'Normal', False),
    Shape.DISK: ('Disk', 'Origin', 'Normal', True),
    Shape.PLATE: ('Plate', 'Origin', 'Span', False),
}

#: What each shape is for, and the one rule that will otherwise cost the user
#: a failed meshing run. The plate rule is OpenFOAM's:
#: ``plate_searchableSurface.C:59-85`` refuses a span that does not have two
#: positive entries and exactly one zero one.
OPEN_SURFACE_NOTES = {
    Shape.PLANE: ('An unbounded plane. It cuts the whole domain, so it is for '
                  'refining along a shear layer or a symmetry cut, not for '
                  'enclosing anything.'),
    Shape.DISK: ('A flat circular disc facing along the normal -- a fan face, '
                 'an actuator disc, an orifice.'),
    Shape.PLATE: ('A flat rectangle aligned with the axes. The span needs two '
                  'positive entries and exactly one zero; the zero names the '
                  'direction the plate faces.'),
}


class OpenSurfacePage(QWidget):
    """One stack page for a plane, a disk or a plate.

    ``objectName`` is the shape's OpenFOAM name, because ``showStackPage``
    finds a page by object name and the dialog already keys everything else
    off the same string.
    """

    def __init__(self, shape: Shape, parent=None):
        super().__init__(parent)
        self._shape = shape
        title, firstLabel, secondLabel, hasRadius = OPEN_SURFACE_FORMS[shape]
        self.setObjectName(shape.value)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        note = QLabel(self.tr(OPEN_SURFACE_NOTES[shape]), self)
        note.setWordWrap(True)
        note.setObjectName(f'{shape.value}Note')
        layout.addWidget(note)

        form = QFormLayout()
        self._first = self._vectorRow(form, firstLabel, f'{shape.value}First')
        self._second = self._vectorRow(form, secondLabel, f'{shape.value}Second')
        self._radius = None
        if hasRadius:
            self._radius = QLineEdit(self)
            self._radius.setObjectName(f'{shape.value}Radius')
            form.addRow(self.tr('Radius'), self._radius)
        layout.addLayout(form)

    def _vectorRow(self, form: QFormLayout, label: str, prefix: str):
        row = QWidget(self)
        rowLayout = QHBoxLayout(row)
        rowLayout.setContentsMargins(0, 0, 0, 0)
        edits = []
        for axis in ('X', 'Y', 'Z'):
            edit = QLineEdit(row)
            edit.setObjectName(f'{prefix}{axis}')
            rowLayout.addWidget(edit)
            edits.append(edit)
        form.addRow(self.tr(label), row)
        return tuple(edits)

    # values ---------------------------------------------------------------

    def setValues(self, first, second, radius=None) -> None:
        for edit, value in zip(self._first, first):
            edit.setText(str(value))
        for edit, value in zip(self._second, second):
            edit.setText(str(value))
        if self._radius is not None and radius is not None:
            self._radius.setText(str(radius))

    def first(self) -> tuple[str, str, str]:
        return tuple(edit.text() for edit in self._first)

    def second(self) -> tuple[str, str, str]:
        return tuple(edit.text() for edit in self._second)

    def radius(self) -> str | None:
        return None if self._radius is None else self._radius.text()

    def vectors(self):
        """The two vectors as floats, or ``None`` if either does not parse.

        Returning ``None`` rather than raising is what the dialog's preview
        already expects of every other shape.
        """
        try:
            return ([float(text) for text in self.first()],
                    [float(text) for text in self.second()])
        except ValueError:
            return None
