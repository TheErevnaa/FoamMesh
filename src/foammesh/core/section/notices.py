"""What a section on screen is, in the words the section tool shows.

Plan 37 UF6 (DP-1037). Above 200 000 cells the preview is the boundary only
(Plan 35 CR3). A clip of that hollows the shells and a slice of it is lines,
and nothing said so: the picture looked like a section through cells. The
section tool now says which picture it is. The words are the plan's.
"""
from __future__ import annotations

#: The cut goes through cells (a volume is loaded) or through a closed
#: geometry surface that is exactly what it is.
EXACT = 'Exact section'
#: The mesh on screen is its boundary only; there are no cells to cut.
BOUNDARY_ONLY = 'Boundary-only preview'
#: The cap was filled from a decimated surface.
APPROXIMATE_CAP = 'Approximate cap — no cell data'
#: A large-mesh drag: the cut is made when the handle is let go.
COMPUTING = 'Computing section'
#: A surface the plane crosses does not close into loops, so no cap is drawn.
CAP_UNAVAILABLE = 'Cap unavailable'
#: Plan 37 UF10. The section worker refused; the section on screen stays.
NOT_COMPUTED = 'Section not computed'
#: Plan 37 UF8. A large clip being dragged: the preview on screen is cut by
#: the renderer, not a whole-cell result.
PREVIEW_WHILE_MOVING = 'Preview while moving'
#: Plan 37 UF8. The exact section on screen is where the plane was.
PREVIOUS_POSITION = 'Section at previous position'

#: Cap outcomes (`rendering.section_fill.sectionCap`), repeated here so this
#: module needs nothing but itself.
CAP_CLOSED = 'closed'
CAP_APPROXIMATE = 'approximate'
CAP_OPEN = 'open'
CAP_MISSED = 'missed'

#: What each notice means, for its tooltip.
EXPLANATIONS = {
    EXACT: 'The section is cut through the cells that are loaded, or through '
           'a closed surface as it is.',
    BOUNDARY_ONLY: 'Only the mesh boundary is loaded (the mesh is above the '
                   'preview\'s automatic volume limit), so the cut face is '
                   'filled from the boundary: it shows the outline of the '
                   'section, not its cells.',
    APPROXIMATE_CAP: 'The boundary was decimated to fit the preview; the '
                     'filled face follows the simplified surface and has no '
                     'cells in it.',
    COMPUTING: 'This mesh is too large to re-cut on every mouse move; the '
               'section is cut when you let go.',
    CAP_UNAVAILABLE: 'Where the plane crosses it, a surface does not close '
                     'into loops (it is open or non-manifold), so the cut '
                     'face is left empty rather than filled with a guess.',
    NOT_COMPUTED: 'The section worker did not cut the cells; the section on '
                  'screen is the last one it did cut.',
    PREVIEW_WHILE_MOVING: 'While the plane moves, the preview on screen is '
                          'clipped by the renderer so it can follow the '
                          'mouse. It is not a whole-cell result; the exact '
                          'section is cut when you let go.',
    PREVIOUS_POSITION: 'The exact section shown was cut where the plane was '
                       'before it moved; the new one is cut when you let go.',
}


#: What the section worker's answer on screen is (`sectionNotices` *worker*).
WORKER_CURRENT = 'current'
WORKER_PREVIOUS = 'previous'
WORKER_REFUSED = 'refused'


def sectionNotices(*, hasVolume: bool, previewKind, caps, computing: bool,
                   worker=None, moving: bool = False,
                   previous: bool = False):
    """The notices for a section, in the order they are shown.

    *hasVolume*: cells are loaded and cut. *previewKind*: the mesh preview's
    notice kind (``'surface'``, ``'decimated'``, ``'outline'``) or ``None``.
    *caps*: the outcome of every cap drawn (``CAP_*``). *computing*: a cut is
    waiting for a drag to end, or the section worker is running.
    *worker* (UF10): the worker's section on screen is the one wanted
    (``WORKER_CURRENT``), one cut where the plane was (``WORKER_PREVIOUS``),
    or the worker refused (``WORKER_REFUSED``). *moving* (UF8): a renderer
    clip of the preview stands in while the plane is dragged. *previous*
    (UF8): the plane is dragged and what is on screen was cut where it was.
    """
    notices = []
    boundaryOnly = not hasVolume and previewKind in (
        'surface', 'decimated', 'outline')
    if moving:
        # Never an exact whole-cell result, whatever else is loaded.
        notices.append(PREVIEW_WHILE_MOVING)
        if computing:
            notices.append(COMPUTING)
        if boundaryOnly:
            notices.append(BOUNDARY_ONLY)
        return notices
    if computing:
        notices.append(COMPUTING)
    if worker == WORKER_CURRENT and not previous:
        notices.append(EXACT)
        return notices
    if worker == WORKER_CURRENT:
        notices += [PREVIOUS_POSITION, EXACT]
        return notices
    if worker == WORKER_PREVIOUS or (previous and worker != WORKER_REFUSED):
        notices.append(PREVIOUS_POSITION)
    elif worker == WORKER_REFUSED:
        notices.append(NOT_COMPUTED)
    if hasVolume:
        notices.append(EXACT)
        return notices
    if boundaryOnly:
        notices.append(BOUNDARY_ONLY)
    drawn = [cap for cap in caps if cap != CAP_MISSED]
    if not boundaryOnly and any(cap == CAP_CLOSED for cap in drawn):
        notices.append(EXACT)
    if previewKind == 'decimated' or CAP_APPROXIMATE in drawn:
        if any(cap in (CAP_CLOSED, CAP_APPROXIMATE) for cap in drawn):
            notices.append(APPROXIMATE_CAP)
    if CAP_OPEN in drawn:
        notices.append(CAP_UNAVAILABLE)
    return notices
