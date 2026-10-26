"""Per-area parts of the batch runner (Plan 48 seam).

``runner_v1.py`` keeps one hook list per stage. A part is a module in this
package that defines ``register(add_hook)`` and calls
``add_hook(stage, function)`` for each stage it takes part in. The function
is called as ``function(run, **context)`` with the ``GmshRun`` first.

A part does not import ``runner_v1``: the runner is usually the ``__main__``
module, so importing it by name would load a second copy of it. The runner
hands ``add_hook`` in instead.

Stages, in the order a job passes them:

``before_import``, ``after_import``, ``geometry``, ``fields``,
``before_mesh_pass`` and ``after_mesh_pass`` (context ``dimension``: once for
each ``generate`` call), ``export``.

Modules are discovered by name, so a package adds its part by adding its own
file here (``runner_parts/<area>.py``) and never edits ``runner_v1.py``.
Files whose name starts with an underscore are not parts.
"""
from __future__ import annotations

import importlib
import pkgutil

STAGES = ('before_import', 'after_import', 'geometry', 'fields',
          'before_mesh_pass', 'after_mesh_pass', 'export')


def part_names() -> list[str]:
    """Names of the part modules in this package, sorted."""
    return sorted(
        info.name for info in pkgutil.iter_modules(__path__)
        if not info.name.startswith('_'))


def load_parts(add_hook) -> list[str]:
    """Import every part and let it register its hooks; return the names."""
    loaded = []
    for name in part_names():
        module = importlib.import_module(f'{__name__}.{name}')
        register = getattr(module, 'register', None)
        if callable(register):
            register(add_hook)
        loaded.append(name)
    return loaded
