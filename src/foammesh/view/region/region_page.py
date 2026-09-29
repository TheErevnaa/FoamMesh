#!/usr/bin/env python
# -*- coding: utf-8 -*-

from PySide6.QtWidgets import QVBoxLayout

from foammesh.app import app
from foammesh.view.step_page import StepPage
from foammesh.db.configurations_schema import CFDType
from .region_form import RegionForm
from .region_card import RegionCard
from foammesh.view.facade_client import query, submit


class RegionPage(StepPage):
    def __init__(self, ui):
        super().__init__(ui, ui.regionPage)

        self._ui = ui
        self._form = None
        self._regions = {}
        self._bounds = None

        self._form = RegionForm(self._ui.renderingView, self)
        self._focusing = self._form

        layout = QVBoxLayout(self._ui.regionList)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addStretch()

        self._ui.regionList.layout().insertWidget(0, self._form)

        self._connectSignalsSlots()

    def isNextStepAvailable(self):
        db = app.facadeClient.checkout()
        if not self._regions:
            if db.elementCount('region') > 0:
                return True

            self._ui.regionEmptyMessage.setVisible(True)
            self._ui.regionValidationMessage.hide()
            return False

        self._ui.regionEmptyMessage.setVisible(False)

        # hasFluid = False
        available = True
        for card in self._regions.values():
            # hasFluid = hasFluid or card.type() == RegionType.FLUID.value
            # R148. `self._bounds` is None until a geometry actor is in the
            # scene, and this raised `AttributeError` out of `_add()` when it
            # was not -- which is inside `load()`, the only code that rebuilds
            # the cards. One raise there left the page permanently showing a
            # heading, a `+` and nothing else. An unmeasurable box cannot
            # contradict a point, so the containment check is skipped rather
            # than crashed on.
            if self._bounds is None:
                card.hideWarning()
            elif not self._bounds.includes(card.point()):
                card.showWarning(self.tr(
                    'Invalid point — outside bounding box.'))
                available = False
            elif reason := self._seedWarning(card.point()):
                card.showWarning(reason)
            else:
                card.hideWarning()

        multiRegion = len(self._regions) > 1
        hasInterRegionInterface = db.elementCount(
            'geometry', lambda i, e: e['cfdType'] == CFDType.INTERFACE.value and e['interRegion']) > 0

        if multiRegion == hasInterRegionInterface:
            self._ui.regionValidationMessage.hide()
            return available

        if multiRegion and self._regionsSeparated():
            # DP-543. Regions that never meet have no interface to couple.
            self._ui.regionValidationMessage.hide()
            return available

        if multiRegion:
            self._ui.regionValidationMessage.setText(self.tr(
                'No inter-region interface is configured, and these regions may '
                'share a boundary. Add one on the Geometry step to couple them.'))
        else:
            self._ui.regionValidationMessage.setText(self.tr(
                'Only one region point is configured while an inter-region interface is configured on the Geometry step.'))
        self._ui.regionValidationMessage.show()

        return False

    def _regionsSeparated(self) -> bool:
        """Do the seeds sit in closed volumes that provably never touch?

        DP-543. MEASURED on the 24 Sep 2026 audit case S6: two closed cubes
        1 m apart, a Fluid seed in each. This page said no interface was
        configured, the sentence stood through Quality and Export, and
        checkMesh read back two fully disconnected regions with no shared
        face -- there was nothing to couple. The warning is for regions that
        touch; ``regions_separated`` answers True only when that is ruled
        out, so anything it cannot judge keeps the warning.
        """
        project = getattr(app, 'project', None)
        if project is None or getattr(project, 'path', None) is None:
            return False
        try:
            from foammesh.core.geometry import GeometryArtifactStore
            from foammesh.core.geometry.region_contact import regions_separated
            entries = GeometryArtifactStore(project.path).entries()
            return regions_separated(
                entries, [card.point() for card in self._regions.values()])
        except Exception:                                   # noqa: BLE001
            return False

    def _seedWarning(self, point) -> str:
        """Why this seed is not a material point, or '' (R164).

        MEASURED on annulus.stl: the page took any point as the material
        point with no test that it lies inside the geometry, the default fell
        outside the annular gap, and `(0.0425, 0, 0.2)` had to be worked out
        by hand from the bounding box and the two radii. The failure then
        appeared several tasks later as an unexplained meshing error.

        This warns and does not block. An open surface has no inside, so the
        probe cannot tell "outside the fluid" from "not a closed volume", and
        a page that refused Next on the second answer would lock a case that
        meshes. The launch validation is still the gate; this is the notice
        that arrives while the point is still on screen.
        """
        try:
            payload = query(
                app.facadeClient, 'geometry.fluid_seed.check',
                {'point': [float(value) for value in point]}).payload
        except Exception:                                   # noqa: BLE001
            # A point that cannot be judged is not a point known to be wrong.
            return ''
        if (not payload.get('known') or payload.get('inside')
                or payload.get('external')):
            # DP-574: an external-flow seed (outside the body, inside the
            # background box) is a valid material point.
            return ''
        if payload.get('on_surface'):
            return self.tr('Invalid point — on the geometry surface.')
        return self.tr('Invalid point — outside the geometry.')

    def open(self):
        if self._loaded:
            self._updateBounds()
            self._syncCards()
        else:
            self.load()

    async def show(self, isWorkingStep: bool, batchRunning: bool):
        if not self._loaded:
            self.load()
        else:
            self._syncCards()

        app.window.meshManager.unload()
        # DP-818. The seed glyphs are drawn while this page is up.
        self._showMarkers(True)

    def _syncCards(self):
        """Put a card back for every region the database still holds (R148).

        MEASURED: pressing Next on 3. Region emptied the page -- the
        `annulus_fluid (Fluid) / Point (0.0425, 0, 0.2)` card disappeared and
        the panel was left with a heading, a `+` and an Unlock button -- while
        the seed was still drawn in the viewport and the mesher still used it.
        Accepting the task commits, a commit raises TRANSACTION_APPLIED, and
        the window's refresh calls `StepManager.load()`, which does
        `page.unload(); page.load()` on every page: `unload()` deletes every
        card, and the cards come back only if `load()` runs to the end. It is
        the single rebuild path in this page, so anything that stops it takes
        the region display away for the rest of the session.

        The cards are not the record -- the database is -- so showing this
        page reconciles the two instead of trusting a flag.
        """
        try:
            regions = app.facadeClient.checkout().getElements('region')
        except Exception:                                   # noqa: BLE001
            # A page that cannot read the case shows what it already has.
            return
        for id_ in regions:
            if id_ not in self._regions:
                self._add(id_)

    async def hide(self):
        self._form.cancel()
        self._ui.regionValidationMessage.hide()
        self._showMarkers(False)

        return True

    def removeForm(self, form):
        self._ui.regionList.layout().removeWidget(self._form)
        self._form.setOwner(None)

    def _outputPath(self):
        return None

    def _enableStep(self):
        self._ui.regionAdd.setEnabled(True)

        for card in self._regions.values():
            card.setEnabled(True)

    def _disableStep(self):
        self._ui.regionAdd.setEnabled(False)

        for card in self._regions.values():
            card.setEnabled(False)

    def _clear(self):
        # Walk the layout rather than the tracked cards: a card that lost its
        # entry in `_regions` is exactly the one that has to go.
        layout = self._ui.regionList.layout()
        for index in reversed(range(layout.count())):
            widget = layout.itemAt(index).widget()
            if isinstance(widget, RegionCard):
                layout.takeAt(index)
                widget.setParent(None)
                widget.deleteLater()

        self._regions = {}

    def _connectSignalsSlots(self):
        self._ui.regionArea.verticalScrollBar().rangeChanged.connect(self._focus)
        self._ui.regionAdd.clicked.connect(self._showFormForAdding)
        self._form.regionAdded.connect(self._add)
        self._form.regionEdited.connect(self._update)
        self._form.canceled.connect(self._formCanceled)

    def load(self):
        self._updateBounds()

        regions = app.facadeClient.checkout().getElements('region')
        for id_ in regions:
            self._add(id_)

        self._loaded = True

    def _updateBounds(self):
        bounds = app.window.geometryManager.getBounds()
        if bounds is not None:
            self._bounds = bounds
            self._form.setBounds(self._bounds)
            self._suggestSeed()

    def _suggestSeed(self):
        """Ask the facade for a point inside the geometry, and offer it.

        Plan 28 WP6. Failing to find one is not an error -- an open surface
        has no inside -- so the form keeps the bounding-box centre it already
        had and the user places the seed themselves.
        """
        try:
            payload = query(
                app.facadeClient, 'geometry.fluid_seed.suggest').payload
        except Exception:                                   # noqa: BLE001
            return
        if payload.get('inside'):
            self._form.setSuggestedPoint(payload.get('point'))

    def _showFormForAdding(self):
        layout = self._ui.regionList.layout()
        if self._form.owner() == self:
            if index := layout.indexOf(self._form):
                layout.takeAt(index)
                layout.insertWidget(0, self._form)
        else:
            self._form.owner().removeForm(self._form)
            layout.insertWidget(0, self._form)

        self._form.setupForAdding()
        self._form.setOwner(self)
        self._showForm()

    def _showFormForEditing(self, id_):
        self._suppressMarker(id_)
        self._form.owner().removeForm(self._form)
        self._regions[id_].addForm(self._form)
        self._form.setupForEditing(id_)
        self._form.setOwner(self._regions[id_])
        self._showForm()

    def _add(self, id_):
        if id_ in self._regions:
            # `load()` is lazy, so a region added before the page loaded
            # arrives here twice -- once from the form, once from the load.
            # The second card was never tracked: it could not be removed, and
            # it kept the step's "at least one region" gate looking satisfied
            # after the region behind it was deleted.
            return

        card = RegionCard(id_)
        self._regions[id_] = card
        card.editClicked.connect(self._showFormForEditing)
        card.removeClicked.connect(self._remove)
        # R148. A card rebuilt while the step is locked must arrive locked:
        # the page is re-`load()`ed by the window's refresh, which can happen
        # at any moment, and an editable card behind an Unlock button is an
        # edit the user was told they had to ask for.
        card.setEnabled(not self._locked)
        self._ui.regionList.layout().insertWidget(0, card)

        self._moveFocus(card)
        self._updateNextStepAvailable()
        self._form.hide()
        self._suppressMarker(None)
        self._refreshMarkers()

    def _update(self, id_):
        self._regions[id_].load()
        self._hideForm()
        self._refreshMarkers()

    def _remove(self, id_):
        def dropCard(_result=None):
            card = self._regions.pop(id_, None)
            if card is not None:
                self._ui.regionList.layout().removeWidget(card)
                card.deleteLater()

            self._updateNextStepAvailable()
            self._refreshMarkers()

        db = app.facadeClient.checkout()
        try:
            db.removeElement('region', id_)
        except KeyError:
            # The card outlived the region it stood for.  Drop the card
            # instead of raising out of a click handler.
            dropCard()
        else:
            # C31-12. Scheduled; the card is dropped when the removal has been
            # committed, which is the order the blocking version had.
            submit(app.facadeClient, 'configuration.commit_working_copy',
                   {'working_copy': db, 'action': 'update regions',
                    'reason': None, 'target': None},
                   then=dropCard)

    @classmethod
    def _suppressMarker(cls, id_):
        """Hand one region's marker over to the form's own handle."""
        manager = getattr(app.window, 'geometryManager', None) if app.window else None
        suppress = getattr(manager, 'suppressRegionMarker', None)
        if suppress is not None:
            suppress(id_)

    @staticmethod
    def _showMarkers(shown):
        """DP-818. Show the seed glyphs on this page and nowhere else."""
        manager = getattr(app.window, 'geometryManager', None) if app.window else None
        setShown = getattr(manager, 'setRegionMarkersShown', None)
        if setShown is not None:
            setShown(shown)

    @staticmethod
    def _refreshMarkers():
        """Keep the seed glyphs in the viewport equal to the cards here."""
        manager = getattr(app.window, 'geometryManager', None) if app.window else None
        reload_ = getattr(manager, 'reloadRegions', None)
        if reload_ is not None:
            reload_()

    def _formCanceled(self):
        self._hideForm()

    def _hideForm(self):
        # The handle goes away with the form, so the glyph comes back.
        self._suppressMarker(None)
        if owner := self._form.owner():
            owner.removeForm(self._form)

        self._ui.regionList.layout().insertWidget(0, self._form)
        self._form.setOwner(self)
        self._form.hide()
        self._updateNextStepAvailable()

    def _showForm(self):
        self._form.show()
        self._moveFocus(self._form)
        self.stepReset.emit()

    def _moveFocus(self, widget):
        self._focusing = widget
        self._focus()

    def _focus(self):
        if self._focusing.isVisible():
            self._ui.regionArea.ensureWidgetVisible(self._focusing)
