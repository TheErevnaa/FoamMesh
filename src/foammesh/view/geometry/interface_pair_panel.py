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

from PySide6.QtWidgets import QLabel, QMessageBox

from foammesh.core.geometry.interface_contact import (
    contacting_face_pairs, face_contact, group_manifest_of)
from foammesh.core.geometry.periodic_match import periodic_match
from foammesh.view.facade_client import query
from foammesh.view.workflow_controls.child_controls import ChildControlPanel
from widgets import async_message_box as messages

_MASTER, _SLAVE = 'master_scope_token', 'slave_scope_token'

#: DP-6008 (user decision 2026-10-25). The table makes periodic (cyclic)
#: pairs only. A conformal or non-conformal interface is a surface's Role and
#: Partner, set in the surface dialog and held in one relation; the local API
#: refuses the two here by name (``field_retired``).
CYCLIC = 'cyclic'
_KEPT_ON_SURFACES = ('conformal', 'non_conformal')


def _camel(name: str) -> str:
    """``translation_x`` -> ``translationX``: a form field as stored."""
    head, *rest = str(name).split('_')
    return head + ''.join(part[:1].upper() + part[1:] for part in rest)


class InterfacePairPanel(ChildControlPanel):
    """``geometry.interface_pairs``, proposing and checking the two faces."""

    #: Plan 44 FIT (DP-2667/2668). Most cases have no pair; one empty row is
    #: the empty state, and the second took Geometry past the 1280x800 sheet.
    MIN_VISIBLE_ROWS = 1

    def __init__(self, facade_client, collection_id: str, title: str,
                 *args, **kwargs):
        # The three the base cannot do without are named here, so the
        # signature says what a caller must pass (and a widget smoke walk
        # does not take this panel for one built from nothing).
        self._proposing = False
        choices = dict(kwargs.pop('choices', None) or {})
        choices.setdefault('coupling', self.couplingChoices())
        super().__init__(facade_client, collection_id, title, *args,
                         choices=choices, **kwargs)
        # DP-6008. Said where the conformal and non-conformal choices were.
        self.surfaceInterfaceNote = QLabel(self.surfaceInterfaceText(), self)
        self.surfaceInterfaceNote.setObjectName('interfacePairsSurfaceNote')
        self.surfaceInterfaceNote.setWordWrap(True)
        layout = self.layout()
        if layout is not None:
            layout.insertWidget(0, self.surfaceInterfaceNote)

    # -- DP-6008: periodic pairs only ------------------------------------- #

    def surfaceInterfaceText(self) -> str:
        return self.tr(
            'This table makes periodic (cyclic) pairs. A conformal or '
            'non-conformal interface is set on the surface: open it and '
            'choose its Role and Partner. Rows saved before are kept as '
            'they were; their interfaces are on the surfaces.')

    def couplingChoices(self) -> list:
        """The coupling picker: cyclic, with the two retired ones shown
        greyed out so a row saved with one still reads as what it is."""
        reason = self.tr('set on the surface, as its role and partner')
        return [(self.tr('Cyclic / periodic'), CYCLIC, '', True),
                (self.tr('Conformal'), 'conformal', reason, False),
                (self.tr('Non-conformal'), 'non_conformal', reason, False)]

    def fresh_value(self, key: str, descriptor):
        if key == 'coupling':
            return CYCLIC
        return super().fresh_value(key, descriptor)

    def selectedCoupling(self) -> str:
        index = self._selected_index()
        if not 0 <= index < len(self._rows):
            return ''
        value = self._rows[index].get('coupling')
        return str(getattr(value, 'value', value) or 'conformal')

    def open_edit_dialog(self, *args) -> None:
        if (self.selected_key() is not None
                and self.selectedCoupling() in _KEPT_ON_SURFACES):
            title = self.tr('Interface set on the surfaces')
            text = self.tr(
                'This interface was saved before interfaces moved to the '
                'surfaces. It is kept as it was. To change it, open either '
                'surface and choose its Role and Partner.')
            if self._inline:
                self._tell(title, text)
            else:
                messages.information(self, title, text)
            return
        super().open_edit_dialog(*args)

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
        if self._proposing:
            self._proposing = False
            pair = self.proposedPair()
            if pair is not None:
                self._editors[_MASTER].set_value(pair[0])
                self._editors[_SLAVE].set_value(pair[1])
        return dialog

    # -- OK checks that a coincident pair's faces touch -------------------- #

    def contactProblem(self, values: dict) -> str:
        """Why the two faces of a coincident pair cannot be one interface."""
        return self.contactCheck(values)['problem']

    def contactCheck(self, values: dict) -> dict:
        """The contact check OK makes, with everything it would say.

        ``checked`` is False for a pair the check does not apply to (a
        non-cyclic translated pair, or a face is not chosen yet). A cyclic
        translational or rotational pair is checked by its transform instead
        of by contact (Plan 49 SR3): ``periodic`` then holds the verdict,
        reason and measures of ``periodic_match``. ``problem`` is the reason the faces do not touch, '' when
        they do or when that is not known; ``question`` is the sentence OK
        would ask before saving such a pair, and ``proposal`` the touching
        pair the prepared geometry offers instead (Plan 48 PA5: the local API
        returns this).
        """
        result = {'checked': False, 'touching': None, 'reason': '',
                  'problem': '', 'would_ask': False, 'question': '',
                  'proposal': None, 'periodic': None}
        transform = str(values.get('transform') or '')
        if (str(values.get('coupling') or '') == 'cyclic'
                and transform in ('translational', 'rotational')):
            return self._periodicCheck(values, result)
        if transform != 'coincident':
            return result
        master = str(values.get(_MASTER) or '')
        slave = str(values.get(_SLAVE) or '')
        if not master or not slave:
            return result
        try:
            tolerance = float(values.get('match_tolerance') or 0.0)
        except (TypeError, ValueError):
            tolerance = 0.0
        contact = face_contact(self.preparedGeometry(), master, slave,
                               tolerance)
        result.update(checked=True, touching=contact.touching,
                      reason=str(contact.reason or ''))
        if contact.touching is False:
            result['problem'] = contact.reason
            result['would_ask'] = True
            result['question'] = self.notTouchingQuestion(contact.reason)
        proposal = self.proposedPair()
        if proposal is not None:
            names = self._face_names()
            result['proposal'] = {
                'master': proposal[0], 'slave': proposal[1],
                'master_name': names.get(proposal[0], proposal[0]),
                'slave_name': names.get(proposal[1], proposal[1])}
        return result

    def _periodicCheck(self, values: dict, result: dict) -> dict:
        """Plan 49 SR3. Does the transform map the master onto the slave?

        Measured on the prepared geometry like the relation it becomes
        (``periodic_match``); ``periodic`` carries the verdict and its
        numbers, and a pair that does not map is asked about before saving.
        """
        master = str(values.get(_MASTER) or '')
        slave = str(values.get(_SLAVE) or '')
        if not master or not slave:
            return result
        fields = {_camel(key): value for key, value in values.items()}
        match = periodic_match(self.preparedGeometry() or {}, master, slave,
                               fields)
        result.update(checked=True, reason=str(match.reason or ''),
                      periodic={'verified': match.verified,
                                'reason': str(match.reason or ''),
                                'measures': dict(match.measures)})
        if match.verified is False:
            result['problem'] = match.reason
            result['would_ask'] = True
            result['question'] = self.notMappingQuestion(match.reason)
        return result

    def notMappingQuestion(self, problem: str) -> str:
        """The question OK asks before it saves a pair that does not map."""
        return self.tr(
            'The transform of this periodic pair does not map one side onto '
            'the other: %s. The pair is kept unresolved and the run is '
            'refused until it maps. Save it anyway?') % problem

    def notTouchingQuestion(self, problem: str) -> str:
        """The question OK asks before it saves a pair that does not touch."""
        text = self.tr(
            'The two faces of this coincident pair do not touch: '
            '%s. The pair cannot apply, and the mesher will report it as not '
            'applied. Save it anyway?') % problem
        proposal = self.proposedPair()
        if proposal is not None:
            names = self._face_names()
            text += '\n\n' + self.tr(
                'The prepared geometry has touching faces %s and %s.') % (
                    names.get(proposal[0], proposal[0]),
                    names.get(proposal[1], proposal[1]))
        return text

    def _validated_child_values(self):
        values = super()._validated_child_values()
        if values is None:
            return None
        check = self.contactCheck(values)
        if not check['would_ask']:
            return values
        title = (self.tr('Transform does not map')
                 if check.get('periodic') is not None
                 else self.tr('Faces do not touch'))
        answer = messages.question(
            self, title, check['question'],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        return values if answer == QMessageBox.StandardButton.Yes else None

    def _face_names(self) -> dict:
        manifest = group_manifest_of(self.preparedGeometry())
        return {str(group.get('patch_uuid')): str(group.get('display_name')
                                                  or group.get('patch_uuid'))
                for group in manifest.get('groups') or ()
                if isinstance(group, dict)}
