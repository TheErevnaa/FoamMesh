#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 43 LY12. Continuous colour scales live in ``colour_standard``.

This module used to build the white-to-red table, the diverging deviation
table and the quality band table. Every continuous field now takes its table
from :func:`foammesh.support.colour_standard.lookup_table`; "White to red" is
one preset of that standard. What stays here is the name other modules know.
"""
from foammesh.support.colour_standard import NAN_GREY  # noqa: F401

STEP = 256
