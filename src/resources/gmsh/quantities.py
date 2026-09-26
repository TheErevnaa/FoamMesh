#!/usr/bin/env python3
"""How a number is written, for the code that runs inside the runtime.

DP-199. The host has these two rules in ``foammesh.core.quantities``, and
every count the application writes goes through them. The runner cannot: it is
executed by the Gmsh interpreter inside the WSL runtime, where the only things
on the path are this directory and the standard library, so importing anything
from ``foammesh`` is not available to it at all.

So the two rules sit here as well, beside the runner that needs them, the way
``shell_topology`` and ``field_graph`` sit beside it for the same reason. They
are six lines of arithmetic on a word, they have no dependencies, and the gate
that forbids a bracketed plural reads this tree too, so a copy that drifted
would be caught by the very sentence it was written to fix.
"""

from __future__ import annotations


def agreeing(count, singular: str, plural: str = '') -> str:
    """The form that agrees with a count: ``volume``/``volumes``, ``is``/``are``.

    The comparison is against the count as given rather than ``int(count)``,
    because a fractional quantity is not singular: a box half a diagonal
    larger takes ``diagonals``.
    """
    return singular if count == 1 else (plural or singular + 's')


def count_text(count, singular: str, plural: str = '') -> str:
    """The one spelling for how many: ``1 volume``, ``1,284 volumes``."""
    count = int(count)
    return f'{count:,} {agreeing(count, singular, plural)}'
