"""The Gmsh run's readout: warnings, notes and what the mesh can publish.

Plan 42 SG18. Lifted out of :class:`GmshTaskPage` so the one Quality page,
which both engines mount, can show a Gmsh run's warnings without being a Gmsh
task page. The methods are unchanged; :class:`GmshTaskPage` inherits them.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from foammesh.core.quantities import count_text
from foammesh.view.facade_client import query
from foammesh.view.widgets.link_button import as_link


class GmshRunReadout:
    """Mixin: read the newest Gmsh run and say what it reported."""

    #: Which page a runner warning belongs to, matched on the wording the
    #: runner writes (``runner_v1.apply_volume_controls`` and friends). R66/
    #: R67: ``result.json`` carried ``volume control 'tee_fluid' has no
    #: resolvable volume scope and was skipped`` and the background-field
    #: warning that names Global Sizing, and neither reached any surface in
    #: the GUI -- the Console tab showed nine progress lines and the run
    #: reported success. First match wins, so the background-size-field
    #: warning is attributed before the generic ``size field`` rule.
    RUN_WARNING_TASKS: tuple[tuple[str, tuple[str, ...]], ...] = (
        ('gmsh.global_sizing', ('background size field',)),
        ('gmsh.size_fields', ('size field',)),
        ('gmsh.curve_controls', ('curve control',)),
        ('gmsh.volume_controls', ('volume control',)),
        ('gmsh.boundary_layers', ('boundary layer', 'prism')),
        ('gmsh.periodic', ('periodic pair',)),
        # Plan 31 CP-08 item 7. The farfield cut is part of describing
        # the geometry, and what it did with the volumes it left is the
        # first thing a reader of that page needs: a discarded sealed
        # cavity is a region the mesh does not contain.
        ('gmsh.describe_geometry',
         ('classified surface', 'sealed cavity', 'farfield')),
    )

    #: Pages that show every warning the last run produced rather than the
    #: ones routed to them: Compute Mesh, where the run happened, and the
    #: Quality page, where its result is read. DP-528 (MA G1-P2): no runner
    #: wording routes to ``gmsh.qa``, so the G1 run's ``face6`` warning could
    #: never appear on the page the user checks the mesh on.
    SHOWS_EVERY_RUN_WARNING = False

    # -- what this mesh can be written as ---------------------------------- #

    def element_order(self) -> int:
        try:
            values = self._client.field_values(('gmsh.compute.element_order',))
        except Exception:                                    # noqa: BLE001
            return 1
        try:
            return int(float(values.get('gmsh.compute.element_order') or 1))
        except (TypeError, ValueError):
            return 1

    def publication_plan(self) -> tuple[bool, str]:
        """``(publishes, why not)`` for a run of this case.

        Plan 41 GN16 (G2). The mesh decides, not the target: a first-order
        mesh can be written as ``constant/polyMesh``, a higher-order one
        cannot, because polyMesh is a first-order format. Whether a run
        publishes by itself is the Method page preset's default; pressing
        Publish is always the user's own request.
        """
        if self.element_order() > 1:
            return False, self.tr(
                'This mesh is second order and constant/polyMesh is a '
                'first-order format, so the run keeps its native Gmsh .msh. '
                'There is nothing to publish.')
        return True, ''

    # -- warnings the run produced ----------------------------------------- #

    def last_run(self) -> dict:
        """The newest Gmsh run manifest, or ``{}`` when there is none."""
        try:
            payload = query(self._client, 'mesh.gmsh.runs', {}).payload or {}
        except Exception:                        # noqa: BLE001 - advisory note
            return {}
        runs = [item for item in (payload.get('runs') or ())
                if isinstance(item, dict)]
        if not runs:
            return {}
        # `created_at` has one-second resolution, so the listing order settles
        # ties -- `list_runs` walks the run directory in sorted order.
        return max(enumerate(runs),
                   key=lambda item: (str(item[1].get('created_at') or ''),
                                     item[0]))[1]

    def warning_task(self, text: str) -> str:
        """Which task page a runner warning is about."""
        lowered = str(text).lower()
        for task_id, keywords in self.RUN_WARNING_TASKS:
            if any(keyword in lowered for keyword in keywords):
                return task_id
        return self.run_all_task_id or ''

    def run_warnings(self) -> tuple[str, tuple[str, ...]]:
        """``(run_id, warnings)`` from the last run, for this page.

        The Compute Mesh page shows every warning -- it is the page the run
        happened on -- and each other page shows the ones about its own
        controls, so a discarded volume control is reported where the user
        would go to check it.
        """
        run = self.last_run()
        if not run:
            return '', ()
        texts, seen = [], set()
        # DP-567. Only a pair that did not take effect is a warning; the
        # applied ones are notes, read by `run_notes`.
        pairs = self.interface_pair_lines(run, applied=False)
        for value in (list(run.get('warnings') or ())
                      + list(run.get('runner_warnings') or ())):
            text = str(value).strip()
            # Plan 40 UXB7 (DP-1352). The thread note is information about
            # the run, not a fault in it; `run_notes` shows it.
            if text and text not in seen and not self.is_information_note(text):
                seen.add(text)
                texts.append(text)
        if (self.task_id != self.run_all_task_id
                and not self.SHOWS_EVERY_RUN_WARNING):
            texts = [text for text in texts
                     if self.warning_task(text) == self.task_id]
        elif pairs:
            # DP-549. The pair's own line says what its runner warning says,
            # with its faces; saying it twice reads as two problems.
            texts = [text for text in texts
                     if not text.startswith('interface pair ')] + pairs
        return str(run.get('run_id') or ''), tuple(texts)

    def run_notes(self) -> tuple[str, ...]:
        """What the last run did that is worth saying and is not a problem.

        DP-567. 0924 rerun, G6: "interface pair 'tee_plug_contact':
        conformal, 36 faces matched" is the pair working, and it was printed
        in the warning colour among the warnings. Shown on the pages that show
        every warning, in the informational colour.
        """
        if (self.task_id != self.run_all_task_id
                and not self.SHOWS_EVERY_RUN_WARNING):
            return ()
        run = self.last_run() or {}
        lines = list(self.interface_pair_lines(run, applied=True))
        for value in (list(run.get('warnings') or ())
                      + list(run.get('runner_warnings') or ())):
            text = str(value).strip()
            if (text and self.is_information_note(text)
                    and text not in lines):
                lines.append(text)
        return tuple(lines)

    @staticmethod
    def is_information_note(text: str) -> bool:
        """True for a run message that tells and does not warn.

        Plan 40 UXB7. "The delaunay volume algorithm is single-threaded, so
        the requested 16 threads apply to surface meshing only" is what the
        run did, and nothing for the user to repair.
        """
        return 'volume algorithm is single-threaded' in text

    #: The pair states that mean the pair took effect (DP-548 grades them).
    APPLIED_PAIR_STATES = frozenset({'conformal'})

    @classmethod
    def interface_pair_lines(cls, run, applied=None) -> list[str]:
        """One line per authored interface pair: its faces, state and why.

        DP-549. G6 (audit 0924) saved ``tee_plug_contact`` and the Generate
        page said nothing about it, so a 36-face interface could not be told
        apart from an effective pair. The run now grades each pair (DP-548)
        and this is where the user reads the grade.

        ``applied`` keeps only the pairs that took effect (``True``) or only
        the ones that did not (``False``); ``None`` keeps both.
        """
        lines = []
        for row in (run or {}).get('interface_pairs') or ():
            if not isinstance(row, dict):
                continue
            if (applied is not None and applied
                    != (row.get('state') in cls.APPLIED_PAIR_STATES)):
                continue

            def side(key):
                # DP-566. By the face the user picked, and the name fusion
                # gave it only as a note: G6's slave body1_face4 was shown as
                # body0_face2, a pair that read as one face paired with itself.
                values = row.get(key) or {}
                meshed = [str(name) for name in values.get('names') or ()]
                authored = [str(name) for name in values.get('authored') or ()]
                text = f'{key} ' + (', '.join(authored or meshed) or 'nothing')
                if authored and meshed and authored != meshed:
                    text += f' (meshed as {", ".join(meshed)})'
                return text

            faces = int(row.get('matchedFaces') or 0)
            counted = f', {count_text(faces, "face")} matched' if faces else ''
            lines.append(
                f'interface pair {row.get("name")!r}: '
                f'{row.get("state") or "unknown"}{counted} '
                f'({side("master")}; {side("slave")}) — '
                f'{row.get("reason") or ""}'.rstrip(' —'))
        return lines

    def updateRunWarnings(self) -> None:
        run_id, texts = self.run_warnings()
        notes = self.run_notes() if hasattr(self, '_runNotes') else ()
        # UXB7 (DP-1352). The run id is a reference, not a warning: it leaves
        # the amber block for the tooltip and the information block.
        heading = (self.tr('Run %s:') % run_id if run_id
                   else self.tr('The last run:'))
        self._runWarnings.setText(
            self.tr('The last run reported:') + '\n'
            + '\n'.join(f'• {text}' for text in texts)
            if texts else '')
        self._runWarnings.setToolTip(
            self.tr('Run %s') % run_id if run_id else '')
        self._runWarnings.setVisible(bool(texts))
        # Plan 44 FX2. A folded read-out says what the run reported on its
        # one line, without the heading; it opens to the whole list.
        summarise(self._runWarnings,
                  self.tr('Last run: ') + ' · '.join(texts) if texts else '')
        if not hasattr(self, '_runNotes'):
            return
        # The information block carries the run id (UXB7), so the amber block
        # above it holds only what went wrong.
        self._runNotes.setText(
            (heading + '\n')
            + '\n'.join(f'• {text}' for text in notes) if notes else '')
        self._runNotes.setVisible(bool(notes))
        summarise(self._runNotes,
                  heading + ' ' + ' · '.join(notes) if notes else '')


def summarise(label, summary: str) -> None:
    """Give a folded read-out label (Plan 44 FX2) its one line."""
    setter = getattr(label, 'setSummary', None)
    if callable(setter):
        setter(summary)


def resync_body_spacer(page) -> int:
    """Point ``_bodySpacerIndex`` at the body's trailing spacer again.

    DP-2595 (Plan 44 FITG2). ``EngineTaskPage`` records the index of the
    spacer that closes the body once its sections are built; the Gmsh pages
    (``GmshTaskPage.__init__``) and the Gmsh Quality items (``after_init``)
    then insert the run-warning and run-note labels at 0 and 1, which moved
    the spacer two rows down and left the index two short. MEASURED at
    435d1ded: *More Gmsh options* was inserted at the stale index, two rows
    above the end of every Gmsh page (between Size at points and Gmsh size
    fields on Sizing, above Further quality limits on Quality), and
    ``claimBodyStretch`` would have set the stretch of the wrong row. The
    spacer is the last spacer item of the body layout, so it is found, not
    counted.
    """
    layout = getattr(page, '_body_layout', None)
    if layout is None or getattr(page, '_bodySpacerIndex', -1) < 0:
        return getattr(page, '_bodySpacerIndex', -1)
    for index in range(layout.count() - 1, -1, -1):
        if layout.itemAt(index).spacerItem() is not None:
            page._bodySpacerIndex = index
            break
    return page._bodySpacerIndex


class GmshOptionsTier:
    """Mixin: the Advanced tier holding the Gmsh options a page owns."""

    # -- the option catalogue, as an Advanced tier --------------------------- #

    def mountOptionsTier(self) -> None:
        """Plan 41 GN2. Every Gmsh option the catalogue says this page owns.

        Closed by default, so the guided form is as short as it was; the
        header says how many options the user has set. The Compute page
        carries the complete, searchable list (the Options dock of the plan).
        """
        from foammesh.core.gmsh.option_catalogue import (
            MOUNTED_PAGES, OPTIONS_PANEL_PAGE, options_for_page)
        from foammesh.view.gmsh_workflow.options_panel import GmshOptionsPanel

        task = self.task_id
        if task not in MOUNTED_PAGES:
            return
        complete = task == OPTIONS_PANEL_PAGE
        if not complete and not options_for_page(task):
            return
        title = (self.tr('Gmsh options') if complete
                 else self.tr('More Gmsh options'))
        self._optionsTitle = title
        self._optionsHeader = QPushButton(title, self)
        self._optionsHeader.setObjectName('gmshOptionsHeader')
        self._optionsHeader.setCheckable(True)
        as_link(self._optionsHeader)
        self._optionsHeader.setAccessibleDescription(
            self.tr('Show every Gmsh option on this page, with its '
                    'default and its manual text.'))
        self._options = self.adoptPanel(
            GmshOptionsPanel(self._client, None if complete else task, self))
        self._options.setVisible(False)
        self._optionsHeader.toggled.connect(self._options.setVisible)
        self._options.dirtyChanged.connect(self._refreshOptionsHeader)
        # DP-2595. Last on the page: just above the trailing spacer.
        index = resync_body_spacer(self)
        self._body_layout.insertWidget(index, self._optionsHeader, 0,
                                       Qt.AlignmentFlag.AlignLeft)
        self._body_layout.insertWidget(index + 1, self._options)
        self._bodySpacerIndex = index + 2
        self._refreshOptionsHeader()

    def _refreshOptionsHeader(self, *_args) -> None:
        header = getattr(self, '_optionsHeader', None)
        panel = getattr(self, '_options', None)
        if header is None or panel is None:
            return
        count = len(panel.effective())
        header.setText(self._optionsTitle if not count
                       else f'{self._optionsTitle}  ({count} set)')
