"""Snappy workflow page: ``snappy.domain_regions``.

Plan 30 WP-09 / F-17. The legacy Designer page edited the ``region``
collection through cards of its own; the collection is registered
(``regions.items``) and the shared child-control table is what every other
collection in the app is edited with, so the port keeps one editor idiom
rather than a second one for this task alone.

Plan 31 adds the other half of the same question. A material point only means
something once OpenFOAM can tell inside from outside, and until now the app
never said which surfaces it could tell that for: every staged surface was
written ``type triSurface``, so a surface with a pinhole in it silently
answered "no" and any refinement region or cellZone built on it was warned
about in a log and dropped. The seven fields named here are the ones that
decide that -- whether the surfaces are declared closed, how narrow a gap
counts as closed, and how finely the surfaces are searched -- so they belong
on the page where the user is already reasoning about what encloses what.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from foammesh.view.workflow_controls.child_controls import ChildControlPanel

from .base import SnappyTaskPage


class SnappyDomainRegionsPage(SnappyTaskPage):
    """The material points that say which side of the surface is meshed."""

    task_id_default = 'snappy.domain_regions'

    #: ``regions.items`` element fields, in the order they are read in.
    COLUMNS = ('name', 'type', 'point.x', 'point.y', 'point.z')

    #: Plan 31. Written into every ``geometry`` entry of snappyHexMeshDict,
    #: absent from the task descriptor. They are not ``castellatedMeshControls``
    #: keys -- they describe the surfaces themselves -- so they are named here
    #: rather than declared on a stage that does not own them.
    extra_field_ids = (
        'meshing.geometry.tri_surface_declaration',
        'meshing.geometry.gap_detection',
        'meshing.geometry.gap_width',
        'meshing.geometry.tolerance',
        'meshing.geometry.max_tree_depth',
        'meshing.geometry.min_quality',
        'meshing.geometry.scale',
    )

    def build_sections(self, layout) -> None:
        note = QLabel(self.tr(
            'A region names a point inside the volume to keep. snappyHexMesh '
            'keeps the cells reachable from it, so a point on the wrong side '
            'of a surface produces the complement of the mesh you wanted.'),
            self)
        note.setWordWrap(True)
        layout.addWidget(note)

        closure = QLabel(self.tr(
            'Inside and outside only exist for a surface OpenFOAM considers '
            'closed. It decides that by looking for open edges, so a surface '
            'meant to be watertight that has a pinhole or arrives in several '
            'parts answers "neither" -- and refinement regions and cell zones '
            'built on it are then dropped with a note in the log. Declare the '
            'surfaces closed to override that, and close narrow gaps when the '
            'geometry has slots the base grid is too coarse to see.'), self)
        closure.setWordWrap(True)
        layout.addWidget(closure)

        self.panel = ChildControlPanel(
            self._client, 'regions.items', self.tr('Regions'),
            columns=self.COLUMNS, parent=self)
        self.panel.childrenChanged.connect(self.refresh)
        layout.addWidget(self.panel)
