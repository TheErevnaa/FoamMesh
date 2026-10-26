"""Importing CAD sources and settling the model (Plan 48 RR, DP-4009).

The CAD half of the runner's import, the identity receipts, the duplicate-face
fusion and the checks that follow, shared by the batch run and the session."""
from __future__ import annotations

import math

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)

from quantities import count_text

from .common import (MeshFailure)


class GeometryStage:
    def _source_record(self, index, path) -> dict:
        """The prepared source one entry of ``job['geometry']`` is.

        Positional, because ``build_job`` writes ``sources`` from the same
        prepared manifest order it writes ``geometry`` from, with the file
        name checked so a mismatched pair is reported rather than silently
        mapping one file's faces onto another file's identity.
        """
        table = self.job.get('sources') or ()
        if index >= len(table) or not isinstance(table[index], dict):
            return {}
        record = table[index]
        expected = str(record.get('prepared_name') or '')
        actual = str(path).replace('\\', '/').rsplit('/', 1)[-1]
        if expected and actual and expected != actual:
            self.warnings.append(
                f'the job lists {actual} as prepared source {expected!r}; its '
                'entity identities were not used, so any control scoped to '
                'that source resolves by position only')
            return {}
        return record

    def _source_receipt(self, index, path, *, stage, surfaces, volumes,
                        shells, added) -> dict:
        """One import receipt, with the entity mapping this source produced.

        The counts are the WP-01 fields, unchanged and still read by the run
        manifest. What is added is the part a second source made necessary:
        which Gmsh tags carry which prepared entity, so a scope resolves
        against its own file.
        """
        record = self._source_record(index, path)
        source_id = str(record.get('source_id') or '')
        declared = int(record.get('declared_surfaces') or 0)
        found = len(added.get('surface') or ())
        shared = self._shared_listings(record, declared, found)
        entities = []
        if source_id:
            for kind, tags in added.items():
                dimension = 2 if kind == 'surface' else 3
                listings = self._listing_tags(
                    tags, shared if kind == 'surface' else {})
                for local, owned in enumerate(listings):
                    identity = f'{source_id}:{kind}:{local}'
                    self.entity_tags[identity] = {
                        'kind': kind, 'dim': dimension, 'tags': list(owned)}
                    entities.append({'entity_id': identity, 'kind': kind,
                                     'dim': dimension, 'tags': list(owned)})
        if shared:
            self.ledger.record(
                f'sharedFaces:{source_id}', len(shared), len(shared),
                note=(f'{count_text(len(shared), "face")} two solids share '
                      'imported once and answer to both listings: '
                      + ', '.join(f'{alias} = {first}' for alias, first
                                  in sorted(shared.items()))))
        if not source_id:
            status = 'unmapped'
        elif declared and declared - len(shared) != found:
            # The prepared revision named a different number of faces than
            # Gmsh made. Saying so is the difference between a scope that is
            # known to be approximate and one that quietly points elsewhere.
            status = 'partial'
        else:
            status = 'exact'
        return {
            'schema_version': int(self.job.get('entitySchemaVersion') or 1),
            'path': str(path),
            'source_id': source_id,
            'import_stage': stage,
            'surfaces': surfaces, 'volumes': volumes, 'shells': shells,
            'entities': entities,
            # DP-399. Kept so the status can be decided again later, against
            # the tags the model holds rather than the ones import made.
            'declared_surfaces': declared,
            'mapping_confidence': float(record.get('mapping_confidence', 1.0)
                                        if record else 0.0),
            'mapping_status': status,
        }

    @staticmethod
    def _shared_listings(record, declared, found) -> dict:
        """``{second listing: first listing}`` for this source's shared faces.

        DP-546. Used only when the import agrees with it: the file listed
        *declared* faces, the job says ``len(shared)`` of those are a face two
        solids share, and Gmsh added exactly the rest. Any other count means
        the import did something the prepared walk did not foresee, and the
        plain positional join -- with its ``partial`` status -- says so.
        """
        shared = {}
        for alias, first in ((record or {}).get('shared_surfaces')
                             or {}).items():
            try:
                shared[int(alias)] = int(first)
            except (TypeError, ValueError):
                return {}
        if not shared or declared - len(shared) != found:
            return {}
        return shared

    @staticmethod
    def _listing_tags(tags, shared) -> list:
        """The tags each listing of one source owns, listing by listing.

        DP-546. Gmsh binds a face two solids share at its first listing and
        numbers every later face from there, so the second listing takes the
        first one's tag and spends none of its own.
        """
        if not shared:
            return [[int(tag)] for tag in tags]
        listings: list[list[int]] = []
        remaining = iter(tags)
        for local in range(len(tags) + len(shared)):
            if local in shared:
                listings.append(list(listings[shared[local]]))
            else:
                listings.append([int(next(remaining))])
        return listings

    def retarget_entities(self, stage, successors, *, dimensions=(2,)):
        """Follow every entity identity through one topology change.

        C31-04/§4.1. Healing, classification and the farfield Boolean all
        renumber: the tags an identity was mapped to during import may be
        gone, replaced by several pieces, or consumed outright. *successors*
        is ``{old tag: [new tags]}`` for the dimensions given; an entity of
        those dimensions with no successor is recorded as consumed and its
        tag list emptied, so a control scoped to it is refused rather than
        applied to whatever now occupies that position.
        """
        moved: dict[str, dict] = {}
        for identity, record in self.entity_tags.items():
            if record['dim'] not in dimensions:
                continue
            before = list(record['tags'])
            after: list[int] = []
            for tag in before:
                after.extend(successors.get(tag, ()))
            after = sorted(set(after))
            if after != before:
                record['tags'] = after
                moved[identity] = {'from': before, 'to': after}
        if moved:
            self.topology_changes.append({'stage': stage, 'entities': moved})
        return moved

    def revalidate_scopes(self, stage, *, dimensions=(2, 3)):
        """Re-check every identity against the model, after it has changed.

        C31-04/§4.1: "resolve and validate mappings after every relevant
        topology change". Import, classification and the farfield cut are
        followed as they happen; this is the general re-check for a change
        made later -- a volume removed by an exclusion control is the one the
        runner performs today -- so a scope that was resolved before it is not
        still handing a later control a tag the model no longer has.

        The boundary-layer extrusion is deliberately not routed through here:
        it replaces surfaces with the rim faces that took their place, and
        ``layer_replacements`` already carries that lineage for naming.
        """
        alive = {dimension: {int(tag) for _dim, tag
                             in self.gmsh.model.getEntities(dimension)}
                 for dimension in dimensions}
        # DP-527. Per dimension: one tag-keyed map over both let a surviving
        # volume 7 keep a consumed surface 7 alive.
        moved: dict[str, dict] = {}
        for dimension in dimensions:
            moved.update(self.retarget_entities(
                stage, {tag: [tag] for tag in alive[dimension]},
                dimensions=(dimension,)))
        if not moved:
            return moved
        scoped = self.job.get('scopeEntities') or {}
        if not self.entity_tags or not (scoped.get('surfaces')
                                        or scoped.get('volumes')):
            return moved
        self.scope_surfaces, _missing = self._scopes_by_identity(
            scoped.get('surfaces') or {}, 'surface')
        self.scope_volumes, _absent = self._scopes_by_identity(
            scoped.get('volumes') or {}, 'volume')
        return moved

    def _refresh_receipt_entities(self) -> None:
        """Republish each receipt's entity tags after the topology changes.

        The receipt is written during import and read after it, so what it
        reports has to be what the model holds when the run is described --
        not the numbering the import happened to start with.
        """
        for receipt in self.import_receipts:
            for entry in receipt.get('entities') or ():
                current = self.entity_tags.get(entry['entity_id'])
                if current is not None:
                    entry['tags'] = list(current['tags'])
            # DP-399. The status used to be settled during import, before
            # healing and before the duplicate fusion, so a source that lost
            # a face to either still reported `exact` -- the one field a
            # reader would consult to ask whether the mapping held. It is the
            # same question; this is the first point at which it can be
            # answered.
            if receipt.get('mapping_status') == 'unmapped':
                continue
            declared = int(receipt.get('declared_surfaces') or 0)
            alive = sum(1 for entry in receipt.get('entities') or ()
                        if entry['kind'] == 'surface' and entry['tags'])
            receipt['mapping_status'] = (
                'exact' if not declared or declared == alive else 'partial')

    def _entity_signatures(self, dimensions=(2, 3)) -> dict:
        """Where each entity is and how big it is, keyed by tag.

        DP-410. The one thing that survives ``removeAllDuplicates``: the tag
        numbering does not, but two coincident faces have one centre of mass
        and one area between them, and that is still true of the single face
        the fusion leaves behind. Rounded, because the fused face is rebuilt
        rather than kept and the arithmetic is not bit-identical.

        DP-527. Keyed by ``(dim, tag)``, not by the tag alone. Gmsh numbers
        each dimension from 1, so surface 1 and volume 1 are two entities with
        one number, and a tag-keyed map let the volume's signature overwrite
        the surface's. MEASURED on G1 `jacketed_pipe.step`: tag 7, the jacket's
        copy of the core's cylinder, has exactly the centre and area of the
        surviving tag 1 -- but surface 1's signature had been replaced by
        volume 1's, so the fusion found "no survivor", face6 was reported as a
        name no surface carried and its scope as unresolved.
        """
        signatures: dict[tuple, tuple] = {}
        occ = self.gmsh.model.occ
        for dimension in dimensions:
            for _dim, tag in occ.getEntities(dimension):
                try:
                    centre = occ.getCenterOfMass(dimension, tag)
                    mass = occ.getMass(dimension, tag)
                except Exception:  # noqa: BLE001 - OCC raises many ways
                    continue
                signatures[(int(dimension), int(tag))] = (
                    dimension,
                    tuple(round(float(value), 9) for value in centre),
                    # Nine significant figures, not nine decimal places: the
                    # area of a large face carries its magnitude with it and
                    # an absolute rounding would compare the noise.
                    float('%.8e' % float(mass)))
        return signatures

    def _follow_duplicate_fusion(self, before: dict, owners=None) -> dict:
        """Point each identity the fusion consumed at the face that replaced it.

        DP-410. ``removeAllDuplicates`` fuses two coincident faces into one,
        which is what makes a shared interface conformal and is why it is on
        by default. It also drops one tag of the pair. ``revalidate_scopes``
        maps every *surviving* tag to itself, so the dropped one had no
        successor at all: it was recorded as consumed, the prepared name it
        carried became a name no surface held, and DP-399 refused the run.

        MEASURED on `baffled_chamber.step` in the campaign runtime: 14
        surfaces in, tag 14 out, and the survivor -- tag 13, same centre of
        mass, same area, adjacent to the same single volume -- is an external
        face that can carry exactly the boundary layer the run was refused
        for. The two tags were one face all along. So the successor is found
        by that signature and the identity follows it.

        A dropped tag that matches no survivor, or more than one, is left
        consumed: that is the case this cannot read, and guessing it would
        put a user's patch name on a face chosen by arithmetic.
        """
        dimensions = tuple(sorted({dimension for dimension, _tag in before}))
        after = self._entity_signatures(dimensions)
        if not before:
            return {}
        by_signature: dict[tuple, list[tuple]] = {}
        for key, signature in after.items():
            by_signature.setdefault(signature, []).append(key)
        # One successor map per dimension, for the reason the signatures are
        # keyed by dimension: a surviving surface 3 is no successor for a
        # consumed volume 3.
        successors = {dimension: {tag: [tag] for dim, tag in after
                                  if dim == dimension}
                      for dimension in dimensions}
        for dimension, tag in sorted(set(before) - set(after)):
            candidates = by_signature.get(before[(dimension, tag)]) or []
            if len(candidates) > 1 and dimension == 2 and owners:
                # DP-5880 (Plan 49 SR5). A face kept apart from its twin has
                # the twin's signature: two coincident faces, one centre, one
                # area. MEASURED live on three boxes a|b|c, a|b fused and b|c
                # kept apart: fragmenting a with b renumbered b's x = 2 face,
                # it matched itself and c's x = 2 face, and the pair b|c lost
                # its side. The volume the face bounded (followed through the
                # fragment) tells the two apart.
                wanted = set(owners.get(int(tag)) or ())
                candidates = [key for key in candidates
                              if wanted & set(self._face_volumes(key[1]))]
            if len(candidates) != 1:
                self.ledger.record(
                    f'duplicateFusion:{tag}', tag, None, applied=False,
                    note=('the fusion consumed this entity and '
                          + ('no survivor' if not candidates
                             else f'{len(candidates)} survivors')
                          + ' share its centre of mass and size, so anything '
                            'scoped to it is refused rather than moved'))
                continue
            survivor = candidates[0][1]
            successors[dimension][tag] = [survivor]
            if dimension == 2:
                self.duplicate_face_successors[int(tag)] = int(survivor)
            else:
                self.duplicate_volume_successors[int(tag)] = int(survivor)
        if self.duplicate_face_successors or self.duplicate_volume_successors:
            for dimension in dimensions:
                self.retarget_entities('duplicate-fusion',
                                       successors[dimension],
                                       dimensions=(dimension,))
        return self.duplicate_face_successors

    def _entity_counts(self) -> dict:
        """How many entities of each dimension the model holds right now.

        Differencing this across one ``importShapes`` is what turns "the model
        has 30 surfaces" into "this file brought 6 of them".
        """
        return {dimension: len(self.gmsh.model.getEntities(dimension))
                for dimension in (2, 3)}

    def non_conformal_pairs(self) -> list:
        """The enabled coincident pairs that ask to be coupled non-conformally.

        Plan 42 SG22 Q2. Their two bodies are meshed apart.
        """
        return [pair for pair in self.job.get('interfacePairs') or ()
                if isinstance(pair, dict)
                and str(pair.get('coupling') or '') == 'non_conformal'
                and str(pair.get('transform') or 'coincident') == 'coincident']

    def check_nonconformal_contacts(self):
        """Refuse a job whose unfused contacts are not all non-conformal pairs.

        Plan 42 SG22 Q2. With fusion skipped, every pair of coincident faces
        stays two faces; each is then two boundary patches. That is what a
        non-conformal pair wants and nothing else does: a contact no pair
        covers would publish as two unnamed walls, or as a conformal pair the
        user asked for and did not get. So the job is stopped, naming the
        contact, rather than meshed as something it was not asked to be.
        """
        if not self.nonconformal_unfused:
            return
        groups: dict = {}
        for (_dim, tag), signature in self._entity_signatures((2,)).items():
            groups.setdefault(signature, []).append(int(tag))
        contacts = [sorted(tags) for tags in groups.values() if len(tags) > 1]
        sides = []
        for pair in self.non_conformal_pairs():
            master = {int(tag) for tag in self.scope_surfaces.get(
                str(pair.get('masterScope') or '')) or ()}
            slave = {int(tag) for tag in self.scope_surfaces.get(
                str(pair.get('slaveScope') or '')) or ()}
            sides.append((master, slave))
        uncovered = []
        for tags in contacts:
            if len(tags) == 2 and any(
                    (tags[0] in master and tags[1] in slave)
                    or (tags[1] in master and tags[0] in slave)
                    for master, slave in sides):
                continue
            uncovered.append(tags)
        if not uncovered:
            return
        named = '; '.join(
            ' and '.join(self.surface_patch_name(tag)[0] for tag in tags)
            for tags in uncovered)
        raise MeshFailure(
            'a non-conformal interface pair keeps its bodies apart and '
            'duplicate-face fusion (removeDuplicateFaces) is off, so the '
            f'contact at {named} would be left as unmatched walls. Turn the '
            'fusion on, or author a conformal or non-conformal pair for that '
            'contact.')

    def conformal_pairs(self) -> list:
        """The enabled coincident pairs that ask for one shared face.

        Plan 49 SR5, DP-5781. The pair drives the build: its contact is
        fragmented into one face whatever the healing switch says.
        """
        return [pair for pair in self.job.get('interfacePairs') or ()
                if isinstance(pair, dict)
                and str(pair.get('coupling') or 'conformal') == 'conformal'
                and str(pair.get('transform') or 'coincident') == 'coincident']

    @staticmethod
    def _pair_label(pair) -> str:
        return str(pair.get('name') or pair.get('pairId') or '')

    def _pair_sides(self, pair, scopes=None) -> tuple:
        scopes = self.scope_surfaces if scopes is None else scopes
        return tuple({int(tag) for tag in scopes.get(
            str(pair.get(f'{side}Scope') or '')) or ()}
            for side in ('master', 'slave'))

    def import_time_scopes(self) -> dict:
        """``{token: surface tags}`` as the model stands during the import.

        The scopes are resolved for the run in :meth:`settle_import`, after
        the fusion; the fusion itself has to know which faces a pair names
        before that. The same two routes are read (the source identity map,
        else the version-1 indices into the surfaces as imported), with no
        warning or refusal: those are :meth:`resolve_scopes`'s to make.
        """
        if self.scope_surfaces:
            return self.scope_surfaces
        scoped = self.job.get('scopeEntities') or {}
        if self.entity_tags and scoped.get('surfaces'):
            placed, _missing = self._scopes_by_identity(
                scoped.get('surfaces') or {}, 'surface')
            return placed
        tags = [tag for _dim, tag in self.gmsh.model.getEntities(2)]
        return {str(token): [tags[index] for index in indices or ()
                             if isinstance(index, int)
                             and 0 <= index < len(tags)]
                for token, indices in
                (self.job.get('scopeSurfaces') or {}).items()}

    def _coincident_contacts(self) -> list:
        """Every pair of coincident faces, as sorted ``(face, face)`` tags."""
        groups: dict = {}
        for (_dim, tag), signature in self._entity_signatures((2,)).items():
            groups.setdefault(signature, []).append(int(tag))
        return [tuple(sorted(tags)) for tags in groups.values()
                if len(tags) == 2]

    def _face_volumes(self, tag) -> list:
        try:
            upward, _down = self.gmsh.model.getAdjacencies(2, int(tag))
        except Exception:  # noqa: BLE001 - OCC raises many ways
            return []
        return [int(volume) for volume in upward]

    def _covering_pair(self, contact, pairs, scopes=None):
        first, second = contact
        for pair in pairs:
            master, slave = self._pair_sides(pair, scopes)
            if ((first in master and second in slave)
                    or (second in master and first in slave)):
                return pair
        return None

    def fuse_contacts(self, healing):
        """Make coincident faces one face, contact by contact.

        Plan 49 SR5 (DP-5781, DP-5787). Two solids that share a face import
        as two copies of it, and the volume mesher then fails with "Could not
        recover boundary mesh"; fusing the copies makes the interface
        conformal. Before Plan 49 this was one operation on the whole model
        (``removeAllDuplicates``) or none, so one non-conformal pair unfused
        every contact, and a conformal pair with the healing switch off was
        left two faces and only reported.

        Now each contact is decided on its own. A contact a conformal pair
        names is always fused; a contact a non-conformal pair names is never
        fused; any other contact follows ``removeDuplicateFaces``. Fusing a
        chosen contact is ``occ.fragment`` on the two volumes that meet there
        (MEASURED in SR0 probe 4: fragmenting B with C leaves A | B two
        faces). With no non-conformal pair and the switch on nothing is
        selective, and the old whole-model call is kept unchanged.
        """
        gmsh = self.gmsh
        nonconformal = self.non_conformal_pairs()
        conformal = self.conformal_pairs()
        fuse_rest = bool(healing.get('removeDuplicateFaces', True))
        if not nonconformal and fuse_rest:
            signatures = self._entity_signatures((2, 3))
            before = len(gmsh.model.getEntities(2))
            gmsh.model.occ.removeAllDuplicates()
            gmsh.model.occ.synchronize()
            after = len(gmsh.model.getEntities(2))
            self._follow_duplicate_fusion(signatures)
            if after != before:
                self.duplicate_faces_fused = before - after
                self.ledger.record(
                    'gmsh/healing/removeDuplicateFaces', True, True,
                    note=f'{count_text(before - after, "duplicate face")}'
                         ' fused')
            return
        if not nonconformal and not conformal:
            return
        if nonconformal:
            # Still read by the node weld and the tessellated route: some
            # contact in this model is meshed apart (SG22).
            self.nonconformal_unfused = True
        scopes = self.import_time_scopes()
        fuse_volumes: dict[tuple, str] = {}
        apart_volumes: dict[tuple, str] = {}
        for contact in self._coincident_contacts():
            volumes = sorted({volume for face in contact
                              for volume in self._face_volumes(face)})
            if len(volumes) != 2:
                continue
            key = tuple(volumes)
            apart = self._covering_pair(contact, nonconformal, scopes)
            joined = self._covering_pair(contact, conformal, scopes)
            if apart is not None:
                apart_volumes[key] = self._pair_label(apart)
            elif joined is not None:
                fuse_volumes[key] = self._pair_label(joined)
            elif fuse_rest:
                fuse_volumes.setdefault(key, '')
        clash = sorted(set(fuse_volumes) & set(apart_volumes))
        if clash:
            key = clash[0]
            joined = fuse_volumes[key]
            raise MeshFailure(
                f'the non-conformal interface pair {apart_volumes[key]!r} '
                f'keeps volumes {key[0]} and {key[1]} apart, and '
                + (f'the conformal pair {joined!r} asks'
                   if joined else 'duplicate-face fusion asks')
                + ' for another contact between the same two volumes to be '
                  'one face. Gmsh fragments a pair of volumes at every '
                  'contact or at none, so both cannot be built. Give both '
                  'contacts one coupling, or split a body.')
        if not fuse_volumes:
            self._record_selective_fusion(nonconformal, 0)
            return
        signatures = self._entity_signatures((2, 3))
        before = len(gmsh.model.getEntities(2))
        owners = {int(tag): set(self._face_volumes(tag))
                  for _dim, tag in gmsh.model.getEntities(2)}
        renamed: dict[int, int] = {}
        for first, second in sorted(fuse_volumes):
            first = renamed.get(first, first)
            second = renamed.get(second, second)
            _out, out_map = gmsh.model.occ.fragment([(3, first)],
                                                    [(3, second)])
            for (_dim, old), children in zip(((3, first), (3, second)),
                                             out_map or ()):
                volumes = [int(tag) for dim, tag in children if int(dim) == 3]
                if len(volumes) == 1 and volumes[0] != old:
                    renamed[old] = volumes[0]
            gmsh.model.occ.synchronize()
        after = len(gmsh.model.getEntities(2))

        def successor(volume):
            while volume in renamed and renamed[volume] != volume:
                volume = renamed[volume]
            return volume

        self._follow_duplicate_fusion(
            signatures, owners={face: {successor(volume) for volume in found}
                                for face, found in owners.items()})
        if after != before:
            self.duplicate_faces_fused = before - after
        self._record_selective_fusion(nonconformal, before - after)

    def _record_selective_fusion(self, nonconformal, fused):
        names = ', '.join(repr(self._pair_label(pair))
                          for pair in nonconformal)
        self.ledger.record(
            'gmsh/healing/removeDuplicateFaces', True, bool(fused),
            applied=bool(fused),
            note=(f'{count_text(fused, "duplicate face")} fused contact by '
                  'contact'
                  + (f'; non-conformal pair {names} kept its bodies apart'
                     if names else '')))

    def check_conformal_contacts(self):
        """Refuse a conformal pair whose contact is still two faces.

        Plan 49 SR5, DP-5781. :meth:`fuse_contacts` fragments the pair's
        contact; when its two sides are still two coincident faces, the run
        stops naming the pair rather than meshing a coupling it does not
        have.
        """
        contacts = self._coincident_contacts()
        for pair in self.conformal_pairs():
            master, slave = self._pair_sides(pair)
            if not master or not slave or master & slave:
                continue
            left = [contact for contact in contacts
                    if (contact[0] in master and contact[1] in slave)
                    or (contact[1] in master and contact[0] in slave)]
            if left:
                names = '; '.join(
                    ' and '.join(self.surface_patch_name(tag)[0]
                                 for tag in contact) for contact in left)
                raise MeshFailure(
                    f'the conformal interface pair '
                    f'{self._pair_label(pair)!r} asks for one shared face, '
                    f'and its contact at {names} is still two coincident '
                    'faces after fragmenting. Check that the two bodies '
                    'touch over the whole face.')

    def import_cad_sources(self, sources, healing):
        """Import each CAD source, heal, and fuse duplicate faces.

        The CAD half of :meth:`import_geometry`, shared with the
        resident session (Plan 48 RR, DP-4009): the run and the
        session import, heal and fuse in one place.
        """
        gmsh = self.gmsh
        for index, item in enumerate(sources):
            before_surfaces = {tag for _dim, tag in gmsh.model.getEntities(2)}
            before_volumes = {tag for _dim, tag in gmsh.model.getEntities(3)}
            before = self._entity_counts()
            gmsh.model.occ.importShapes(str(item))
            gmsh.model.occ.synchronize()
            after = self._entity_counts()
            # C31-04. Which tags *this* file brought, not how many. The
            # prepared revision numbers a source's faces from zero within
            # the source, and Gmsh numbers them from wherever the previous
            # file stopped, so the join has to be made here or not at all.
            added_surfaces = sorted(
                tag for _dim, tag in gmsh.model.getEntities(2)
                if tag not in before_surfaces)
            added_volumes = sorted(
                tag for _dim, tag in gmsh.model.getEntities(3)
                if tag not in before_volumes)
            # A CAD solid is one closed shell, so the volumes this file
            # added are the shells it contributed.
            self.import_receipts.append(self._source_receipt(
                index, item, stage='cad',
                surfaces=after[2] - before[2],
                volumes=after[3] - before[3],
                shells=after[3] - before[3],
                added={'surface': added_surfaces,
                       'volume': added_volumes}))
        gmsh.model.occ.synchronize()
        # Repair the imported bodies before the assembly is made
        # conformal: a sliver face that healing would have removed is a
        # face removeAllDuplicates would otherwise try to match.
        self.heal_shapes(healing)
        # Two solids that share a face import as two copies of that face,
        # and the volume mesher then fails with "Could not recover boundary
        # mesh". Fusing the duplicates makes the interface conformal, which
        # is what a multi-region assembly needs.
        self.fuse_contacts(healing)
        # Plan 49 SR5. A free internal-face or baffle sheet, into its volume.
        self.embed_role_sheets()
        # DP-399. Both steps above can take a face away, and neither said
        # so to the identity map. MEASURED on two boxes sharing a face:
        # the fusion drops one tag of each duplicate pair and renumbers
        # nothing, so the identity whose face was the dropped one still
        # points at a tag the model no longer has -- and a prepared name
        # is then carried forward onto a dead tag, where the census, the
        # receipt and the boundary-layer scope all read over the surfaces
        # that remain, agree with each other, and are wrong. This is the
        # first point at which both are done, so it is where they are
        # followed. It costs nothing when nothing moved.
        self.revalidate_scopes('healing')

    def settle_import(self, healing):
        """What follows an import: the checks, the farfield, the scopes.

        Shared with the resident session (Plan 48 RR, DP-4009).
        """
        gmsh = self.gmsh
        volumes = gmsh.model.getEntities(3)
        surfaces = gmsh.model.getEntities(2)
        # MEASURED: a sewn import silently has no volumes and meshes to a
        # surface. Refusing here is the difference between an error and a
        # polyMesh with no cells.
        if self.planar:
            # FC-E. The refusal below is correct for a three-dimensional job
            # and wrong for this one, so the run asks which job it is instead
            # of dropping the check. A section is a face: no volumes is what
            # a correct import looks like here, and volumes are the error.
            mode = self.dimensionality.get('mode') or 'two_d'
            if volumes:
                raise MeshFailure(
                    f'the job asked for a {mode} mesh and the import '
                    f'produced {count_text(len(volumes), "volume")}. This '
                    'route meshes a planar section and extrudes it into '
                    'exactly one cell of '
                    'thickness, so a solid cannot be its input: supply the '
                    'section face, or mesh in three dimensions.')
            if not surfaces:
                raise MeshFailure(
                    f'the job asked for a {mode} mesh and the import produced '
                    'no surfaces, so there is no section to mesh.')
        elif not volumes:
            detail = ''
            if healing.get('sewFaces') and not healing.get('makeSolids'):
                detail = (' Face sewing is enabled and solid reconstruction is '
                          'not: sewing converts closed solids into face sets. '
                          'Turn sewing off, or turn on "rebuild solids" to '
                          'recover a solid from the sewn shell.')
            elif not healing.get('makeSolids'):
                detail = (' If this CAD is a surface model rather than a '
                          'solid, turn on "rebuild solids".')
            raise MeshFailure(
                f'the import produced {len(surfaces)} surfaces and no volumes, '
                f'so there is nothing to mesh.{detail}')

        if self.build_farfield():
            # The cut replaced every entity: the fluid domain is one new
            # volume and the body faces carry new tags. Re-read before
            # anything is measured or named off them.
            volumes = gmsh.model.getEntities(3)
            surfaces = gmsh.model.getEntities(2)

        bounds = gmsh.model.getBoundingBox(-1, -1)
        diagonal = math.dist(bounds[0:3], bounds[3:6])
        self._refresh_receipt_entities()
        self.statistics['import'] = {
            'volumes': len(volumes), 'surfaces': len(surfaces),
            'boundingBox': list(bounds), 'diagonal': diagonal,
            # WP-01 F-35, extended by C31-04. See `self.import_receipts` for
            # the schema.
            'entitySchemaVersion': int(
                self.job.get('entitySchemaVersion') or 1),
            'sources': list(self.import_receipts),
            'topologyChanges': list(self.topology_changes),
        }
        for receipt in self.import_receipts:
            if not receipt['surfaces'] and not receipt['volumes']:
                self.warnings.append(
                    f'{receipt["path"]} contributed no geometry to the model')
        self.resolve_scopes(surfaces, volumes)
        self.check_nonconformal_contacts()
        self.check_conformal_contacts()
        census = self.describe_surfaces([tag for _dim, tag in surfaces])
        self.statistics['import'].update(census['statistics'])
        self.reporter.emit(
            'progress', 'import', 0.15,
            f'imported {count_text(len(volumes), "volume")}, '
            f'{count_text(len(surfaces), "surface")}{census["message"]}',
            {'diagonal_m': round(diagonal, 6), **census['details']})
