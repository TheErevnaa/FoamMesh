#!/usr/bin/env python
# -*- coding: utf-8 -*-

import platform
import re
from enum import IntEnum, auto

import asyncio
from foammesh.support.process import runExternalCommand


class ParallelType(IntEnum):
    LOCAL_MACHINE = 0
    CLUSTER = 1
    SLURM = 2


class MPIStatus(IntEnum):
    OK = 0
    NOT_FOUND = auto()
    LOW_VERSION = auto()


if platform.system() == 'Windows':
    MPICMD = 'mpiexec'
    VERSION_CHECK_OPTION = '-help'
    MAJOR_VERSION = 10
    MINOR_VERSION = 1
    MPI_PREFIX = None
else:
    MPICMD = 'mpirun'
    VERSION_CHECK_OPTION = '--version'
    MAJOR_VERSION = 4
    MINOR_VERSION = 1


async def checkMPI():
    try:
        process = await runExternalCommand(MPICMD, VERSION_CHECK_OPTION,
                                          stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        m = re.search(r'([0-9]+)\.([0-9]+)\.', stdout.decode())
        major = int(m.group(1))

        # if major < MAJOR_VERSION or (major == MAJOR_VERSION and minor < MINOR_VERSION):
        if major < MAJOR_VERSION:
            return MPIStatus.LOW_VERSION

        return MPIStatus.OK
    except FileNotFoundError:
        return MPIStatus.NOT_FOUND


class ParallelEnvironment:
    def __init__(self, np: int, type_: ParallelType, hosts: str):
        self._np: int = np
        self._type = type_
        self._hosts = '' if hosts is None else hosts

    def np(self):
        return self._np

    def type(self):
        return self._type

    def hosts(self):
        return self._hosts

    def isParallelOn(self):
        return self._np > 1
