"""Session operations for the export area (Plan 48 seam, owner GM4).

A mixin of ``session_v1.Session``. ``dispatch()`` finds a public
method by name (``session.<name>``), so a method added here is reachable as
the operation ``session.<name>`` with no other change. The mixin must not
import ``session_v1``: it is mixed into ``Session``, and uses ``self.gmsh``
(the recording wrapper) and ``self.raw`` (the bare module) from it.
"""
from __future__ import annotations


class SessionExportMixin:
    """Operations of the export area (Plan 48 GM4)."""

    __slots__ = ()

    def export_converted(self, path, options=None, kind='mesh', outputs=None,
                         convert=None, **_):
        """Write the mesh through an explicit conversion (GM4, §4.9.1).

        ``convert='first_order_copy'`` writes a corner-node copy of the mesh
        and puts the session back on the model it had: the mesh in hand, the
        user's files and the op log are untouched, which is why the bare
        module (``self.raw``) is used and nothing of the copy is recorded.
        """
        import export_session_v1 as exporter
        return exporter.export(self.raw, path, options=options, kind=kind,
                               outputs=outputs, convert=convert)

    def export_periodic(self, **_):
        """How many entities of the model are the slave side of a periodic pair."""
        import export_session_v1 as exporter
        return {'periodic': exporter.periodic_entities(self.raw)}
