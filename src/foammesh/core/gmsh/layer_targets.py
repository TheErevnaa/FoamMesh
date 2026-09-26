"""The host side of the layer target rule, loaded from the resource tree.

Plan 33 section 1.1. ``src/resources/gmsh/layer_targets.py`` decides which
boundaries receive layers, and the runner imports it as a sibling because it
executes inside WSL with nothing of this application on its path. Nothing is
reimplemented here: the page, the derivation and the schema migration all ask
the same file the run will ask, so what a user is shown and what Gmsh is
given cannot disagree.

The loader is the one ``foammesh.core.gmsh.size_fields`` already uses for the
field graph and ``foammesh.core.geometry.domain_topology`` for the shell
classifier.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

#: Kept here so callers can name the modes without loading the resource file.
MODE_SELECTED = 'selected'
MODE_ALL_WALLS = 'all_eligible_walls'
MODE_UNSET = ''
MODES = (MODE_SELECTED, MODE_ALL_WALLS)


def layer_targets_module():
    """The shared rule, loaded once per process."""
    module = sys.modules.get('foammesh._layer_targets')
    if module is not None:
        return module
    from resources import resource

    location = Path(resource.file('gmsh/layer_targets.py')).resolve()
    spec = importlib.util.spec_from_file_location(
        'foammesh._layer_targets', location)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution, as the field graph loader explains.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def boundary_category(name, category='') -> str:
    """The category a boundary announces, as the run will read it."""
    return layer_targets_module().boundary_category(name, category)


def eligible_wall_names(boundaries) -> tuple[str, ...]:
    """The boundaries ``All eligible walls`` means on this geometry."""
    return tuple(layer_targets_module().eligible_wall_names(boundaries))


def mesh_constrained_categories() -> tuple[str, ...]:
    """The categories no three-dimensional mesh can publish (DP-445)."""
    return tuple(layer_targets_module().MESH_CONSTRAINED_CATEGORIES)


def publishable_category(category):
    """The category a boundary publishes with, as the writer will read it."""
    return layer_targets_module().publishable_category(category)


def patch_names(value) -> tuple[str, ...]:
    """A stored selection, list or comma separated string alike."""
    return tuple(layer_targets_module().patch_names(value))


def normalise_mode(value) -> str:
    """A stored mode, or the empty string when a case predates the choice."""
    return layer_targets_module().normalise_mode(value)


def resolve_layer_patches(mode, patches, boundaries=()) -> tuple[str, ...]:
    """The boundaries that receive layers, decided by the shared rule."""
    return tuple(layer_targets_module().resolve_layer_patches(
        mode, patches, boundaries))
