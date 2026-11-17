"""Plan 49 SR3. The Gmsh Periodic page's pair table, checked before OK.

A translation or rotation pair is measured on the prepared geometry
(``periodic_match``) when OK is pressed, like a cyclic pair on the Geometry
page: a transform that does not carry the source surface onto the target is
asked about before it is saved. Saved anyway, the pair's relation is kept
unresolved with the reason and the run refuses it until it maps (DP-5834).
An affine pair is not measured here (DP-5832).
"""
from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from foammesh.core.geometry.periodic_match import periodic_match
from foammesh.view.facade_client import query
from foammesh.view.workflow_controls.child_controls import ChildControlPanel
from widgets import async_message_box as messages

_MASTER, _SLAVE = 'master_scope_token', 'slave_scope_token'
_TRANSFORMS = {'translation': 'translational', 'rotation': 'rotational'}

#: Words that name the two ends of one direction. DP-6125: the other end is
#: the face a periodic pair most likely maps onto, so it is proposed first.
_OPPOSITE_WORDS = (('min', 'max'), ('left', 'right'), ('inlet', 'outlet'),
                   ('top', 'bottom'), ('upper', 'lower'), ('front', 'back'),
                   ('in', 'out'), ('low', 'high'), ('start', 'end'))


def opposite_label(label: str, labels) -> str:
    """The label in *labels* that is *label* with one direction word swapped."""
    import re
    wanted = {str(item).lower(): str(item) for item in labels}
    for first, second in _OPPOSITE_WORDS:
        for here, there in ((first, second), (second, first)):
            swapped = re.sub(r'(?<![a-z])' + here + r'(?![a-z])', there,
                             str(label).lower())
            if swapped != str(label).lower() and swapped in wanted:
                return wanted[swapped]
    return ''


def relation_fields(values: dict) -> dict:
    """The editor's values as relation fields.

    ``translation.x`` -> ``translationX``, ``rotation_centre.y`` ->
    ``rotationCentreY``, ``translation`` -> ``translational``.
    """
    fields = {}
    for key, value in values.items():
        head, *rest = str(key).replace('.', '_').split('_')
        fields[head + ''.join(part[:1].upper() + part[1:]
                              for part in rest)] = value
    transform = str(values.get('transform') or '')
    fields['transform'] = _TRANSFORMS.get(transform, transform)
    return fields


class GmshPeriodicPairPanel(ChildControlPanel):
    """``gmsh.periodic_pairs.controls``, with the pair measured on OK."""

    def fresh_value(self, key: str, descriptor):
        """A new pair starts with two different surfaces.

        DP-6125. Both pickers opened on the first boundary, so a pair read
        "bottom maps onto bottom" until the user changed the target.
        """
        if key != _SLAVE:
            return super().fresh_value(key, descriptor)
        options = [option for option in self._scope_choices(key) if option[3]]
        valid = [option[1] for option in options]
        first = super().fresh_value(_MASTER, self._descriptors[_MASTER]) \
            if _MASTER in self._descriptors else None
        if len(valid) < 2:
            return super().fresh_value(key, descriptor)
        # The master picker's own default is its first valid option.
        source = first if first in valid else valid[0]
        names = {option[1]: option[0] for option in options}
        wanted = opposite_label(names.get(source, ''),
                                [option[0] for option in options])
        for option in options:
            if wanted and option[0] == wanted:
                return option[1]
        return next((value for value in valid if value != source),
                    super().fresh_value(key, descriptor))

    def preparedGeometry(self) -> dict:
        """The ``geometry.prepared.current`` payload, or an empty dict."""
        try:
            payload = query(self._client, 'geometry.prepared.current',
                            {}).payload or {}
        except Exception:                       # noqa: BLE001 - advisory only
            return {}
        prepared = payload.get('prepared')
        return prepared if isinstance(prepared, dict) else {}

    def mapCheck(self, values: dict) -> dict:
        """``{checked, verified, reason, measures, question}`` for *values*."""
        result = {'checked': False, 'verified': None, 'reason': '',
                  'measures': {}, 'question': ''}
        fields = relation_fields(values)
        master = str(values.get(_MASTER) or '')
        slave = str(values.get(_SLAVE) or '')
        if fields['transform'] not in ('translational', 'rotational') \
                or not master or not slave:
            return result
        match = periodic_match(self.preparedGeometry(), master, slave, fields)
        result.update(checked=True, verified=match.verified,
                      reason=str(match.reason or ''),
                      measures=dict(match.measures))
        if match.verified is False:
            result['question'] = self.tr(
                'The transform of this periodic pair does not map the source '
                'surface onto the target: %s. The pair is kept unresolved and '
                'the run is refused until it maps. Save it anyway?') % (
                    match.reason)
        return result

    def _validated_child_values(self):
        return super()._validated_child_values()

    def _confirmChildValues(self, values, then):
        """Plan 50 IP19: a transform that does not map is asked about
        without a nested loop."""
        check = self.mapCheck(values)
        if check['verified'] is not False:
            then(values)
            return
        messages.askQuestion(
            self, self.tr('Transform does not map'), check['question'],
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
            then=lambda answer: then(values)
            if answer == QMessageBox.StandardButton.Yes else None)
