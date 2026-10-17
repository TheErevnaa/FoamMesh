#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""One call that builds a named widget or layout."""
from __future__ import annotations


def make(cls, *args, name: str | None = None, **properties):
    """``cls(*args)`` with its object name and tool tip set.

    Named so a test, the register and an accessibility tool can find the
    control by ``objectName``; *properties* are Qt property setters by
    name, for example ``toolTip='...'``.
    """
    widget = cls(*args)
    if name:
        widget.setObjectName(name)
    for key, value in properties.items():
        getattr(widget, 'set' + key[0].upper() + key[1:])(value)
    return widget


def compose(function, *args, **kwargs):
    """``function(*args, **kwargs)``: a picture or a view built by a helper."""
    return function(*args, **kwargs)
