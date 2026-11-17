"""A read-only look at the case configuration for code that only reads it.

Plan 50 (DP-6341). ``checkout()`` deep-copies the whole document; on a
500-surface case one copy took 0.35 s, and the refresh after an import made
twelve of them to read an engine id, a farfield, a body name and a count.
"""


def configuration_view(client=None):
    """The client's ``view()``, uncopied; its checkout for a stand-in without one.

    Use it within one synchronous stretch, never across an ``await``.
    """
    if client is None:
        from foammesh.app import app
        client = app.facadeClient
    view = getattr(client, 'view', None)
    return view() if callable(view) else client.checkout()


def case_configuration(app):
    """``app.db`` for a reader: the open case's view, ``None`` with no case.

    Plan 50 (DP-6341, live w17). ``app.db`` is a whole checkout; the menu
    refresh and the disk probe took one each to read the engine and a count.
    A stand-in app without a facade client gets its own ``db``.
    """
    client = getattr(app, 'facadeClient', None)
    view = getattr(client, 'view', None)
    if callable(view) and getattr(app, 'caseSession', None) is not None:
        return view()
    return app.db
