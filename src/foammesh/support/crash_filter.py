#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan 35 CR0 step 5: the in-process half of an out-of-process dump.

A minidump written from inside a crashing process can deadlock (the loader
lock, the heap lock), so FoamMesh never writes one itself. What runs in the
process at the moment of a fatal native exception is a top-level
``SetUnhandledExceptionFilter`` that:

1. stores the ``EXCEPTION_POINTERS`` address and the faulting thread id in
   the heartbeat block (``support.heartbeat``) and marks it crashed;
2. signals the crash helper's *request* event;
3. waits up to 30 s for the helper's *done* event -- or for the helper
   process to end, so a dead helper costs no wait at all;
4. chains to the filter that was installed before it (or lets the search
   continue), so Windows Error Reporting still sees the crash.

The helper then calls ``MiniDumpWriteDump`` on this process from outside,
with ``ClientPointers=TRUE`` (the pattern Crashpad uses).

The filter cannot be Python: a Python callback must take the GIL, which the
crashing thread may not hold and another thread may never release, and it
allocates. So it is forty-odd bytes of x64 machine code, written into an
executable page with every address baked in. It allocates nothing, takes no
lock and calls four kernel32 functions. faulthandler's vectored handler runs
before any unhandled-exception filter, so the Python stacks are already in
``faulthandler.log`` when the helper is asked for the dump.

Windows x64 only; anywhere else :func:`install` returns ``False``.
This module imports no Qt and no VTK.
"""
from __future__ import annotations

import struct
import sys

from foammesh.support import heartbeat

WAIT_MS = 30_000

_state: dict = {'installed': False, 'pages': None}


def _supported() -> bool:
    return sys.platform == 'win32' and struct.calcsize('P') == 8


def _imm64(value: int) -> bytes:
    return struct.pack('<Q', value & 0xFFFFFFFFFFFFFFFF)


def _code(block_address: int, request_event: int, handles_address: int,
          previous_slot: int, functions: dict) -> bytes:
    """The filter, ``LONG WINAPI filter(EXCEPTION_POINTERS *pointers)``."""
    code = b''.join((
        b'\x53',                                   # push rbx
        b'\x48\x83\xEC\x30',                       # sub rsp, 0x30
        b'\x48\x89\xCB',                           # mov rbx, rcx
        b'\x48\xB8', _imm64(block_address),        # mov rax, block
        b'\x48\x89\x48', bytes((heartbeat.OFFSET_EXCEPTION,)),
                                                   # mov [rax+EXC], rcx
        b'\xC7\x40', bytes((heartbeat.OFFSET_STATE,)),
        struct.pack('<I', heartbeat.STATE_CRASHED),
                                                   # mov dword [rax+STATE], 3
        b'\x48\xB8', _imm64(functions['GetCurrentThreadId']),
        b'\xFF\xD0',                               # call rax
        b'\x48\xB9', _imm64(block_address),        # mov rcx, block
        b'\x89\x41', bytes((heartbeat.OFFSET_THREAD_ID,)),
                                                   # mov [rcx+TID], eax
        b'\x48\xB9', _imm64(request_event),        # mov rcx, request
        b'\x48\xB8', _imm64(functions['SetEvent']),
        b'\xFF\xD0',                               # call rax
        b'\xB9\x02\x00\x00\x00',                   # mov ecx, 2
        b'\x48\xBA', _imm64(handles_address),      # mov rdx, handles
        b'\x45\x31\xC0',                           # xor r8d, r8d (wait any)
        b'\x41\xB9', struct.pack('<I', WAIT_MS),   # mov r9d, WAIT_MS
        b'\x48\xB8', _imm64(functions['WaitForMultipleObjects']),
        b'\xFF\xD0',                               # call rax
        b'\x48\xB8', _imm64(previous_slot),        # mov rax, &previous
        b'\x48\x8B\x00',                           # mov rax, [rax]
        b'\x48\x85\xC0',                           # test rax, rax
        b'\x74\x07',                               # jz .none
        b'\x48\x89\xD9',                           # mov rcx, rbx
        b'\xFF\xD0',                               # call rax
        b'\xEB\x02',                               # jmp .done
        b'\x31\xC0',                               # .none: xor eax, eax
        b'\x48\x83\xC4\x30',                       # .done: add rsp, 0x30
        b'\x5B',                                   # pop rbx
        b'\xC3',                                   # ret
    ))
    return code


def install(request_event: int, done_event: int, helper_process: int) -> bool:
    """Install the filter once; ``True`` if it is in place.

    ``request_event``/``done_event`` are this process's handles to the
    helper's named events, ``helper_process`` a handle to the helper process.
    The heartbeat block must already exist.
    """
    if _state['installed']:
        return True
    if not _supported():
        return False
    shared = heartbeat.block()
    block_address = heartbeat.address(shared) if shared is not None else None
    if not block_address:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.VirtualAlloc.restype = ctypes.c_void_p
        kernel32.VirtualAlloc.argtypes = (ctypes.c_void_p, ctypes.c_size_t,
                                          wintypes.DWORD, wintypes.DWORD)
        kernel32.VirtualProtect.argtypes = (
            ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD))
        kernel32.SetUnhandledExceptionFilter.restype = ctypes.c_void_p
        kernel32.SetUnhandledExceptionFilter.argtypes = (ctypes.c_void_p,)
        kernel32.FlushInstructionCache.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p

        functions = {
            name: ctypes.cast(getattr(kernel32, name), ctypes.c_void_p).value
            for name in ('GetCurrentThreadId', 'SetEvent',
                         'WaitForMultipleObjects')}
        # The code page is allocated executable from the start and only then
        # made read-only: memory that *becomes* executable through
        # VirtualProtect is not a valid Control Flow Guard call target, and
        # SetUnhandledExceptionFilter silently refuses such a filter
        # (MEASURED: it then reads back as NULL). PAGE_TARGETS_NO_UPDATE
        # keeps the targets valid through the protection change.
        code_address = kernel32.VirtualAlloc(None, 0x1000, 0x3000, 0x40)
        data_address = kernel32.VirtualAlloc(None, 0x1000, 0x3000, 0x04)
        if not code_address or not data_address:
            return False
        pages = (code_address, data_address)
        handles_address = data_address
        previous_slot = data_address + 16
        struct.pack_into(
            '<QQQ', (ctypes.c_char * 24).from_address(data_address), 0,
            done_event, helper_process, 0)
        code = _code(block_address, request_event, handles_address,
                     previous_slot, functions)
        ctypes.memmove(code_address, code, len(code))
        old = wintypes.DWORD()
        kernel32.VirtualProtect(code_address, 0x1000, 0x20 | 0x40000000,
                                ctypes.byref(old))
        kernel32.FlushInstructionCache(kernel32.GetCurrentProcess(),
                                       code_address, len(code))
        previous = kernel32.SetUnhandledExceptionFilter(code_address) or 0
        struct.pack_into('<Q', (ctypes.c_char * 8).from_address(
            previous_slot), 0, previous)
        current = kernel32.SetUnhandledExceptionFilter(code_address) or 0
        if current != code_address:
            # Refused: put back whatever was there and report no filter.
            kernel32.SetUnhandledExceptionFilter(previous or None)
            return False
    except Exception:                                      # noqa: BLE001
        return False
    _state['installed'] = True
    _state['pages'] = pages
    return True


def installed() -> bool:
    return _state['installed']
