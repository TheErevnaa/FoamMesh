"""Gmsh workflow page: gmsh.describe_geometry."""
from __future__ import annotations

from .base import GmshTaskPage


class GmshDescribePage(GmshTaskPage):
    task_id_default = 'gmsh.describe_geometry'
