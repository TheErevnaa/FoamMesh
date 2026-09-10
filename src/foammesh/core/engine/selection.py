"""Previewed, reversible meshing-engine selection.

Selection never deletes native state or accepted artifacts.  It changes one
persisted engine identity through :class:`ProjectState`, retains shared intent,
and reports the downstream artifacts that consumers must mark stale.
"""
from __future__ import annotations

from dataclasses import dataclass

from foammesh.core.project import Source
from foammesh.db.configurations_schema import MeshEngine

from .registry import (
    configured_engine_id, configured_target_solver, engine_incompatibility,
)


_KNOWN = (MeshEngine.SNAPPY.value, MeshEngine.GMSH.value)
_SHARED_STATE = (
    'geometry', 'geometryPreparation', 'region',
)
_NATIVE_STATE = {
    'snappy': ('baseGrid', 'castellation', 'snap', 'addLayers', 'meshQuality'),
    'gmsh': ('gmsh',),
}
_INVALIDATED = {
    'snappy': (
        'snappy.domain_regions', 'snappy.base_grid', 'snappy.surface_features',
        'snappy.castellation', 'snappy.snap', 'snappy.layers', 'snappy.qa',
        'common.export',
    ),
    'gmsh': (
        'gmsh.describe_geometry', 'gmsh.global_sizing', 'gmsh.size_fields',
        'gmsh.curve_controls', 'gmsh.volume_controls', 'gmsh.boundary_layers',
        'gmsh.periodic', 'gmsh.compute', 'gmsh.publish', 'gmsh.qa',
        'common.export',
    ),
}


@dataclass(frozen=True)
class EngineSwitchPreview:
    current_engine: str
    requested_engine: str
    changed: bool
    retained_shared_state: tuple[str, ...]
    inactive_native_state: tuple[str, ...]
    activated_native_state: tuple[str, ...]
    invalidated_tasks: tuple[str, ...]
    retained_artifacts: tuple[str, ...]
    missing_capabilities: tuple[str, ...]
    runtime_available: bool
    unavailable_reason: str
    confirmation_required: bool
    # Plan 28. Empty unless the engine cannot produce a mesh the project's
    # target solver can read. Distinct from `unavailable_reason`, which is
    # about this machine: an incompatible engine would be just as wrong on
    # a machine where its runtime is present.
    incompatible_reason: str = ''

    @property
    def selectable(self) -> bool:
        return (self.runtime_available and not self.missing_capabilities
                and not self.incompatible_reason)

    def to_dict(self) -> dict:
        return {
            'current_engine': self.current_engine,
            'requested_engine': self.requested_engine,
            'changed': self.changed,
            'retained_shared_state': list(self.retained_shared_state),
            'inactive_native_state': list(self.inactive_native_state),
            'activated_native_state': list(self.activated_native_state),
            'invalidated_tasks': list(self.invalidated_tasks),
            'retained_artifacts': list(self.retained_artifacts),
            'missing_capabilities': list(self.missing_capabilities),
            'runtime_available': self.runtime_available,
            'unavailable_reason': self.unavailable_reason,
            'confirmation_required': self.confirmation_required,
            'incompatible_reason': self.incompatible_reason,
            'selectable': self.selectable,
        }


class EngineSelectionService:
    def known_engines(self) -> tuple[str, ...]:
        return _KNOWN

    def preview(self, db, requested_engine: str, *, probe=None,
                retained_artifacts=()) -> EngineSwitchPreview:
        requested = self._normalize(requested_engine)
        current = configured_engine_id(db)
        changed = requested != current
        missing = ()
        available = True
        reason = ''
        if probe is not None:
            available = bool(probe.available)
            reason = str(probe.reason or '')
            missing = tuple(
                name for name, supported in probe.capabilities if not supported)
        artifacts = tuple(str(item) for item in retained_artifacts)
        return EngineSwitchPreview(
            current_engine=current,
            requested_engine=requested,
            changed=changed,
            retained_shared_state=_SHARED_STATE,
            inactive_native_state=_NATIVE_STATE.get(current, ()) if changed else (),
            activated_native_state=_NATIVE_STATE.get(requested, ()) if changed else (),
            invalidated_tasks=_INVALIDATED.get(requested, ()) if changed else (),
            retained_artifacts=artifacts,
            missing_capabilities=missing,
            runtime_available=available,
            unavailable_reason=reason,
            confirmation_required=changed and bool(artifacts),
            incompatible_reason=engine_incompatibility(
                requested, configured_target_solver(db)),
        )

    def apply(self, state, requested_engine: str, *, probe=None,
              retained_artifacts=(), source: Source = Source.GUI,
              reason: str = 'meshing method selected'):
        preview = self.preview(
            state.db, requested_engine, probe=probe,
            retained_artifacts=retained_artifacts)
        # The page hides an incompatible engine; this is what a CLI call or a
        # restored session meets, so it has to refuse in its own right.
        if preview.incompatible_reason:
            raise ValueError(
                f'meshing engine cannot serve this solver: '
                f'{preview.requested_engine}: {preview.incompatible_reason}')
        if not preview.selectable:
            detail = preview.unavailable_reason or ', '.join(preview.missing_capabilities)
            raise ValueError(
                f'meshing engine is unavailable: {preview.requested_engine}: {detail}')
        if not preview.changed:
            return preview, None
        editable = state.checkout()
        editable.setValue('mesh/engine', MeshEngine(preview.requested_engine))
        transaction = state.commit(
            editable,
            action='select meshing engine', source=source,
            target='mesh/engine', reason=reason)
        return preview, transaction

    @staticmethod
    def _normalize(engine_id: str) -> str:
        if isinstance(engine_id, MeshEngine):
            return engine_id.value
        token = str(engine_id or '').strip().lower()
        if token not in _KNOWN:
            raise ValueError(
                f'unknown meshing engine {engine_id!r}; supported: {", ".join(_KNOWN)}')
        return token
