#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QMessageBox
import qasync

from foammesh.support.simple_db.simple_schema import ValidationError
from foammesh.view.widgets.commit_guard import (CONFLICT_ERRORS, commit_guard,
                                                conflict_message)
from widgets.async_message_box import AsyncMessageBox
from widgets.radio_group import RadioGroup
from foammesh.view.theming.metrics import place_unit
from widgets.rendering.point_widget import PointWidget

from foammesh.app import app
from foammesh.db.configurations_schema import RegionType
from .region_form_ui import Ui_RegionForm
from .seed_feedback import SeedFeedback


class RegionForm(QWidget):
    regionAdded = Signal(str)
    regionEdited = Signal(str)
    canceled = Signal()

    _types = {
        'fluid': RegionType.FLUID.value,
        'solid': RegionType.SOLID.value
    }

    _baseName = 'Region_'

    def __init__(self, renderingView, owner):
        super().__init__()
        self._ui = Ui_RegionForm()
        self._ui.setupUi(self)

        self._id = None
        self._dbElement = None
        self._typeRadios = RadioGroup(self._ui.typeRadios)
        self._pointWidget = PointWidget(renderingView)
        if app.themeManager is not None:
            if app.themeManager.tokens is not None:
                self._pointWidget.applyTheme(app.themeManager.tokens)
            app.themeManager.themeChanged.connect(
                lambda _name: self._pointWidget.applyTheme(app.themeManager.tokens))
        self._owner = owner
        self._defaultPoint = None

        self._pointWidget.off()
        self._typeRadios.setObjectMap(self._types)

        # DP-198. The row label said `Point inside the region (m)`, three
        # boxes away from the numbers it was describing. One `m` closes the
        # row instead, after the last of them.
        place_unit(self._ui.z, 'm')

        # DP-818. Whether the seed is in the fluid, said under the point as it
        # moves. The page owns the stored glyph, so this line never takes it.
        self._seedFeedback = SeedFeedback(self._ui.widget_6)
        self._ui.formLayout_2.addRow('', self._seedFeedback)

        self.hide()

        self._connectSignalsSlots()

    def setBounds(self, bounds):
        self._defaultPoint = self._pointWidget.setBounds(bounds)
        self._showBoundsHint(bounds)

    def _showBoundsHint(self, bounds) -> None:
        """Say, in the same unit as the fields, where the geometry actually is.

        E1. The fields are metres, the toolbar reports the model in
        millimetres, and neither said so -- a point typed in millimetres lands
        outside the domain, and the run comes back with an empty mesh and no
        error to explain it. The bounds are the cheapest possible check.
        """
        hint = getattr(self._ui, 'pointHint', None)
        if hint is None:
            return
        try:
            limits = [float(getattr(bounds, legacy)
                            if hasattr(bounds, legacy)
                            else getattr(bounds, modern))
                      for legacy, modern in (('xMin', 'xmin'), ('xMax', 'xmax'),
                                             ('yMin', 'ymin'), ('yMax', 'ymax'),
                                             ('zMin', 'zmin'), ('zMax', 'zmax'))]
        except (AttributeError, TypeError, ValueError):
            hint.setText('')
            return
        hint.setText(self.tr(
            'The geometry spans X {0} to {1}, Y {2} to {3}, Z {4} to {5} '
            'metres.').format(*(f'{value:.4g}' for value in limits)))

    def setSuggestedPoint(self, point) -> None:
        """Offer a seed known to be inside the geometry, not the box centre.

        Plan 28 WP6. The default was the centre of the bounding box, which for
        anything that is not a convex blob -- an elbow, an annulus, a duct with
        a bend -- sits outside the fluid. snappy then meshed the wrong side of
        the surface, or refused to launch at all. It stays a suggestion: the
        fields and the 3D handle both still move it.
        """
        if point is None:
            return
        try:
            self._defaultPoint = tuple(float(value) for value in point)
        except (TypeError, ValueError):
            return

    def setOwner(self, widget):
        self._owner = widget

    def owner(self):
        return self._owner

    def setupForAdding(self):
        self._id = None
        db = app.facadeClient.checkout()
        self._dbElement = db.newElement('region')

        self._ui.regionForm.setTitle(self.tr('Add region'))
        self._ui.name.setText(f"{self._baseName}{db.getUniqueSeq('region', 'name', self._baseName, 1)}")
        self._setPoint(self._defaultPoint)
        self._ui.ok.setText(self.tr('Add'))

        self._ui.name.setFocus()
        self._pointWidget.on()
        self._beginSeedFeedback()

    def setupForEditing(self, id_):
        self._id = id_
        self._dbElement = app.facadeClient.checkout(f'region/{id_}')

        self._ui.regionForm.setTitle(self.tr('Edit region'))
        self._ui.name.setText(self._dbElement.getValue('name'))
        self._typeRadios.setValue(self._dbElement.getValue('type'))
        x, y, z = self._dbElement.getVector('point')
        self._ui.x.setText(x)
        self._ui.y.setText(y)
        self._ui.z.setText(z)
        self._movePointWidget()
        self._ui.ok.setText(self.tr('Update'))

        self._pointWidget.on()
        self._beginSeedFeedback()

    def cancel(self):
        self._pointWidget.off()
        self._seedFeedback.end()
        self.canceled.emit()

    def seedFeedback(self):
        return self._seedFeedback

    def _beginSeedFeedback(self):
        self._seedFeedback.begin(None)
        self._judgeSeed()

    def _judgeSeed(self):
        try:
            point = (float(self._ui.x.text()), float(self._ui.y.text()),
                     float(self._ui.z.text()))
        except (TypeError, ValueError):
            return
        self._seedFeedback.judge(point)

    def _connectSignalsSlots(self):
        self._ui.x.editingFinished.connect(self._movePointWidget)
        self._ui.y.editingFinished.connect(self._movePointWidget)
        self._ui.z.editingFinished.connect(self._movePointWidget)
        self._pointWidget.pointMoved.connect(self._setPoint)

        self._ui.name.textChanged.connect(self._validate)
        self._ui.ok.clicked.connect(self._accept)
        self._ui.cancel.clicked.connect(self.cancel)

    def _movePointWidget(self):
        try:
            x = float(self._ui.x.pFloat())
            y = float(self._ui.y.pFloat())
            z = float(self._ui.z.pFloat())
        except ValueError:
            return

        rx, ry, rz = self._pointWidget.setPosition(x, y, z)  # real position returned
        self._setPoint((rx, ry, rz))

    def _setPoint(self, point):
        x, y, z = point
        self._ui.x.setText('{:.6g}'.format(x))
        self._ui.y.setText('{:.6g}'.format(y))
        self._ui.z.setText('{:.6g}'.format(z))
        # Typed or dragged, every new point is judged again.
        self._judgeSeed()

    def _validate(self):
        self._ui.ok.setEnabled(self._ui.name.text().strip() != '')

    @qasync.asyncSlot()
    async def _accept(self):
        # One click, one commit: the write queue may hold this for
        # seconds on a cold machine, and a second OK in that window
        # submitted the same element twice.
        with commit_guard(self._ui.ok):
            name = self._ui.name.text()
            if app.facadeClient.checkout().getElements(
                    'region', lambda i, e: e['name'] == name and i != self._id):
                QMessageBox.warning(self, self.tr('Input error'), self.tr('Region "{0}" already exists.').format(name))
                return

            try:
                x = self._ui.x.pFloat(self.tr('X coordinate'))
                y = self._ui.y.pFloat(self.tr('Y coordinate'))
                z = self._ui.z.pFloat(self.tr('Z coordinate'))

            except ValueError as e:
                await AsyncMessageBox().warning(
                    self, self.tr('Input error'), str(e))
                return

            if not self._pointWidget.bounds().includes((float(x), float(y), float(z))):
                QMessageBox.warning(self, self.tr('Input error'),
                                        self.tr('The point is outside the bounding box.'))
                return

            try:
                self._dbElement.setValue('name', name)
                self._dbElement.setValue('type', self._typeRadios.value())
                self._dbElement.setValue('point/x', str(x), self.tr('Point'))
                self._dbElement.setValue('point/y', str(y), self.tr('Point'))
                self._dbElement.setValue('point/z', str(z), self.tr('Point'))

                if self._id:    # Edit
                    await app.facadeClient.commit_working_copy(self._dbElement, action='edit region')
                    self.regionEdited.emit(self._id)
                else:           # Add
                    db = app.facadeClient.checkout()
                    id_ = db.addElement('region', self._dbElement)
                    await app.facadeClient.commit_working_copy(db, action='edit region')

                    # DP-16. If another copy had already taken this key the
                    # commit moved the region rather than refusing it, and the
                    # id in hand names somebody else's region until it is put
                    # through the remap.
                    self.regionAdded.emit(db.remappedKey('region', id_))

                self._pointWidget.off()
                self._seedFeedback.end()
            except CONFLICT_ERRORS as error:
                # Stay open: the user's entries are still here, and the only
                # thing that changed is what the case looked like underneath.
                QMessageBox.warning(
                    self, self.tr('Case changed'), self.tr(conflict_message(error)))
            except ValidationError as e:
                QMessageBox.warning(self, self.tr("Input error"), e.toMessage())
