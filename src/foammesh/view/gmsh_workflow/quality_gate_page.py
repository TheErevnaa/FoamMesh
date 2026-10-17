"""The Gmsh Quality page with the gate it judges the mesh by.

Plan 40 UXD9 (#54), Plan 42 SG18. The gate's limits are edited on the Quality
page, which is where the verdict is read; the page itself is the one Quality
page both engines share, and its gate section is Gmsh's item set. This name is
kept for the registry and the tests.
"""
from __future__ import annotations

from .qa_page import GmshQaPage


class GmshQualityGatePage(GmshQaPage):
    """``gmsh.qa``: the checks, and the limits the mesh is judged against."""
