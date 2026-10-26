"""The names and scopes follow the window's geometry build (DP-5716).

The import resolves every prepared identity to a Gmsh tag and names it
(``ScopeStage.resolve_scopes``). A geometry build made after it -- the
``geometry`` hook of the batch run, a ``session.build`` in the resident
session -- can delete those entities and make new ones, and OpenCASCADE gives
the new ones the freed numbers. MEASURED on the kept pipe (three walls
``pipe_wall1..3``): delete the volume, a rectangle, ``case_2d`` on it; the
slab's faces 1-3 were then named ``pipe_wall1..3`` from the import, the
layers asked for on those walls grew on the slab and the mesh was refused
(520 elements at the hard floor of gamma, 9,858 layer cells).

So a build is followed the way healing and the duplicate fusion are: every
surface and volume is fingerprinted (bounding box and size) before it and
after it. An entity whose fingerprint is unchanged keeps its identity and
name; one that moved to another tag with the same fingerprint follows it; one
with none is gone, and its name with it. A new entity takes its name from
the physical group the build made over it (``front``/``back``/``sides``,
``fluid``), else the generated ``face_<tag>``. Nothing is ever named by a tag
the model reused.
"""
from __future__ import annotations


def _rounded(value) -> float:
    return float('%.6e' % float(value))


class BuildFollowStage:
    #: Set by :meth:`follow_geometry_build` once a build changed the model:
    #: from then on the names below are the model's names, and the job's
    #: tag-keyed ``surfaceNames``/``volumeNames`` are never read.
    geometry_built = False

    def _build_state(self):
        if not hasattr(self, 'build_surface_names'):
            #: Surface tag -> the name it carried before the build.
            self.build_surface_names: dict[int, str] = {}
            self.build_volume_names: dict[int, str] = {}
            #: ``(dim, tag)`` -> the physical group the build put it in.
            self.build_group_names: dict[tuple, str] = {}
            #: Patch names whose every surface the build took away.
            self.build_replaced_names: list[str] = []
            self.build_dropped_scopes: list[str] = []
            #: The layer patches dropped because the build replaced them.
            self.layer_patches_replaced: list[str] = []

    # -- fingerprints ------------------------------------------------------ #

    def build_signatures(self) -> dict:
        """``{(dim, tag): fingerprint}`` of every surface and volume.

        The bounding box and the area or volume: what stays the same for an
        entity a build left alone, whatever kernel made it.
        """
        gmsh = self.gmsh
        signatures = {}
        for dim in (2, 3):
            for _d, tag in gmsh.model.getEntities(dim):
                try:
                    box = gmsh.model.getBoundingBox(dim, tag)
                except Exception:                        # noqa: BLE001
                    continue
                try:
                    mass = _rounded(gmsh.model.occ.getMass(dim, tag))
                except Exception:                        # noqa: BLE001
                    mass = None
                signatures[(int(dim), int(tag))] = (
                    tuple(_rounded(v) for v in box), mass)
        return signatures

    def build_snapshot(self) -> dict:
        """What the model holds before a build step: fingerprints, names."""
        self._build_state()
        gmsh = self.gmsh
        surfaces = [int(t) for _d, t in gmsh.model.getEntities(2)]
        volumes = [int(t) for _d, t in gmsh.model.getEntities(3)]
        names = {}
        for tag in surfaces:
            name, known = self.surface_patch_name(tag)
            if known:
                names[tag] = name
        return {'signatures': self.build_signatures(),
                'surfaces': names,
                'volumes': {tag: self.region_name(tag) for tag in volumes
                            if self.region_name(tag)},
                'groups': self._group_keys()}

    def _group_keys(self) -> set:
        try:
            return {(int(d), int(t))
                    for d, t in self.gmsh.model.getPhysicalGroups()}
        except Exception:                                # noqa: BLE001
            return set()

    # -- following --------------------------------------------------------- #

    def follow_geometry_build(self, snapshot, kinds=()) -> dict:
        """Carry identities and names across one or more build steps.

        Returns what changed: ``{'replaced': [...], 'new': n, ...}``, empty
        when the build left every surface and volume as it was.
        """
        self._build_state()
        # DP-5717. The presets run so far, for `one_cell_thick_build`.
        self.build_kinds = sorted(set(getattr(self, 'build_kinds', None)
                                      or ()) | {str(k) for k in kinds})
        before = snapshot['signatures']
        after = self.build_signatures()
        successors = {2: {}, 3: {}}
        taken = set()
        for key, signature in before.items():
            if after.get(key) == signature:
                successors[key[0]][key[1]] = [key[1]]
                taken.add(key)
        loose: dict = {}
        for key, signature in after.items():
            if key not in taken:
                loose.setdefault(signature, []).append(key)
        for key, signature in sorted(before.items()):
            if key in taken:
                continue
            candidates = [c for c in loose.get(signature, ())
                          if c[0] == key[0] and c not in taken]
            if len(candidates) == 1:
                successors[key[0]][key[1]] = [candidates[0][1]]
                taken.add(candidates[0])
        new = [key for key in after if key not in taken]
        gone = [key for key in before
                if not successors[key[0]].get(key[1])]
        moved = any(succ != [tag] for dim in successors
                    for tag, succ in successors[dim].items())
        groups_now = self._group_keys() - set(snapshot.get('groups') or ())
        if not (new or gone or moved or groups_now):
            return {}
        self.geometry_built = True
        for dim in (2, 3):
            self.retarget_entities('geometry_build', successors[dim],
                                   dimensions=(dim,))

        def carry(table, dim):
            out = {}
            for tag, name in table.items():
                for new_tag in successors[dim].get(int(tag), ()):
                    out.setdefault(int(new_tag), name)
            return out

        self.build_surface_names = carry(snapshot['surfaces'], 2)
        self.build_volume_names = carry(snapshot['volumes'], 3)
        self.entity_surface_names = carry(self.entity_surface_names, 2)
        self.entity_volume_names = carry(self.entity_volume_names, 3)
        self.merged_surface_names = carry(self.merged_surface_names, 2)
        self.generated_names = carry(self.generated_names, 2)
        kept = set(self.build_surface_names.values())
        replaced = sorted({name for tag, name in snapshot['surfaces'].items()
                           if not successors[2].get(int(tag))} - kept)
        self.build_replaced_names = sorted(
            set(self.build_replaced_names) | set(replaced))
        dropped = self._follow_scopes(successors)
        self.build_dropped_scopes = sorted(
            set(self.build_dropped_scopes) | set(dropped))
        self.take_build_groups()
        record = {'replaced': list(self.build_replaced_names),
                  'newSurfaces': sum(1 for d, _t in new if d == 2),
                  'newVolumes': sum(1 for d, _t in new if d == 3),
                  'droppedScopes': list(self.build_dropped_scopes),
                  'groups': sorted(set(self.build_group_names.values())),
                  'surfaces': self.built_surfaces()}
        self.statistics['geometryBuild'] = record
        if replaced:
            self.warnings.append(
                'the geometry build replaced the surfaces named '
                + ', '.join(replaced) + '; those names are no longer on the '
                'model, and the new surfaces are named from the groups the '
                'build made')
        return record

    def _follow_scopes(self, successors) -> list:
        """The scope maps after the build; returns the tokens left empty."""
        scoped = self.job.get('scopeEntities') or {}
        lost = []
        if self.entity_tags and (scoped.get('surfaces')
                                 or scoped.get('volumes')):
            self.scope_surfaces, missing = self._scopes_by_identity(
                scoped.get('surfaces') or {}, 'surface')
            self.scope_volumes, absent = self._scopes_by_identity(
                scoped.get('volumes') or {}, 'volume')
            return missing + absent
        for dim, table in ((2, self.scope_surfaces), (3, self.scope_volumes)):
            for token in list(table):
                tags = sorted({t for tag in table[token]
                               for t in successors[dim].get(int(tag), ())})
                if tags:
                    table[token] = tags
                else:
                    del table[token]
                    lost.append(str(token))
        return lost

    def take_build_groups(self) -> dict:
        """Read the physical groups on the model (the build's, and any the
        group table renamed since) as names of the entities they hold."""
        self._build_state()
        if not self.geometry_built:
            return {}
        gmsh = self.gmsh
        names: dict[tuple, str] = {}
        tags: dict[tuple, int] = {}
        try:
            groups = gmsh.model.getPhysicalGroups()
        except Exception:                                # noqa: BLE001
            groups = ()
        for dim, group in groups:
            if int(dim) not in (2, 3):
                continue
            label = str(gmsh.model.getPhysicalName(dim, group)).strip()
            if not label:
                continue
            for tag in gmsh.model.getEntitiesForPhysicalGroup(dim, group):
                names.setdefault((int(dim), int(tag)), label)
                tags.setdefault((int(dim), int(tag)), int(group))
        self.build_group_names = names
        self.build_group_tags = tags
        return names

    def built_surfaces(self) -> list:
        """DP-5717. ``[{'tag', 'name', 'group'}]`` for every surface of a
        built model, named as the run will name its patch: what the window
        lists in place of the prepared patches."""
        tags = getattr(self, 'build_group_tags', None) or {}
        rows = []
        for _d, tag in self.gmsh.model.getEntities(2):
            tag = int(tag)
            rows.append({'tag': tag, 'name': self.built_surface_name(tag)[0],
                         'group': tags.get((2, tag))})
        return rows

    def clear_build_groups(self) -> None:
        """Take the build's surface and volume groups off the model before
        the run names its own, so a patch is one group, not two."""
        if not self.geometry_built:
            return
        gmsh = self.gmsh
        try:
            groups = [(int(d), int(t)) for d, t
                      in gmsh.model.getPhysicalGroups() if int(d) in (2, 3)]
        except Exception:                                # noqa: BLE001
            return
        if groups:
            gmsh.model.removePhysicalGroups(groups)

    # -- names on a built model -------------------------------------------- #

    def built_surface_name(self, tag):
        """``(name, known)`` of surface *tag* on a built model."""
        tag = int(tag)
        # DP-5725: the window's name for a built surface (a group-table
        # rename since the build included) comes first
        renamed = (self.job.get('builtSurfaceNames') or {}).get(str(tag))
        named = (renamed or self.build_group_names.get((2, tag))
                 or self.build_surface_names.get(tag)
                 or self.generated_names.get(tag))
        return (str(named) if named else f'face_{tag}'), bool(named)

    def region_name(self, tag) -> str:
        """The name of volume *tag*, or ``''`` when nothing names it."""
        tag = int(tag)
        if self.geometry_built:
            self._build_state()
            return str(self.build_group_names.get((3, tag))
                       or self.build_volume_names.get(tag) or '')
        return str(self.entity_volume_names.get(tag)
                   or (self.job.get('volumeNames') or {}).get(str(tag))
                   or '')

    def replaced_layer_patches(self, wanted, asked, mode_all, present):
        """*wanted* without the patches the build took away.

        On a built model "every eligible wall" means the walls the person
        was shown and ticked (*asked*), not whatever the new surfaces are
        called: an unnamed ``face_<tag>`` of a new slab is no wall anyone
        chose. A ticked wall the build replaced is dropped and remembered,
        never re-bound to a surface that now has its tag.
        """
        self._build_state()
        if not self.geometry_built:
            return set(wanted)
        wanted = set(wanted)
        if mode_all and asked:
            wanted &= set(asked)
        replaced = set(self.build_replaced_names)
        gone = sorted(n for n in (set(asked) | wanted)
                      if n in replaced and n not in present)
        self.layer_patches_replaced = sorted(
            set(getattr(self, 'layer_patches_replaced', None) or ()) | set(gone))
        return wanted - set(gone)

    #: The build presets that leave a mesh one cell thick (DP-5717).
    ONE_CELL_PRESETS = ('case_2d', 'axisymmetric_wedge')

    def one_cell_thick_build(self) -> list:
        """DP-5717. The patches of a built model that make it one cell
        thick -- the front/back a 2D-case or wedge preset declared -- or
        ``[]``. Read from the roles the job carries (``surfaceCategories``
        ``empty``/``wedge``), else from the build steps it followed."""
        if not self.geometry_built:
            return []
        categories = self.job.get('surfaceCategories') or {}
        names = sorted({self.surface_patch_name(tag)[0]
                        for _d, tag in self.gmsh.model.getEntities(2)})
        declared = [name for name in names
                    if str(categories.get(name, '')).lower()
                    in ('empty', 'wedge')]
        if declared:
            return declared
        kinds = set(getattr(self, 'build_kinds', None) or ())
        if kinds & set(self.ONE_CELL_PRESETS):
            return ([name for name in names if name in ('front', 'back')]
                    or ['front', 'back'])
        return []

    def one_cell_layers_note(self, declared) -> str:
        # DP-5718. Reached only when no ticked wall meets those faces: the
        # layer of such a case grows in its plane, off the shared edges.
        return ('the geometry build made a mesh one cell thick (%s), and no '
                'ticked wall meets those faces, so no layer was grown: the '
                'layer of such a case grows in its plane, off the edges a '
                'wall shares with them. Tick the walls around the section, '
                'or turn boundary layers off.' % ', '.join(declared))

    def layers_replaced_note(self) -> str:
        names = ', '.join(self.layer_patches_replaced)
        return ('the geometry build replaced ' + names + ', the surfaces '
                'the boundary layers were asked for on, so no layer was '
                'grown. Choose walls of the built model on the Layers page, '
                'or turn boundary layers off.')
