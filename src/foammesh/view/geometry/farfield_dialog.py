"""Geometry > Farfield...: the one farfield record, edited from the Geometry page.

Plan 37 (user request after UF13/UF14): "add a Farfield entry to the Geometry
page". The farfield -- a box, sphere or cylinder around the model with the
bodies cut out of it -- is one authored record, ``gmsh/farfield``, read by
both engines through ``farfield_spec.read`` (plans/evidence/plan37/
farfield-spec.md). Until now it could be switched on only among the Gmsh
import-and-healing settings on Preparation, and a snappy case could only read
a note about it.

This dialog is not a Geometry-page primitive (DP-668 keeps the Add dialog's
box, sphere and cylinder as refinement and zone shapes): it edits the same
facade fields the Gmsh panel edits, through ``configuration.patch``, so there
is no second store to drift. Every control is the registry's own
``FieldEditor``; which of them are offered follows the shape and centre mode
as they are changed (``farfield_spec.leaves_read``), and a spec the engines
would refuse -- a zero axis, a sphere too small to hold the geometry -- is
said here and cannot be accepted.
"""
from __future__ import annotations

import qasync
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QVBoxLayout, QWidget)

from foammesh.core.mesh import farfield_spec
from foammesh.view.theming.metrics import align_unit_column, apply_form_metrics
from foammesh.view.workflow_controls.field_widgets import FieldEditor

PREFIX = 'gmsh.describe_geometry.'

#: Field id -> the ``gmsh/farfield`` leaf it writes. The Gmsh panel renders
#: the same ids (``ui_location == workflow.gmsh.describe_geometry``).
FIELDS = (
    (PREFIX + 'enabled', 'enabled'),
    (PREFIX + 'shape', 'shape'),
    (PREFIX + 'centre_mode', 'centreMode'),
    (PREFIX + 'centre.x', 'centre'),
    (PREFIX + 'centre.y', 'centre'),
    (PREFIX + 'centre.z', 'centre'),
    (PREFIX + 'padding', 'padding'),
    (PREFIX + 'radius', 'radius'),
    (PREFIX + 'length', 'length'),
    (PREFIX + 'axis.x', 'axis'),
    (PREFIX + 'axis.y', 'axis'),
    (PREFIX + 'axis.z', 'axis'),
)
ENABLED = PREFIX + 'enabled'
SHAPE = PREFIX + 'shape'
CENTRE_MODE = PREFIX + 'centre_mode'
ALWAYS = ('enabled', 'shape', 'centreMode')

ENGINE_FIELD = 'mesh.engine'
UNSELECTED = 'unselected'


def engine_of(client) -> str:
    """The case's meshing engine id, or ``''`` when it cannot be read."""
    try:
        value = client.field_values((ENGINE_FIELD,))[ENGINE_FIELD]
    except Exception:                                        # noqa: BLE001
        return ''
    value = getattr(value, 'value', value)
    return str(value or '').strip().lower()


def spec_from_values(values: dict) -> farfield_spec.FarfieldSpec:
    """A ``FarfieldSpec`` from field-id values (the dialog's or the store's)."""
    leaves: dict = {}
    for field_id, leaf in FIELDS:
        if field_id not in values:
            continue
        value = values[field_id]
        if leaf in ('centre', 'axis'):
            leaves.setdefault(leaf, {})[field_id.rsplit('.', 1)[-1]] = value
        else:
            leaves[leaf] = value
    for leaf in ('centre', 'axis'):
        if leaf in leaves:
            part = leaves[leaf]
            default = (0.0, 0.0, 0.0) if leaf == 'centre' else (1.0, 0.0, 0.0)
            leaves[leaf] = tuple(
                part.get(axis, default[index])
                for index, axis in enumerate('xyz'))
    return farfield_spec.FarfieldSpec.from_values(leaves)


def read_spec(client) -> farfield_spec.FarfieldSpec:
    """The case's farfield, read through the facade client's fields."""
    ids = tuple(field_id for field_id, _leaf in FIELDS)
    try:
        values = client.field_values(ids)
    except Exception:                                        # noqa: BLE001
        return farfield_spec.FarfieldSpec()
    return spec_from_values(values)


def lock_text(error) -> str:
    """UF5: why the write was refused, and the way through (unlock)."""
    details = getattr(error, 'details', None) or {}
    titles = [str(title) for title in (details.get('titles') or ())]
    what = ', '.join(titles) if titles else 'A meshing step'
    verb = 'is' if len(titles) <= 1 else 'are'
    return (f'Not saved: {what} {verb} locked, because the mesh on disk was '
            'made with this farfield. Unlock the step (right-click it in the '
            'workflow) to change it; the results after it are discarded.')


class FarfieldDialog(QDialog):
    """Switch the farfield on or off and say its shape and size."""

    #: Emitted after the facade accepted the edit.
    farfieldChanged = Signal()

    def __init__(self, client, parent=None, *, bounds=None, engine='',
                 geometry_kind=farfield_spec.CAD):
        """*bounds* is the model extent in Gmsh order (``xmin ymin zmin xmax
        ymax zmax``, metres) or ``None`` when there is no geometry yet."""
        super().__init__(parent)
        self.setObjectName('farfieldDialog')
        self.setWindowTitle(self.tr('Farfield'))
        self._client = client
        self._bounds = list(bounds) if bounds is not None else None
        self._engine = str(engine or '')
        self._kind = geometry_kind
        self._editors: dict[str, FieldEditor] = {}
        self._loaded: dict = {}

        self._fitTimer = QTimer(self)
        self._fitTimer.setSingleShot(True)
        self._fitTimer.setInterval(0)
        self._fitTimer.timeout.connect(self._fitContents)

        outer = QVBoxLayout(self)
        intro = QLabel(self.tr(
            'The outer boundary of an external-flow domain: a box, sphere or '
            'cylinder around the geometry, with the bodies cut out of it. '
            'Both engines build this one farfield; the Gmsh import settings '
            'on Preparation edit the same values.'), self)
        intro.setWordWrap(True)
        intro.setObjectName('farfieldDialogIntro')
        outer.addWidget(intro)

        form_host = QWidget(self)
        self._form = QFormLayout(form_host)
        apply_form_metrics(self._form)
        for field_id, _leaf in FIELDS:
            editor = FieldEditor(client.descriptor(field_id), self)
            row = QHBoxLayout()
            row.addWidget(editor.editor, 1)
            row.addWidget(editor.unit_label)
            cell = QWidget(form_host)
            cell.setLayout(row)
            self._form.addRow(editor.label, cell)
            editor.setRow(self._form, cell)
            editor.valueChanged.connect(self._changed)
            self._editors[field_id] = editor
        outer.addWidget(form_host)

        self._fitNote = QLabel(self)
        self._fitNote.setObjectName('farfieldDialogFitNote')
        self._fitNote.setWordWrap(True)
        self._fitNote.hide()
        outer.addWidget(self._fitNote)

        self._supportNote = QLabel(self)
        self._supportNote.setObjectName('farfieldDialogSupportNote')
        self._supportNote.setWordWrap(True)
        self._supportNote.setProperty('foammeshStatus', 'warning')
        self._supportNote.hide()
        outer.addWidget(self._supportNote)

        self._problems = QLabel(self)
        self._problems.setObjectName('farfieldDialogProblems')
        self._problems.setWordWrap(True)
        self._problems.setProperty('foammeshStatus', 'error')
        self._problems.hide()
        outer.addWidget(self._problems)

        # Plan 37 UF5. A write to a locked step is refused by the facade; the
        # refusal is said here, under the fields it refused, and the dialog
        # stays open with what was typed.
        self._lockNotice = QLabel(self)
        self._lockNotice.setObjectName('farfieldDialogLockNotice')
        self._lockNotice.setWordWrap(True)
        self._lockNotice.setProperty('foammeshStatus', 'error')
        self._lockNotice.hide()
        outer.addWidget(self._lockNotice)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel, self)
        self._buttons.setObjectName('farfieldDialogButtons')
        self._ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setObjectName('farfieldDialogOk')
        self._buttons.button(
            QDialogButtonBox.StandardButton.Cancel).setObjectName(
                'farfieldDialogCancel')
        self._buttons.accepted.connect(self._okClicked)
        self._buttons.rejected.connect(self.reject)
        outer.addWidget(self._buttons)

        self.load()

    # -- reading ------------------------------------------------------------ #

    def load(self) -> None:
        values = self._client.field_values(tuple(self._editors))
        self._loaded = {}
        for field_id, editor in self._editors.items():
            value = values.get(field_id, editor.descriptor.default)
            editor.set_value(value)
            # What the editor shows, so an unchanged control writes nothing.
            self._loaded[field_id] = editor.value()
        self._lockNotice.hide()
        self._refresh()

    def editor(self, field_id: str) -> FieldEditor:
        return self._editors[field_id]

    def values(self) -> dict:
        return {field_id: editor.value()
                for field_id, editor in self._editors.items()}

    def spec(self) -> farfield_spec.FarfieldSpec:
        return spec_from_values(self.values())

    def shownFieldIds(self) -> tuple[str, ...]:
        return tuple(field_id for field_id, editor in self._editors.items()
                     if editor.applies())

    def problems(self) -> list[str]:
        return farfield_spec.check(self.spec(), self._bounds)

    def pendingPatch(self) -> dict:
        """The edited fields the store does not hold yet.

        Only what was changed: an untouched radius on a box is not rewritten,
        so opening and accepting the dialog stales nothing; and a control
        the chosen shape does not read is kept out, as on every field page.
        """
        return {field_id: value for field_id, value in self.values().items()
                if self._editors[field_id].applies()
                and value != self._loaded.get(field_id)}

    def lockNotice(self) -> QLabel:
        return self._lockNotice

    def problemsLabel(self) -> QLabel:
        return self._problems

    def okButton(self):
        return self._ok

    # -- live behaviour ----------------------------------------------------- #

    def _changed(self, field_id: str, _value) -> None:
        self._lockNotice.hide()
        if field_id in (SHAPE, CENTRE_MODE, ENABLED):
            self._fit()
        self._refresh()

    def _fit(self) -> None:
        """A sphere or cylinder chosen too small is sized to hold the model.

        ``farfield_primitives.suggest``, the rule the spec names for this: a
        new sphere or cylinder should not start out refused. Only with an
        automatic centre -- an explicit one is the user's -- and only when
        the current size would be refused for containment.
        """
        self._fitNote.hide()
        spec = self.spec()
        if (self._bounds is None or not spec.enabled
                or spec.shape == 'box' or spec.centre_mode != 'auto'
                or spec.problems() or not farfield_spec.check(
                    spec, self._bounds)):
            return
        sized = farfield_spec.fitted(spec, self._bounds)
        self._editors[PREFIX + 'radius'].set_value(sized.radius)
        if spec.shape == 'cylinder':
            self._editors[PREFIX + 'length'].set_value(sized.length)
        self._fitNote.setText(self.tr(
            'Sized to hold the geometry with room around it; change it if you '
            'need a different size.'))
        self._fitNote.show()

    def _refresh(self) -> None:
        """Offer what the shape reads; say what would be refused."""
        spec = self.spec()
        read = set(ALWAYS) | set(farfield_spec.leaves_read(
            spec.shape, spec.centre_mode))
        for field_id, leaf in FIELDS:
            editor = self._editors[field_id]
            applies = leaf in read
            reason = '' if applies else self.tr(
                'The farfield {0} does not read this.').format(spec.shape)
            if editor.applies() != applies:
                editor.setApplicability(applies, reason)
        align_unit_column((self._form,))

        problems = self.problems()
        self._problems.setText('\n'.join(
            self.tr('Not accepted: {0}.').format(problem)
            for problem in problems))
        self._problems.setVisible(bool(problems))
        self._ok.setEnabled(not problems)

        note = ''
        if spec.enabled and self._engine and self._engine != UNSELECTED:
            answer = farfield_spec.support(spec, self._engine, self._kind)
            if not answer.supported and not problems:
                note = answer.reason[:1].upper() + answer.reason[1:] + '.'
        self._supportNote.setText(note)
        self._supportNote.setVisible(bool(note))
        self._fitContents()
        # Rows just shown settle their heights on the next pass of the
        # event loop; fit again once they have.
        self._fitTimer.start()

    def _fitContents(self) -> None:
        """Grow to what the fields and the notes under them need.

        Plan 37 UF20. A top-level window does not follow a word-wrapped
        label's height-for-width, so a "Not accepted" line or a support
        note appearing under the fields took its height out of the rows
        above: live, the shape, centre and radius were drawn clipped to half
        their height while the refusal named the radius. Grow only, so a
        dialog the user made larger keeps its size.
        """
        if not self.isVisible():
            return
        layout = self.layout()
        layout.activate()
        # Plan 37 UF20 follow-up, MEASURED live (FOLLOWUP_WALK.md DP
        # candidate 5): the cylinder rows widen the form, and the dialog kept
        # the width the box had -- 332 px against a 364 px size hint -- so
        # the radius spinbox was drawn clipped ("2.0920025C"). Grow the width
        # to the size hint too (within the screen), then the height for it.
        width = self.width()
        hint = layout.totalSizeHint().width()
        screen = self.screen()
        if screen is not None:
            hint = min(hint, screen.availableGeometry().width())
        width = max(width, hint)
        need = (layout.totalHeightForWidth(width)
                if layout.hasHeightForWidth()
                else layout.totalSizeHint().height())
        if width > self.width() or need > self.height():
            self.resize(width, max(need, self.height()))

    def showEvent(self, event) -> None:
        # The first size is the layout's size hint, which leaves out the
        # wrapped intro's height-for-width as well.
        super().showEvent(event)
        self._fitContents()

    # -- writing ------------------------------------------------------------ #

    @qasync.asyncSlot()
    async def _okClicked(self):
        if await self.save():
            self.accept()

    async def save(self) -> bool:
        """Write the edit through the facade; ``True`` once it is accepted."""
        if self.problems():
            self._refresh()
            return False
        patch = self.pendingPatch()
        if not patch:
            return True
        from foammesh.core.facade.domain_operations import TaskLockedError
        from foammesh.core.facade.errors import FacadeError

        try:
            result = await self._client.run('configuration.patch',
                                             {'patch': patch})
        except TaskLockedError as error:
            self._lockNotice.setText(self.tr(lock_text(error)))
            self._lockNotice.show()
            self._fitContents()
            return False
        except FacadeError as error:
            self._problems.setText(self.tr('Not saved: {0}').format(error))
            self._problems.show()
            self._fitContents()
            return False
        if getattr(result, 'status', 'accepted') != 'accepted':
            self._problems.setText(self.tr('Not saved: {0}').format(
                getattr(result, 'message', '') or 'the edit was refused'))
            self._problems.show()
            self._fitContents()
            return False
        self.load()
        self.farfieldChanged.emit()
        return True


async def switch_off(client) -> None:
    """Turn the farfield off, keeping its shape and size (Remove on the list).

    Raises what the facade raises -- a locked step refuses it like any other
    edit of the record.
    """
    result = await client.run('configuration.patch',
                              {'patch': {ENABLED: False}})
    if getattr(result, 'status', 'accepted') != 'accepted':
        from foammesh.core.facade.errors import FacadeError

        raise FacadeError(str(getattr(result, 'message', '')
                              or 'the farfield could not be switched off'))
