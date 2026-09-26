"""The Mesh lines control: opacity, colour and width of the interior edges.

DP-710 (viewport audit 0925 F7). The grid was drawn at 22% opacity in a muted
grey with no way to change it, so on a light surface it was effectively not
there -- "can it be made slightly more gradient and an option to increase the
gradients of that particular grid". One button in the display-style group
opens a small panel; every change repaints every actor at once through
``actor_info.setMeshLineStyle`` and is kept in the application settings.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, Qt, Signal
from PySide6.QtGui import (
    QGuiApplication, QIcon, QPainter, QPalette, QPixmap)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QFormLayout, QHBoxLayout, QLabel,
    QMenu,
    QSlider, QToolButton, QWidget, QWidgetAction)

from foammesh.rendering import actor_info
from foammesh.rendering.actor_info import (
    MESH_LINE_DARK, MESH_LINE_LIGHT, MESH_LINE_WIDTH_RANGE, meshLineStyle,
    meshLineStyleNotifier, setMeshLineStyle)
from foammesh.view.theming.metrics import (
    FORM_MARGIN, CompactDoubleSpinBox, unit_cell,
)

#: A 3x3 grid on a square: what the control changes, drawn as that.
_ICON_SVG = (
    '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" fill="none" '
    'stroke="{c}" stroke-width="1.5" stroke-linecap="round">'
    '<rect x="3.5" y="3.5" width="17" height="17" rx="1.5"/>'
    '<path d="M9.2 3.5v17M14.8 3.5v17M3.5 9.2h17M3.5 14.8h17" '
    'stroke-opacity="0.75"/></svg>')

COLOR_CHOICES = (
    ('auto', 'Auto (contrast with the surface)'),
    (MESH_LINE_DARK, 'Dark'),
    (MESH_LINE_LIGHT, 'Light'),
    ('custom', 'Custom…'),
)


def meshLinesIcon(size: int = 64) -> QIcon:
    palette = QGuiApplication.palette()
    icon = QIcon()
    for mode, group in ((QIcon.Mode.Normal, QPalette.ColorGroup.Active),
                        (QIcon.Mode.Disabled, QPalette.ColorGroup.Disabled)):
        color = palette.color(group, QPalette.ColorRole.ButtonText).name()
        renderer = QSvgRenderer(QByteArray(_ICON_SVG.format(c=color).encode()))
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        icon.addPixmap(pixmap, mode)
    return icon


def _settings():
    from foammesh.settings.app_settings import appSettings
    return appSettings


class MeshLinesPanel(QWidget):
    """Opacity slider, colour choice and width, bound to the live style."""
    styleChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('meshLinesPanel')
        layout = QFormLayout(self)
        layout.setContentsMargins(FORM_MARGIN, FORM_MARGIN, FORM_MARGIN, FORM_MARGIN)

        self._opacity = QSlider(Qt.Orientation.Horizontal)
        self._opacity.setObjectName('meshLinesOpacity')
        self._opacity.setRange(0, 100)
        self._opacity.setMinimumWidth(140)
        self._opacity.setAccessibleName(self.tr('Mesh line opacity'))
        self._opacityLabel = QLabel()
        self._opacityLabel.setMinimumWidth(36)
        row = QWidget()
        rowLayout = QHBoxLayout(row)
        rowLayout.setContentsMargins(0, 0, 0, 0)
        rowLayout.addWidget(self._opacity)
        rowLayout.addWidget(self._opacityLabel)
        layout.addRow(self.tr('Opacity'), row)

        self._color = QComboBox()
        self._color.setObjectName('meshLinesColor')
        self._color.setAccessibleName(self.tr('Mesh line colour'))
        for data, text in COLOR_CHOICES:
            self._color.addItem(self.tr(text), data)
        layout.addRow(self.tr('Colour'), self._color)

        self._width = CompactDoubleSpinBox()
        self._width.setObjectName('meshLinesWidth')
        self._width.setAccessibleName(self.tr('Mesh line width in pixels'))
        self._width.setRange(*MESH_LINE_WIDTH_RANGE)
        self._width.setSingleStep(0.5)
        self._width.setDecimals(1)
        layout.addRow(self.tr('Width'), unit_cell(self._width, 'px'))

        self._customColor = None
        self.syncFromStyle()
        self._opacity.valueChanged.connect(self._opacityMoved)
        self._color.activated.connect(self._colorChosen)
        self._width.valueChanged.connect(self._widthChanged)
        # DP-738. The toolbar's panel and the display panel's are two views
        # of one style: a change from either (or from a saved setting) is
        # shown by both at once.
        meshLineStyleNotifier().changed.connect(self.syncFromStyle)

    def syncFromStyle(self):
        style = meshLineStyle()
        for widget in (self._opacity, self._color, self._width):
            widget.blockSignals(True)
        self._opacity.setValue(round(style.opacity * 100))
        self._opacityLabel.setText(f'{round(style.opacity * 100)}%')
        index = self._color.findData(style.color)
        if index < 0:
            self._customColor = style.color
            index = self._color.findData('custom')
        self._color.setCurrentIndex(index)
        self._width.setValue(style.width)
        for widget in (self._opacity, self._color, self._width):
            widget.blockSignals(False)

    def _apply(self, **changes):
        style = setMeshLineStyle(**changes)
        try:
            _settings().updateMeshLineStyle(
                opacity=style.opacity, color=style.color, width=style.width)
        except Exception:
            # A settings file that cannot be written must not stop the
            # viewport from repainting; the choice just lasts this session.
            pass
        self.styleChanged.emit()

    def _opacityMoved(self, value):
        self._opacityLabel.setText(f'{value}%')
        self._apply(opacity=value / 100.0)

    def _colorChosen(self, index):
        data = self._color.itemData(index)
        if data == 'custom':
            chosen = QColorDialog.getColor(parent=self)
            if not chosen.isValid():
                self.syncFromStyle()
                return
            data = chosen.name()
            self._customColor = data
        self._apply(color=data)

    def _widthChanged(self, value):
        self._apply(width=value)


class MeshLinesButton(QToolButton):
    """The toolbar entry: an icon that opens the Mesh lines panel."""
    styleChanged = Signal()

    def __init__(self, parent=None, *, size=None, iconSize=None):
        super().__init__(parent)
        self.setObjectName('viewportMeshLines')
        self.setIcon(meshLinesIcon())
        if iconSize is not None:
            self.setIconSize(iconSize)
        if size is not None:
            self.setMaximumSize(size)
            self.setMinimumSize(size)
        tip = self.tr('Mesh lines: opacity, colour and width of the grid')
        self.setToolTip(tip)
        self.setAccessibleName(tip)
        self.setAutoRaise(True)
        self.setProperty('foammeshFlat', True)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        loadSavedStyle()
        self._panel = MeshLinesPanel()
        self._panel.styleChanged.connect(self.styleChanged)
        menu = QMenu(self)
        action = QWidgetAction(menu)
        action.setDefaultWidget(self._panel)
        menu.addAction(action)
        menu.aboutToShow.connect(self._panel.syncFromStyle)
        self.setMenu(menu)

    def panel(self) -> MeshLinesPanel:
        return self._panel


def loadSavedStyle():
    """Apply the persisted style, once, before any actor asks for it."""
    try:
        saved = _settings().getMeshLineStyle()
    except Exception:
        saved = {}
    if saved:
        setMeshLineStyle(**{key: saved[key] for key in
                            ('opacity', 'color', 'width') if key in saved})
    return actor_info.meshLineStyle()
