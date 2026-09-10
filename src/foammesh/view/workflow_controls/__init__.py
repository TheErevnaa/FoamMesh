"""Engine-neutral task-page widgets shared by every meshing workflow.

These began life inside the SALOME workflow package but never depended on it:
they render whatever the facade's field registry describes.  They live here so
the Gmsh workflow reuses them verbatim rather than forking a second copy.
"""
from .child_controls import ChildControlPanel
from .field_widgets import FieldEditor
from .selection_bridge import CanonicalSelectionBridge
from .task_page import EngineTaskPage

__all__ = ['CanonicalSelectionBridge', 'ChildControlPanel', 'EngineTaskPage',
           'FieldEditor']
