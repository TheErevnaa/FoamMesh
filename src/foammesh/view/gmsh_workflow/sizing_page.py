"""Gmsh workflow page: the Sizing row.

Plan 41 GN18 (section 4.1, row 2). Global sizing (O-02), size at points
(M-01), size fields of every type (M-02..M-22), surface sizes and curve
controls (M-23) answer one question, how fine the mesh is and where, and the
outline asked it on two rows. The row is the task `gmsh.global_sizing`; the
page draws its own settings first and then the three collections and the two
panels the size-field task owns. The tasks stay tasks (the job document and the
saved task states do not move) and the one press settles them in order.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel, QWidget

from .base import GmshTaskPage
from .folds import add_fold, refresh_folds
from .global_sizing_page import GmshGlobalSizingPage
from .size_fields_page import GmshSizeFieldsPage


class GmshSizingPage(GmshGlobalSizingPage, GmshSizeFieldsPage):
    task_id_default = 'gmsh.global_sizing'

    #: Plan 44 FIT-G (MF27). The sections that fold, in page order:
    #: ``(attribute or object name, fold name, title, description)``.
    #: MEASURED at b59ac794 in the 300 px sheet of a 1280x800 window: the
    #: card 215 px, the three tables 150 px each, size at points 299 px and
    #: the native catalogue 422 px, all open under the guided form, so the
    #: page ran 1492 px past a 491 px scroller. Each is one press away now,
    #: and its header counts the rows behind it.
    FOLDED_SECTIONS = (
        ('sizing_card', 'AutomaticSizing', 'Automatic sizing',
         'Compute a size field from the geometry, preview it and apply the '
         'proposed sizes.'),
        ('surface_panel', 'SurfaceSizing', 'Surface sizing',
         'A target size on chosen boundaries, ramping back to the global '
         'size over a blend distance.'),
        ('panel', 'SpatialFields', 'Spatial size fields',
         'Distance, box, ball, cylinder, frustum, expression, restrict and '
         'curvature fields.'),
        ('edgeCurveSection', 'EdgeControls', 'Edge/curve controls',
         'Transfinite node counts or local sizes along the curves of a face '
         'group.'),
        ('pointSizesSection', 'PointSizes', 'Size at points',
         'A local size at points picked in the viewport.'),
        ('nativeFieldsSection', 'NativeFields', 'Gmsh size fields',
         'Every Gmsh size-field type: rows, the field graph and a preview.'),
    )

    #: The section titles the fold header now says, hidden inside the fold.
    _FOLDED_TITLES = ('pointSizesTitle', 'nativeFieldsTitle',
                      'automaticSizingTitle')

    def build_sections(self, layout) -> None:
        GmshGlobalSizingPage.build_sections(self, layout)
        GmshSizeFieldsPage.build_sections(self, layout)
        self._folds = []
        for attribute, name, title, description in self.FOLDED_SECTIONS:
            section = getattr(self, attribute, None)
            if section is None:
                section = self.findChild(QWidget, attribute)
            if section is None or layout.indexOf(section) < 0:
                continue
            index = layout.indexOf(section)
            layout.removeWidget(section)
            header = add_fold(self, layout, name=name, title=self.tr(title),
                              description=self.tr(description),
                              panel=section, index=index)
            setattr(self, f'_fold{name}', header)
            self._folds.append((header, section))
        for name in self._FOLDED_TITLES:
            label = self.findChild(QLabel, name)
            if label is not None:
                label.setVisible(False)

    def folds(self) -> tuple:
        """``(header, section)`` per folded section, in page order."""
        return tuple(getattr(self, '_folds', ()))

    def refresh(self) -> None:
        super().refresh()
        refresh_folds(self.folds())


class GmshSizeFieldsHostedPage(GmshTaskPage):
    """The size-field task, whose controls are drawn on the Sizing row.

    The class stays because every engine task has a page, and the outline
    walks the task as a substep of Sizing. It draws nothing, as the curve task
    does: what it would draw is drawn where the reader is.
    """

    task_id_default = 'gmsh.size_fields'

    def renders_field(self, field_id: str) -> bool:
        return False

    def build_sections(self, layout) -> None:
        return None
