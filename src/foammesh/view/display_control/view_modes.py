"""What the viewport is for, right now.

Plan 31 CP-09 item 5: "offer Geometry, Boundary mesh, Volume mesh, Slice and
Quality modes; selecting a control's scope highlights the corresponding
entities."

MEASURED before this: there was no mode concept anywhere in
``view/display_control``. The only thing resembling one was the per-actor
right-click menu (Wireframe / Surface / Surface with Edges), which changes one
prop at a time. Getting from "look at my imported geometry" to "look at the
boundary mesh on it" meant hiding the internal volume by hand, then remembering
to unhide it; getting to a slice meant opening the section panel and aiming a
plane; getting to quality meant a different dock entirely. Five questions
people ask constantly, and five different manual routes, none of which is
reversible by pressing the same control again.

A mode is a *plan*, computed here with no Qt and no VTK: which actors are
visible, how they are drawn, whether the section plane is on and whether the
quality colouring is on. The window applies it. Keeping the decision here is
what lets it be measured -- and what stops the five modes drifting into five
hand-written branches in ``MainWindow``.

Nothing here hides a mode that has nothing to show: a mode with no entities
stays selectable and reports *why* it is empty, because a control that
disappears when it would be useful is the harder thing to debug.
"""
from __future__ import annotations

from dataclasses import dataclass, field


#: Mode ids. Strings rather than an enum so a saved view state, a test and a
#: combo box can all name a mode without importing this module.
GEOMETRY = 'geometry'
BOUNDARY = 'boundary'
VOLUME = 'volume'
SLICE = 'slice'
QUALITY = 'quality'

#: Display modes, matching :class:`foammesh.rendering.actor_info.DisplayMode`
#: by name without importing it -- this module stays free of the render stack.
SURFACE = 'surface'
SURFACE_EDGE = 'surface_edge'
WIREFRAME = 'wireframe'


@dataclass(frozen=True)
class Mode:
    id: str
    label: str
    question: str


#: In workflow order: what you imported, what the mesher put on its surface,
#: what it filled the inside with, what that looks like cut open, and how good
#: it is.
MODES: tuple[Mode, ...] = (
    Mode(GEOMETRY, 'Geometry', 'What did I give the mesher?'),
    Mode(BOUNDARY, 'Boundary mesh', 'What did it put on the surface?'),
    Mode(VOLUME, 'Volume mesh', 'What did it fill the inside with?'),
    Mode(SLICE, 'Slice', 'What does the inside look like cut open?'),
    Mode(QUALITY, 'Quality', 'Where are the bad cells?'),
)

MODE_IDS: tuple[str, ...] = tuple(mode.id for mode in MODES)


def mode(mode_id: str) -> Mode:
    for entry in MODES:
        if entry.id == mode_id:
            return entry
    raise KeyError(mode_id)


@dataclass(frozen=True)
class ViewPlan:
    """Everything the window has to do to enter a mode."""

    mode: str
    visible: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()
    display_mode: str = SURFACE
    section: bool = False
    quality: bool = False
    #: Empty when the mode has something to show; otherwise the sentence
    #: explaining what is missing.
    unavailable: str = ''
    #: What this mode is showing, for the overlay line.
    summary: str = ''

    def is_available(self) -> bool:
        return not self.unavailable


@dataclass(frozen=True)
class Scene:
    """The entities a plan can be made from.

    Deliberately plain lists of actor ids: the mesh publishes exactly these
    (``MeshManager.patchIds``/``zoneIds``/``regions``) and the geometry
    manager publishes its own, so this module needs neither.
    """

    geometry: tuple[str, ...] = ()
    patches: tuple[str, ...] = ()
    volume: tuple[str, ...] = ()
    zones: tuple[str, ...] = ()

    def mesh_parts(self) -> tuple[str, ...]:
        return tuple(self.patches) + tuple(self.volume) + tuple(self.zones)

    def everything(self) -> tuple[str, ...]:
        return tuple(self.geometry) + self.mesh_parts()


def _split(scene: Scene, wanted) -> tuple[tuple[str, ...], tuple[str, ...]]:
    wanted = tuple(wanted)
    keep = set(wanted)
    hidden = tuple(key for key in scene.everything() if key not in keep)
    return wanted, hidden


_NO_GEOMETRY = ('No geometry is loaded, so there is nothing to compare the '
                'mesh against. Import a surface or a CAD file first.')
_NO_MESH = ('No mesh has been generated yet, so there are no cells to show. '
            'Run the meshing workflow first.')
_NO_PATCHES = ('This mesh has no named boundary patches, so there is no '
               'boundary mesh to show separately from the volume.')
_NO_VOLUME = ('This result has no internal volume -- it is a surface mesh, '
              'so there is nothing to cut open or fill.')


def plan(mode_id: str, scene: Scene) -> ViewPlan:
    """The plan for entering *mode_id* over *scene*.

    An unavailable mode still returns a plan naming what is missing; the
    window shows that sentence rather than doing nothing to the picture.
    """
    if mode_id not in MODE_IDS:
        raise KeyError(mode_id)

    if mode_id == GEOMETRY:
        if not scene.geometry:
            return ViewPlan(mode_id, unavailable=_NO_GEOMETRY)
        visible, hidden = _split(scene, scene.geometry)
        return ViewPlan(mode_id, visible, hidden, SURFACE_EDGE,
                        summary=_summary('geometry part', len(visible)))

    if mode_id == BOUNDARY:
        if not scene.mesh_parts():
            return ViewPlan(mode_id, unavailable=_NO_MESH)
        if not scene.patches:
            return ViewPlan(mode_id, unavailable=_NO_PATCHES)
        visible, hidden = _split(scene, scene.patches)
        return ViewPlan(mode_id, visible, hidden, SURFACE_EDGE,
                        summary=_summary('boundary patch', len(visible),
                                         'boundary patches'))

    if mode_id == VOLUME:
        if not scene.mesh_parts():
            return ViewPlan(mode_id, unavailable=_NO_MESH)
        if not scene.volume:
            return ViewPlan(mode_id, unavailable=_NO_VOLUME)
        wanted = tuple(scene.volume) + tuple(scene.zones)
        visible, hidden = _split(scene, wanted)
        return ViewPlan(mode_id, visible, hidden, SURFACE_EDGE,
                        summary=_summary('volume part', len(visible)))

    if mode_id == SLICE:
        if not scene.mesh_parts():
            return ViewPlan(mode_id, unavailable=_NO_MESH)
        if not scene.volume:
            return ViewPlan(mode_id, unavailable=_NO_VOLUME)
        wanted = tuple(scene.volume) + tuple(scene.zones)
        visible, hidden = _split(scene, wanted)
        return ViewPlan(mode_id, visible, hidden, SURFACE_EDGE, section=True,
                        summary='the cut plane through the volume; anything '
                                'measured here describes the section')

    # QUALITY
    if not scene.mesh_parts():
        return ViewPlan(mode_id, unavailable=_NO_MESH)
    if not scene.volume:
        return ViewPlan(mode_id, unavailable=_NO_VOLUME)
    wanted = tuple(scene.volume) + tuple(scene.zones)
    visible, hidden = _split(scene, wanted)
    return ViewPlan(mode_id, visible, hidden, SURFACE, quality=True,
                    summary='the volume coloured by cell quality')


def _summary(singular: str, count: int, plural: str = '') -> str:
    plural = plural or singular + 's'
    return f'{count:,} {singular if count == 1 else plural}'


# --------------------------------------------------------------------------- #
# Scope highlighting
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ScopeMatch:
    """Which entities a control's scope names, and what could not be found."""

    found: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    #: Regions are named in a scope but are not actors; their members are.
    via_region: tuple[str, ...] = field(default=())

    def describe(self) -> str:
        if not self.found and not self.missing:
            return 'This control has no scope, so it applies to everything.'
        parts = []
        if self.found:
            parts.append(f'{len(self.found)} highlighted')
        if self.missing:
            parts.append(
                f"not in this mesh: {', '.join(self.missing)}")
        return '; '.join(parts) + '.'


def resolve_scope(names, *, patches=(), zones=(), regions=None) -> ScopeMatch:
    """Turn the names a control is scoped to into actor ids to highlight.

    A scope is written in the user's vocabulary -- patch names, zone names, a
    region name -- and the scene is keyed by actor id, which for a
    multi-region case is ``region:name``. Matching on the bare name as well as
    the full id is what makes "inlet" find "air:inlet" without asking the user
    to know the prefix.

    Names that match nothing are reported rather than dropped: a control
    silently scoped to nothing is the failure mode CP-03 exists to stop, and
    the viewport should not hide its view of it.
    """
    regions = dict(regions or {})
    known: dict[str, list[str]] = {}
    for actor_id in list(patches) + list(zones):
        known.setdefault(actor_id, []).append(actor_id)
        bare = actor_id.split(':')[-1]
        if bare != actor_id:
            known.setdefault(bare, []).append(actor_id)

    found: list[str] = []
    missing: list[str] = []
    via_region: list[str] = []
    for name in names:
        name = str(name)
        members = regions.get(name)
        if members:
            via_region.append(name)
            for member in members:
                if member not in found:
                    found.append(member)
            continue
        matches = known.get(name)
        if not matches:
            missing.append(name)
            continue
        for match in matches:
            if match not in found:
                found.append(match)
    return ScopeMatch(tuple(found), tuple(missing), tuple(via_region))


# --------------------------------------------------------------------------- #
# Layer coverage
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class LayerRow:
    """One patch's achieved prism layers, as the log reported them."""

    patch: str
    layers: float = 0.0
    requested: int = 0
    coverage_pct: float = 0.0
    frozen: bool = False

    @property
    def ok(self) -> bool:
        """Frozen patches asked for nothing, so nothing is the right answer."""
        if self.frozen:
            return True
        if not self.requested:
            return self.layers > 0
        return self.layers >= self.requested - 1e-9

    def describe(self) -> str:
        if self.frozen:
            return f'{self.patch}: none, by request'
        if self.requested:
            return (f'{self.patch}: {self.layers:.3g} of {self.requested} '
                    f'layers ({self.coverage_pct:.0f}% covered)')
        return f'{self.patch}: {self.layers:.3g} layers'


_NO_LAYER_RUN = ('No boundary-layer run has been measured on this case, so '
                 'there is no coverage to show. Run the layer stage first.')


@dataclass(frozen=True)
class LayerCoverage:
    """What the layer stage achieved, and which patches to light up.

    ``measured`` is kept separate from "no shortfalls": snappyHexMesh exits
    successfully when every layer was rejected, so "nobody measured" and
    "everything is fine" are different facts and were being reported the same
    way -- as silence.
    """

    measured: bool = False
    rows: tuple[LayerRow, ...] = ()

    @property
    def short(self) -> tuple[str, ...]:
        return tuple(row.patch for row in self.rows if not row.ok)

    @property
    def frozen(self) -> tuple[str, ...]:
        return tuple(row.patch for row in self.rows if row.frozen)

    def describe(self) -> str:
        if not self.measured:
            return _NO_LAYER_RUN
        if not self.rows:
            return _NO_LAYER_RUN
        short = [row for row in self.rows if not row.ok]
        if not short:
            frozen = len(self.frozen)
            tail = (f' ({frozen} asked for none)' if frozen else '')
            return (f'All {len(self.rows) - frozen} patches that asked for '
                    f'layers got them{tail}.')
        listed = '; '.join(row.describe() for row in short)
        return (f'{len(short)} of {len(self.rows)} patches fell short and are '
                f'highlighted -- {listed}.')


def summarise_layers(document) -> LayerCoverage:
    """Read the stored layer-coverage document into rows worth highlighting.

    Tolerant of a document that is missing, empty or half-written, because
    the alternative is a viewport action that raises on a case whose layer
    stage was interrupted.
    """
    document = document if isinstance(document, dict) else {}
    if not document.get('measured'):
        return LayerCoverage(False)
    rows = []
    for entry in document.get('patches') or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get('patch') or '').strip()
        if not name:
            continue
        rows.append(LayerRow(
            name, _number(entry.get('layers')),
            int(_number(entry.get('requested_layers'))),
            _number(entry.get('coverage_pct')),
            bool(entry.get('frozen'))))
    return LayerCoverage(True, tuple(rows))


def _number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
