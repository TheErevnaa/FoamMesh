"""Gmsh workflow page: gmsh.global_sizing."""
from __future__ import annotations

from foammesh.core.facade.plan48 import pa3_routes
from foammesh.view.widgets.folded_note import FoldedNote

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
        # Plan 44 FX2. One line until opened: three wrapped lines took Gmsh
        # Sizing past the 300 px sheet on a meshed case (G-FITG-1).
        self._threadNote = FoldedNote(self)
        self._threadNote.setObjectName('gmshThreadEffectNote')
        self._threadNote.setWordWrap(True)
        self._threadNote.setProperty('foammeshStatus', 'warning')
        self._threadNote.setVisible(False)
        layout.addWidget(self._threadNote)
        # Plan 31 FC-B. Two more controls on this page can be set to something
        # that reaches nothing, and both were measured rather than reasoned
        # about: a quad surface algorithm on a target that reads tetrahedra,
        # and a per-turn element floor with size-from-curvature switched off.
        self._surfaceNote = FoldedNote(self)
        self._surfaceNote.setObjectName('gmshQuadSurfaceNote')
        self._surfaceNote.setWordWrap(True)
        self._surfaceNote.setProperty('foammeshStatus', 'warning')
        self._surfaceNote.setVisible(False)
        layout.addWidget(self._surfaceNote)
        # Plan 41 GN25c. The Automatic tier of the Sizing row: the shared
        # card, first on the page.
        from foammesh.view.sizing.automatic_sizing_card import (
            AutomaticSizingCard)
        self.sizing_card = AutomaticSizingCard(self._client, 'gmsh', self)
        self.sizing_card.applied.connect(self.refresh_keeping_edits)
        self.sizing_card.rowOpenRequested.connect(self.showAutomaticRow)
        layout.insertWidget(0, self.sizing_card)
        # Plan 48 PA3: the local API reads the notes of this page.
        self._api = GlobalSizingApi(self)
        session = pa3_routes.announce(self._client, 'global_sizing', self._api)
        if session is not None:
            self.destroyed.connect(
                lambda *_a, s=session, a=self._api: pa3_routes.unregister_panel(
                    s, 'global_sizing', a))

    def showAutomaticRow(self, name: str) -> None:
        """M-20. Open the Gmsh size fields fold on the row the card made."""
        panel = getattr(self, 'native_panel', None)
        fold = getattr(self, '_foldNativeFields', None)
        if fold is not None:
            fold.setChecked(True)
        if panel is not None:
            panel.reload()
            panel.select_row(name)

    def refresh(self) -> None:
        super().refresh()
        card = getattr(self, 'sizing_card', None)
        if card is not None:
            card.refresh()
        self._showMeshSetupThreads()
        self._showStageThreadsAuto()
        self._watchReproducible()
        if hasattr(self, '_threadNote'):
            self._updateThreadNote()
        if hasattr(self, '_surfaceNote'):
            self._updateSurfaceNote()

    #: Plan 48 ST (DP-4605). The box beside Threads.
    REPRODUCIBLE = 'gmsh.global_sizing.reproducible'

    def reproducibleEditor(self):
        return self._editors.get(self.REPRODUCIBLE)

    def reproducibleOn(self) -> bool:
        """Whether "Reproducible (single-thread)" is ticked on the page."""
        editor = self.reproducibleEditor()
        return bool(editor is not None and editor.value())

    def _watchReproducible(self) -> None:
        """Ticking the box re-reads the thread note, as an edit to Threads
        does; connected once per editor (a reload rebuilds the editors)."""
        editor = self.reproducibleEditor()
        if editor is None or getattr(editor, '_stWatched', False):
            return
        editor._stWatched = True                          # noqa: SLF001
        editor.valueChanged.connect(
            lambda *_a: self._updateThreadNote()
            if hasattr(self, '_threadNote') else None)
        tip = self.tr(
            'Gmsh 4.15.2 with more than one thread can mesh the same model '
            'into a different number of elements each run. Ticked, the run '
            'uses one thread for every stage, so two runs of one case '
            'agree. Unticked keeps the thread count above, which is faster.')
        editor.editor.setToolTip(tip)
        editor.label.setToolTip(tip)

    def meshSetupThreads(self) -> int:
        """The thread count Mesh setup gives a Gmsh run (Plan 40 UXB8, #29).

        Asked of ``resolve_parallel_threads`` with the execution policy the
        Mesh setup page stores, which is what the job asks too.
        """
        from foammesh.core.gmsh.plan_derivation import resolve_parallel_threads

        values = self._client.field_values((
            'mesh.execution.mode', 'mesh.execution.max_cpu_cores',
            'mesh.execution.max_memory_bytes'))

        def plain(name):
            value = values.get(name)
            return getattr(value, 'value', value)

        def number(name):
            try:
                return max(int(plain(name) or 0), 0)
            except (TypeError, ValueError):
                return 0

        policy = {
            'mode': str(plain('mesh.execution.mode') or 'auto'
                        ).rsplit('.', 1)[-1].lower(),
            'max_cpu_cores': number('mesh.execution.max_cpu_cores') or None,
            'max_memory_bytes':
                number('mesh.execution.max_memory_bytes') or None}
        return resolve_parallel_threads(policy)

    def _showMeshSetupThreads(self) -> None:
        """Threads reads the Mesh setup value until someone overrides it.

        Plan 40 UXB8 (#29). The stored count is 1 until it is overridden, and
        the job reads 1 as "nobody asked" and takes Mesh setup's count, so the
        box used to say 1 against a Mesh setup that said 2. At the floor it
        now says where the number comes from; a count above it is the
        override, and the tooltip says so. (The Expert tier that will hide the
        override waits for UXD4.)
        """
        editor = self._editors.get('gmsh.global_sizing.threads')
        if editor is None:
            return
        try:
            count = self.meshSetupThreads()
        except Exception:                                    # noqa: BLE001
            return
        words = (self.tr('{0} thread').format(count) if count == 1
                 else self.tr('{0} threads').format(count))
        widget = editor.editor
        special = getattr(widget, 'setSpecialValueText', None)
        if special is not None:
            special(self.tr('Mesh setup ({0})').format(words))
            # DP-2873 (live, 2026-10-21). Set here, past the editor's own
            # fit, so it had no elider: the 150 px box showed "sh setup (15
            # threads)", cut on the left.
            from foammesh.view.workflow_controls.field_widgets import (
                fit_special_text)
            fit_special_text(widget)
            # Plan 48 DP-2952: the box now reaches down to 0 (Gmsh's own
            # "automatic"), so the special text sits at 0, not at the stored
            # default of 1. 1 and 0 both mean "nobody asked" to the job, so
            # an untouched 1 is shown as the Mesh setup text (DP-4651).
            if widget.value() <= 1 and widget.minimum() == 0:
                blocked = widget.blockSignals(True)
                try:
                    widget.setValue(0)
                finally:
                    widget.blockSignals(blocked)
        tip = self.tr(
            'Taken from Mesh setup ({0}). A count typed above the minimum '
            'overrides it for Gmsh only.').format(words)
        widget.setToolTip(tip)
        editor.label.setToolTip(tip)

    #: Plan 48 DP-2952. One count per stage; 0 is automatic.
    STAGE_THREADS = ('gmsh.global_sizing.curve_threads',
                     'gmsh.global_sizing.surface_threads',
                     'gmsh.global_sizing.volume_threads')

    def _showStageThreadsAuto(self) -> None:
        """Each stage's thread box reads "Auto" at 0, its floor: Gmsh's own
        0, as many threads as the Threads box gives."""
        from foammesh.view.workflow_controls.field_widgets import (
            fit_special_text)

        for field_id in self.STAGE_THREADS:
            editor = self._editors.get(field_id)
            widget = getattr(editor, 'editor', None)
            special = getattr(widget, 'setSpecialValueText', None)
            if special is None or widget.minimum() != 0:
                continue
            if widget.specialValueText() != self.tr('Auto'):
                special(self.tr('Auto'))
                fit_special_text(widget)
            tip = self.tr('Auto (0) uses the Threads count.')
            widget.setToolTip(tip)

    def _updateSurfaceNote(self) -> None:
        """Say what a quadrilateral surface algorithm costs at export.

        Plan 41 GN2 (G2). The algorithm is no longer swapped for another one
        when the target cannot read quadrilaterals: the mesh is made as asked
        and the polyMesh export is the thing that says it cannot write it.
        """
        from foammesh.core.gmsh.plan_derivation import QUAD_SURFACE_ALGORITHMS

        values = self._client.field_values(('gmsh.global_sizing.surface',))
        algorithm = str(
            getattr(values.get('gmsh.global_sizing.surface'), 'value',
                    values.get('gmsh.global_sizing.surface') or '')
        ).split('.')[-1].lower()
        if algorithm not in QUAD_SURFACE_ALGORITHMS:
            self._surfaceNote.setVisible(False)
            return
        self._surfaceNote.setText(self.tr(
            'The %s algorithm meshes the surfaces in quadrilaterals. The '
            'mesh is made as chosen; a quadrilateral surface mesh can be '
            'written in the Gmsh formats but not as an OpenFOAM polyMesh.')
            % algorithm)
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

        if self.reproducibleOn():
            # One thread everywhere: nothing is left to say about the volume
            # algorithm ignoring a count that is not used.
            self._threadNote.setVisible(False)
            return
        values = self._client.field_values(
            ('gmsh.global_sizing.volume', 'gmsh.global_sizing.threads',
             'gmsh.global_sizing.volume_threads'))
        algorithm = str(
            getattr(values.get('gmsh.global_sizing.volume'), 'value',
                    values.get('gmsh.global_sizing.volume') or '')
        ).split('.')[-1].lower()
        default = self.default_threads()
        try:
            threads = int(values.get('gmsh.global_sizing.threads') or default)
        except (TypeError, ValueError):
            threads = default
        # Plan 48 DP-2952: a volume count of its own is what the volume pass
        # was asked for; Auto (0) is the Threads count.
        try:
            threads = int(values.get('gmsh.global_sizing.volume_threads')
                          or 0) or threads
        except (TypeError, ValueError):
            pass

        if algorithm in THREADED_VOLUME_ALGORITHMS or threads <= 1:
            self._threadNote.setVisible(False)
            return
        self._threadNote.setText(self.tr(
            'The %s volume algorithm is single-threaded, so the volume pass '
            'will use 1 thread, not %d. The requested threads still apply to '
            'surface meshing. Choose hxt for a threaded volume pass.')
            % (algorithm, threads))
        self._threadNote.setVisible(True)


class GlobalSizingApi:
    """What the local API reads on the open Gmsh Global sizing page (Plan 48
    PA3); registered on the case session, called by ``pa3_routes``."""

    def __init__(self, page: GmshGlobalSizingPage):
        self._page = page

    def notes(self) -> dict:
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread

        def read():
            page = self._page
            page._updateThreadNote()                    # noqa: SLF001
            page._updateSurfaceNote()                   # noqa: SLF001

            def note(label):
                return {'text': label.text(), 'shown': not label.isHidden()}
            editor = page._editors.get('gmsh.global_sizing.threads')  # noqa: SLF001
            special = getattr(getattr(editor, 'editor', None),
                              'specialValueText', None)
            return {'thread_note': note(page._threadNote),  # noqa: SLF001
                    'quad_note': note(page._surfaceNote),   # noqa: SLF001
                    'threads_text': special() if callable(special) else '',
                    'mesh_setup_threads': page.meshSetupThreads()}
        return on_gui_thread(read)

    def reproducible(self, enabled=None, timeout: float = 20.0) -> dict:
        """Plan 48 ST (DP-4605). Read, or tick / untick and apply, the
        "Reproducible (single-thread)" box -- as a click on the box and on
        Apply do. Answers the box and the stored value after."""
        import time

        from foammesh.view.gmsh_tools.window_actions import on_gui_thread

        page = self._page

        def state():
            editor = page.reproducibleEditor()
            if editor is None:
                raise ValueError(
                    'this page has no Reproducible (single-thread) box')
            stored = page._client.field_values(            # noqa: SLF001
                (page.REPRODUCIBLE,)).get(page.REPRODUCIBLE)
            return {'reproducible': bool(editor.value()),
                    'stored': bool(getattr(stored, 'value', stored)),
                    'pending': page.REPRODUCIBLE in page._pending,
                    'thread_note_shown': not page._threadNote.isHidden()}

        def click():
            editor = page.reproducibleEditor()
            if editor is None:
                raise ValueError(
                    'this page has no Reproducible (single-thread) box')
            if bool(editor.value()) != bool(enabled):
                editor.editor.setChecked(bool(enabled))
            page.apply()
            return state()

        if enabled is None:
            return on_gui_thread(state)
        done = on_gui_thread(click)
        deadline = time.time() + timeout
        while done['stored'] != bool(enabled) and time.time() < deadline:
            time.sleep(0.05)
            done = on_gui_thread(state)
        return done

    def sizing_view(self, layer: str = '', shown=None) -> dict:
        """The two viewport toggles of the Automatic sizing card.

        ``field`` is the card's Preview button (the proposed size field) and
        ``conformity`` its "Show on the mesh" box (the cells whose size is off
        the field). With *layer* and *shown* the toggle is set as a click sets
        it, so the card draws or clears the same overlay; the answer is the
        state of both.
        """
        from foammesh.view.gmsh_tools.window_actions import on_gui_thread

        def act():
            card = self._page.sizing_card
            toggles = {'field': card.preview_button,
                       'conformity': card.conformity_view}
            if layer:
                if layer not in toggles:
                    raise ValueError(
                        f'{layer!r} is not a sizing view; they are '
                        + ', '.join(toggles))
                toggle = toggles[layer]
                if card.after_mesh.isHidden() and layer == 'conformity':
                    raise ValueError(
                        'size conformity is shown only on the snappy route')
                if shown is not None:
                    if not toggle.isEnabled():
                        raise ValueError(
                            'compute the automatic sizing field first')
                    toggle.setChecked(bool(shown))
            return {name: {'shown': toggle.isChecked(),
                           'enabled': toggle.isEnabled(),
                           'available': not (name == 'conformity'
                                             and card.after_mesh.isHidden())}
                    for name, toggle in toggles.items()}
        return on_gui_thread(act)
