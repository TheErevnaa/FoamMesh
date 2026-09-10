"""Gmsh workflow page: gmsh.global_sizing."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from .base import GmshTaskPage


class GmshGlobalSizingPage(GmshTaskPage):
    """Sizing, algorithms and threads -- and the link between two of them.

    Plan 26 WP6.6. The volume algorithm and the thread count sit on this page
    with nothing linking them, and two of the three algorithms ignore threads
    entirely: only HXT is the parallel tetrahedraliser, so ``delaunay`` and
    ``frontal`` mesh the volume on one thread whatever the control says.

    The derivation has always computed ``effectiveVolumeThreads`` and no view
    read it. Measured: with ``delaunay`` selected, sixteen threads produced
    **identical** cell counts to one thread across annulus (187,891), duct
    (78,305), elbow (91,220) and sphere (253,612) -- more threads changed no
    mesh.

    The control is annotated rather than disabled: 2D meshing still threads,
    so the value is not inert, it just does not reach the pass a user is most
    likely to be waiting for.
    """

    task_id_default = 'gmsh.global_sizing'

    def build_sections(self, layout) -> None:
        self._threadNote = QLabel(self)
        self._threadNote.setObjectName('gmshThreadEffectNote')
        self._threadNote.setWordWrap(True)
        self._threadNote.setProperty('foammeshStatus', 'warning')
        self._threadNote.setVisible(False)
        layout.addWidget(self._threadNote)
        # Plan 31 FC-B. Two more controls on this page can be set to something
        # that reaches nothing, and both were measured rather than reasoned
        # about: a quad surface algorithm on a target that reads tetrahedra,
        # and a per-turn element floor with size-from-curvature switched off.
        self._surfaceNote = QLabel(self)
        self._surfaceNote.setObjectName('gmshQuadSurfaceNote')
        self._surfaceNote.setWordWrap(True)
        self._surfaceNote.setProperty('foammeshStatus', 'warning')
        self._surfaceNote.setVisible(False)
        layout.addWidget(self._surfaceNote)

    def refresh(self) -> None:
        super().refresh()
        if hasattr(self, '_threadNote'):
            self._updateThreadNote()
        if hasattr(self, '_surfaceNote'):
            self._updateSurfaceNote()

    def _updateSurfaceNote(self) -> None:
        """Say when the chosen surface algorithm will not be the one used.

        Plan 31 FC-B. ``frontal_delaunay_quads`` and ``quasi_structured_quad``
        mesh in quadrilaterals, and the derivation clears them for any target
        but SU2 -- the same gate recombination goes through, for the same
        measured reason. Said here so the user reads it while they can act on
        it, rather than in a warning attached to the finished run.
        """
        from foammesh.core.gmsh.plan_derivation import (
            QUAD_SURFACE_ALGORITHMS,
            RECOMBINING_SOLVERS,
        )

        values = self._client.field_values(('gmsh.global_sizing.surface',))
        algorithm = str(
            getattr(values.get('gmsh.global_sizing.surface'), 'value',
                    values.get('gmsh.global_sizing.surface') or '')
        ).split('.')[-1].lower()
        target = self.target_solver()
        if algorithm not in QUAD_SURFACE_ALGORITHMS:
            self._surfaceNote.setVisible(False)
            return
        if target in RECOMBINING_SOLVERS:
            self._surfaceNote.setVisible(False)
            return
        self._surfaceNote.setText(self.tr(
            'The %s algorithm meshes the surfaces in quadrilaterals, which '
            'the %s route cannot read, so the mesh will be made with '
            'frontal_delaunay instead. Choose the su2 target to mesh in '
            'quads.') % (algorithm, target))
        self._surfaceNote.setVisible(True)

    def default_threads(self) -> int:
        """The shipped thread count, read from the registry rather than typed.

        Plan 29 WP8. This page used to fall back to a 1 of its own while the
        derivation fell back to 4, so a project that had never touched the
        control was described one way and meshed another. There is one default
        and it lives in the schema.
        """
        try:
            default = self._client.descriptor('gmsh.global_sizing.threads').default
        except Exception:                       # noqa: BLE001 - advisory note
            return 1
        try:
            return int(default)
        except (TypeError, ValueError):
            return 1

    def _updateThreadNote(self) -> None:
        """State the thread count that will actually reach the volume pass."""
        from foammesh.core.gmsh.plan_derivation import (
            THREADED_VOLUME_ALGORITHMS,
        )

        values = self._client.field_values(
            ('gmsh.global_sizing.volume', 'gmsh.global_sizing.threads'))
        algorithm = str(
            getattr(values.get('gmsh.global_sizing.volume'), 'value',
                    values.get('gmsh.global_sizing.volume') or '')
        ).split('.')[-1].lower()
        default = self.default_threads()
        try:
            threads = int(values.get('gmsh.global_sizing.threads') or default)
        except (TypeError, ValueError):
            threads = default

        if algorithm in THREADED_VOLUME_ALGORITHMS or threads <= 1:
            self._threadNote.setVisible(False)
            return
        self._threadNote.setText(self.tr(
            'The %s volume algorithm is single-threaded, so the volume pass '
            'will use 1 thread, not %d. The requested threads still apply to '
            'surface meshing. Choose hxt for a threaded volume pass.')
            % (algorithm, threads))
        self._threadNote.setVisible(True)
