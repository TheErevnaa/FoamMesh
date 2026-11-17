"""A Qt slot that calls a plain object's method without keeping anything.

DP-6407. PySide 6.9 keeps one weak reference, for the life of the process,
every time a signal is connected straight to a bound method of an object that
is not a QObject; disconnecting or deleting the sender does not free it. Code
that connects such a method on every project opened therefore grew by one
reference (and its callback) a project. Connect ``weak_slot(obj.method)``
instead: a plain function, which PySide drops with the connection.
"""
from __future__ import annotations

import weakref


def weak_slot(method):
    """Return a plain function that calls ``method`` while its object lives.

    The function holds the object weakly, so the connection never keeps it
    alive; once the object is gone the function does nothing and returns None.
    Anything that is not a bound method (a plain function) is returned as it
    is: PySide keeps nothing for those.
    """
    if getattr(method, '__self__', None) is None or not hasattr(method, '__func__'):
        return method
    held = weakref.WeakMethod(method)

    def slot(*args):
        target = held()
        if target is not None:
            return target(*args)
        return None

    slot.__name__ = getattr(method, '__name__', 'slot')
    slot.__qualname__ = getattr(method, '__qualname__', 'slot')
    return slot
