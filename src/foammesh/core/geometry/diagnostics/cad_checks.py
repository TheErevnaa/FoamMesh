"""Lazy native CAD findings used alongside tessellation diagnostics."""
from __future__ import annotations

from foammesh.core.quantities import agreeing, count_text

from .checks import Finding, Severity


#: A boolean intersection of two solids that merely touch comes back with a
#: shell but no volume. Measured on OCCT 7.8.1: two unit cubes sharing a face
#: give a common volume of 0.0, the same pair overlapped by half give
#: 0.4999999999999999, and two cubes a unit apart give 0.0 as well, with a
#: distance of 1.0. So the distance alone cannot tell contact from
#: interpenetration -- it is 0.0 for both -- and the shared volume can.
#: The fraction below is the sliver a contact is allowed to report before it
#: counts as interpenetration: a shared volume under a billionth of the
#: smaller body is boolean noise on a curved contact patch, not one body
#: growing into another. On annulus_shell.step, whose two solids share an
#: interface, the shared volume is exactly 0.0 and the whole measurement
#: costs 15 ms.
_CONTACT_VOLUME_FRACTION = 1e-9


def _volume_of(shape) -> float:
    from OCC.Core.BRepGProp import brepgprop
    from OCC.Core.GProp import GProp_GProps
    props = GProp_GProps()
    brepgprop.VolumeProperties(shape, props)
    return abs(float(props.Mass()))


def _shared_volume(left, right):
    """Volume the two solids have in common, or ``None`` if OCCT refused.

    ``None`` is not "the bodies do not overlap" -- it is "the boolean did
    not answer", and the caller keeps such a pair on the overlapping side,
    which is where this check put every contacting pair before the shared
    volume was measured at all.
    """
    from OCC.Core.BRepAlgoAPI import BRepAlgoAPI_Common
    common = BRepAlgoAPI_Common(left, right)
    common.Build()
    if not common.IsDone():
        return None
    return _volume_of(common.Shape())


def _overlap_floor(tolerance: float, volumes) -> float:
    """The smallest shared volume that still means interpenetration."""
    smallest = min((value for value in volumes if value > 0.0), default=0.0)
    return max(float(tolerance) ** 3, _CONTACT_VOLUME_FRACTION * smallest)


def _body_pairs(bodies, *, tolerance, pair_limit, intersection_limit):
    """Sort the body pairs that are not apart into overlaps and contacts.

    The distance is the cheap screen: pairs that stand apart never reach the
    boolean. Pairs that do not stand apart are then separated by the volume
    they share, because a conjugate assembly is exactly a set of bodies that
    touch and share no volume, and routing one to a wrap fuses the regions
    the user named into a single one.
    """
    from OCC.Core.BRepExtrema import BRepExtrema_DistShapeShape
    overlaps, contacts, evaluated, intersections = [], [], 0, 0
    own_volume: dict[int, float] = {}
    for left in range(len(bodies)):
        for right in range(left + 1, len(bodies)):
            if evaluated >= pair_limit:
                break
            evaluated += 1
            distance = BRepExtrema_DistShapeShape(bodies[left], bodies[right])
            distance.Perform()
            if not (distance.IsDone() and float(distance.Value()) <= tolerance):
                continue
            pair = {'body_a': left, 'body_b': right,
                    'distance': float(distance.Value())}
            if intersections >= intersection_limit:
                # Out of boolean budget. The pair is known to be in contact
                # and unknown beyond that, and unknown stays on the side the
                # check used to report for every contact.
                pair['shared_volume'] = None
                overlaps.append(pair)
                continue
            intersections += 1
            for index in (left, right):
                if index not in own_volume:
                    own_volume[index] = _volume_of(bodies[index])
            shared = _shared_volume(bodies[left], bodies[right])
            pair['shared_volume'] = shared
            floor = _overlap_floor(
                tolerance, (own_volume[left], own_volume[right]))
            if shared is None or shared > floor:
                overlaps.append(pair)
            else:
                contacts.append(pair)
        if evaluated >= pair_limit:
            break
    complete = evaluated < pair_limit and intersections < intersection_limit
    return overlaps, contacts, evaluated, complete


def check_cad(shape, *, tolerance: float | None = None, pair_limit: int = 200,
              intersection_limit: int = 64, unit_factor: float = 1.0):
    """Native CAD findings for *shape*.

    DP-530. *unit_factor* is metres per unit of the shape's coordinates
    (0.001 for a STEP or IGES read). A *tolerance* given is in metres and is
    converted into those units for OCCT; left out, the check runs at OCCT's
    own confusion scale in the shape's units. The census the findings carry
    is in metres either way -- it is what the Repair plan suggests a
    tolerance from, and its edge length is reported under the unit ``m``.
    """
    from foammesh.core.geometry.cad.healing_pipeline import OcctHealingBackend, _analyze
    backend = OcctHealingBackend(unit_factor)
    tolerance = backend.applied_tolerance({'tolerance': tolerance})
    census = _analyze(shape, tolerance, backend.unit_factor)
    free = census['free_closed_wires'] + census['free_open_wires']
    invalid = int(not census['valid'])
    validity_count = invalid + free
    findings = [Finding(
        'cad_validity', validity_count,
        Severity.ERROR if validity_count else Severity.OK,
        ('CAD B-Rep is valid and has no free wires.' if not validity_count else
         f"CAD B-Rep has {count_text(free, 'free wire')}; "
         f"valid={census['valid']}."),
        repairable_by=('cad.fix_shape', 'cad.sew', 'cad.fix_wireframe'),
        engine_impact={'snappy': 'Invalid or unsewn B-Rep creates a leaking tessellation.'},
        details={'census': census})]
    small = census['small_faces'] + census['small_edges']
    findings.append(Finding(
        'cad_small_features', small,
        Severity.WARNING if small else Severity.OK,
        f"{count_text(census['small_faces'], 'small CAD face')}, "
        f"{count_text(census['small_edges'], 'small edge')}.",
        # DP-223. This reported whichever of a face area and an edge
        # length was the smaller number, which weighs a square metre
        # against a metre and is not a quantity at all. The length is
        # the one a column can state; the area stays in the census.
        characteristic_size=census['edge_length_min'],
        characteristic_unit='m',
        repairable_by=('cad.fix_wireframe', 'cad.remove_small_faces') if small else (),
        engine_impact={'snappy': 'Sub-cell CAD details can produce noisy surface refinement.'},
        details={'census': census}))

    bodies = backend.split_bodies(shape)
    overlaps, contacts, evaluated, complete = [], [], 0, True
    if len(bodies) > 1:
        overlaps, contacts, evaluated, complete = _body_pairs(
            bodies, tolerance=tolerance, pair_limit=pair_limit,
            intersection_limit=intersection_limit)
    counts = {'pairs_evaluated': evaluated, 'pair_limit': pair_limit,
              'intersection_limit': intersection_limit}
    findings.append(Finding(
        'overlapping_shells', len(overlaps),
        Severity.ERROR if overlaps else Severity.OK,
        f"{count_text(len(overlaps), 'interpenetrating CAD body pair')}.",
        repairable_by=(), evaluated=complete,
        engine_impact={
            'snappy': 'Interpenetrating bodies are a canonical wrap candidate.'},
        details={'pairs': overlaps[:50], **counts}))
    findings.append(Finding(
        'touching_shells', len(contacts),
        Severity.INFO if contacts else Severity.OK,
        f"{count_text(len(contacts), 'CAD body pair')} "
        f"{agreeing(len(contacts), 'touches', 'touch')} without "
        'interpenetrating.',
        repairable_by=(), evaluated=complete,
        engine_impact={
            'gmsh': 'Touching bodies are a conjugate assembly: the shared '
                    'face is an interface between named regions, not a defect.'},
        details={'pairs': contacts[:50], **counts}))
    return findings
