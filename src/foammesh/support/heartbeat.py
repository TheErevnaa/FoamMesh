#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR1: a heartbeat another process can read without asking us.

A small named shared-memory block, one per GUI process, that the CR0 crash
helper (``support.crash_helper``) reads from its own process. It carries:

* a tick counter the GUI thread bumps every 250 ms (``support.watchdog``), so
  the helper can tell a frozen GUI thread from a live one without the frozen
  process's co-operation -- whether it is stuck in Python, in a native call
  holding the GIL, or in a deadlock;
* the *last operation*: what the GUI was doing, written by

  - the facade instrumentation on every facade dispatch
    (``core.facade.instrumentation``), as the facade operation name;
  - the render path (CR8), as ``render:<scene>``, before a first render or
    a render-style change;

  so a hang dump or a death is recorded with what was running;
* the crash hand-off fields the native unhandled-exception filter
  (``support.crash_filter``) fills in before it signals the helper: the
  faulting thread id and the ``EXCEPTION_POINTERS`` address;
* a state: starting, ticking, clean exit, crashed.

Writers call :func:`set_last_op` and :func:`beat`; both are no-ops until
:func:`create` has run, cost a few hundred nanoseconds, and never raise.
``crash_helper.set_last_op`` is the same function.

This module imports no Qt and no VTK.
"""
from __future__ import annotations

import mmap
import os
import struct
import threading
import time

BLOCK_SIZE = 512
MAGIC = b'FMHB'
VERSION = 1

#: Layout (little-endian), shared with the helper and the native filter.
OFFSET_MAGIC = 0          # 4s
OFFSET_VERSION = 4        # I
OFFSET_SEQ = 8            # Q   tick counter
OFFSET_STATE = 16         # I   see STATE_*
OFFSET_THREAD_ID = 20     # I   faulting thread (native filter)
OFFSET_EXCEPTION = 24     # Q   EXCEPTION_POINTERS* in the GUI (native filter)
OFFSET_STAMP = 32         # d   time.monotonic() of the last tick
OFFSET_OP_SEQ = 40        # I   bumped around each last-op write
OFFSET_OP_LENGTH = 44     # I
OFFSET_OP = 48            # utf-8 text
OP_CAPACITY = BLOCK_SIZE - OFFSET_OP

STATE_STARTING = 0
STATE_TICKING = 1
STATE_CLEAN_EXIT = 2
STATE_CRASHED = 3

_lock = threading.Lock()
_state: dict = {'block': None, 'pid': None, 'seq': 0}


def block_name(pid: int) -> str:
    return f'Local\\FoamMesh.heartbeat.{int(pid)}'


def create(pid: int | None = None) -> mmap.mmap | None:
    """Create this process's block (idempotent); ``None`` where unsupported."""
    pid = pid or os.getpid()
    with _lock:
        if _state['block'] is not None:
            return _state['block']
        try:
            block = mmap.mmap(-1, BLOCK_SIZE, tagname=block_name(pid))
        except (OSError, TypeError, ValueError):
            return None
        block[:BLOCK_SIZE] = bytes(BLOCK_SIZE)
        block[OFFSET_MAGIC:OFFSET_MAGIC + 4] = MAGIC
        struct.pack_into('<I', block, OFFSET_VERSION, VERSION)
        _state['block'] = block
        _state['pid'] = pid
        _state['seq'] = 0
        return block


def attach(pid: int) -> mmap.mmap | None:
    """The helper's view of ``pid``'s block, or ``None`` if it has none."""
    try:
        block = mmap.mmap(-1, BLOCK_SIZE, tagname=block_name(pid))
    except (OSError, TypeError, ValueError):
        return None
    if bytes(block[OFFSET_MAGIC:OFFSET_MAGIC + 4]) != MAGIC:
        block.close()
        return None
    return block


def block() -> mmap.mmap | None:
    return _state['block']


def beat() -> int:
    """One GUI-thread tick; returns the new tick count (0 when not created)."""
    shared = _state['block']
    if shared is None:
        return 0
    _state['seq'] += 1
    try:
        struct.pack_into('<Q', shared, OFFSET_SEQ, _state['seq'])
        struct.pack_into('<d', shared, OFFSET_STAMP, time.monotonic())
        if struct.unpack_from('<I', shared, OFFSET_STATE)[0] == STATE_STARTING:
            struct.pack_into('<I', shared, OFFSET_STATE, STATE_TICKING)
    except (ValueError, TypeError):
        return 0
    return _state['seq']


def set_state(state: int) -> None:
    shared = _state['block']
    if shared is None:
        return
    try:
        struct.pack_into('<I', shared, OFFSET_STATE, state)
    except (ValueError, TypeError):
        pass


def mark_clean_exit() -> None:
    """Tell the helper that the coming process exit is deliberate."""
    set_state(STATE_CLEAN_EXIT)


def set_last_op(text: str) -> None:
    """Record what the GUI is doing now (``render:<scene>``, a facade op).

    Truncated to the block's capacity. Safe from any thread; never raises.
    """
    shared = _state['block']
    if shared is None:
        return
    try:
        data = str(text).encode('utf-8', 'replace')[:OP_CAPACITY]
        sequence = struct.unpack_from('<I', shared, OFFSET_OP_SEQ)[0]
        # Odd while writing, so a reader can discard a torn value.
        struct.pack_into('<I', shared, OFFSET_OP_SEQ, (sequence + 1) & 0xFFFFFFFF)
        shared[OFFSET_OP:OFFSET_OP + len(data)] = data
        struct.pack_into('<I', shared, OFFSET_OP_LENGTH, len(data))
        struct.pack_into('<I', shared, OFFSET_OP_SEQ, (sequence + 2) & 0xFFFFFFFF)
    except (ValueError, TypeError):
        pass


def read(shared) -> dict:
    """Decode a block (the helper's side)."""
    for _attempt in range(3):
        before = struct.unpack_from('<I', shared, OFFSET_OP_SEQ)[0]
        length = min(struct.unpack_from('<I', shared, OFFSET_OP_LENGTH)[0],
                     OP_CAPACITY)
        op = bytes(shared[OFFSET_OP:OFFSET_OP + length])
        if before % 2 == 0 and before == struct.unpack_from(
                '<I', shared, OFFSET_OP_SEQ)[0]:
            break
    return {
        'seq': struct.unpack_from('<Q', shared, OFFSET_SEQ)[0],
        'state': struct.unpack_from('<I', shared, OFFSET_STATE)[0],
        'thread_id': struct.unpack_from('<I', shared, OFFSET_THREAD_ID)[0],
        'exception_pointers': struct.unpack_from(
            '<Q', shared, OFFSET_EXCEPTION)[0],
        'stamp': struct.unpack_from('<d', shared, OFFSET_STAMP)[0],
        'last_op': op.decode('utf-8', 'replace'),
    }


def address(shared) -> int | None:
    """The block's address in this process (for the native filter)."""
    try:
        import ctypes
        return ctypes.addressof(ctypes.c_char.from_buffer(shared))
    except (TypeError, ValueError, ImportError):
        return None


def reset() -> None:
    """Forget the block (tests). The mapping itself stays alive if exported."""
    with _lock:
        shared = _state['block']
        _state['block'] = None
        _state['seq'] = 0
        if shared is not None:
            try:
                shared.close()
            except BufferError:
                pass
