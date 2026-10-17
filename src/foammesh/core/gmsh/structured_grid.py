"""The regular grid file of Gmsh's ``Structured`` size field (Plan 41 GN8, M-15).

The field reads a size from a grid written as, in text form::

    Ox Oy Oz
    Dx Dy Dz
    Nx Ny Nz
    v(0,0,0) v(0,0,1) ... v(Nx-1,Ny-1,Nz-1)

``O`` is the origin, ``D`` the step along each axis, ``N`` the node count on
each axis, and ``v(i,j,k)`` the size at ``O + (i*Dx, j*Dy, k*Dz)``, with **k
fastest** and i slowest.  The binary form is three doubles, three doubles,
three 32-bit integers (little endian) and the ``Nx*Ny*Nz`` values as doubles,
in the same order.  Both were MEASURED on Gmsh 4.15.2 on 2026-10-10: a size
that grows with ``i`` only changed the density along x, with ``j`` only along
y, with ``k`` only along z, in text and in binary
(``plans/evidence/plan41/gn8/grid-order.json``).

Nothing here imports Qt or Gmsh.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .geometry_build import BuildRefusal, evaluate

#: The most nodes one grid may hold.  Not a cell cap: a grid is a lookup
#: table, and this keeps the evaluation of an expression over it bounded.
MAX_NODES = 4_000_000


class GridRefusal(ValueError):
    """A grid that cannot be written; the message says what to change."""


@dataclass(frozen=True)
class GridSpec:
    origin: tuple
    step: tuple
    counts: tuple
    #: a number or an expression in x, y and z (metres)
    expression: str = '0.1'
    text: bool = True

    def nodes(self) -> int:
        return self.counts[0] * self.counts[1] * self.counts[2]

    def extent(self) -> tuple:
        return tuple(self.step[a] * (self.counts[a] - 1) for a in range(3))


def spec_covering(bounds, target: float, expression: str = '',
                  text: bool = True) -> GridSpec:
    """A grid over *bounds* ``(xmin, ymin, zmin, xmax, ymax, zmax)`` whose
    step is *target*; the expression defaults to that constant."""
    if target <= 0:
        raise GridRefusal('the grid step must be greater than zero')
    origin = tuple(float(b) for b in bounds[:3])
    size = [float(bounds[3 + a]) - origin[a] for a in range(3)]
    counts = tuple(max(2, int(round(s / target)) + 1) for s in size)
    step = tuple((size[a] / (counts[a] - 1)) if counts[a] > 1 else target
                 for a in range(3))
    return GridSpec(origin, step, counts, expression or f'{target:g}', text)


def check(spec: GridSpec) -> None:
    if len(spec.origin) != 3 or len(spec.step) != 3 or len(spec.counts) != 3:
        raise GridRefusal('a grid has an origin, a step and a count on each '
                          'of the three axes')
    for a, axis in enumerate('xyz'):
        if spec.counts[a] < 2 or int(spec.counts[a]) != spec.counts[a]:
            raise GridRefusal(
                f'the grid needs at least two nodes along {axis} '
                f'(it has {spec.counts[a]})')
        if not spec.step[a] > 0:
            raise GridRefusal(
                f'the grid step along {axis} must be greater than zero')
    if spec.nodes() > MAX_NODES:
        raise GridRefusal(
            f'the grid has {spec.nodes():,} nodes; the limit is '
            f'{MAX_NODES:,} (use a larger step or a smaller region)')


def values(spec: GridSpec) -> list:
    """The sizes at every node, k fastest.  Raises :class:`GridRefusal` for
    an expression that is wrong or gives a size that is not positive."""
    check(spec)
    expression = str(spec.expression).strip()
    try:
        constant = float(evaluate(expression))
    except BuildRefusal:
        constant = None
    out = []
    if constant is not None:
        if not constant > 0:
            raise GridRefusal(f'the size {expression!r} must be positive')
        return [constant] * spec.nodes()
    ox, oy, oz = spec.origin
    dx, dy, dz = spec.step
    nx, ny, nz = spec.counts
    for i in range(nx):
        x = ox + i * dx
        for j in range(ny):
            y = oy + j * dy
            for k in range(nz):
                z = oz + k * dz
                try:
                    value = evaluate(expression, {'x': x, 'y': y, 'z': z})
                except BuildRefusal as error:
                    raise GridRefusal(
                        f'the size expression cannot be evaluated: {error}'
                    ) from None
                if not value > 0:
                    raise GridRefusal(
                        f'the size expression gives {value:g} at '
                        f'({x:g}, {y:g}, {z:g}); a size must be positive')
                out.append(value)
    return out


def write(path, spec: GridSpec) -> dict:
    """Write the grid file; returns what was written."""
    data = values(spec)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec.text:
        with open(path, 'w', encoding='ascii', newline='\n') as handle:
            handle.write('%.17g %.17g %.17g\n' % tuple(spec.origin))
            handle.write('%.17g %.17g %.17g\n' % tuple(spec.step))
            handle.write('%d %d %d\n' % tuple(spec.counts))
            handle.write('\n'.join('%.9g' % v for v in data) + '\n')
    else:
        with open(path, 'wb') as handle:
            handle.write(struct.pack('<3d3d3i', *spec.origin, *spec.step,
                                     *spec.counts))
            handle.write(struct.pack('<%dd' % len(data), *data))
    return {'path': str(path), 'nodes': len(data), 'text': spec.text,
            'minimum': min(data), 'maximum': max(data)}


def read(path) -> tuple:
    """``(GridSpec, values)`` from a file written by :func:`write`; the text
    form is told from the binary by its first byte."""
    raw = Path(path).read_bytes()
    try:
        head = raw[:200].decode('ascii')
        text = all(ch in '0123456789+-.eE \n\r' for ch in head.split('\n')[0])
    except UnicodeDecodeError:
        text = False
    if text:
        words = raw.decode('ascii').split()
        origin = tuple(float(w) for w in words[0:3])
        step = tuple(float(w) for w in words[3:6])
        counts = tuple(int(w) for w in words[6:9])
        data = [float(w) for w in words[9:]]
    else:
        head = struct.unpack('<3d3d3i', raw[:60])
        origin, step, counts = head[0:3], head[3:6], head[6:9]
        n = counts[0] * counts[1] * counts[2]
        data = list(struct.unpack('<%dd' % n, raw[60:60 + 8 * n]))
    return GridSpec(origin, step, counts, text=text), data
