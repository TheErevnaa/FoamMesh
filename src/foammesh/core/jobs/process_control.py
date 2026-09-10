#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Cross-platform process-group launch and whole-tree termination.

The retired launcher bug: pressing Cancel started a *new* snappyHexMesh process
instead of killing the running one. The fix is to (a) launch each utility in its
own process group / session, and (b) on cancel, kill the entire process tree of
the tracked PID — never re-spawn.
"""
from __future__ import annotations

import subprocess
import sys

import psutil


def new_process_group_kwargs() -> dict:
    """subprocess kwargs that put the child in its own killable group/session."""
    if sys.platform == 'win32':
        return {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


def is_running(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def kill_process_tree(pid: int, timeout: float = 5.0) -> int:
    """Kill *pid* and all descendants. Returns the number of processes killed."""
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0

    procs = parent.children(recursive=True)
    procs.append(parent)

    for p in procs:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass

    gone, _alive = psutil.wait_procs(procs, timeout=timeout)
    return len(gone)
