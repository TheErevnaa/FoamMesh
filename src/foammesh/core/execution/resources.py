"""Portable resource intent and deterministic effective allocations."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import os
from typing import Mapping


class ResourceError(ValueError):
    pass


class ResourceMode(str, Enum):
    AUTO = 'auto'
    SERIAL = 'serial'
    PARALLEL = 'parallel'


@dataclass(frozen=True)
class ResourcePolicy:
    mode: ResourceMode = ResourceMode.AUTO
    max_cpu_cores: int | None = None
    max_memory_bytes: int | None = None
    allow_distributed: bool = False
    preferred_backend: str = 'local'

    def __post_init__(self) -> None:
        object.__setattr__(self, 'mode', ResourceMode(self.mode))
        if self.max_cpu_cores is not None and self.max_cpu_cores < 1:
            raise ResourceError('max_cpu_cores must be positive')
        if self.max_memory_bytes is not None and self.max_memory_bytes < 1:
            raise ResourceError('max_memory_bytes must be positive')
        if not self.preferred_backend.strip():
            raise ResourceError('preferred_backend must not be empty')
        if not self.allow_distributed and self.preferred_backend not in {
                'local', 'openfoam-mpi', 'gmsh-threads'}:
            raise ResourceError('distributed backend requires allow_distributed')

    def to_dict(self) -> dict:
        return {
            'mode': self.mode.value,
            'max_cpu_cores': self.max_cpu_cores,
            'max_memory_bytes': self.max_memory_bytes,
            'allow_distributed': self.allow_distributed,
            'preferred_backend': self.preferred_backend,
        }


@dataclass(frozen=True)
class ResourceRequest:
    cpu_ranks: int
    threads_per_rank: int = 1
    memory_bytes: int | None = None
    wall_time_seconds: float | None = None
    backend_id: str = 'local'
    profile_id: str | None = None
    placement: str = 'local'
    explicit: bool = False

    def __post_init__(self) -> None:
        if self.cpu_ranks < 1 or self.threads_per_rank < 1:
            raise ResourceError('ranks and threads_per_rank must be positive')
        if self.memory_bytes is not None and self.memory_bytes < 1:
            raise ResourceError('memory_bytes must be positive')
        if self.wall_time_seconds is not None and self.wall_time_seconds <= 0:
            raise ResourceError('wall_time_seconds must be positive')

    @property
    def cpu_slots(self) -> int:
        return self.cpu_ranks * self.threads_per_rank

    def to_dict(self) -> dict:
        return {
            'cpu_ranks': self.cpu_ranks,
            'threads_per_rank': self.threads_per_rank,
            'memory_bytes': self.memory_bytes,
            'wall_time_seconds': self.wall_time_seconds,
            'backend_id': self.backend_id,
            'profile_id': self.profile_id,
            'placement': self.placement,
            'explicit': self.explicit,
        }


@dataclass(frozen=True)
class ResourceFacts:
    cpu_cores: int
    memory_bytes: int | None = None
    affinity_cores: int | None = None
    backend_capabilities: Mapping[str, bool] = field(default_factory=dict)

    @property
    def effective_cpu_limit(self) -> int:
        return max(1, min(
            self.cpu_cores,
            self.affinity_cores if self.affinity_cores is not None
            else self.cpu_cores))

    @classmethod
    def local(cls) -> 'ResourceFacts':
        cores = os.cpu_count() or 1
        affinity = None
        if hasattr(os, 'sched_getaffinity'):
            try:
                affinity = len(os.sched_getaffinity(0))
            except OSError:
                pass
        memory = None
        try:
            import psutil
            memory = int(psutil.virtual_memory().available)
        except (ImportError, OSError):
            pass
        return cls(cores, memory, affinity, {'local': True})


@dataclass(frozen=True)
class ResourceAllocation:
    effective_ranks: int
    effective_threads_per_rank: int
    backend_id: str
    profile_id: str | None = None
    memory_bytes: int | None = None
    hosts_digest: str | None = None
    runtime_fingerprint: str | None = None
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.effective_ranks < 1 or self.effective_threads_per_rank < 1:
            raise ResourceError('effective allocation must have positive CPU values')

    @property
    def cpu_slots(self) -> int:
        return self.effective_ranks * self.effective_threads_per_rank

    def to_dict(self) -> dict:
        return {
            'effective_ranks': self.effective_ranks,
            'effective_threads_per_rank': self.effective_threads_per_rank,
            'backend_id': self.backend_id,
            'profile_id': self.profile_id,
            'memory_bytes': self.memory_bytes,
            'hosts_digest': self.hosts_digest,
            'runtime_fingerprint': self.runtime_fingerprint,
            'warnings': list(self.warnings),
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':')).encode()
        return hashlib.sha256(encoded).hexdigest()


def allocate_resources(policy: ResourcePolicy, request: ResourceRequest,
                       facts: ResourceFacts) -> ResourceAllocation:
    """Resolve resources without silently weakening explicit requests."""
    cpu_limit = facts.effective_cpu_limit
    if policy.max_cpu_cores is not None:
        cpu_limit = min(cpu_limit, policy.max_cpu_cores)
    requested_slots = request.cpu_slots
    warnings = []
    if policy.mode is ResourceMode.SERIAL:
        ranks, threads = 1, 1
    elif requested_slots <= cpu_limit:
        ranks, threads = request.cpu_ranks, request.threads_per_rank
    elif request.explicit or policy.mode is ResourceMode.PARALLEL:
        raise ResourceError(
            f'explicit request needs {requested_slots} CPU slots; {cpu_limit} available')
    else:
        if request.cpu_ranks > 1 and request.threads_per_rank == 1:
            ranks, threads = cpu_limit, 1
        elif request.cpu_ranks == 1:
            ranks, threads = 1, cpu_limit
        else:
            ranks, threads = max(1, cpu_limit // request.threads_per_rank), \
                request.threads_per_rank
        warnings.append(
            f'auto allocation reduced {requested_slots} CPU slots to {ranks * threads}')
    memory_limit = policy.max_memory_bytes
    if facts.memory_bytes is not None:
        memory_limit = min(
            facts.memory_bytes,
            memory_limit if memory_limit is not None else facts.memory_bytes)
    if request.memory_bytes is not None and memory_limit is not None:
        if request.memory_bytes > memory_limit and request.explicit:
            raise ResourceError(
                f'explicit request needs {request.memory_bytes} bytes; '
                f'{memory_limit} available')
        memory = min(request.memory_bytes, memory_limit)
    else:
        memory = request.memory_bytes or memory_limit
    if request.backend_id.startswith('gmsh') and ranks != 1:
        raise ResourceError('Gmsh threaded execution requires exactly one rank')
    if request.backend_id.startswith('openfoam') and threads != 1:
        raise ResourceError('OpenFOAM MPI execution requires one thread per rank')
    return ResourceAllocation(
        ranks, threads, request.backend_id, request.profile_id, memory,
        warnings=tuple(warnings))
