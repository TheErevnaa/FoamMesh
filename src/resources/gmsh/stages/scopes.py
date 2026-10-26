"""Entity identity, scopes and patch names (Plan 48 RR, DP-4009).

Moved out of ``runner_v1.py``: a scope token resolves to Gmsh tags, and a tag
to the name the prepared geometry gave it, the same way in the batch run and
in the resident session."""
from __future__ import annotations


from .common import (MeshFailure)


class ScopeStage:
    def surface_patch_name(self, tag):
        """``(patch name, whether the prepared geometry named it)``.

        Plan 28 WP7: a classified piece publishes under the imported solid it
        was cut from, so the lookup goes through ``surface_origin`` first.
        """
        if getattr(self, 'geometry_built', False):
            # DP-5716. A geometry build changed the model: the names are the
            # ones followed through it, never a job name keyed on a tag the
            # build may have reused.
            return self.built_surface_name(tag)
        source = self.surface_origin.get(tag, tag)
        # C31-04. The identity map first: it names this exact tag, and it is
        # the only lookup that stays right once a second source has shifted
        # every tag away from the index the prepared revision recorded.
        named = self.entity_surface_names.get(int(tag))
        if not named:
            named = (self.job.get('surfaceNames') or {}).get(str(source))
        if not named:
            # Plan 30 WP12. A face the runner made rather than imported -- the
            # six sides of a generated farfield box -- has no prepared name to
            # look up, and `face_41` would publish as a wall.
            named = self.generated_names.get(tag)
        return (str(named) if named else f'face_{source}'), bool(named)

    def declared_surface_names(self) -> set[str]:
        """Every patch name the prepared geometry declared, alive or not.

        The counterpart to :meth:`surface_patch_name`, which asks a surface
        what it is called. DP-399: a name with no surface is not a surface
        with no name, and the census only ever counted the second kind.
        """
        names = (self.job.get('entityNames') or {}).get('surfaces') or {}
        if not names:
            # The version-1 form, keyed by a source-local tag. Its keys
            # collide across sources; its values do not, and the values are
            # the whole of the question here.
            names = self.job.get('surfaceNames') or {}
        return {str(label) for label in names.values() if label}

    @staticmethod
    def _tag_ranges(tags):
        """``[1, 2, 3, 7]`` as ``1-3, 7``: the numbering, short enough to read."""
        spans, ordered = [], sorted(tags)
        for tag in ordered:
            if spans and tag == spans[-1][1] + 1:
                spans[-1][1] = tag
            else:
                spans.append([tag, tag])
        return ', '.join(str(low) if low == high else f'{low}-{high}'
                         for low, high in spans)

    def describe_surfaces(self, tags):
        """R64. Say which surface is which, by the number the user must quote.

        MEASURED on venturi.stl: the run log read ``imported 1 volume(s), 7
        surface(s)`` for a geometry with five named boundaries, and nothing
        anywhere said what the other two were. Per-surface sizing addresses a
        surface by exactly this number -- it reaches Gmsh as a raw surface tag
        in ``SurfacesList`` -- so a numbering the app never prints is a
        numbering the user has to guess.
        """
        named: dict[str, list[int]] = {}
        unnamed: list[int] = []
        for tag in tags:
            name, known = self.surface_patch_name(tag)
            if known:
                named.setdefault(name, []).append(tag)
            else:
                unnamed.append(tag)
            # DP-410. A name the fusion merged into this one names these
            # faces too. It publishes under the other name -- a face belongs
            # to one patch -- but it is not a name without a surface, and
            # counting it as one is what refused the run.
            for alias in self.merged_surface_names.get(int(tag), ()):
                named.setdefault(alias, []).append(tag)
        parts = [f'{name} = {self._tag_ranges(owned)}'
                 for name, owned in sorted(named.items())]
        message = f': {"; ".join(parts)}' if parts else ''
        if unnamed:
            message += (
                f'{";" if parts else ":"} surface {self._tag_ranges(unnamed)} '
                f'{"is" if len(unnamed) == 1 else "are"} not named by the '
                'prepared geometry and will publish as '
                + ', '.join(f'face_{self.surface_origin.get(tag, tag)}'
                            for tag in unnamed))
        lost = sorted(self.declared_surface_names() - set(named))
        self.names_without_surface = set(lost)
        if lost:
            message += (
                f'{";" if parts or unnamed else ":"} '
                + ', '.join(lost)
                + (' is a prepared patch name' if len(lost) == 1
                   else ' are prepared patch names')
                + ' that no surface in this model carries')
            self.warnings.append(
                'the prepared geometry names ' + ', '.join(lost)
                + ', which no imported surface carries, so any control '
                'scoped to ' + ('it' if len(lost) == 1 else 'them')
                + ' reaches nothing')
        census = {'surfacesByPatch': {name: sorted(owned)
                                      for name, owned in named.items()},
                  'unnamedSurfaces': sorted(unnamed),
                  # DP-399. The count this is measured against is the
                  # receipt's own: 14 declared against 13 surfaces read as
                  # healthy because nobody subtracted them.
                  'namesWithoutSurface': lost}
        return {'message': message, 'details': census, 'statistics': census}

    def _announce_merged_names(self) -> None:
        """Say which prepared names the fusion made into one face.

        DP-410. Two names on one face is not an error -- the faces really are
        one face now -- but it is a thing the user did that the run quietly
        changed, and a patch they named that will not appear in the mesh
        under its own name. It is said once, with both names in it.
        """
        for tag in sorted(self.merged_surface_names):
            published = self.entity_surface_names.get(int(tag))
            extras = self.merged_surface_names[tag]
            if not published:
                continue
            if self._bounds_two_volumes(tag):
                # DP-527. The two sides of an interface between two solids
                # are the one pair the fusion exists for: each solid brings
                # its own copy, and fusing them is what makes the interface
                # conformal. Neither name publishes as a patch -- the face is
                # interior -- so "publishes under" was false and "turn off
                # remove duplicate faces" was advice that breaks the volume
                # mesh. MEASURED on G1 `jacketed_pipe`: face0/face6 is the
                # core-to-jacket interface, and the prepared group manifest
                # already declares it as an interface pair.
                # DP-557. The requested side is the list of names that fused
                # and the effective side the one surface they became, so the
                # ledger's string equality compared a list with a name and
                # filed every conformal interface under `controlMismatches`.
                # The fusion is what an interface between two volumes asks
                # for: it matched.
                self.ledger.record(
                    f'mergedInterface:{published}', sorted(extras), published,
                    note='the two sides of an interface between two volumes '
                         'fused into one conformal surface',
                    matched=True)
                continue
            self.warnings.append(
                'the import fused coincident faces, so '
                + ', '.join(sorted(extras))
                + (' names' if len(extras) == 1 else ' name')
                + f' the same surface as {published} and the patch publishes '
                  f'under {published}. Turn off "remove duplicate faces" if '
                  'these were meant to stay separate patches.')
            self.ledger.record(
                f'mergedPatch:{published}', sorted(extras), published,
                note='coincident faces fused into one surface')

    def _bounds_two_volumes(self, tag) -> bool:
        """Whether surface *tag* lies between two volumes, i.e. is interior."""
        try:
            upward, _down = self.gmsh.model.getAdjacencies(2, int(tag))
        except Exception:  # noqa: BLE001 - an unanswerable model is not one
            return False
        return len(upward) >= 2

    def resolve_scopes(self, surfaces, volumes=()):
        """Map prepared scope tokens onto Gmsh surface and volume tags.

        C31-04. Two routes, and the identity route wins wherever the job
        carries one. ``scopeEntities`` names entities by source, and
        :attr:`entity_tags` says which tags each source actually produced --
        followed through every topology change since. ``scopeSurfaces`` and
        ``scopeVolumes`` are the version-1 form: bare indices into one global
        tag list, which are only the right entity while a case holds a single
        imported file. MEASURED on ``duct.step`` + ``pipe.step`` prepared
        together: both regions carried volume index 0 and the pipe's three
        faces carried surface indices 0-2, the duct's numbers, so a control
        authored on the pipe reached the duct.

        C31-05. A scope that resolves to nothing is never simply skipped. A
        token from the prepared maps that no control mentions is a warning --
        those maps name every prepared group, and most groups are not scoped
        by anything. A token an *enabled* control authored stops the run, in
        :meth:`refuse_unresolved_scopes`.
        """
        surface_tags = [tag for _dim, tag in surfaces]
        volume_tags = [tag for _dim, tag in volumes]
        scoped = self.job.get('scopeEntities') or {}
        by_identity = bool(self.entity_tags) and bool(
            scoped.get('surfaces') or scoped.get('volumes'))
        if by_identity:
            self.scope_surfaces, missing = self._scopes_by_identity(
                scoped.get('surfaces') or {}, 'surface')
            self.scope_volumes, absent = self._scopes_by_identity(
                scoped.get('volumes') or {}, 'volume')
            self.unresolved_scopes.extend(missing + absent)
        else:
            mapping = self.job.get('scopeSurfaces') or {}
            # Plan 28 WP7. After a tessellated import the indices name the
            # solids as imported; the surfaces that exist now are their
            # pieces. Widening one authored entity onto the pieces it was cut
            # into is the mapping working, not a scope failing to resolve:
            # what C31-05 refuses is a scope that reaches *no* entity.
            if self.surface_origin:
                origins = self.import_surface_tags
                pieces: dict[int, list[int]] = {}
                for piece, origin in self.surface_origin.items():
                    pieces.setdefault(origin, []).append(piece)

                def expand(indices):
                    out = []
                    for index in indices:
                        if isinstance(index, int) and 0 <= index < len(origins):
                            out.extend(pieces.get(origins[index], []))
                    return out
            else:
                def expand(indices):
                    return [surface_tags[index] for index in indices
                            if isinstance(index, int)
                            and 0 <= index < len(surface_tags)]
            for token, indices in mapping.items():
                resolved = expand(indices)
                if resolved:
                    self.scope_surfaces[token] = resolved
                else:
                    self.unresolved_scopes.append(token)
            for token, indices in (self.job.get('scopeVolumes') or {}).items():
                resolved = [volume_tags[index] for index in indices
                            if isinstance(index, int)
                            and 0 <= index < len(volume_tags)]
                if resolved:
                    self.scope_volumes[token] = resolved
                else:
                    self.unresolved_scopes.append(token)
        # DP-58. This was inside the identity branch above, which is
        # reached only when the job carries a scope. A case with no scoped
        # control -- 19 of the 33 tessellated runs this repository has
        # published -- therefore threw away names the job was carrying for it.
        # Naming an entity and scoping a control to it are two different
        # questions about the same map.
        self._name_entities()
        if self.unresolved_scopes:
            self.warnings.append(self._unresolved_scope_sentence())
        self.refuse_unresolved_scopes()

    def _scopes_by_identity(self, mapping, kind):
        """``{token: tags}`` and the tokens that reached no entity at all."""
        placed: dict[str, list[int]] = {}
        missing: list[str] = []
        for token, identities in mapping.items():
            tags: list[int] = []
            for identity in identities or ():
                record = self.entity_tags.get(str(identity))
                if record is not None and record['kind'] == kind:
                    tags.extend(record['tags'])
            tags = sorted(set(tags))
            if tags:
                placed[str(token)] = tags
            else:
                missing.append(str(token))
        return placed, missing

    def _name_entities(self) -> None:
        """Gmsh tag -> the name the prepared revision gave that entity.

        C31-04. ``surfaceNames``/``volumeNames`` are keyed on a tag computed
        from a source-local index, which is the same collision as the scopes:
        MEASURED on duct + pipe, six names for nine surfaces, three of the
        duct's overwritten by the pipe's and surfaces 7-9 unnamed. Keyed on
        the entity identity there is one name per entity and it survives
        classification and the farfield cut with it.
        """
        names = self.job.get('entityNames') or {}
        for kind, table in (('surface', names.get('surfaces') or {}),
                            ('volume', names.get('volumes') or {})):
            target = (self.entity_surface_names if kind == 'surface'
                      else self.entity_volume_names)
            for identity, label in sorted(table.items()):
                record = self.entity_tags.get(str(identity))
                if record is None or record['kind'] != kind or not label:
                    continue
                for tag in record['tags']:
                    # DP-410. Two identities can share a tag: the duplicate
                    # fusion makes one face out of two, and both prepared
                    # names then belong to it. `target[tag] = label` alone
                    # let whichever came last win, which is a patch name
                    # decided by dictionary order. The first in sorted order
                    # publishes and the rest are kept as names of the same
                    # faces.
                    held = target.get(int(tag))
                    if held is None:
                        target[int(tag)] = str(label)
                    elif held != str(label) and kind == 'surface':
                        merged = self.merged_surface_names.setdefault(
                            int(tag), [])
                        if str(label) not in merged:
                            merged.append(str(label))
        self._announce_merged_names()
        self._name_unclaimed_volumes()

    def _name_unclaimed_volumes(self) -> None:
        """The volumes a source made beyond the one its region claimed.

        DP-423. A surface that closes into more than one volume still gets a
        single region record, so only ``{source}:volume:0`` is ever named and
        every other volume of that file fell through to ``volume_<tag>`` --
        134 of 140 zones across six meshes, each called after a Gmsh tag
        number with no relation to the model.

        Nothing about those volumes is unknown here. The import recorded
        which file produced each one and in what order, and the job carries
        what that file's single region is called, so the name is that name
        with the volume's own position after it. Volume ``0`` is skipped: the
        region already named it, and renaming it would move a zone name that
        meshes already carry.
        """
        fallback = self.job.get('volumeFallback') or {}
        if not fallback:
            return
        for identity, record in self.entity_tags.items():
            if record.get('kind') != 'volume':
                continue
            source, _, local = str(identity).rpartition(':volume:')
            if not source or not local.isdigit() or int(local) == 0:
                continue
            label = str(fallback.get(source) or '').strip()
            if not label:
                continue
            for tag in record['tags']:
                # Only where nothing else named it. An explicit name from
                # `entityNames` is a name the user's own tree gave this
                # volume and outranks a position.
                self.entity_volume_names.setdefault(
                    int(tag), f'{label}_{int(local) + 1}')

    def authored_scopes(self) -> dict:
        """Scope token -> ``[(what named it, the entity kind it needs)]``.

        Only the intent is walked. The derivation drops every disabled row
        before the job is written, so a token reached here belongs to a
        control the user turned on -- unlike the job's scope maps, which name
        every prepared group whether or not anything scopes to it.
        """
        intent = self.job.get('intent') or {}
        authored: dict[str, list[tuple[str, str]]] = {}

        def claim(token, label, kind):
            token = str(token or '').strip()
            if token:
                authored.setdefault(token, []).append((label, kind))

        for row in (intent.get('sizeFields') or {}).get('fields') or ():
            # Plan 29 WP8: a row naming its own Gmsh surface tags never
            # consults a prepared scope, so an unresolvable token on it is
            # not what stops the run.
            if row.get('surfaces'):
                continue
            claim(row.get('scopeToken'), f'size field {row.get("name")!r}',
                  'any')
        for row in intent.get('curveControls') or ():
            claim(row.get('scopeToken'),
                  f'curve control {row.get("name")!r}', 'surface')
        for row in intent.get('volumeControls') or ():
            claim(row.get('scopeToken'),
                  f'volume control {row.get("name")!r}', 'volume')
        for pair in (intent.get('periodic') or {}).get('pairs') or ():
            name = pair.get('name')
            claim(pair.get('masterScope'),
                  f'periodic pair {name!r} (master)', 'surface')
            claim(pair.get('slaveScope'),
                  f'periodic pair {name!r} (slave)', 'surface')
        return authored

    def _unresolved_scope_sentence(self) -> str:
        """Why each scope reached nothing, where the geometry says why.

        DP-84. "matched no imported entity and were skipped" reads as a
        wiring fault, and on a domain with a body standing in it the most
        common cause is not a fault at all: the scope names a shell the
        nesting made a void, and a void is a hole, so it never becomes a
        volume for a scope to reach. MEASURED on gmsh `drone_quadcopter`,
        where `region-bdfdd4053357f840b89899c8` names the drone body -- five
        shells, every one of them `void` -- and the run meshed correctly
        while reporting a sentence that says something went wrong.

        Saying which is which matters because the two want opposite actions.
        A void wants the control moved to the fluid; a genuinely missing
        entity wants the geometry looked at.
        """
        roles = {}
        record = self.statistics.get('tessellatedImport')
        for shell in (record or {}).get('shells') or ():
            name = str(shell.get('name') or '')
            key = name.split('#', 1)[0]
            roles.setdefault(key, set()).add(str(shell.get('role') or ''))
        scoped = (self.job.get('scopeEntities') or {}).get('volumes') or {}
        voided, plain = [], []
        for token in sorted(set(self.unresolved_scopes)):
            keys = {str(item).split(':', 1)[0]
                    for item in scoped.get(token) or ()}
            seen = set()
            for key in keys:
                seen |= roles.get(key, set())
            if seen and seen <= {'void'}:
                voided.append(token)
            else:
                plain.append(token)
        parts = []
        if plain:
            parts.append('these scopes matched no imported entity and were '
                         'skipped: ' + ', '.join(self._scope_holder(token)
                                                 for token in plain))
        if voided:
            parts.append(
                'these scopes name a shell this domain treats as a void, so '
                'there is no volume for them to reach and nothing scoped to '
                'them was applied — move the control to the fluid volume if '
                'it was meant for the space around the body: '
                + ', '.join(voided))
        return '; '.join(parts)

    def _scope_holder(self, token) -> str:
        """What a user can find a scope token under: its control, its patch.

        DP-547. G6 (audit 0924) warned ``...were skipped:
        640cae55-a49b-4f43-98f5-51a8d5fade54``. That token was the prepared
        patch ``body1_face4``, which no control scoped at all -- the job's
        scope maps name every prepared group -- and nothing on screen let the
        user find that out. The bare token is kept only when the job has no
        word for it.
        """
        token = str(token)
        holders = [label for label, _kind
                   in self.authored_scopes().get(token, ())]
        # DP-548. A pair's scope is not a control the run can apply, so it
        # never stops the run -- but it is what the user authored the token
        # on, and the name they can look for.
        for pair in self.job.get('interfacePairs') or ():
            for side in ('master', 'slave'):
                if str(pair.get(f'{side}Scope') or '') == token:
                    holders.append(
                        f'interface pair {pair.get("name")!r} ({side})')
        scoped = self.job.get('scopeEntities') or {}
        names = self.job.get('entityNames') or {}
        labels = []
        for kind in ('surfaces', 'volumes'):
            for identity in (scoped.get(kind) or {}).get(token) or ():
                label = (names.get(kind) or {}).get(identity)
                if label and label not in labels:
                    labels.append(str(label))
        patch = ', '.join(labels)
        if holders:
            return f'{", ".join(holders)} on {patch or token}'
        if patch:
            return f'{patch} (a prepared patch no enabled control uses)'
        return token

    def refuse_unresolved_scopes(self) -> None:
        """Stop the run when an enabled control's scope reached no entity.

        C31-05. This used to append the token to ``unresolved_scopes``, warn
        once, and mesh anyway: every consumer then found an empty scope, wrote
        its own "was skipped" warning, and the run finished successfully with
        the control absent from the mesh. MEASURED in the job the audit built,
        the whole record of it was one line in ``warnings`` naming a UUID.

        Refusing is the right end of that choice rather than a louder report.
        The alternative -- returning the unresolved list to the caller to act
        on -- is what the code already did, and no caller acted: the run
        published as a candidate, its ledger recorded the control as not
        applied, and the mesh looked like the one the user asked for. A mesh
        that silently omits a refinement the user enabled is worse than no
        mesh, because nothing downstream can tell the difference.

        A scope the prepared geometry carries but no control uses is *not*
        this: it stays a warning. Neither is a scope that widened onto several
        classified pieces, which is the mapping working.
        """
        blocked = []
        for token, claims in self.authored_scopes().items():
            for label, kind in claims:
                if kind in ('any', 'surface') and token in self.scope_surfaces:
                    continue
                if kind in ('any', 'volume') and token in self.scope_volumes:
                    continue
                blocked.append((label, token, kind))
        if not blocked:
            return
        lines = []
        for label, token, kind in blocked:
            lines.append(f'{label} is scoped to {self._scope_description(token, kind)}')
        raise MeshFailure(
            'these enabled controls name geometry this run does not contain, '
            'so the mesh would silently be missing them:\n  '
            + '\n  '.join(lines)
            + '\nRe-prepare the geometry, or turn the control off, and run '
              'again.')

    def _scope_description(self, token, kind) -> str:
        """What a user can act on: the entity names behind a scope token."""
        scoped = self.job.get('scopeEntities') or {}
        names = self.job.get('entityNames') or {}
        identities = list((scoped.get('surfaces') or {}).get(token) or ())
        identities += list((scoped.get('volumes') or {}).get(token) or ())
        labels = []
        for identity in identities:
            label = ((names.get('surfaces') or {}).get(identity)
                     or (names.get('volumes') or {}).get(identity))
            labels.append(f'{label} ({identity})' if label else str(identity))
        consumed = sorted({
            change['stage'] for change in self.topology_changes
            for identity in identities
            if identity in change['entities']
            and not change['entities'][identity]['to']})
        if not labels:
            labels = [f'the prepared scope {token}']
        detail = ', '.join(labels)
        if consumed:
            return f'{detail}, which {" and ".join(consumed)} consumed'
        return f'{detail}, which no imported {kind} carries'
