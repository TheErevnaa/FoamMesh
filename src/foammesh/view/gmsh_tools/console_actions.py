"""Save and Clear for the message console (Plan 48 GM11b; DP-3031).

Gmsh's message console saves its text to a file and clears it.  The Console
widget (``view/main_window/console_view.py``) had ``clear()`` and no control
for either, so this module gives it the two buttons in its filter row and the
two functions behind them; the local API route and the keys call the same
functions.  Nothing here changes what the console logs.
"""
from __future__ import annotations

from pathlib import Path

SAVE_NAME = 'consoleSave'
CLEAR_NAME = 'consoleClear'


def _text_edit(console):
    from PySide6.QtWidgets import QPlainTextEdit

    return console.findChild(QPlainTextEdit)


def log_text(console) -> str:
    """Everything the console shows now, with any held lines written first."""
    flush = getattr(console, 'flush', None)
    if callable(flush):
        flush()
    edit = _text_edit(console)
    return edit.toPlainText() if edit is not None else ''


def save_log(console, path) -> dict:
    """Write the console text to *path* (UTF-8); ``{'path', 'lines', 'bytes'}``."""
    target = Path(path)
    if not str(path).strip():
        raise ValueError('choose a file to save the console to')
    text = log_text(console)
    try:
        from foammesh.core.facade.plan48.gm11b_routes import save_text
        save_text(target, text + ('\n' if text and not text.endswith('\n')
                                  else ''))
    except OSError as error:
        raise ValueError(f'the console text could not be saved: {error}') \
            from error
    return {'path': str(target), 'lines': len(text.splitlines()),
            'bytes': target.stat().st_size}


def clear_log(console) -> dict:
    """Empty the console; how many lines it held."""
    held = len(log_text(console).splitlines())
    console.clear()
    return {'cleared': held}


def install(console, ask_path=None) -> bool:
    """Add Save and Clear to the console's filter row; False if present.

    *ask_path* returns the file the person chose (or ``''``); the default
    opens a Save dialog.
    """
    from PySide6.QtWidgets import QToolButton, QWidget

    # The row may have been handed to the console's header (headerControls).
    bar = getattr(console, '_toolbar', None) or console.findChild(
        QWidget, 'consoleFilterChips')
    if bar is None or bar.findChild(QToolButton, SAVE_NAME) is not None:
        return False
    layout = bar.layout() if bar is not None else None
    if layout is None:
        return False

    def choose() -> str:
        if ask_path is not None:
            return ask_path()
        from PySide6.QtWidgets import QFileDialog

        name, _filter = QFileDialog.getSaveFileName(
            console, console.tr('Save the console'), 'console.log',
            console.tr('Log files (*.log *.txt);;All files (*)'))
        return name

    def save() -> None:
        name = choose()
        if name:
            try:
                save_log(console, name)
            except ValueError:
                pass

    saveButton = QToolButton(bar)
    saveButton.setObjectName(SAVE_NAME)
    saveButton.setText(console.tr('Save'))
    saveButton.setToolTip(console.tr('Save the console text to a file'))
    saveButton.clicked.connect(lambda _c=False: save())
    clearButton = QToolButton(bar)
    clearButton.setObjectName(CLEAR_NAME)
    clearButton.setText(console.tr('Clear'))
    clearButton.setToolTip(console.tr('Empty the console'))
    clearButton.clicked.connect(lambda _c=False: clear_log(console))
    # Before the stretch, after Find.
    at = layout.count()
    last = layout.itemAt(at - 1) if at else None
    if last is not None and last.spacerItem() is not None:
        at -= 1
    layout.insertWidget(at, saveButton)
    layout.insertWidget(at + 1, clearButton)
    return True
