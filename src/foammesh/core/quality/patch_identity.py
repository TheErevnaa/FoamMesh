"""The join between a published patch and the prepared geometry it came from.

Plan 23 WP2. §4 makes ``patch_uuid`` the stable identity of a boundary section,
but a published ``constant/polyMesh`` carries only solver *names* -- the writer
takes patch identity from ``stable_id``/``solver_name``, and nothing downstream
can recover a UUID from a name alone.

The join is *checkable* against the prepared group manifest, which is the
property this module turns into an artifact: a sidecar written beside the
mesh, mapping each published patch to the prepared patch it represents, with
the name verified rather than assumed.

R97. A solver name used to be minted ``<display name>_<sha256(uuid)[:8]>``
unconditionally, and the check here was simply that suffix. The user never
typed those eight hex digits and never saw them, so the store now hands the
solver the display name itself and keeps the suffix for the patches whose
names actually collide. Both forms are therefore legitimate, and the check is
that the published name is one of the two the manifest's own group would
produce -- a name minted for some other patch still fails it.

Both engines converge here. Gmsh reaches a solver name through
``publish.patch_metadata_from``; snappy reaches the same name through
``case_builder._surface_groups``. Neither needs a new mapping -- both need the
existing one persisted and checked.

**Fabricated identity is recorded, never accepted.** Four sites in the tree
invent an identity when prepared data is missing: ``publish.py`` names an
unnamed physical group ``patch_<id>`` and defaults its category to ``wall``,
and ``case_builder`` twice substitutes an STL filename stem or a bare string
for a UUID. A patch whose identity was invented is not a patch that can be
qualified, so it is marked ``fabricated`` and forces the section ``unrated``
rather than silently joining to nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Mapping

IDENTITY_SCHEMA_VERSION = 1

#: Name of the sidecar, written beside the report that consumes it.
IDENTITY_FILENAME = 'patch-identity.json'


class PatchIdentityError(ValueError):
    pass


def solver_name_suffix(patch_uuid: str) -> str:
    """The 8 hex characters ``_solver_name`` appends to a display name."""
    return hashlib.sha256(str(patch_uuid).encode('utf-8')).hexdigest()[:8]


def join_is_verifiable(solver_name: str, patch_uuid: str,
                       display_name: str = '') -> bool:
    """True when ``solver_name`` provably belongs to ``patch_uuid``.

    This is what makes the sidecar evidence rather than an assertion: a
    mismatched pair is detectable without consulting anything else.

    R97. Two forms are minted now. A patch whose display name is unique among
    the prepared groups carries that name and nothing else -- the whole point
    of the row -- so it is verified against ``display_name``; one that shares
    its name with another patch still carries the UUID suffix, and is verified
    against that. A name built from a *different* UUID matches neither.
    """
    if str(solver_name).endswith('_' + solver_name_suffix(patch_uuid)):
        return True
    from foammesh.core.geometry.prepared import solver_stem

    return bool(display_name) and str(solver_name) == solver_stem(display_name)


@dataclass(frozen=True)
class PatchIdentity:
    """One published patch and the prepared patch it represents."""

    solver_name: str
    patch_uuid: str | None = None
    display_name: str = ''
    category: str = ''
    #: ``prepared`` when joined to the group manifest and verified;
    #: ``background`` for a face of the background block, which represents no
    #: imported surface at all; ``fabricated`` when some layer invented the
    #: identity.
    origin: str = 'prepared'
    reason: str = ''
    #: For a background face: ``authored`` when a person named it and said
    #: what it is for, ``generated`` when it still carries the label this
    #: product invented. Empty for every other origin.
    naming: str = ''
    #: For a background face: which face of which block it is.
    block_face: str = ''

    @property
    def rated(self) -> bool:
        """Whether this patch may carry a rated fidelity verdict.

        A background face never can, whoever named it: fidelity asks how well
        the mesh reproduces an imported surface, and this face reproduces no
        surface. That is a different statement from ``fabricated``, which says
        the identity was invented and cannot be trusted -- see
        :meth:`IdentityMap.unrated_reason`, which now tells the two apart.
        """
        return self.origin == 'prepared' and bool(self.patch_uuid)

    def to_dict(self) -> dict:
        return {
            'solver_name': self.solver_name, 'patch_uuid': self.patch_uuid,
            'display_name': self.display_name, 'category': self.category,
            'origin': self.origin, 'reason': self.reason,
            'naming': self.naming, 'block_face': self.block_face,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> 'PatchIdentity':
        return cls(
            solver_name=str(value['solver_name']),
            patch_uuid=value.get('patch_uuid') or None,
            display_name=str(value.get('display_name') or ''),
            category=str(value.get('category') or ''),
            origin=str(value.get('origin') or 'prepared'),
            reason=str(value.get('reason') or ''),
            naming=str(value.get('naming') or ''),
            block_face=str(value.get('block_face') or ''))


@dataclass(frozen=True)
class IdentityMap:
    """Every published patch of one mesh, joined to one prepared revision."""

    engine_id: str
    entries: tuple[PatchIdentity, ...] = ()
    prepared_revision_id: str | None = None
    prepared_fingerprint: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def rated(self) -> bool:
        """A rated check needs a prepared revision *and* a clean join.

        Snappy can mesh with no prepared geometry at all
        (``requires_prepared_geometry`` is ``False``), so this is routinely and
        legitimately false. The verdict is then ``unrated`` -- runnable, never
        green.
        """
        return bool(self.prepared_revision_id and self.entries
                    and all(item.rated for item in self.entries))

    @property
    def fabricated(self) -> tuple[PatchIdentity, ...]:
        return tuple(item for item in self.entries if item.origin == 'fabricated')

    @property
    def background(self) -> tuple[PatchIdentity, ...]:
        """The faces of the background block, which no import produced."""
        return tuple(item for item in self.entries
                     if item.origin == 'background')

    def get(self, solver_name: str) -> PatchIdentity | None:
        for item in self.entries:
            if item.solver_name == solver_name:
                return item
        return None

    def unrated_reason(self) -> str:
        """Why this mesh cannot carry a rated fidelity verdict, in words."""
        if self.rated:
            return ''
        if not self.prepared_revision_id:
            return ('no prepared geometry revision is current for this mesh, so '
                    'no boundary section can be joined to a stable patch UUID')
        if not self.entries:
            return 'the published mesh declares no boundary patches'
        parts = []
        if self.fabricated:
            names = ', '.join(item.solver_name for item in self.fabricated)
            parts.append(f'identity was fabricated for: {names}')
        if self.background:
            # Plan 31 CP-07 item 3. These used to land in the sentence above,
            # which read as an accusation against six faces that are exactly
            # what they should be. Saying which of them a person named is the
            # difference between "you have not finished naming your boundary"
            # and "something invented an identity here".
            authored = [item.solver_name for item in self.background
                        if item.naming == 'authored']
            generated = [item.solver_name for item in self.background
                         if item.naming != 'authored']
            said = ('the faces of the background block carry no imported '
                    'surface, so they cannot be rated for fidelity: ')
            detail = []
            if authored:
                detail.append('named by you: ' + ', '.join(sorted(authored)))
            if generated:
                detail.append(
                    'still carrying the names this product generated: '
                    + ', '.join(sorted(generated)))
            parts.append(said + '; '.join(detail))
        return '. '.join(parts)

    def to_dict(self) -> dict:
        return {
            'schema_version': IDENTITY_SCHEMA_VERSION,
            'engine_id': self.engine_id,
            'prepared_revision_id': self.prepared_revision_id,
            'prepared_fingerprint': self.prepared_fingerprint,
            'rated': self.rated,
            'unrated_reason': self.unrated_reason(),
            'warnings': list(self.warnings),
            'patches': [item.to_dict() for item in self.entries],
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> 'IdentityMap':
        if int(value.get('schema_version', 0)) != IDENTITY_SCHEMA_VERSION:
            raise PatchIdentityError(
                'unsupported patch-identity schema version')
        return cls(
            engine_id=str(value.get('engine_id') or ''),
            entries=tuple(PatchIdentity.from_dict(item)
                          for item in value.get('patches', ())),
            prepared_revision_id=value.get('prepared_revision_id') or None,
            prepared_fingerprint=value.get('prepared_fingerprint') or None,
            warnings=tuple(str(item) for item in value.get('warnings', ())))


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

def prepared_index(group_manifest: Mapping | None) -> dict[str, dict]:
    """Solver name -> prepared group, for the groups that declare both."""
    index: dict[str, dict] = {}
    for group in (group_manifest or {}).get('groups', ()) or ():
        if not isinstance(group, dict):
            continue
        name = str(group.get('solver_name') or '').strip()
        if name:
            index[name] = group
    return index


def background_index(group_manifest: Mapping | None) -> dict[str, dict]:
    """Patch name -> background ownership record, from the case manifest.

    Plan 31 CP-07 item 3. The background block's faces are in the mesh and in
    no prepared group, which is why they used to come out ``fabricated``. The
    case builder now publishes what they are, and this is the lookup that lets
    the join say so.
    """
    index: dict[str, dict] = {}
    for item in (group_manifest or {}).get('background_boundaries', ()) or ():
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        if name:
            index[name] = item
    return index


def build(engine_id: str, *, published_patches: Iterable[Mapping],
          group_manifest: Mapping | None = None,
          prepared_revision_id: str | None = None,
          prepared_fingerprint: str | None = None,
          background_boundaries: Mapping | None = None) -> IdentityMap:
    """Join published patches to prepared groups, verifying every match.

    ``published_patches`` is any iterable of mappings carrying at least a
    ``solver_name``; the publisher's own patch records satisfy this. A patch
    the manifest does not know, or one whose name does not carry the expected
    UUID suffix, is recorded as ``fabricated`` with the reason -- it is never
    dropped and never silently joined.
    """
    index = prepared_index(group_manifest)
    background = background_index(
        background_boundaries if background_boundaries is not None
        else group_manifest)
    entries: list[PatchIdentity] = []
    warnings: list[str] = []
    for record in published_patches:
        solver_name = str(
            record.get('solver_name') or record.get('name') or '').strip()
        if not solver_name:
            warnings.append('a published patch has no solver name')
            continue

        declared = str(record.get('identity_origin') or '').strip()
        group = index.get(solver_name)
        owned = background.get(solver_name)
        if owned is not None and declared != 'fabricated' and group is None:
            # Plan 31 CP-07 item 3. A face of the background block. It is not
            # prepared geometry and never will be, so it is still not rated --
            # but it is not fabricated either, and the record keeps the one
            # thing a reader has to know: whether the name came from a person
            # or from this product. Without that, `xMin` and a user's `inlet`
            # are indistinguishable downstream, which is precisely how a
            # generated label ends up presented as somebody's intended inlet.
            naming = str(owned.get('naming') or 'generated')
            role = str(owned.get('role') or '')
            reason = (
                f'{solver_name} is a face of the background block'
                + (f', named by you and declared {role}' if naming == 'authored'
                   and role not in ('', 'unassigned')
                   else f', named by you' if naming == 'authored'
                   else ', still carrying the name this product generated for '
                        'it')
                + '; it reproduces no imported surface, so it carries no '
                  'prepared patch UUID')
            entries.append(PatchIdentity(
                solver_name=solver_name,
                display_name=str(record.get('name') or solver_name),
                category=('' if role == 'unassigned' else role)
                         or str(record.get('category') or ''),
                origin='background', reason=reason, naming=naming,
                block_face=str(owned.get('block_face') or '')))
            continue
        if declared == 'fabricated' or group is None:
            reason = (str(record.get('identity_reason') or '')
                      or f'{solver_name} is not declared in the prepared group '
                         'manifest')
            entries.append(PatchIdentity(
                solver_name=solver_name,
                display_name=str(record.get('name') or solver_name),
                category=str(record.get('category') or ''),
                origin='fabricated', reason=reason))
            warnings.append(reason)
            continue

        patch_uuid = str(group.get('patch_uuid') or '').strip()
        if not patch_uuid:
            reason = f'prepared group {solver_name} carries no patch UUID'
            entries.append(PatchIdentity(
                solver_name=solver_name,
                display_name=str(group.get('display_name') or solver_name),
                category=str(group.get('category') or ''),
                origin='fabricated', reason=reason))
            warnings.append(reason)
            continue

        if not join_is_verifiable(solver_name, patch_uuid,
                                  str(group.get('display_name') or '')):
            # The name and the UUID disagree. Accepting this would attach a
            # section report to the wrong geometry, which is worse than
            # declining to rate it.
            reason = (f'{solver_name} does not carry the '
                      f'{solver_name_suffix(patch_uuid)} suffix its prepared '
                      'patch UUID requires')
            entries.append(PatchIdentity(
                solver_name=solver_name, patch_uuid=None,
                display_name=str(group.get('display_name') or solver_name),
                category=str(group.get('category') or ''),
                origin='fabricated', reason=reason))
            warnings.append(reason)
            continue

        entries.append(PatchIdentity(
            solver_name=solver_name, patch_uuid=patch_uuid,
            display_name=str(group.get('display_name') or solver_name),
            category=str(group.get('category') or record.get('category') or ''),
            origin='prepared'))

    duplicates = sorted({
        item.solver_name for item in entries
        if sum(1 for other in entries
               if other.solver_name == item.solver_name) > 1})
    if duplicates:
        raise PatchIdentityError(
            f'published mesh declares duplicate patch names: {duplicates}')

    return IdentityMap(
        engine_id=str(engine_id), entries=tuple(entries),
        prepared_revision_id=prepared_revision_id or None,
        prepared_fingerprint=prepared_fingerprint or None,
        warnings=tuple(warnings))


def from_group_manifest(engine_id: str, group_manifest: Mapping, *,
                        prepared_revision_id: str | None = None,
                        prepared_fingerprint: str | None = None) -> IdentityMap:
    """Build directly from a prepared manifest, for the snappy path.

    Snappy's patch names come from ``case_builder._surface_groups``, which
    already carries the prepared group verbatim, so the manifest alone is a
    faithful description of what will be published.
    """
    groups = [group for group in (group_manifest or {}).get('groups', ()) or ()
              if isinstance(group, dict)]
    return build(
        engine_id,
        published_patches=[{
            'solver_name': group.get('solver_name'),
            'name': group.get('display_name'),
            'category': group.get('category'),
        } for group in groups],
        group_manifest=group_manifest,
        prepared_revision_id=prepared_revision_id,
        prepared_fingerprint=prepared_fingerprint)


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def write(path: str | Path, identity: IdentityMap) -> Path:
    """Write the sidecar atomically, so a crash cannot leave a partial join."""
    path = Path(path)
    if path.is_dir() or path.name != IDENTITY_FILENAME:
        path = path / IDENTITY_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(
        json.dumps(identity.to_dict(), indent=2, sort_keys=True) + '\n',
        encoding='utf-8')
    os.replace(temporary, path)
    return path


def read(path: str | Path) -> IdentityMap:
    path = Path(path)
    if path.is_dir():
        path = path / IDENTITY_FILENAME
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except OSError as error:
        raise PatchIdentityError(
            f'patch identity sidecar could not be read: {path}') from error
    except json.JSONDecodeError as error:
        raise PatchIdentityError(
            f'patch identity sidecar is not valid JSON: {path}') from error
    return IdentityMap.from_dict(document)
