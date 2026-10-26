"""Plan 49 SR4: the couplings an exported mesh is given after its split.

OpenFOAM 13 couples two regions non-conformally, and joins a periodic pair
on a snapped mesh, only with ``createNonConformalCouples`` -- and only once
the mesh is split into its regions: ``splitMeshRegions`` drops every coupled
patch made before it, and the positional form of the utility cannot name a
patch in another region (SR0 probe 2). So the export, which is where the
split happens, makes them, from the ``couplings`` the case builder recorded
in the case group manifest.

Engine-agnostic: it reads patch names and region meshes on disk and nothing
about the mesher, so any route that records the same list is coupled the
same way.

The written dictionary is OpenFOAM 13's ``createNonConformalCouplesDict``
(``etc/caseDicts/annotated/createNonConformalCouplesDict``):

* a region interface: ``owner { region <r1>; patch <p1>; }
  neighbour { region <r2>; patch <p2>; }`` -- each region gains a
  ``nonConformalMappedWall`` patch (DP-5761, DP-5762);
* a periodic pair: ``patches (<owner> <neighbour>);`` and its transform,
  with ``region <r>;`` on a multi-region case -- a ``nonConformalCyclic``
  pair.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

DICTIONARY = 'createNonConformalCouplesDict'


class CouplingRefusal(RuntimeError):
    """A coupling the exported mesh cannot be given, named and explained."""


def recorded_couplings(case_path) -> list[dict]:
    """The couplings the mesher of the current mesh recorded; ``[]`` for none.

    snappy records them in the case group manifest, Gmsh in its publication
    record (``core/gmsh/role_publication``). DP-5762 / DP-5790: the export
    read only the manifest, so a Gmsh-meshed interface was never coupled.
    Both records outlive a run of the other mesher, so the one read is the
    publisher's, named by the patch-identity sidecar; with two records and
    no publisher on record, which one applies is not guessed.
    """
    from foammesh.core.gmsh.role_publication import \
        recorded_couplings as gmsh_couplings
    from foammesh.openfoam.case_builder import group_manifest_path

    snappy = _manifest_couplings(group_manifest_path(case_path))
    gmsh = gmsh_couplings(case_path)
    engine = _publishing_engine(case_path)
    if engine == 'gmsh':
        return gmsh
    if engine == 'snappy':
        return snappy
    if snappy and gmsh:
        raise CouplingRefusal(
            'the case holds couplings recorded by both snappyHexMesh and '
            'Gmsh, and no record says which mesher published the current '
            'mesh; run the mesher again before exporting.')
    return snappy or gmsh


def _manifest_couplings(path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    couplings = manifest.get('couplings') if isinstance(manifest, dict) \
        else None
    return [item for item in (couplings or ()) if isinstance(item, dict)]


def _publishing_engine(case_path) -> str:
    """``gmsh``/``snappy`` from the patch-identity sidecar, else ``''``."""
    from foammesh.core.quality import patch_identity

    path = (Path(case_path) / 'foammesh' / 'quality' / 'geometry'
            / patch_identity.IDENTITY_FILENAME)
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return ''
    return str(document.get('engine_id') or '') \
        if isinstance(document, dict) else ''


def boundary_names(path) -> list[str]:
    """The patch names of one ``polyMesh/boundary`` file, in order."""
    path = Path(path)
    if not path.is_file():
        return []
    text = path.read_text(encoding='utf-8', errors='replace')
    start = text.find('(', text.find('}', text.find('FoamFile'))
                      if 'FoamFile' in text else 0)
    body = text[start + 1:] if start >= 0 else text
    names, depth, word = [], 0, ''
    for token in re.findall(r'[{}]|[^\s{}();]+', body):
        if token == '{':
            if depth == 0 and word:
                names.append(word)
            depth += 1
        elif token == '}':
            depth -= 1
        elif depth == 0:
            word = token
    return names


def rename_patch(path, old: str, new: str) -> None:
    """Rename one patch of a ``polyMesh/boundary`` file in place."""
    path = Path(path)
    # The file's own line endings are kept: OpenFOAM reads it in WSL. A
    # blank line may stand between the name and its brace (PyFoam's layout).
    with open(path, encoding='utf-8', newline='') as handle:
        text = handle.read()
    pattern = re.compile(
        rf'(^[ \t]*){re.escape(old)}([ \t]*(?:\r?\n[ \t]*)+\{{)',
        re.MULTILINE)
    replaced, count = pattern.subn(rf'\g<1>{new}\g<2>', text, count=1)
    if count != 1:
        raise CouplingRefusal(f'patch {old} was not found in {path}')
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        handle.write(replaced)


def _vector(values) -> str:
    return '(' + ' '.join(f'{float(value):.17g}' for value in values) + ')'


def _where(found: dict, patch: str) -> list:
    return [region for region, names in found.items() if patch in names]


def plan(couplings, meshes: dict) -> tuple[list[tuple], list[str]]:
    """``(renames, entries)`` for ``couplings`` on the region meshes.

    ``meshes`` maps a region name (``None`` for a single-region case) to its
    ``polyMesh/boundary`` path. ``renames`` is ``(region, old, new)`` per
    patch to rename before the utility runs; ``entries`` are the dictionary
    entries, one per coupling. Refuses -- naming the coupling, the reason and
    the remedy -- what the mesh cannot be given.
    """
    found = {region: boundary_names(path) for region, path in meshes.items()}
    multi = len(found) > 1 or None not in found
    renames, entries = [], []
    for coupling in couplings:
        kind = str(coupling.get('kind') or '')
        name = str(coupling.get('name') or 'coupling')
        owner = str(coupling.get('owner_patch') or '')
        neighbour = str(coupling.get('neighbour_patch') or '')
        if kind == 'non_conformal':
            baffle = str(coupling.get('baffle_patch') or f'{owner}_slave')
            if not multi:
                raise CouplingRefusal(
                    f'Interface {name}: a non-conformal region interface '
                    'joins two regions, and this export has one region, so '
                    'its two sides would stay two uncoupled patches. Add the '
                    'two regions it joins (a point inside each), or make the '
                    'surface a blocking baffle or an internal face.')
            owner_at = _where(found, owner)
            other = baffle if _where(found, baffle) else neighbour
            other_at = _where(found, other)
            if len(owner_at) != 1 or len(other_at) != 1:
                raise CouplingRefusal(
                    f'Interface {name}: the exported mesh does not hold its '
                    f'two sides once each ({owner}: '
                    f'{", ".join(map(str, owner_at)) or "none"}; {other}: '
                    f'{", ".join(map(str, other_at)) or "none"}). Mesh the '
                    'case again so the interface is cut, then export.')
            first, second = owner_at[0], other_at[0]
            if first == second:
                raise CouplingRefusal(
                    f'Interface {name}: both of its sides ended in region '
                    f'{first}, so it joins no two regions. Check the region '
                    'points: one must lie on each side of the interface.')
            owner_zone = coupling.get('owner_zone')
            neighbour_zone = coupling.get('neighbour_zone')
            swapped = ((owner_zone and first != owner_zone
                        and second == owner_zone)
                       or (neighbour_zone and first == neighbour_zone
                           and second != neighbour_zone))
            if swapped:
                # snappy put the owner surface's name on the side that lies
                # in the neighbour's region: the names follow the regions.
                renames.append((first, owner, neighbour))
                if other != owner:
                    renames.append((second, other, owner))
                first, second = second, first
            elif other != neighbour:
                renames.append((second, other, neighbour))
            entries.append(
                f'{name}\n{{\n'
                f'    owner\n    {{\n        region {first};\n'
                f'        patch {owner};\n    }}\n'
                f'    neighbour\n    {{\n        region {second};\n'
                f'        patch {neighbour};\n    }}\n}}\n')
        elif kind == 'periodic':
            owner_at, neighbour_at = _where(found, owner), _where(found, neighbour)
            common = [region for region in owner_at if region in neighbour_at]
            if len(common) != 1:
                raise CouplingRefusal(
                    f'Periodic pair {name}: its two sides {owner} and '
                    f'{neighbour} are not both in one region of the exported '
                    'mesh, and a periodic pair couples one region to itself. '
                    'Pair two boundaries of the same region.')
            lines = [f'{name}\n{{\n']
            if common[0] is not None:
                lines.append(f'    region {common[0]};\n')
            lines.append(f'    patches ({owner} {neighbour});\n')
            transform = str(coupling.get('transform') or '')
            if transform == 'translational':
                lines.append('    transform translational;\n')
                lines.append(
                    f'    separation {_vector(coupling.get("separation"))};\n')
            elif transform == 'rotational':
                lines.append('    transform rotational;\n')
                lines.append(f'    rotationAxis '
                             f'{_vector(coupling.get("rotationAxis"))};\n')
                lines.append(f'    rotationCentre '
                             f'{_vector(coupling.get("rotationCentre"))};\n')
                lines.append(f'    rotationAngle '
                             f'{float(coupling.get("rotationAngle")):.17g};\n')
            else:
                raise CouplingRefusal(
                    f'Periodic pair {name}: it has no translation or rotation.'
                    ' Give the pair its transform.')
            lines.append('}\n')
            entries.append(''.join(lines))
    return renames, entries


def dictionary_text(entries) -> str:
    header = ('FoamFile\n{\n    format      ascii;\n    class       dictionary;\n'
              '    location    "system";\n'
              f'    object      {DICTIONARY};\n}}\n\n')
    return header + '\n'.join(entries)


def write(case_root, entries) -> Path:
    path = Path(case_root) / 'system' / DICTIONARY
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dictionary_text(entries), encoding='ascii', newline='\n')
    return path


def coupled_patch_types(meshes: dict) -> dict:
    """``{region: {patch: type}}`` of the non-conformal patches on disk."""
    result = {}
    for region, path in meshes.items():
        path = Path(path)
        if not path.is_file():
            continue
        text = path.read_text(encoding='utf-8', errors='replace')
        found = {}
        for match in re.finditer(
                r'^[ \t]*(\S+)[ \t]*(?:\r?\n[ \t]*)+\{[^}]*?\btype[ \t]+'
                r'(nonConformal\w*)[ \t]*;', text, re.MULTILINE):
            found[match.group(1)] = match.group(2)
        result[region] = found
    return result
