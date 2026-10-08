"""How many cells castellation starts from, and the RAM it needs. Plan 37 #7.

MEASURED: a 0.5 x 0.5 x 0.15 m quadcopter in the default snappy far field at
a 20 mm target wrote 5.9 M background cells. The legacy castellation page
multiplied the stored ``numCells`` -- which the target-size mode does not
read -- and said about 1,000, and the guided pages said nothing at all;
castellation ran for over 50 minutes and failed.

One answer for every page: the case builder's own `background_plan`, the
background it will write (the coarse far field plus the box around the
bodies refined back to the target), and the snappy bytes a cell from
``resource_budget``. A count over the free RAM is a warning -- the product
never refuses on a cell count (user rule 2026-10-01); only the RAM check at
launch may stop a run.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BackgroundEstimate:
    #: Where the block comes from: blocks, hex6, farfield or geometry.
    source: str
    #: Cells castellation starts from, before surface refinement.
    cells: int
    #: Cells the block alone holds.
    block_cells: int
    #: Cells the far field would have held filled at the target size.
    uniform_cells: int
    #: The background cell edge (the coarse cell in a coarsened far field).
    cell: float | None
    #: The cell edge around the bodies -- the target -- when coarsened.
    near_cell: float | None
    #: Levels the far field is coarsened by (0: not coarsened).
    level: int
    #: Peak RAM castellation needs for ``cells``.
    ram_bytes: int
    #: Physical RAM free when the estimate was made (0: unknown).
    available_bytes: int

    @property
    def over_ram(self) -> bool:
        return self.available_bytes > 0 and self.ram_bytes > self.available_bytes


def estimate(db, geometry_bounds, *, available_bytes: int | None = None
             ) -> BackgroundEstimate | None:
    """The background the writer will write for ``db`` around the surfaces'
    extent ``geometry_bounds`` (six numbers), or ``None`` when it cannot be
    counted (no case, no geometry, a value the writer would refuse)."""
    from foammesh.core.geometry import BBox
    from foammesh.openfoam.case_builder import CaseBuilder
    from foammesh.support import resource_budget

    if db is None:
        return None
    try:
        bbox = None if geometry_bounds is None else BBox(
            *(float(value) for value in geometry_bounds))
        plan = CaseBuilder(db, bbox).background_plan()
    except Exception:                                       # noqa: BLE001
        return None
    cells = int(plan.get('estimate') or 0)
    level = int(plan.get('level') or 0)
    cell = plan.get('cell')
    if available_bytes is None:
        available_bytes = resource_budget.physical_memory()[1]
    return BackgroundEstimate(
        source=str(plan.get('source') or ''), cells=cells,
        block_cells=int(plan.get('block_cells') or 0),
        uniform_cells=int(plan.get('uniform_cells') or cells),
        cell=float(cell) if cell else None,
        near_cell=float(cell) / 2 ** level if cell and level else None,
        level=level,
        ram_bytes=resource_budget.mesher_memory_needed('snappy', cells),
        available_bytes=int(available_bytes or 0))


def _length(value: float) -> str:
    if value >= 1:
        return f'{value:.3g} m'
    return f'{value * 1000:.3g} mm'


def describe(found: BackgroundEstimate | None) -> str:
    """The sentences every page shows; a warning, never a refusal."""
    from foammesh.support.resource_budget import format_bytes

    if found is None:
        return 'Cell-count estimate unavailable until the geometry is loaded.'
    lines = []
    if found.level and found.cell and found.near_cell:
        lines.append(
            'Far field meshed at {0} ({1} levels coarser); the box around the '
            'bodies is refined back to {2}.'.format(
                _length(found.cell), found.level, _length(found.near_cell)))
    elif found.cell:
        lines.append(f'Background cell: {_length(found.cell)}.')
    lines.append(
        'Castellation starts from about {0:,} cells before surface '
        'refinement and needs at least {1} of RAM.'.format(
            found.cells, format_bytes(found.ram_bytes)))
    if found.level and found.uniform_cells > found.cells:
        lines.append('Filled at the target size the far field would have '
                     'held {0:,} cells.'.format(found.uniform_cells))
    if found.over_ram:
        lines.append(
            'Warning: that is more than the {0} of RAM free now; castellation '
            'may run for hours or fail. A larger target cell size or a '
            'smaller far field keeps it in memory.'.format(
                format_bytes(found.available_bytes)))
    return '\n'.join(lines)
