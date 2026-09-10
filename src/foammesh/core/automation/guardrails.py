"""AF6 orchestration guardrails (§10).

Deterministic bounds that stop a recipe safely: cell-count and attempt ceilings,
a wall-clock budget, per-utility timeout, a disk-space floor, allowed roots, a
quality target, and stop-on-regression. A recipe cannot silently apply another
remesh/repair once its authorization scope or a guardrail is exhausted.
"""
from __future__ import annotations

from dataclasses import dataclass


class GuardrailViolation(RuntimeError):
    """A guardrail stopped the orchestration; carries a machine-readable reason."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {'code': self.code, 'message': str(self), 'details': self.details}


@dataclass
class Guardrails:
    max_cells: int | None = None
    max_attempts: int = 3
    wall_clock_seconds: float | None = None
    utility_timeout_seconds: float | None = 3600.0
    max_output_bytes: int | None = 16 * 1024 * 1024
    disk_floor_bytes: int | None = None
    allowed_roots: tuple = ()
    max_non_orthogonality: float | None = None      # quality target
    stop_on_regression: bool = True

    def to_dict(self) -> dict:
        return {
            'max_cells': self.max_cells, 'max_attempts': self.max_attempts,
            'wall_clock_seconds': self.wall_clock_seconds,
            'utility_timeout_seconds': self.utility_timeout_seconds,
            'max_output_bytes': self.max_output_bytes,
            'disk_floor_bytes': self.disk_floor_bytes,
            'allowed_roots': [str(root) for root in self.allowed_roots],
            'max_non_orthogonality': self.max_non_orthogonality,
            'stop_on_regression': self.stop_on_regression,
        }

    # -- individual checks (raise GuardrailViolation on breach) ------------ #

    def check_attempt(self, attempt: int) -> None:
        if attempt > self.max_attempts:
            raise GuardrailViolation('attempts_exhausted',
                                     f'exceeded max_attempts={self.max_attempts}',
                                     details={'attempt': attempt})

    def check_cells(self, estimated_cells: int | None) -> None:
        if self.max_cells is not None and estimated_cells is not None \
                and estimated_cells > self.max_cells:
            raise GuardrailViolation('cell_budget_exceeded',
                                     f'estimated {estimated_cells} > max_cells {self.max_cells}',
                                     details={'estimated_cells': estimated_cells})

    def check_wall_clock(self, elapsed_seconds: float) -> None:
        if self.wall_clock_seconds is not None and elapsed_seconds > self.wall_clock_seconds:
            raise GuardrailViolation('wall_clock_exceeded',
                                     f'elapsed {elapsed_seconds:.1f}s > budget {self.wall_clock_seconds}s',
                                     details={'elapsed_seconds': elapsed_seconds})

    def check_disk(self, free_bytes: int | None) -> None:
        if self.disk_floor_bytes is not None and free_bytes is not None \
                and free_bytes < self.disk_floor_bytes:
            raise GuardrailViolation('disk_floor_reached',
                                     f'{free_bytes} bytes free < floor {self.disk_floor_bytes}',
                                     details={'free_bytes': free_bytes})

    def check_output(self, output_bytes: int) -> None:
        if self.max_output_bytes is not None and output_bytes > self.max_output_bytes:
            raise GuardrailViolation(
                'output_budget_exceeded',
                f'output {output_bytes} bytes > budget {self.max_output_bytes}',
                details={'output_bytes': output_bytes})

    def check_root(self, path) -> None:
        from pathlib import Path
        if not self.allowed_roots:
            return
        candidate = Path(path).resolve()
        roots = [Path(r).resolve() for r in self.allowed_roots]
        if not any(candidate == root or root in candidate.parents for root in roots):
            raise GuardrailViolation('path_outside_roots', 'path is outside the allowed roots',
                                     details={'path': str(candidate)})

    def check_quality(self, max_non_ortho: float | None) -> bool:
        """Return True when quality meets the target (or no target/reading)."""
        if self.max_non_orthogonality is None or max_non_ortho is None:
            return True
        return max_non_ortho <= self.max_non_orthogonality

    def check_regression(self, before: float | None, after: float | None) -> None:
        if (self.stop_on_regression and before is not None and after is not None
                and after > before):
            raise GuardrailViolation('quality_regressed',
                                     f'non-orthogonality regressed {before} -> {after}',
                                     details={'before': before, 'after': after})
