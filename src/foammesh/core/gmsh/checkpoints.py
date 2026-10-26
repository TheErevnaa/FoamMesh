"""Checkpoints, "Remove last script command" and the project's geometry
kernel (Plan 48 GM5).

The resident session keeps its history as the op log (``session.log``).
A checkpoint is a position in that log plus an inventory of the model; going
back empties the model, runs the kept part of the log again and checks the
entities match (``session_geometry_v1``).  This module is the client side:

* :func:`bind` gives a :class:`~foammesh.core.gmsh.geometry_build.
  GeometryBuilder` the kernel the project chose last time and remembers every
  later choice (``foammesh/gmsh/geometry_kernel.json``), so the choice is per
  project and survives a restart;
* :func:`remove_last` is the "Remove last script command" button;
* :func:`guarded` runs several operations as one: if one fails the model goes
  back to where it was.

Nothing here imports Qt.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from . import geometry_build as gb

KERNEL_FILE = 'geometry_kernel.json'


def kernel_path(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'gmsh' / KERNEL_FILE


def load_kernel(case_path) -> str:
    """The kernel the project chose, OpenCASCADE when it never did."""
    try:
        data = json.loads(kernel_path(case_path).read_text(encoding='utf-8'))
        kernel = data.get('kernel')
    except (OSError, ValueError, AttributeError):
        return gb.DEFAULT_KERNEL
    return kernel if kernel in gb.KERNELS else gb.DEFAULT_KERNEL


def save_kernel(case_path, kernel: str) -> str:
    if kernel not in gb.KERNELS:
        raise gb.BuildRefusal('the geometry kernel must be one of %s'
                              % ', '.join(gb.KERNELS))
    path = kernel_path(case_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'kernel': kernel}, indent=1) + '\n',
                    encoding='utf-8', newline='\n')
    return str(path)


def bind(builder, case_path):
    """Apply the project's saved kernel to *builder* and keep it saved."""
    if getattr(builder, '_kernel_case', None) is not None:
        return builder
    builder._kernel_case = Path(case_path)
    saved = load_kernel(case_path)
    if saved != builder.kernel:
        builder.set_kernel(saved, confirm=True)
    original = builder.set_kernel

    def set_kernel(kernel, confirm=False, reimport=True):
        result = original(kernel, confirm, reimport)
        save_kernel(case_path, builder.kernel)
        return result

    builder.set_kernel = set_kernel
    return builder


def remove_last(builder, count: int = 1) -> dict:
    """Take back the last *count* commands; says what went."""
    result = builder.remove_last(count)
    removed = result['removed']
    result['text'] = ('Removed %s.' % ', '.join(removed)
                      if removed else 'Nothing to remove.')
    return result


def describe_kernel_change(builder, kernel: str) -> str:
    """What changing to *kernel* would do, for the confirmation question."""
    built = [s for s in builder.steps if s.op_id != 'parameter']
    if kernel == builder.kernel:
        return 'The project already uses %s.' % gb.KERNEL_TITLES[kernel]
    if not built:
        return 'Nothing is built yet; the change is free.'
    preview = builder.reimport_preview(kernel)
    def steps(n):
        return '%d step%s' % (n, '' if n == 1 else 's')
    text = ('%s built %s %s. Gmsh cannot mix the two kernels, so changing to '
            '%s empties the model and builds again the %s %s has; the script '
            'of the %s built so far is kept.'
            % (steps(len(built)), 'uses' if len(built) == 1 else 'use',
               gb.KERNEL_TITLES[builder.kernel], gb.KERNEL_TITLES[kernel],
               steps(len(preview['kept'])), gb.KERNEL_TITLES[kernel],
               steps(len(built))))
    if preview['refused']:
        text += (' No equivalent, refused by name: %s.'
                 % '; '.join('%s (step %d)' % (r['title'], r['step'])
                             for r in preview['refused']))
    return text


@contextmanager
def guarded(builder, name: str = 'guard'):
    """Run several operations as one: on an error the model goes back.

    A session checkpoint is taken first; if the block raises, the model is
    restored to it (the restore is verified by the session) and the error is
    raised again.
    """
    session = builder._session_provider()
    session.call('session.checkpoint_save', name)
    start = len(builder.steps)
    try:
        yield
    except BaseException:
        try:
            session.call('session.checkpoint_restore', name)
        finally:
            for step in builder.steps[start:]:
                if step.parameter:
                    builder.parameters.remove(step.parameter)
            del builder.steps[start:]
        raise
