"""Geometry part of the batch runner (Plan 48 GM5).

A job may carry ``job['geometry_build']``: the Geometry-module operations the
window's build panel ran (``[{'kind': 'fillet', 'params': {...}}, ...]``, each
``params`` holding the ``kernel`` it was built with). They are applied here,
at the ``geometry`` stage, in the order they were made, by the same
``geometry_build_v1.apply`` the resident session uses, so a batch run builds
the model the person built by hand. A job without the key passes untouched.
"""
from __future__ import annotations

import os
import sys


def _builder():
    try:
        import geometry_build_v1 as builder
    except ImportError:                                    # pragma: no cover
        sys.path.insert(0, os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        import geometry_build_v1 as builder
    return builder


def apply_geometry_build(run, **_context):
    steps = run.job.get('geometry_build')
    if not steps:
        return
    builder = _builder()
    # DP-5716. The names and scopes the import resolved follow the build:
    # an entity it took away loses its name, a new one is named from the
    # build's groups, and no name is carried by a reused tag.
    follow = getattr(run, 'follow_geometry_build', None)
    snapshot = run.build_snapshot() if follow is not None else None
    done = []
    for index, step in enumerate(steps, 1):
        kind = step.get('kind')
        try:
            result = builder.apply(run.gmsh, kind, dict(step.get('params')
                                                        or {}))
        except builder.BuildError as error:
            raise ValueError('geometry build step %d (%s): %s'
                             % (index, kind, error)) from error
        done.append({'kind': kind, 'created': result.get('created', [])})
    run.geometry_build_done = done
    if snapshot is not None:
        follow(snapshot, kinds=[d['kind'] for d in done])


def register(add_hook):
    add_hook('geometry', apply_geometry_build)
