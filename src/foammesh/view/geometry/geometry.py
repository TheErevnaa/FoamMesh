#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Re-export of the boundary-name rules the core owns.

The list used to be defined here, which meant the dialogs enforced it and the
Repair page did not. Core owns it now; this line keeps the existing view
imports working.
"""

from foammesh.core.geometry.patches.ops import RESERVED_NAMES  # noqa: F401
