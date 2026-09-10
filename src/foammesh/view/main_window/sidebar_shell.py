"""Compatibility import for the current three-region shell.

The public function name is retained for callers outside the main window, but
it no longer creates a composite/hideable sidebar.
"""
from __future__ import annotations

from .three_region_shell import install_three_region_shell


def install_sidebar_shell(ui):
    """Install the current shell and return Region A for old call sites."""
    shell = install_three_region_shell(ui)
    ui.sidebarShell = shell.region_a
    return shell.region_a
