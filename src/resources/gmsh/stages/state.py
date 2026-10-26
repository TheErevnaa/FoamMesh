"""The state every stage reads and writes (Plan 48 RR, DP-4009).

One run, whether the batch runner or the resident session, holds the same
attributes: the job, the ledger, the scope maps, the identity tables."""
from __future__ import annotations

from types import MappingProxyType

from .common import (Ledger)


class StageBase:
    #: DP-2102. Also set in `__init__`; the class default keeps a run that a
    #: test (or a resumed job) builds without `__init__` readable. Plan 42
    #: SG22 added the reads without it, which failed 31 older tests.
    nonconformal_unfused = False
    #: DP-6064. The same, for the role-sheet state: a run built without
    #: `__init__` reaches the layer and import stages with no sheets.
    role_sheet_imports = frozenset()
    role_sheet_tolerance = 0.0
    embedded_role_sheets = MappingProxyType({})

    def init_stage_state(self, job: dict, reporter):
        self.job = job
        self.intent = job['intent']
        #: FC-E. The dimension this job asked for, read before anything is
        #: imported because it changes what an import *means*: a section is a
        #: face and has no volume, so the refusal that protects a
        #: three-dimensional run has to ask which run this is rather than fire
        #: on both. `planar` covers two_d and axisymmetric alike -- both mesh
        #: a section here and are extruded one cell thick by the publisher,
        #: and they differ only in how that one cell is swept.
        self.dimensionality = dict(self.intent.get('dimensionality') or {})
        self.planar = bool(self.dimensionality.get('planar'))
        self.generate_dimension = int(
            self.dimensionality.get('generateDimension') or 3)
        #: DP-133. Where to leave the surface pass of a three-dimensional
        #: run, or `''` for a job that did not ask for one. Absent in a job
        #: written before this existed, which is why it is read with a
        #: default rather than indexed.
        self.surface_output = str(job.get('surfaceOutput') or '')
        #: FC-E. Curves bounding the section, for a planar job. The
        #: three-dimensional equivalent is `boundary_surfaces`.
        self.boundary_curves: list[int] = []
        self.reporter = reporter
        self.ledger = Ledger()
        self.statistics: dict = {}
        self.warnings: list[str] = []
        self.gmsh = None
        #: Scope token -> Gmsh surface tags, resolved after import.
        self.scope_surfaces: dict[str, list[int]] = {}
        #: Scope token -> Gmsh volume tags, likewise.
        self.scope_volumes: dict[str, list[int]] = {}
        self.unresolved_scopes: list[str] = []
        #: Plan 28 WP7. Tessellated import only: the surface tags in import
        #: order before classification (what `surfaceNames` and
        #: `scopeSurfaces` are keyed on), and classified tag -> origin tag.
        self.import_surface_tags: list[int] = []
        self.surface_origin: dict[int, int] = {}
        #: Plan 30 WP12. Surface tag -> patch name for faces this runner
        #: created, which the prepared geometry cannot have named.
        self.generated_names: dict[int, str] = {}
        #: Plan 30 WP12. Whether the geometry arrived as triangles. A discrete
        #: surface has no CAD corners and no OCC solid, so transfinite
        #: surfaces and boolean farfield boxes are refused with a reason
        #: rather than attempted and half-applied.
        self.tessellated = False
        #: WP-01 F-35, extended by C31-04. One receipt per entry of
        #: ``job['geometry']``, in the order the job listed them, published as
        #: ``statistics['import']['sources']`` and folded verbatim into the
        #: run manifest. Each receipt is::
        #:
        #:     {'schema_version': int,  # `entitySchemaVersion` of the job
        #:      'path':     str,   # the file as the runner opened it
        #:      'source_id': str,  # the prepared source this file is
        #:      'import_stage': str,  # 'cad' or 'tessellated'
        #:      'surfaces': int,   # model surfaces this file added
        #:      'volumes':  int,   # model volumes this file added
        #:      'shells':   int,   # closed shells it contributed: solids for
        #:                         # CAD, one per `solid` block for a
        #:                         # tessellated file
        #:      'entities': [{'entity_id': str, 'kind': str, 'dim': int,
        #:                    'tags': [int]}],  # identity -> what Gmsh made
        #:      'mapping_confidence': float,   # what the prepared revision
        #:                                     # claimed for this source
        #:      'mapping_status': str}         # 'exact' | 'partial' |
        #:                                     # 'unmapped'
        #:
        #: Without the counts a job listing several sources could not be
        #: audited: the manifest recorded one total and no way to see which
        #: file contributed nothing. Without the ``entities`` mapping a scope
        #: could not be resolved to the right file's entities at all -- the
        #: indices two single-body CAD sources carry are both zero.
        self.import_receipts: list[dict] = []
        #: C31-04. Entity ID -> ``{'kind', 'dim', 'tags'}``: the identity the
        #: prepared revision authored against, against the tags Gmsh actually
        #: made for it *now*. Retargeted by :meth:`retarget_entities` after
        #: every topology change, so a scope resolves through the change
        #: rather than through the numbering the import happened to have.
        self.entity_tags: dict[str, dict] = {}
        #: C31-04. Gmsh tag -> the prepared name of the entity that tag now
        #: carries, resolved through the identity map. Empty for a job that
        #: carries no entity names, which leaves the version-1 lookup by tag.
        self.entity_surface_names: dict[int, str] = {}
        self.entity_volume_names: dict[int, str] = {}
        #: DP-399. Prepared surface names that no surface in the model
        #: carries, filled by `describe_surfaces` once the model has stopped
        #: changing. A control scoped to one of these reaches nothing.
        self.names_without_surface: set[str] = set()
        #: How many faces the duplicate fusion took away, which is the one
        #: cause of the above this runner can name.
        self.duplicate_faces_fused = 0
        #: Plan 42 SG22 Q2. True when duplicate-face fusion was skipped
        #: because a non-conformal pair asked for its two bodies to be
        #: meshed apart.
        self.nonconformal_unfused = False
        #: DP-410. ``{dropped tag: surviving tag}`` for every coincident face
        #: the fusion consumed, matched by centre of mass and area. The pair
        #: is one face now and the identity that pointed at the dropped tag
        #: has to point at the one that replaced it.
        self.duplicate_face_successors: dict[int, int] = {}
        self.duplicate_volume_successors: dict[int, int] = {}
        #: DP-410. ``{tag: [name, ...]}`` -- the further prepared names a
        #: fused surface carries beyond the one it publishes under. A face
        #: can belong to one patch, so the extras name the same faces rather
        #: than naming nothing.
        self.merged_surface_names: dict[int, list[str]] = {}
        #: DP-2876. ``{surface: (first owner, second owner)}`` for each face
        #: two imported bodies share, made one surface before classification
        #: (:func:`separate_shared_faces`), and ``{classified piece: second
        #: owner}`` for what those surfaces were cut into. The piece belongs
        #: to both bodies' volumes.
        self.shared_faces: dict[int, tuple[int, int]] = {}
        self.shared_pieces: dict[int, int] = {}
        #: DP-6056. ``{imported face: (first body, second body)}`` for a face
        #: two bodies share that arrived in a file of its own
        #: (:func:`find_interface_sheets`); its pieces join ``shared_pieces``.
        self.interface_sheets: dict[int, tuple[int, int]] = {}
        #: C31-04. One record per topology change that moved entities:
        #: ``{'stage', 'entities': {entity_id: {'from': [...], 'to': [...]}}}``.
        #: Published as ``statistics['import']['topologyChanges']``.
        self.topology_changes: list[dict] = []
        #: R118. A surface that grew no boundary layer is deleted and rebuilt
        #: from the extrusion's inner rim, so this maps the original tag onto
        #: the faces that took its place and must inherit its patch name.
        self.layer_replacements: dict[int, list[int]] = {}
        #: R118. The same, one dimension up. The volume a layer is carved
        #: out of is deleted and replaced by the layer volumes and a rebuilt
        #: core, and all of them are still the region the user named. Maps
        #: the original tag onto the tags that took its place.
        self.volume_replacements: dict[int, list[int]] = {}
        #: Size fields awaiting combination into one background field. Size
        #: fields and per-volume sizes must share it, or whichever was set
        #: last would silently replace the other.
        self.background_fields: list[int] = []
        #: Plan 41 GN6. Metric fields (BAMG, MMG3D) and the layer field.
        self.anisotropic_fields: list[int] = []
        #: DP-504. ``(row name, local size)`` for each background source, so
        #: the combination can tell whether anything asked for a size the
        #: boundary could outvote. ``None`` is a size the runner cannot read
        #: off the row (a MathEval expression).
        self.background_sizes: list[tuple[str, float | None]] = []
        #: Every curve a transfinite control put a node count on, and what it
        #: asked for. Read back after meshing, because Gmsh answers a
        #: structured request it cannot honour by meshing unstructured
        #: without a word (see :meth:`structured_surface_refusal`).
        self.transfinite_curves: dict[int, dict] = {}
        #: Surfaces and volumes asked to be structured, by tag.
        self.transfinite_surfaces: dict[int, dict] = {}
        self.transfinite_volumes: dict[int, dict] = {}
        #: Plan 31 FC-C. The volumes ``setTransfiniteAutomatic`` was pointed
        #: at, kept so the finished mesh can be read back per volume. The call
        #: succeeds per volume and says nothing about which ones it took, so
        #: the only honest answer comes from the element families afterwards.
        self.automatic_structuring: dict | None = None
        #: Plan 31 FC-C. Whether barycentric refinement actually reached the
        #: option, so the finished mesh can be counted as the answer to it.
        self.barycentric_refinement = False
        #: What the boundary-layer extrusion made, kept so the finished mesh
        #: can be read back against it: the wall surfaces the stacks stand on,
        #: the volume(s) the layer occupies, and the sides of the stack --
        #: which is where the quad option shows up (CP-08 item 6).
        #: The pairs ``setPeriodic`` accepted, kept so the finished mesh can
        #: be read back against them: a pair is only a coupling once the two
        #: surfaces carry corresponding nodes and faces (CP-08 item 5).
        self.periodic_applied: list[dict] = []
        # DP-6054. Pairs whose sides the layer rebuilt, copied after the
        # surface pass, and the node map each copy leaves for the measure.
        self.periodic_copies: list[dict] = []
        self.periodic_node_maps: dict[int, tuple] = {}
        # DP-6066. The lateral strips of such a pair, declared to Gmsh only
        # after the volume pass (``declare_periodic_strips``).
        self.periodic_strips_pending: list[tuple] = []
        # DP-6064. The internal-face and baffle sheets of an STL import, by
        # import tag, set aside from the shells and imprinted on the walls;
        # the tolerance that was done at; and ``{volume: pieces}`` embedded.
        self.role_sheet_imports: set = set()
        self.role_sheet_tolerance: float = 0.0
        self.embedded_role_sheets: dict = {}
        self.layer_bases: list[int] = []
        self.layer_volumes: list[int] = []
        self.layer_laterals: list[int] = []
        #: DP-74. Each wall surface paired with the inner surface its stack
        #: reaches, which is what the fit check reads once both are meshed.
        self.layer_columns: list[tuple[int, int]] = []
        #: DP-5718. Set when a one-cell-thick case grows its layer in the
        #: plane (front/back faces, walls, fields); None otherwise.
        self.layers_in_plane: dict | None = None
        #: DP-75. True when this run meshed the imported facets instead of a
        #: parametrised remesh of them. Set by :meth:`import_tessellated`.
        self.kept_tessellation = False
        #: DP-55. Surfaces that bound a void inside the volume being meshed.
        #: Their triangles are wound out of the void, which points into the
        #: fluid - the opposite of an outer shell - so a layer grown with the
        #: outer shell's sign lands in the hole instead of in the flow.
        self.void_surfaces: set[int] = set()
        #: DP-55. Surface tag -> +1 when its shell is wound outward, -1 when
        #: the shell is inverted, read off the signed enclosed volume the
        #: shell topology already computes.
        self.surface_winding: dict[int, int] = {}
        # DP-457. Which closed body each surface bounds, kept so that a
        # coincidence can be asked whether it spans two of them.
        self.surface_shell: dict[int, str] = {}

    # -- option helpers ---------------------------------------------------- #

    def set_number(self, name, value, *, control=None):
        self.gmsh.option.setNumber(name, float(value))
        effective = self.gmsh.option.getNumber(name)
        self.ledger.record(control or name, value, effective)
        return effective

    def set_string(self, name, value, *, control=None):
        self.gmsh.option.setString(name, str(value))
        effective = self.gmsh.option.getString(name)
        self.ledger.record(control or name, value, effective)
        return effective
