"""The Geometry page's interface-pair table, which knows which faces touch.

DP-556 (audit 0924, case G6 ``tee_with_plug.brep``). The generic child panel
opened Add on the first prepared face in *both* scope pickers -- a pair of one
face with itself -- and saved whatever two faces the user left there. On G6
the saved slave was ``body1_face2``, the tee's outer end cap, so the pair
could never apply. The prepared geometry already knows which faces the two
solids share, so Add now opens on a pair that touches, and OK on a coincident
pair whose faces do not touch asks before saving it.
"""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from foammesh.core.geometry.interface_contact import (
    contacting_face_pairs, face_contact, group_manifest_of)
from foammesh.view.facade_client import query
from foammesh.view.workflow_controls.child_controls import ChildControlPanel

_MASTER, _SLAVE = 'master_scope_token', 'slave_scope_token'


def _engineBuildsNcc() -> bool:
    """Whether the case's meshing engine builds a non-conformal pair (DP-641)."""
    try:
        from foammesh.app import app
        from foammesh.core.engine.registry import (
            ENGINE_REGISTRY, configured_engine_id)
        engine = ENGINE_REGISTRY.get(configured_engine_id(app.db))
    except Exception:                          # noqa: BLE001 - unknown case
        return True
    return bool(getattr(engine, 'builds_non_conformal_interfaces', True))


class InterfacePairPanel(ChildControlPanel):
    """``geometry.interface_pairs``, proposing and checking the two faces."""

    def __init__(self, facade_client, collection_id: str, title: str,
                 *args, **kwargs):
        # The three the base cannot do without are named here, so the
        # signature says what a caller must pass (and a widget smoke walk
        # does not take this panel for one built from nothing).
        self._proposing = False
        super().__init__(facade_client, collection_id, title, *args, **kwargs)

    # -- what the prepared geometry says ----------------------------------- #

    def preparedGeometry(self) -> dict:
        """The ``geometry.prepared.current`` payload, or an empty dict."""
        try:
            payload = query(self._client, 'geometry.prepared.current',
                            {}).payload or {}
        except Exception:                       # noqa: BLE001 - advisory only
            return {}
        prepared = payload.get('prepared')
        return prepared if isinstance(prepared, dict) else {}

    def proposedPair(self):
        """``(master, slave)`` of the first touching pair not yet saved."""
        manifest = group_manifest_of(self.preparedGeometry())
        taken = {frozenset((str(row.get(_MASTER) or ''),
                            str(row.get(_SLAVE) or '')))
                 for row in self.rows()}
        # Only faces the pickers offer as available: a stale scope is refused
        # on OK, so proposing it would propose a refusal.
        offered = {value for _label, value, _status, enabled
                   in self._scope_choices(_MASTER) if enabled}
        for master, slave in contacting_face_pairs(manifest):
            if frozenset((master, slave)) in taken:
                continue
            if not {master, slave} <= offered:
                continue
            return master, slave
        return None

    # -- Add opens on a pair that touches ---------------------------------- #

    def open_add_dialog(self) -> None:
        self._proposing = True
        try:
            super().open_add_dialog()
        finally:
            self._proposing = False

    def editor_dialog(self):
        # `open_add_dialog` resets every editor to its schema default and then
        # asks for the dialog; the proposal goes in between, so it is what
        # the form opens on and it is not written over.
        dialog = super().editor_dialog()
        self._offerCouplings()
        if self._proposing:
            self._proposing = False
            pair = self.proposedPair()
            if pair is not None:
                self._editors[_MASTER].set_value(pair[0])
                self._editors[_SLAVE].set_value(pair[1])
        return dialog

    def _offerCouplings(self) -> None:
        """DP-641. Gmsh builds no NCC coupling, so it is not offered there."""
        editor = self._editors.get('coupling')
        combo = getattr(editor, 'editor', None)
        if combo is None or not hasattr(combo, 'findData'):
            return
        index = combo.findData('non_conformal')
        item = combo.model().item(index) if index >= 0 else None
        if item is None:
            return
        builds = _engineBuildsNcc()
        item.setEnabled(builds)
        item.setToolTip('' if builds else self.tr(
            'Gmsh builds no non-conformal (NCC) coupling; choose conformal '
            'or cyclic, or mesh this case with snappyHexMesh.'))

    # -- OK checks that a coincident pair's faces touch -------------------- #

    def contactProblem(self, values: dict) -> str:
        """Why the two faces of a coincident pair cannot be one interface."""
        if str(values.get('transform') or '') != 'coincident':
            # A cyclic or translated pair joins faces that are apart.
            return ''
        master = str(values.get(_MASTER) or '')
        slave = str(values.get(_SLAVE) or '')
        if not master or not slave:
            return ''
        try:
            tolerance = float(values.get('match_tolerance') or 0.0)
        except (TypeError, ValueError):
            tolerance = 0.0
        contact = face_contact(self.preparedGeometry(), master, slave,
                               tolerance)
        if contact.touching is False:
            return contact.reason
        return ''

    def _validated_child_values(self):
        values = super()._validated_child_values()
        if values is None:
            return None
        problem = self.contactProblem(values)
        if not problem:
            return values
        text = self.tr(
            'The master and slave faces of this coincident pair do not touch: '
            '%s. The pair cannot apply, and the mesher will report it as not '
            'applied. Save it anyway?') % problem
        proposal = self.proposedPair()
        if proposal is not None:
            names = self._face_names()
            text += '\n\n' + self.tr(
                'The prepared geometry has touching faces %s and %s.') % (
                    names.get(proposal[0], proposal[0]),
                    names.get(proposal[1], proposal[1]))
        answer = QMessageBox.question(
            self, self.tr('Faces do not touch'), text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return values if answer == QMessageBox.StandardButton.Yes else None

    def _face_names(self) -> dict:
        manifest = group_manifest_of(self.preparedGeometry())
        return {str(group.get('patch_uuid')): str(group.get('display_name')
                                                  or group.get('patch_uuid'))
                for group in manifest.get('groups') or ()
                if isinstance(group, dict)}
