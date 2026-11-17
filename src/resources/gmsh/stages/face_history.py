"""Face identity through a Boolean, read from the kernel's own map (Plan 50
IP29, GM-9, DP-6324).

A Boolean renumbers faces. The import's patch names and scopes are keyed on
the faces as they were imported, so after the farfield cut and the contact
fusion each new face had to be matched back to the face it came from. That
match was by centre of mass and area, which is right only while a face comes
through whole: a face the Boolean splits in two has neither child at the
parent's centre or area, so both lost the parent's name, and two different
faces with one centre and area (two coincident twins) could not be told
apart.

``gmsh.model.occ.fragment`` returns, for every input it is given, the
entities that input became (``outDimTagsMap``). Faces are not inputs of a
volume Boolean, so the map said nothing about them; passing a volume's own
boundary faces as further inputs changes nothing about the result (they are
already its boundary) and makes the map say, for each face, which faces it
became. Each child keeps its parent's identity, so a face split in two keeps
its name on both halves. A face the map does not account for is still matched
by centre and area, as a fallback, and ``statistics['faceIdentity']`` counts
both.

Gmsh's ``cut`` cannot take faces as tools of a volume (OpenCASCADE refuses a
tool of lower dimension than the object in a cut), so the farfield is made
as a fragment of the primitive with the bodies and their faces, with the
pieces inside a body removed afterwards: the same volume the cut leaves.
"""
from __future__ import annotations

#: Plan 50 IP29. Read face identity from the Boolean's own map. ``False``
#: goes back to the plain cut and fragment with centre-and-area matching
#: alone (the behaviour before IP29).
FACE_HISTORY_BOOLEANS = True


def boundary_faces(gmsh, volumes) -> list:
    """The faces bounding *volumes* (``[(3, tag), ...]``), each once."""
    seen = []
    for dim, tag in gmsh.model.getBoundary(list(volumes), combined=False,
                                           oriented=False, recursive=False):
        tag = abs(int(tag))
        if int(dim) == 2 and tag not in seen:
            seen.append(tag)
    return seen


def children_by_input(out_map, inputs, dimension) -> dict:
    """``{input tag: [child tags of *dimension*]}`` from ``outDimTagsMap``.

    *inputs* are the dimtags the Boolean was given, objects then tools, in
    the order the map is in; only inputs of *dimension* are returned.
    """
    children = {}
    for (dim, tag), result in zip(inputs, out_map or ()):
        if int(dim) != dimension:
            continue
        children[int(tag)] = sorted({int(t) for d, t in result
                                     if int(d) == dimension})
    return children


def follow(lineage: dict, mapped: dict) -> dict:
    """Carry *lineage* ``{original: [current]}`` through one Boolean.

    *mapped* is ``{current input: [children]}`` of that Boolean; a current
    tag that was not an input of it is unchanged.
    """
    out = {}
    for original, current in lineage.items():
        after = []
        for tag in current:
            after.extend(mapped.get(tag, [tag]))
        out[original] = sorted(set(after))
    return out


def record(statistics, operation, *, by_map=0, by_fallback=0, unmatched=0,
           map_used=False, note='') -> dict:
    """Add one Boolean's counts to ``statistics['faceIdentity']``.

    ``byMap``: faces whose identity the kernel's map gave. ``byFallback``:
    faces matched by centre of mass and area because the map did not cover
    them (or was not read). ``unmatched``: faces neither placed, which the
    operation itself made (the farfield's own faces) or could not follow.
    A stage with no statistics (a bare test double) records nothing.
    """
    if statistics is None:
        return None
    block = statistics.setdefault('faceIdentity', {
        'byMap': 0, 'byFallback': 0, 'unmatched': 0, 'operations': []})
    block['byMap'] += int(by_map)
    block['byFallback'] += int(by_fallback)
    block['unmatched'] += int(unmatched)
    row = {'operation': operation, 'mapUsed': bool(map_used),
           'byMap': int(by_map), 'byFallback': int(by_fallback),
           'unmatched': int(unmatched)}
    if note:
        row['note'] = str(note)
    block['operations'].append(row)
    return block
