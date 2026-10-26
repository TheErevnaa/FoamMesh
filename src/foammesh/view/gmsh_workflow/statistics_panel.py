"""Gmsh's Statistics window, for the mesh in the viewport.

Plan 41 GN14 (T-06, FM-14).  Nodes; elements by type; min, average and
maximum of each quality measure; timing per pass; a histogram of any measure
("Plot"); and a button that runs Gmsh's ``AnalyseMeshQuality`` plugin for the
Jacobian ratio, IGE and ICN.  Every number is read from the arrays the session
handed to the viewport (``core.gmsh.element_quality``), so the panel describes
the cells that are drawn and needs no polyMesh and no ``.msh``.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from foammesh.core.gmsh import element_quality
from foammesh.core.quantities import aligned
from foammesh.view.main_window.quality_histogram import QualityHistogram


def _table(parent, name: str, headings) -> QTableWidget:
    table = QTableWidget(0, len(headings), parent)
    table.setObjectName(name)
    table.setHorizontalHeaderLabels(list(headings))
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.horizontalHeader().setSectionResizeMode(
        QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _number(value) -> str:
    return f'{float(value):.6g}'


#: Plan 48 GM11b. The Geometry tab of Gmsh's Statistics window: how many
#: points, curves, surfaces and volumes the model has.
GEOMETRY_KINDS = ((0, 'Points'), (1, 'Curves'), (2, 'Surfaces'),
                  (3, 'Volumes'))


def geometry_counts(entities, hidden=(), visible_only: bool = False) -> dict:
    """``{dim: count}`` of the ``(dim, tag)`` pairs in *entities*.

    With *visible_only* the entities in *hidden* are left out, as Gmsh's
    "Visible only" option does.
    """
    hidden = {tuple(h) for h in hidden} if visible_only else set()
    counts = {dim: 0 for dim, _name in GEOMETRY_KINDS}
    for dim, tag in {tuple(e) for e in entities}:
        if (dim, tag) in hidden or dim not in counts:
            continue
        counts[dim] += 1
    return counts


class GmshStatisticsPanel(QWidget):
    """Counts, quality extremes, timings and a histogram of the session mesh."""

    #: The user asked to plot a measure; the Gmsh name (``minSICN``).
    plotRequested = Signal(str)
    #: The gate judged the mesh on screen; the verdict as the strip reads it.
    verdictReady = Signal(dict)

    GATE_FIELDS = (
        ('qualityType', 'gmsh.compute.quality_type'),
        ('minQuality', 'gmsh.compute.min_quality'),
        ('allowedFraction', 'gmsh.compute.allowed_fraction'),
        ('allowedCount', 'gmsh.compute.allowed_count'),
        ('hardFloor', 'gmsh.compute.hard_floor'),
        ('extraLimits', 'gmsh.compute.extra_limits'),
    )

    def __init__(self, controller=lambda: None, parent=None, *, client=None):
        super().__init__(parent)
        self._controller = controller
        self._client = client
        self._stats: dict = {}
        self._plotted = ''
        self.setObjectName('gmshStatisticsPanel')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._summary = QLabel(self)
        self._summary.setObjectName('gmshStatisticsSummary')
        self._summary.setWordWrap(True)
        layout.addWidget(self._summary)

        self._bounds = QLabel(self)
        self._bounds.setObjectName('gmshStatisticsBounds')
        self._bounds.setWordWrap(True)
        layout.addWidget(self._bounds)

        self._groups = _table(
            self, 'gmshStatisticsGroups',
            (self.tr('Physical group'), self.tr('Kind'), self.tr('Elements')))
        self._groups.setMaximumHeight(130)
        layout.addWidget(self._groups)

        # Plan 48 GM11b. Gmsh's Geometry tab: entity counts, all or visible.
        self._geometry = _table(
            self, 'gmshStatisticsGeometry',
            (self.tr('Geometry'), self.tr('Count')))
        self._geometry.setMaximumHeight(130)
        layout.addWidget(self._geometry)
        self._visibleOnly = QCheckBox(self.tr('Visible entities only'), self)
        self._visibleOnly.setObjectName('gmshStatisticsVisibleOnly')
        self._visibleOnly.setToolTip(self.tr(
            'Count only the entities that are not hidden in the viewport'))
        self._visibleOnly.toggled.connect(lambda _on: self.refreshGeometry())
        layout.addWidget(self._visibleOnly)

        self._counts = _table(self, 'gmshStatisticsCounts',
                              (self.tr('Element type'), self.tr('Count')))
        self._counts.setMaximumHeight(160)
        layout.addWidget(self._counts)

        self._quality = _table(
            self, 'gmshStatisticsQuality',
            (self.tr('Dim'), self.tr('Measure'), self.tr('Min'),
             self.tr('Average'), self.tr('Max'), ''))
        self._quality.setMinimumHeight(140)
        layout.addWidget(self._quality)

        self._histogram = QualityHistogram(self)
        self._histogram.setObjectName('gmshStatisticsHistogram')
        layout.addWidget(self._histogram)

        self._passes = _table(self, 'gmshStatisticsPasses',
                              (self.tr('Pass'), self.tr('Seconds')))
        self._passes.setMaximumHeight(130)
        layout.addWidget(self._passes)

        self._plugin = _table(
            self, 'gmshStatisticsPlugin',
            (self.tr('Plugin output'), self.tr('Min'), self.tr('Average'),
             self.tr('Max'), self.tr('Elements')))
        self._plugin.setMaximumHeight(110)
        self._plugin.setVisible(False)
        layout.addWidget(self._plugin)

        self._message = QLabel(self)
        self._message.setObjectName('gmshStatisticsMessage')
        self._message.setWordWrap(True)
        self._message.setVisible(False)
        layout.addWidget(self._message)

        buttons = QHBoxLayout()
        self._refresh = QPushButton(self.tr('Refresh'), self)
        self._refresh.setObjectName('gmshStatisticsRefresh')
        self._refresh.clicked.connect(self.refresh)
        self._analyse = QPushButton(self.tr('Analyse mesh quality'), self)
        self._analyse.setObjectName('gmshStatisticsAnalyse')
        self._analyse.setToolTip(self.tr(
            "Run Gmsh's AnalyseMeshQuality plugin: the Jacobian ratio, the "
            'inverse gradient error and the inverse condition number'))
        self._analyse.clicked.connect(self.analyse)
        buttons.addWidget(self._refresh)
        buttons.addWidget(self._analyse)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.clear()

    # -- content ------------------------------------------------------------ #

    def clear(self) -> None:
        self._stats = {}
        self._summary.setText(self.tr('No mesh has been generated yet.'))
        self._bounds.setText('')
        for table in (self._counts, self._quality, self._passes,
                      self._plugin, self._groups):
            table.setRowCount(0)
        self._groups.setVisible(False)
        self._histogram.clear()
        self._plugin.setVisible(False)

    def statistics(self) -> dict:
        return dict(self._stats)

    # -- what the panel shows (Plan 48 PA4: the local API reads the same) ------ #

    @staticmethod
    def _rows_of(table) -> list:
        return [[(table.item(row, column).text()
                  if table.item(row, column) is not None else '')
                 for column in range(table.columnCount())]
                for row in range(table.rowCount())]

    def shown(self) -> dict:
        """Every read-out of the panel as the person reads it (text)."""
        histogram = self._histogram
        return {
            'summary': self._summary.text(),
            'bounds': self._bounds.text(),
            'groups': self._rows_of(self._groups),
            'groups_shown': not self._groups.isHidden(),
            'geometry': self._rows_of(self._geometry),
            'counts': self._rows_of(self._counts),
            'quality': self._rows_of(self._quality),
            'passes': self._rows_of(self._passes),
            'plugin': self._rows_of(self._plugin),
            'plugin_shown': not self._plugin.isHidden(),
            'message': self._message.text(),
            'plotted': self._plotted,
            'histogram': {'metric': histogram._metric,
                          'edges': list(histogram.edges()),
                          'counts': list(histogram.counts()),
                          'total': int(histogram.total)},
        }

    # -- Geometry tab (Plan 48 GM11b) ----------------------------------------- #

    def visibleOnly(self) -> bool:
        return self._visibleOnly.isChecked()

    def setVisibleOnly(self, on: bool) -> None:
        self._visibleOnly.setChecked(bool(on))

    def geometryCounts(self) -> dict:
        """``{'Points': n, ...}`` as the Geometry table shows them."""
        return {self._geometry.item(row, 0).text(): int(
            self._geometry.item(row, 1).text().replace(',', ''))
            for row in range(self._geometry.rowCount())}

    def _geometry_source(self) -> tuple:
        """``(entities, hidden)`` of the model on screen, from the picker."""
        try:
            from foammesh.view.gmsh_workflow.shared_pick import (
                selection_service)
            source = selection_service().pick_source
            picker = source.picker() if source is not None else None
            entities = picker.map.entities() if picker is not None else []
        except Exception:                             # noqa: BLE001
            entities = []
        hidden = ()
        try:
            from foammesh.app import app
            window = app.window
            panel = window._renderingTool.gmshDisplayPanel()
            hidden = panel.visibilityModel().hidden
        except Exception:                             # noqa: BLE001
            hidden = ()
        return entities, hidden

    def show_geometry(self, entities, hidden=()) -> dict:
        """Fill the Geometry table from the model's ``(dim, tag)`` pairs."""
        counts = geometry_counts(entities, hidden, self.visibleOnly())
        self._geometry.setRowCount(len(GEOMETRY_KINDS))
        for row, (dim, name) in enumerate(GEOMETRY_KINDS):
            self._geometry.setItem(row, 0, QTableWidgetItem(self.tr(name)))
            self._geometry.setItem(row, 1, QTableWidgetItem(
                f'{counts[dim]:,}'))
        return counts

    def refreshGeometry(self) -> dict:
        entities, hidden = self._geometry_source()
        return self.show_geometry(entities, hidden)

    def show_statistics(self, stats: dict) -> None:
        """Fill every table from *stats* (``element_quality.statistics``)."""
        self._stats = dict(stats or {})
        stats = self._stats
        self._summary.setText(self.tr(
            '%s nodes, %s elements.') % (f"{stats.get('nodes', 0):,}",
                                         f"{stats.get('elements', 0):,}"))
        self._fill_info(stats)
        counts = stats.get('counts') or {}
        self._counts.setRowCount(len(counts))
        for row, (name, count) in enumerate(sorted(
                counts.items(),
                key=lambda item: (element_quality.FAMILIES.index(
                    element_quality.family_of(item[0]))
                    if element_quality.family_of(item[0])
                    in element_quality.FAMILIES else 99, item[0]))):
            self._counts.setItem(row, 0, QTableWidgetItem(name))
            self._counts.setItem(row, 1, QTableWidgetItem(f'{count:,}'))
        self._fill_quality(stats)
        passes = stats.get('passes') or []
        self._passes.setRowCount(len(passes))
        for row, item in enumerate(passes):
            self._passes.setItem(row, 0, QTableWidgetItem(
                str(item.get('title', ''))))
            self._passes.setItem(row, 1, QTableWidgetItem(
                f"{float(item.get('seconds', 0.0)):.3f}"))
        self._passes.setVisible(bool(passes))
        measure = self._plotted or element_quality.STATISTICS_MEASURES[0]
        self.plot(measure)

    def _fill_info(self, stats: dict) -> None:
        """The Mesh info facts: bounds and the physical groups (FM-14)."""
        box = stats.get('bounds')
        if box:
            self._bounds.setText(self.tr(
                'Bounds: min (%s), max (%s), span (%s).') % tuple(
                    ', '.join(aligned(box[key]))  # DP-165
                    for key in ('min', 'max', 'span')))
        else:
            self._bounds.setText('')
        groups = stats.get('groups') or []
        self._groups.setRowCount(len(groups))
        kinds = {'patch': self.tr('Patch (surface)'),
                 'zone': self.tr('Zone (volume)')}
        for row, group in enumerate(groups):
            self._groups.setItem(row, 0, QTableWidgetItem(str(group['name'])))
            self._groups.setItem(row, 1, QTableWidgetItem(
                kinds.get(group['kind'], group['kind'])))
            self._groups.setItem(row, 2, QTableWidgetItem(
                f"{int(group['elements']):,}"))
        self._groups.setVisible(bool(groups))

    def _fill_quality(self, stats: dict) -> None:
        labels = element_quality.labels()
        rows = []
        for dim, block in sorted((stats.get('quality') or {}).items(),
                                 reverse=True):
            for name, spread in block.items():
                if name == 'elements' or not isinstance(spread, dict):
                    continue
                rows.append((dim, name, spread))
        self._quality.setRowCount(len(rows))
        for row, (dim, name, spread) in enumerate(rows):
            self._quality.setItem(row, 0, QTableWidgetItem(str(dim)))
            self._quality.setItem(row, 1, QTableWidgetItem(
                labels.get(name, name)))
            for column, key in ((2, 'min'), (3, 'avg'), (4, 'max')):
                self._quality.setItem(row, column, QTableWidgetItem(
                    _number(spread[key])))
            button = QPushButton(self._quality)
            button.setText(self.tr('Plot'))
            button.setObjectName(f'gmshStatisticsPlot_{dim}_{name}')
            button.setAccessibleName(self.tr('Plot %s') % labels.get(name, name))
            button.clicked.connect(
                lambda _checked=False, which=name, d=dim: self.plot(which, d))
            self._quality.setCellWidget(row, 5, button)

    def plot(self, measure: str, dim: str | None = None) -> None:
        """Draw the histogram of *measure* (the highest dimension by default)."""
        plots = self._stats.get('histograms') or {}
        if dim is None and plots:
            dim = max(plots)
        shown = (plots.get(str(dim)) or {}).get(measure)
        if not shown:
            self._histogram.clear()
            return
        self._plotted = measure
        self._histogram.set_distribution(
            element_quality.labels().get(measure, measure), shown)
        self.plotRequested.emit(measure)

    def refreshButton(self) -> QPushButton:
        return self._refresh

    def histogram(self) -> QualityHistogram:
        return self._histogram

    # -- actions ------------------------------------------------------------ #

    def refresh(self) -> None:
        """Read the statistics of the mesh the viewport shows."""
        self.refreshGeometry()
        controller = self._controller()
        if controller is None:
            self.clear()
            return
        try:
            self.show_statistics(controller.statistics())
            self._say('')
        except Exception as error:                    # noqa: BLE001
            self.clear()
            self._say(str(error))

    def analyse(self) -> None:
        """Run the AnalyseMeshQuality plugin and list what it measured."""
        controller = self._controller()
        if controller is None:
            self._say(self.tr('There is no mesh yet; generate one first.'))
            return
        try:
            dim = 3 if '3' in (self._stats.get('quality') or {}) else 2
            result = controller.analyse(dim=dim)
        except Exception as error:                    # noqa: BLE001
            self._say(str(error))
            return
        self._say('')
        self.show_plugin(result)

    def thresholds(self):
        """The limits as stored on this page, as the gate derives them."""
        from foammesh.core.gmsh.quality import derive_thresholds

        values = {}
        if self._client is not None:
            stored = self._client.field_values(
                tuple(field for _key, field in self.GATE_FIELDS))
            for key, field in self.GATE_FIELDS:
                value = getattr(stored.get(field), 'value', stored.get(field))
                if value is not None:
                    values[key] = value
        return derive_thresholds(values)

    def judge(self) -> dict:
        """Judge every element of the mesh on screen against the mesher's
        limits (the quality gate on the Quality page).  Plan 42 SG7: there is
        no button for this; the tab's one "Run mesh check" calls it while a
        Gmsh session mesh is on screen."""
        from foammesh.core.gmsh.quality import QualityError, assess_arrays

        controller = self._controller()
        if controller is None:
            self._say(self.tr('There is no mesh yet; generate one first.'))
            return {}
        try:
            arrays = element_quality.read_handover_arrays(
                controller.handover_path())
            verdict = assess_arrays(self.thresholds(), arrays,
                                    centroids=arrays['__centroids'])
        except (QualityError, element_quality.HandoverError, OSError) as error:
            self._say(str(error))
            return {}
        # A session mesh has no batch run behind it to re-publish, so there
        # is nothing for Accept anyway to bind to.
        shown = dict(verdict.to_dict(), overridable=False)
        self._say('')
        self.verdictReady.emit(shown)
        return shown

    def show_plugin(self, result: dict) -> None:
        measures = (result or {}).get('measures') or {}
        self._plugin.setRowCount(len(measures))
        for row, (name, spread) in enumerate(sorted(measures.items())):
            self._plugin.setItem(row, 0, QTableWidgetItem(name))
            for column, key in ((1, 'min'), (2, 'avg'), (3, 'max')):
                self._plugin.setItem(row, column, QTableWidgetItem(
                    _number(spread[key])))
            self._plugin.setItem(row, 4, QTableWidgetItem(
                f"{spread.get('elements', 0):,}"))
        self._plugin.setVisible(bool(measures))

    def _say(self, text: str) -> None:
        self._message.setText(text)
        self._message.setVisible(bool(text))
