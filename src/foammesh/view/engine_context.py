"""The one place the view learns which engine is active (Plan 42 SG1).

Before this module the view read ``mesh/engine`` through seven different
routes (``configured_engine_id(app.db)``, ``checkout().getValue``, the field
client, ``mesh.engine.list``, ``mesh.engine.workflow``, run state and mesh
provenance) and nothing told a panel when it changed. This module gives:

* :class:`EngineContext` -- the engine, the provenance of the mesh on screen,
  the engine's capabilities and one change signal;
* an item registry using the existing ``applies_when`` clause grammar
  (:mod:`foammesh.core.facade.applicability`), with the two item kinds of
  decision D3: ``mesh`` items follow the mesh on screen, ``workflow`` items
  follow the chosen engine;
* :class:`EngineSection` and :class:`EngineSwap`, which show engine items
  only while their engine is active and hide a frame with nothing left in it;
* the zone names and the lists that the zone test and the static literal
  check read.

Nothing here changes what any page shows: a page that registers nothing is
untouched.
"""
from __future__ import annotations

import logging
import weakref

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

from foammesh.core.facade import applicability

logger = logging.getLogger(__name__)

UNSELECTED = 'unselected'
SNAPPY = 'snappy'
GMSH = 'gmsh'

ENGINE_FIELD = 'mesh.engine'
PROVENANCE_FIELD = 'mesh.shown'

#: Item kinds (decision D3).
KIND_MESH = 'mesh'
KIND_WORKFLOW = 'workflow'
KINDS = (KIND_MESH, KIND_WORKFLOW)

#: Zones. A registration with an engine clause in a ``pre_fork`` container is
#: an error (the zone test).
ZONE_SELECTION = 'selection'
ZONE_PRE_FORK = 'pre_fork'
ZONE_POST_FORK = 'post_fork'
ZONE_SURFACE = 'surface'
ZONES = (ZONE_SELECTION, ZONE_PRE_FORK, ZONE_POST_FORK, ZONE_SURFACE)

#: What the mesh on screen is.
PROVENANCE_NONE = 'none'
PROVENANCE_OPENFOAM = 'openfoam'
PROVENANCE_GMSH_NATIVE = 'gmsh-native'

#: The view files that may name an engine literal (``'gmsh'``/``'snappy'``)
#: in a comparison: the engine rows of the fork itself and the 8 legitimate
#: post-fork switches of the map (engine-context.md section 1). Every other
#: view file reads the engine through this module. The static check in
#: ``tests/unit/test_plan42_sg1_engine_context.py`` reads this tuple.
LEGITIMATE_SWITCH_FILES = (
    'view/engine_context.py',
    'view/main_window/meshing_method_branch.py',
    'view/main_window/engine_branch.py',
    'view/main_window/navigation_view.py',
    'view/main_window/step_manager.py',
    'view/main_window/main_window.py',
    'view/snappy_workflow/base.py',
    'view/gmsh_workflow/base.py',
    'view/gmsh_workflow/qa_page.py',
    'view/workflow_controls/task_page.py',
    'view/snappy_workflow/mesher_limits.py',
)

#: Files still comparing an engine literal that the map lists as violations.
#: Each is removed from this ratchet by the package that moves it (SG3, SG6,
#: SG7, SG11 ...). The check fails when a file NOT in either tuple gains one,
#: and when a file in this tuple no longer needs its entry.
PENDING_VIOLATION_FILES = ()


def _normalise(value) -> str:
    if value is None:
        return UNSELECTED
    inner = getattr(value, 'value', value)
    text = str(inner or UNSELECTED)
    if text.startswith('MeshEngine.'):
        text = text.split('.', 1)[1]
    return text.lower()


#: The clauses a view file asks of an engine it holds in hand (a radio the
#: user is pointing at, a page's remembered engine). The same grammar the
#: registry judges, so no view file spells an engine literal itself.
WHEN_GMSH = ('mesh.engine == gmsh',)
WHEN_SNAPPY = ('mesh.engine == snappy',)
WHEN_NOT_GMSH = ('mesh.engine != gmsh',)
WHEN_NOT_SNAPPY = ('mesh.engine != snappy',)


def engine_holds(engine, *when) -> bool:
    """Whether ``engine`` satisfies every clause in ``when``.

    For the places that judge an engine the context does not own (the radio
    being pointed at on the selection page, a page's last-seen engine).
    """
    return bool(applicability.evaluate(
        when, {ENGINE_FIELD: _normalise(engine)}).applies)


def engine_from_listing(payload):
    """The engine a ``mesh.engine.list`` answer says is configured.

    The listing is read for its catalogue, its target solver and its QA
    route; the one key of it that names the configured engine is read here
    and nowhere else in the view.
    """
    try:
        return (payload or {}).get('current_engine')
    except AttributeError:
        return None


def read_engine(db) -> str:
    """The configured engine id read from a db/checkout, never raising."""
    try:
        from foammesh.core.engine.registry import configured_engine_id
        return configured_engine_id(db)
    except Exception:                                        # noqa: BLE001
        return UNSELECTED


class EngineContext(QObject):
    """The engine, the mesh on screen and what they allow, with one signal."""

    engineChanged = Signal(str)
    provenanceChanged = Signal(str)

    def __init__(self, db=None, *, provenance=None, db_source=None,
                 parent=None):
        super().__init__(parent)
        self._db = db
        #: ``() -> db or None``. WALK-2 (2026-10-11, measured live): the
        #: application's context was made before a case was open and held
        #: ``app.db`` as it was then (``None``); ``app.db`` is a fresh
        #: snapshot per read, so a held one never sees the engine move. With
        #: a source, every refresh reads the case that is open now.
        self._dbSource = db_source
        self._provenanceSource = provenance
        current = self._currentDb()
        self._engine = (read_engine(current) if current is not None
                        else UNSELECTED)
        self._provenance = self._readProvenance()
        self._items: list[_Item] = []

    # -- facts ---------------------------------------------------------
    @property
    def engine(self) -> str:
        return self._engine

    @property
    def provenance(self) -> str:
        return self._provenance

    @property
    def capabilities(self) -> dict:
        """The engine registry attributes the view asks about."""
        try:
            from foammesh.core.engine.registry import ENGINE_REGISTRY
            engine = ENGINE_REGISTRY.get(self._engine)
        except Exception:                                    # noqa: BLE001
            return {}
        return {name: getattr(engine, name)
                for name in ('mixes_cad_and_surfaces',
                             'builds_non_conformal_interfaces',
                             'non_conformal_route',
                             'needs_generated_dictionaries')
                if hasattr(engine, name)}

    def values(self) -> dict:
        """The mapping the clause grammar judges against."""
        return {ENGINE_FIELD: self._engine, PROVENANCE_FIELD: self._provenance}

    def setDb(self, db) -> None:
        self._db = db
        self._dbSource = None
        self.refresh()

    def _currentDb(self):
        if self._dbSource is not None:
            try:
                return self._dbSource()
            except Exception:                                # noqa: BLE001
                return None
        return self._db

    def setProvenanceSource(self, source) -> None:
        """``source()`` returns the provenance token of the mesh on screen."""
        self._provenanceSource = source
        self.refresh()

    # -- change --------------------------------------------------------
    def refresh(self) -> None:
        """Reread both facts; emit only on change, then re-judge the items."""
        if self._dbSource is not None:
            current = self._currentDb()
            engine = (read_engine(current) if current is not None
                      else UNSELECTED)
        else:
            engine = (read_engine(self._db) if self._db is not None
                      else self._engine)
        provenance = self._readProvenance()
        engineMoved = engine != self._engine
        provenanceMoved = provenance != self._provenance
        self._engine, self._provenance = engine, provenance
        if engineMoved:
            self.engineChanged.emit(engine)
        if provenanceMoved:
            self.provenanceChanged.emit(provenance)
        if engineMoved or provenanceMoved:
            self.applyItems()

    def refreshProvenance(self) -> None:
        """Reread the provenance of the mesh on screen only.

        Plan 39 VI14d. ``refresh`` also reads the configured engine out of
        the project database (a checkout, 10 ms on the GUI thread, three
        times for one section raise). The engine moves on the project
        events this context subscribes to; a cell count changing says
        nothing about it.
        """
        provenance = self._readProvenance()
        if provenance == self._provenance:
            return
        self._provenance = provenance
        self.provenanceChanged.emit(provenance)
        self.applyItems()

    def _readProvenance(self) -> str:
        source = self._provenanceSource
        if source is None:
            return PROVENANCE_NONE
        try:
            return str(source() or PROVENANCE_NONE)
        except Exception:                                    # noqa: BLE001
            return PROVENANCE_NONE

    def subscribeTo(self, events) -> None:
        """Reread on the project events (the engine_branch pattern)."""
        subscribe = getattr(events, 'subscribe', None)
        if not callable(subscribe):
            return
        this = weakref.ref(self)

        def changed(**_payload):
            context = this()
            if context is not None:
                context.refresh()

        try:
            from foammesh.core.project import Event
            for name in ('TRANSACTION_APPLIED', 'UNDONE', 'REDONE',
                         'PROJECT_OPENED'):
                subscribe(getattr(Event, name), changed)
        except Exception:                                    # noqa: BLE001
            logger.exception('engine context could not subscribe')

    # -- registry ------------------------------------------------------
    def register(self, widget, *, when=(), kind=KIND_WORKFLOW,
                 zone=ZONE_SURFACE) -> None:
        """Show ``widget`` only while every clause in ``when`` holds.

        ``kind`` picks what the clause reads: ``workflow`` items judge the
        configured engine as ``mesh.engine``; ``mesh`` items judge the
        provenance of the mesh on screen, spelled in the clause as
        ``mesh.shown == gmsh-native``.
        """
        if kind not in KINDS:
            raise ValueError(f'item kind must be one of {KINDS}: {kind!r}')
        if zone not in ZONES:
            raise ValueError(f'zone must be one of {ZONES}: {zone!r}')
        if zone == ZONE_PRE_FORK and when:
            raise ValueError(
                'a pre-fork container may not carry an engine clause '
                '(Plan 42 section 1)')
        item = _Item(weakref.ref(widget), tuple(when), kind, zone)
        self._items.append(item)
        item.apply(self.values())

    def items(self) -> tuple:
        return tuple(self._items)

    def applyItems(self) -> None:
        values = self.values()
        live = []
        for item in self._items:
            if item.apply(values):
                live.append(item)
        self._items = live


class _Item:
    __slots__ = ('ref', 'when', 'kind', 'zone')

    def __init__(self, ref, when, kind, zone):
        self.ref, self.when, self.kind, self.zone = ref, when, kind, zone

    def apply(self, values) -> bool:
        widget = self.ref()
        if widget is None:
            return False
        try:
            visible = applicability.evaluate(self.when, values).applies
            widget.setVisible(bool(visible))
        except RuntimeError:                                  # C++ gone
            return False
        return True


class EngineSection(QFrame):
    """A framed group that hides itself when nothing in it can show.

    It hides when its own clause fails or when every child item is hidden,
    so no empty frame is left behind.
    """

    def __init__(self, title='', *, when=(), parent=None):
        super().__init__(parent)
        self.setObjectName('engineSection')
        self._title = title
        self._when = tuple(when)
        self._children: list[QWidget] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)

    def title(self) -> str:
        return self._title

    def addItem(self, widget, *, when=()) -> None:
        self._layout.addWidget(widget)
        self._children.append(widget)
        widget._engineWhen = tuple(when)

    def apply(self, values) -> bool:
        """Judge the section and its items; return whether it shows."""
        own = applicability.evaluate(self._when, values).applies
        shown = 0
        for child in self._children:
            visible = own and applicability.evaluate(
                getattr(child, '_engineWhen', ()), values).applies
            child.setVisible(visible)
            shown += bool(visible)
        showing = own and shown > 0
        self.setVisible(showing)
        return showing

    def attach(self, context: EngineContext) -> None:
        context.engineChanged.connect(lambda *_: self.apply(context.values()))
        context.provenanceChanged.connect(
            lambda *_: self.apply(context.values()))
        self.apply(context.values())


class EngineSwap:
    """Two groups of widgets on one row: one shows per engine.

    ``EngineSwap(row, snappy=[...], gmsh=[...])`` -- the Compare check boxes,
    the run-panel charts versus bar and facts. Widgets not named for the
    active engine are hidden, not disabled.
    """

    def __init__(self, row=None, **groups):
        self.row = row
        self.groups = {name: list(widgets) for name, widgets in groups.items()}

    def apply(self, engine: str) -> None:
        for name, widgets in self.groups.items():
            for widget in widgets:
                widget.setVisible(name == engine)

    def attach(self, context: EngineContext) -> None:
        context.engineChanged.connect(self.apply)
        self.apply(context.engine)


def engine_context(app=None) -> EngineContext:
    """The application's context, created on first use."""
    if app is None:
        from foammesh.app import app as shared
        app = shared
    context = getattr(app, 'engineContext', None)
    if context is None:
        holder = app
        context = EngineContext(
            db_source=lambda: getattr(holder, 'db', None))
        try:
            context.subscribeTo(getattr(app, 'events', None))
            app.engineContext = context
        except Exception:                                    # noqa: BLE001
            pass
    return context


def mesh_provenance(manager) -> str:
    """What the mesh the viewport shows is: a Gmsh ``.msh``, a case or none."""
    if manager is None:
        return PROVENANCE_NONE
    try:
        if manager.nativePath() is not None:
            return PROVENANCE_GMSH_NATIVE
        if manager.dataSets():
            return PROVENANCE_OPENFOAM
    except Exception:                                        # noqa: BLE001
        pass
    return PROVENANCE_NONE


def wire_mesh_provenance(app, manager) -> EngineContext:
    """Make ``MeshManager`` the provenance source of ``app.engineContext``.

    The window calls :func:`refresh_engine_context` from its cell-count
    slot, so the context rereads when the mesh on screen changes and emits
    only if the provenance token moved.
    """
    context = engine_context(app)
    context.setProvenanceSource(lambda: mesh_provenance(manager))
    return context


def refresh_engine_context(app=None) -> None:
    """Reread the engine and the provenance; never raises."""
    try:
        engine_context(app).refresh()
    except Exception:                                        # noqa: BLE001
        logger.debug('engine context refresh failed', exc_info=True)


def refresh_mesh_provenance(app=None) -> None:
    """Reread only the provenance of the mesh on screen; never raises."""
    try:
        engine_context(app).refreshProvenance()
    except Exception:                                        # noqa: BLE001
        logger.debug('engine context refresh failed', exc_info=True)


def configured_engine(app=None) -> str:
    """The configured engine id, read through the context's own db."""
    try:
        if app is None:
            from foammesh.app import app as shared
            app = shared
        return read_engine(app.db)
    except Exception:                                        # noqa: BLE001
        return UNSELECTED
