#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json

from PySide6.QtCore import (
    QCoreApplication, QEvent, QMargins, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QKeySequence, QShortcut,
    QTextBlockFormat, QTextCharFormat, QTextCursor, QTextDocument)
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QTextEdit,
    QToolButton, QVBoxLayout, QWidget)
from PySide6QtAds import CDockWidget

from foammesh.view.theming.metrics import GAP_TIGHT, MARGIN_TIGHT

from . import console_lines


#: The line prefix the meshing runners use to report progress as JSON.
#: See ``src/resources/gmsh/runner_v1.py`` (``Reporter.emit``).
PROGRESS_PREFIX = 'FOAMMESH_PROGRESS '


def readable_line(text):
    """Turn a progress record into a line of English (F12).

    The runners report stage progress as one JSON object per line so the job
    manager can parse it, and the console showed that object verbatim -- a wall
    of ``{"schema_version": 1, "sequence": 8, ...}`` in the middle of the
    utility's own output. The job manager writes every raw line to the stage
    log *before* it publishes, so the record itself is not lost by rendering
    it here.
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped.startswith(PROGRESS_PREFIX):
        return text
    try:
        record = json.loads(stripped[len(PROGRESS_PREFIX):])
    except ValueError:
        return text
    if not isinstance(record, dict):
        return text
    message = str(record.get('message')
                  or record.get('stage_id')
                  or record.get('kind') or '').strip()
    try:
        percent = '[{0:3.0f}%] '.format(float(record['fraction']) * 100)
    except (KeyError, TypeError, ValueError):
        percent = ''
    return (percent + message).rstrip() or text


def _is_progress(line):
    return line.lstrip().startswith(PROGRESS_PREFIX)


def readable_batch(text):
    """Render a batch of output lines for the console (Plan 35 CR4).

    Output arrives in batches of up to 500 lines. Only the *last* progress
    record of a batch is parsed and shown -- it supersedes the ones before
    it, and parsing every record was a ``json.loads`` per line on the GUI
    thread. The earlier records stay in the stage log.
    """
    if not isinstance(text, str) or '\n' not in text:
        return readable_line(text)
    lines = text.split('\n')
    last = next((index for index in range(len(lines) - 1, -1, -1)
                 if _is_progress(lines[index])), None)
    if last is None:
        return text
    return '\n'.join(readable_line(line) if index == last else line
                     for index, line in enumerate(lines)
                     if index == last or not _is_progress(line))


#: CR4. Appends arriving within this window of the last one are held and
#: written as one block of text; a flood also flushes at this many lines.
APPEND_BATCH_MS = 100
APPEND_BATCH_LINES = 500
#: The console keeps the last CONSOLE_LINES lines. It trims itself in one
#: cut once CONSOLE_TRIM_SLACK more have arrived: the document's own
#: ``maximumBlockCount`` trims a block at a time on every append, which cost
#: 6 ms per 500-line batch against 0.8 ms for this (CR4).
CONSOLE_LINES = 20_000
CONSOLE_TRIM_SLACK = 10_000


#: The level filter's choices: the least severe level still shown.
LEVEL_CHOICES = (
    ('All lines', console_lines.INFO),
    ('Warnings and errors', console_lines.WARNING),
    ('Errors only', console_lines.ERROR),
)

# A block's ``userState``: bits 0-1 the level, then two flags.
#: Plan 44 MF6. The log is set in Cascadia Mono, then Consolas, then whatever
#: the platform calls monospace (a user decision); 12.5 px type on a 19 px line
#: so that six lines fit the 144 px console under its 28 px header.
LOG_FONT_FAMILIES = ('Cascadia Mono', 'Consolas', 'monospace')
LOG_FONT_PX = 12.5
LOG_LINE_PX = 19
#: A line colour for the "-- Stage --" separators (the ``console.stage`` token).
_STAGE = 'stage'

_LEVEL_MASK = 0b11
_HEADING_BIT = 0b100
_FOLDED_BIT = 0b1000


def _theme_color(token, fallback):
    """The theme's colour for ``token`` (UXC6: no hard-coded status colour)."""
    try:
        from foammesh.app import app
        manager = app.themeManager
        if manager is not None and manager.tokens is not None:
            return QColor(manager.tokens.value(token))
    except Exception:                                         # noqa: BLE001
        pass
    return QColor(fallback)


def log_font() -> QFont:
    """The log's face: Cascadia Mono, then Consolas, then monospace."""
    font = QFont()
    font.setFamilies(list(LOG_FONT_FAMILIES))
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSizeF(LOG_FONT_PX * 72.0 / 96.0)
    return font


def _line_format() -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    fmt.setLineHeight(
        float(LOG_LINE_PX), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
    return fmt


class Console(QWidget):
    #: A path in the log was clicked (it has been opened) / a patch or region
    #: name was clicked (the viewport selection follows).
    pathActivated = Signal(str)
    patchActivated = Signal(str)

    def __init__(self):
        super().__init__()
        self._view = QPlainTextEdit(self)
        self._view.setProperty('foammeshConsole', True)
        self._view.setReadOnly(True)

        self._view.setFont(log_font())
        charFormat = self._view.currentCharFormat()
        charFormat.setFont(log_font())
        self._view.setCurrentCharFormat(charFormat)
        self._prime()

        self._minLevel = console_lines.INFO
        self._foldAll = False
        self._tailFolded = False
        self._needle = ''
        self._nameResolver = None
        self._names = None
        self._openPath = self._openWithDesktop

        layout = QVBoxLayout(self)
        layout.setContentsMargins(QMargins(0, 0, 0, 0))
        layout.setSpacing(GAP_TIGHT)
        self._toolbar = self._buildToolbar()
        layout.addWidget(self._toolbar)
        layout.addWidget(self._buildSearchBar())
        layout.addWidget(self._view)

        # CR4 (F4). A stage run hands the console every line through
        # `on_line`; one `appendPlainText` per line is a layout pass per
        # line on the GUI thread. The first append after a quiet spell is
        # written at once; the ones that follow within APPEND_BATCH_MS are
        # held and written together when the window closes.
        self._pending: list[str] = []
        self._pendingLines = 0
        self._batchTimer = QTimer(self)
        self._batchTimer.setSingleShot(True)
        self._batchTimer.setInterval(APPEND_BATCH_MS)
        self._batchTimer.timeout.connect(self.flush)

        # UXC6. Ctrl+F opens the search bar while focus is anywhere inside the
        # console; F3 / Shift+F3 step through the matches.
        # Plan 40 UXE1: Ctrl+G / Ctrl+Shift+G step through the matches. F3
        # is the Gmsh route's "mesh volumes" and a key on both would fire
        # neither; the shortcut registry lists them all and refuses a clash.
        from foammesh.view.main_window.shortcut_registry import adopt
        for command_id, sequence, handler in (
                ('console.find', QKeySequence('Ctrl+F'), self.showSearch),
                ('console.findNext', QKeySequence('Ctrl+G'), self.findNext),
                ('console.findPrevious', QKeySequence('Ctrl+Shift+G'),
                 self.findPrevious)):
            shortcut = QShortcut(sequence, self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            adopt(command_id, shortcut)
            shortcut.activated.connect(handler)

        self._view.viewport().installEventFilter(self)
        self._view.viewport().setMouseTracking(True)
        self._connectTheme()

    # ------------------------------------------------------------ toolbar
    def _buildToolbar(self):
        bar = QWidget(self)
        bar.setObjectName('consoleFilterChips')
        row = QHBoxLayout(bar)
        row.setContentsMargins(QMargins(MARGIN_TIGHT, MARGIN_TIGHT, MARGIN_TIGHT, 0))
        self._toolbarRow = row
        self._levelBox = QComboBox(bar)
        self._levelBox.setAccessibleName(self.tr('Console level filter'))
        self._levelBox.setToolTip(self.tr('Show only the lines at or above a level'))
        for label, level in LEVEL_CHOICES:
            self._levelBox.addItem(self.tr(label), level)
        self._levelBox.currentIndexChanged.connect(
            lambda _index: self.setMinimumLevel(self._levelBox.currentData()))
        self._foldButton = QToolButton(bar)
        self._foldButton.setText(self.tr('Fold stages'))
        self._foldButton.setCheckable(True)
        self._foldButton.setToolTip(self.tr(
            'Collapse every stage to its heading. Double-click a heading to '
            'fold one stage.'))
        self._foldButton.toggled.connect(self.foldAll)
        self._findButton = QToolButton(bar)
        self._findButton.setText(self.tr('Find'))
        self._findButton.setToolTip(
            self.tr('Search the log ({0})').format('Ctrl+F'))
        self._findButton.clicked.connect(self.showSearch)
        row.addWidget(self._levelBox)
        row.addWidget(self._foldButton)
        row.addWidget(self._findButton)
        row.addStretch(1)
        return bar

    def headerControls(self):
        """Hand the filter chips (level, Fold stages, Find) to the console's
        header row (Plan 44 MF6).

        The three controls were a row of their own above the log; the console
        now has one 28 px header row, and they sit in it while this page is the
        current tab. Called with no header (a console on its own, in a test or a
        dock), they stay where they were built.
        """
        bar = self._toolbar
        layout = self.layout()
        if layout is not None and layout.indexOf(bar) >= 0:
            layout.removeWidget(bar)
            row = self._toolbarRow
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(GAP_TIGHT)
            last = row.itemAt(row.count() - 1)
            if last is not None and last.spacerItem() is not None:
                row.takeAt(row.count() - 1)
        return bar

    def _prime(self):
        """Give the empty log its fixed line height (every later block takes
        it from the one before)."""
        cursor = QTextCursor(self._view.document())
        cursor.mergeBlockFormat(_line_format())

    def _buildSearchBar(self):
        self._searchBar = QWidget(self)
        row = QHBoxLayout(self._searchBar)
        row.setContentsMargins(QMargins(MARGIN_TIGHT, 0, MARGIN_TIGHT, 0))
        self._searchEdit = QLineEdit(self._searchBar)
        self._searchEdit.setPlaceholderText(self.tr('Find in the log'))
        self._searchEdit.setAccessibleName(self.tr('Find in the log'))
        self._searchEdit.setClearButtonEnabled(True)
        self._searchEdit.textChanged.connect(self.search)
        self._searchEdit.returnPressed.connect(self.findNext)
        self._searchCount = QLabel('', self._searchBar)
        previous = QToolButton(self._searchBar)
        previous.setText(self.tr('Previous'))
        previous.clicked.connect(self.findPrevious)
        following = QToolButton(self._searchBar)
        following.setText(self.tr('Next'))
        following.clicked.connect(self.findNext)
        close = QToolButton(self._searchBar)
        close.setText(self.tr('Close'))
        close.clicked.connect(self.hideSearch)
        for widget in (self._searchEdit, self._searchCount, previous, following, close):
            row.addWidget(widget)
        row.setStretch(0, 1)
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), self._searchBar)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        from foammesh.view.main_window.shortcut_registry import adopt
        adopt('console.findClose', escape)
        escape.activated.connect(self.hideSearch)
        self._searchBar.hide()
        return self._searchBar

    # ------------------------------------------------------------ writing
    def clear(self):
        self._pending.clear()
        self._pendingLines = 0
        self._tailFolded = False
        self._view.clear()
        self._prime()
        self._view.setExtraSelections([])
        self._searchCount.setText('')

    def append(self, text):
        text = '' if text is None else str(text)
        if not self._batchTimer.isActive():
            self._write(readable_batch(text))
            self._batchTimer.start()
            return
        self._pending.append(text)
        self._pendingLines += text.count('\n') + 1
        if self._pendingLines >= APPEND_BATCH_LINES:
            self.flush()

    def flush(self):
        """Write the held appends as one block of text."""
        if not self._pending:
            return
        text = '\n'.join(self._pending)
        self._pending.clear()
        self._pendingLines = 0
        self._write(readable_batch(text))
        self._batchTimer.start()

    def _write(self, text, forced_level=None):
        view = self._view
        view.appendPlainText(text)
        document = view.document()
        count = text.count('\n') + 1
        blocks = []
        block = document.lastBlock()
        while block.isValid() and len(blocks) < count:
            blocks.append(block)
            block = block.previous()
        self._fixLineHeight(blocks)
        self._classify(reversed(blocks), forced_level)
        excess = document.blockCount() - CONSOLE_LINES
        if excess > CONSOLE_TRIM_SLACK:
            cursor = QTextCursor(document)
            cursor.setPosition(document.findBlockByNumber(excess).position(),
                               QTextCursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

    def _fixLineHeight(self, blocks):
        """Every block carries the log's fixed line height (the block before
        it usually hands it on; this is for the ones it did not reach)."""
        wanted = _line_format()
        stale = [block for block in blocks
                 if block.isValid()
                 and block.blockFormat().lineHeight() != LOG_LINE_PX]
        if not stale:
            return
        edit = QTextCursor(self._view.document())
        edit.beginEditBlock()
        for block in stale:
            QTextCursor(block).mergeBlockFormat(wanted)
        edit.endEditBlock()

    def _classify(self, blocks, forced_level=None):
        """Give each new block its level, section and visibility."""
        document = self._view.document()
        coloured = []
        hidden_any = False
        for block in blocks:
            text = block.text()
            level = (forced_level if forced_level is not None
                     else console_lines.line_level(text))
            state = level
            heading = forced_level is None and console_lines.is_heading(text)
            if heading:
                state |= _HEADING_BIT
                self._tailFolded = self._foldAll
                if self._foldAll:
                    state |= _FOLDED_BIT
            block.setUserState(state)
            visible = level >= self._minLevel and (heading or not self._tailFolded)
            if not visible:
                block.setVisible(False)
                hidden_any = True
            if level > console_lines.INFO:
                coloured.append((block, level))
            elif heading:
                coloured.append((block, _STAGE))
        if coloured:
            self._colourBlocks(coloured)
        if hidden_any:
            document.markContentsDirty(0, document.characterCount())
            self._view.viewport().update()

    def _colourBlocks(self, blocks):
        document = self._view.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        for block, level in blocks:
            if not block.isValid():
                continue
            fmt = QTextCharFormat()
            fmt.setForeground(self._levelColour(level))
            cursor.setPosition(block.position())
            cursor.setPosition(block.position() + max(block.length() - 1, 0),
                               QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(fmt)
        cursor.endEditBlock()

    @staticmethod
    def _levelColour(level):
        if level == _STAGE:
            return _theme_color('console.stage', '#7fa6ea')
        if level >= console_lines.ERROR:
            return _theme_color('status.error', '#ef5350')
        return _theme_color('status.warning', '#d2a106')

    def appendError(self, text):
        """Write ``text`` as an error, in the theme's error colour."""
        self.flush()
        self._write(text, forced_level=console_lines.ERROR)

    # ----------------------------------------------- level filter and folds
    def minimumLevel(self):
        return self._minLevel

    def setMinimumLevel(self, level):
        self._minLevel = int(level)
        index = self._levelBox.findData(self._minLevel)
        if index >= 0 and index != self._levelBox.currentIndex():
            self._levelBox.blockSignals(True)
            self._levelBox.setCurrentIndex(index)
            self._levelBox.blockSignals(False)
        self._reflow()

    def foldAll(self, folded=True):
        """Fold every stage to its heading, or open them all."""
        self.flush()
        self._foldAll = bool(folded)
        if self._foldButton.isChecked() != self._foldAll:
            self._foldButton.blockSignals(True)
            self._foldButton.setChecked(self._foldAll)
            self._foldButton.blockSignals(False)
        block = self._view.document().firstBlock()
        while block.isValid():
            state = block.userState()
            if state >= 0 and state & _HEADING_BIT:
                block.setUserState((state | _FOLDED_BIT) if self._foldAll
                                   else (state & ~_FOLDED_BIT))
            block = block.next()
        self._reflow()

    def toggleFold(self, block):
        """Fold or open the stage that ``block`` heads."""
        state = block.userState()
        if state < 0 or not state & _HEADING_BIT:
            return False
        block.setUserState(state ^ _FOLDED_BIT)
        self._reflow()
        return True

    def _reflow(self):
        """Recompute every block's visibility from level filter and folds."""
        document = self._view.document()
        folded = False
        block = document.firstBlock()
        while block.isValid():
            state = max(block.userState(), 0)
            heading = bool(state & _HEADING_BIT)
            if heading:
                folded = bool(state & _FOLDED_BIT)
            visible = (state & _LEVEL_MASK) >= self._minLevel and (heading or not folded)
            if block.isVisible() != visible:
                block.setVisible(visible)
            block = block.next()
        self._tailFolded = folded
        document.markContentsDirty(0, document.characterCount())
        self._view.viewport().update()

    def visibleLineCount(self):
        count = 0
        block = self._view.document().firstBlock()
        while block.isValid():
            count += block.isVisible()
            block = block.next()
        return count

    # --------------------------------------------------------------- search
    def showSearch(self):
        self._searchBar.show()
        self._searchEdit.setFocus()
        self._searchEdit.selectAll()
        if self._searchEdit.text():
            self.search(self._searchEdit.text())

    def hideSearch(self):
        self._searchBar.hide()
        self._view.setExtraSelections([])
        self._view.setFocus()

    def search(self, text):
        """Select the first match of ``text`` at or after the cursor.

        Returns the number of matches in the log. The count is one
        ``str.count`` over the text and the match is ``QTextDocument.find``,
        both native, so a 20 000-line log answers well inside 100 ms.
        """
        self.flush()
        self._needle = str(text)
        if not self._needle:
            self._searchCount.setText('')
            self._view.setExtraSelections([])
            return 0
        total = self._view.toPlainText().lower().count(self._needle.lower())
        if total:
            self._find(forward=True)
            self._searchCount.setText(self.tr('{0} matches').format(total))
        else:
            self._view.setExtraSelections([])
            self._searchCount.setText(self.tr('No matches'))
        return total

    def findNext(self):
        return self._find(forward=True)

    def findPrevious(self):
        return self._find(forward=False)

    def _find(self, forward=True):
        if not self._needle:
            return False
        document = self._view.document()
        flags = QTextDocument.FindFlag(0)
        if not forward:
            flags |= QTextDocument.FindFlag.FindBackward
        cursor = self._view.textCursor()
        wrapped = False
        while True:
            found = document.find(self._needle, cursor, flags)
            if found.isNull():
                if wrapped:
                    return False
                wrapped = True
                cursor = QTextCursor(document)
                if not forward:
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                continue
            if found.block().isVisible():
                break
            cursor = found
        self._view.setTextCursor(found)
        self._view.ensureCursorVisible()
        selection = QTextEdit.ExtraSelection()
        selection.cursor = found
        selection.format.setBackground(_theme_color('selection.background', '#264f78'))
        self._view.setExtraSelections([selection])
        return True

    def currentMatch(self):
        cursor = self._view.textCursor()
        return cursor.selectedText() if cursor.hasSelection() else ''

    # ---------------------------------------------------------------- links
    def setNameResolver(self, resolver):
        """Use ``resolver(word) -> stable id or None`` for clickable names."""
        self._nameResolver = resolver

    @staticmethod
    def _openWithDesktop(path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def setPathOpener(self, opener):
        self._openPath = opener or self._openWithDesktop

    def _resolveName(self, word):
        if self._nameResolver is not None:
            return self._nameResolver(word)
        try:
            from foammesh.app import app
            service = app.selectionService
        except Exception:                                     # noqa: BLE001
            return None
        if self._names is None:
            self._names = {entity.label: entity.stable_id
                           for entity in service.entities(include_invalid=False)
                           if entity.label}
            if not getattr(self, '_namesWatched', False):
                service.state_changed.connect(self._forgetNames)
                self._namesWatched = True
        return self._names.get(word)

    def _forgetNames(self, *_args):
        self._names = None

    def linkAt(self, position):
        """The link under a viewport ``position``: ``(kind, text, start, end)``."""
        cursor = self._view.cursorForPosition(position)
        if abs(position.x() - self._view.cursorRect(cursor).x()) > 24:
            return None
        return self.linkInBlock(cursor.block().text(), cursor.positionInBlock())

    def linkInBlock(self, text, column):
        found = console_lines.path_at(text, column)
        if found:
            return ('path', found[2], found[0], found[1])
        word = console_lines.word_at(text, column)
        if word and self._resolveName(word[2]) is not None:
            return ('name', word[2], word[0], word[1])
        return None

    def activateLink(self, link):
        kind, value = link[0], link[1]
        if kind == 'path':
            self._openPath(value)
            self.pathActivated.emit(value)
            return
        identity = self._resolveName(value)
        if identity is not None and self._nameResolver is None:
            try:
                from foammesh.app import app
                app.selectionService.select((identity,))
            except Exception:                                 # noqa: BLE001
                pass
        self.patchActivated.emit(value)

    def eventFilter(self, watched, event):
        if watched is self._view.viewport():
            kind = event.type()
            if kind == QEvent.Type.MouseMove:
                link = (None if event.buttons()
                        else self.linkAt(event.position().toPoint()))
                self._view.viewport().setCursor(
                    Qt.CursorShape.PointingHandCursor if link
                    else Qt.CursorShape.IBeamCursor)
            elif (kind == QEvent.Type.MouseButtonRelease
                  and event.button() == Qt.MouseButton.LeftButton
                  and not self._view.textCursor().hasSelection()):
                link = self.linkAt(event.position().toPoint())
                if link:
                    self.activateLink(link)
            elif (kind == QEvent.Type.MouseButtonDblClick
                  and event.button() == Qt.MouseButton.LeftButton):
                block = self._view.cursorForPosition(
                    event.position().toPoint()).block()
                if self.toggleFold(block):
                    return True
        return super().eventFilter(watched, event)

    # ---------------------------------------------------------------- theme
    def _connectTheme(self):
        try:
            from foammesh.app import app
            manager = app.themeManager
            if manager is not None:
                manager.themeChanged.connect(self._recolour)
        except Exception:                                     # noqa: BLE001
            pass

    def _recolour(self, *_args):
        """A theme change repaints the coloured lines in the new tokens."""
        coloured = []
        block = self._view.document().firstBlock()
        while block.isValid():
            state = max(block.userState(), 0)
            level = state & _LEVEL_MASK
            if level > console_lines.INFO:
                coloured.append((block, level))
            elif state & _HEADING_BIT:
                coloured.append((block, _STAGE))
            block = block.next()
        if coloured:
            self._colourBlocks(coloured)


class ConsoleView(CDockWidget):
    def __init__(self):
        super().__init__(self._title())

        self.setWidget(Console())

    def changeEvent(self, event):
        if event.type() == QEvent.Type.LanguageChange:
            self.setWindowTitle(self._title())

        super().changeEvent(event)

    def clear(self):
        self.widget().clear()

    def _title(self):
        return QCoreApplication.translate('ConsoleView', 'Console')
