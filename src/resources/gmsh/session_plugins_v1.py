"""Session operations for the plugins area (Plan 48 seam, owner GM9).

A mixin of ``session_v1.Session``. ``dispatch()`` finds a public method by name
(``session.<name>``), so a method added here is reachable as the operation
``session.<name>`` with no other change. The mixin must not import
``session_v1``: it is mixed into ``Session``, and uses ``self.gmsh`` (the
recording wrapper) and ``self.raw`` (the bare module) from it.

``plugin_run`` / ``plugin_edit`` / ``plugin_views`` / ``plugin_remove`` stay on
``Session`` (they delegate to ``plugin_session_v1``); the operations added here
are the ones the Plan 48 plugins panel and local API need beyond them.
"""
from __future__ import annotations

import sys


class SessionPluginsMixin:
    """Operations of the plugins area."""

    __slots__ = ()

    def plugin_describe(self, tag, directory, **_):
        """Write any view of the session as a ``.vtu`` and describe it.

        A plugin's input view can be any view in the session (a plugin result,
        a quality view, a size-data view loaded for a PostView field), and the
        panel draws it the way it draws a plugin result.  ``tag`` is the view
        tag; the row has the same shape as the rows ``plugin_run`` returns.
        """
        import plugin_session_v1 as plugins

        if int(tag) not in list(self.gmsh.view.getTags()):
            raise ValueError('there is no view %s' % tag)
        write_vtk = sys.modules[type(self).__module__]._write_vtk
        return plugins.describe(self.gmsh, int(tag), directory, write_vtk)
