"""Plan 44 MF20. A table that is empty says so in one line and is not tall.

An empty table used to be a header over a blank rectangle with a minimum
height. :func:`attach_empty_state` fits the table to its rows (the
``foammeshFitRows`` rule of Plan 44 MF19, so an empty table is its header and
nothing else) and shows a one-line label under it while it holds no row.
"""
from __future__ import annotations

from PySide6.QtWidgets import QBoxLayout, QLabel

from foammesh.view.theming.metrics import autofit_table

EMPTY_ROLE = 'emptyState'


def attach_empty_state(table, text: str, name: str, cap_rows: int = 8):
    """Fit *table* to its rows and show *text* while it has none.

    Returns the label. It is put right under the table when the table's
    parent lays its children out in a box; otherwise it is only returned.
    """
    table.setMinimumHeight(0)
    autofit_table(table, cap_rows)
    label = QLabel(text, table.parentWidget())
    label.setObjectName(name)
    label.setProperty('foammeshRole', EMPTY_ROLE)
    label.setWordWrap(False)
    parent = table.parentWidget()
    layout = parent.layout() if parent is not None else None
    if isinstance(layout, QBoxLayout):
        index = layout.indexOf(table)
        if index >= 0:
            layout.insertWidget(index + 1, label)

    def sync(*_args):
        try:
            model = table.model()
            label.setVisible(model is None or model.rowCount() == 0)
        except RuntimeError:           # the table was destroyed under us
            return

    model = table.model()
    if model is not None:
        for signal in (model.rowsInserted, model.rowsRemoved,
                       model.modelReset, model.layoutChanged):
            signal.connect(sync)
    sync()
    return label
