#!/usr/bin/env python
# -*- coding: utf-8 -*-


import weakref
import types
from typing import Callable

from foammesh.support.sync_signal import SyncSignal


class ConsensusSignal(SyncSignal):
    def connect(self, func: Callable[..., bool], obj=None):
        if func in self._callbacks:
            raise AssertionError('Already connected')

        if obj is not None:
            self._callbacks[func] = weakref.ref(obj)
        else:
            self._callbacks[func] = None

    def emit(self, *args, **kwargs) -> bool:
        if len(args) != len(self._types):
            raise AssertionError('Wrong number of Parameters')

        for arg, type_ in zip(args, self._types):
            runtime_type = type_.__origin__ if isinstance(type_, types.GenericAlias) else type_
            if not isinstance(arg, runtime_type):
                raise AssertionError('Wrong parameter type')

        dead = []
        for cb, wref in self._callbacks.items():
            if wref is None or wref() is not None:
                if not cb(*args, **kwargs):
                    return False
            else:
                dead.append(cb)

        for cb in dead:
            del self._callbacks[cb]

        return True
