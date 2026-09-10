"""Qt palette construction from semantic token values."""
from PySide6.QtGui import QColor, QPalette

from .tokens import ThemeTokens


def build_palette(tokens: ThemeTokens) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(tokens.value('background.canvas')))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(tokens.value('foreground.primary')))
    palette.setColor(QPalette.ColorRole.Base, QColor(tokens.value('input.background')))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens.value('background.surface')))
    palette.setColor(QPalette.ColorRole.Text, QColor(tokens.value('foreground.primary')))
    palette.setColor(QPalette.ColorRole.Button, QColor(tokens.value('background.surface')))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(tokens.value('foreground.primary')))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(tokens.value('selection.background')))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(tokens.value('foreground.primary')))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(tokens.value('tooltip.background')))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(tokens.value('tooltip.foreground')))
    palette.setColor(QPalette.ColorRole.Link, QColor(tokens.value('accent.default')))
    palette.setColor(QPalette.ColorRole.LinkVisited, QColor(tokens.value('accent.hover')))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(tokens.value('foreground.muted')))
    group = QPalette.ColorGroup.Disabled
    palette.setColor(group, QPalette.ColorRole.Text, QColor(tokens.value('disabled.foreground')))
    palette.setColor(group, QPalette.ColorRole.WindowText, QColor(tokens.value('disabled.foreground')))
    palette.setColor(group, QPalette.ColorRole.ButtonText, QColor(tokens.value('disabled.foreground')))
    palette.setColor(group, QPalette.ColorRole.Button, QColor(tokens.value('disabled.background')))
    palette.setColor(group, QPalette.ColorRole.Base, QColor(tokens.value('disabled.background')))
    return palette
