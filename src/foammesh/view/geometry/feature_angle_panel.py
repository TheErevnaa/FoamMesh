"""The one Feature-angle control, on Preparation (Plan 42 SG5).

``mesh/featureAngle`` is the project's feature angle. It used to be editable
only on the snappy Surface features page, and six other places held an angle of
their own. This is the editor every engine reaches before the fork; the other
places read the project angle (``seed_from_project``) or are derived from it
(``core/facade/feature_angle.py``).
"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from foammesh.core.facade import feature_angle as angles
from foammesh.view.facade_client import submit
from foammesh.view.theming.metrics import CompactDoubleSpinBox, UnitLabel

TOOLTIP = ('How far a surface must turn before it counts as a feature. One '
           'value for the project: the split on import, Split by angle, the '
           'snappy feature edges and layer angles, and (until you type a '
           'value of your own) the angle Gmsh classifies an STL by, follow it.')


def project_feature_angle(client=None) -> float:
    """The project feature angle, or the default with no case open."""
    if client is None:
        try:
            from foammesh.app import app
            client = app.facadeClient
        except Exception:                                    # noqa: BLE001
            client = None
    try:
        if client is None or not client.has_case():
            return angles.DEFAULT_FEATURE_ANGLE
        return angles.project_angle(client.configuration())
    except Exception:                                        # noqa: BLE001
        return angles.DEFAULT_FEATURE_ANGLE


def seed_from_project(spin, client=None) -> None:
    """Start *spin* from the project angle without announcing a user edit."""
    blocked = spin.blockSignals(True)
    try:
        spin.setValue(project_feature_angle(client))
    finally:
        spin.blockSignals(blocked)


class FeatureAnglePanel(QWidget):
    """``Feature angle [30] deg``, bound to ``mesh/featureAngle``."""

    def __init__(self, client=None, parent=None):
        super().__init__(parent)
        self._client = client
        self.setObjectName('projectFeatureAnglePanel')
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        label = QLabel(self.tr('Feature angle'), self)
        self._spin = CompactDoubleSpinBox(self)
        self._spin.setObjectName('projectFeatureAngle')
        self._spin.setAccessibleName(self.tr('Feature angle in degrees'))
        self._spin.setRange(angles.MIN_FEATURE_ANGLE, angles.MAX_FEATURE_ANGLE)
        self._spin.setDecimals(1)
        self._spin.setSingleStep(5.0)
        self._spin.setToolTip(self.tr(TOOLTIP))
        label.setBuddy(self._spin)
        row.addWidget(label)
        row.addWidget(self._spin)
        row.addWidget(UnitLabel('deg', self))
        row.addStretch(1)
        self.reload()
        self._spin.editingFinished.connect(self.apply)

    def _the_client(self):
        if self._client is not None:
            return self._client
        from foammesh.app import app
        return app.facadeClient

    def spin(self):
        return self._spin

    def value(self) -> float:
        return float(self._spin.value())

    def reload(self) -> None:
        seed_from_project(self._spin, self._client)

    def apply(self, then=None):
        """Write the angle on screen as the project angle when it changed."""
        client = self._the_client()
        if client is None or not client.has_case():
            return None
        configuration = client.configuration()
        value = angles.normalise(self._spin.value())
        if abs(value - angles.project_angle(configuration)) < 1e-9:
            return None
        return submit(client, 'configuration.patch',
                      {'patch': angles.project_patch(configuration, value)},
                      then=then)
