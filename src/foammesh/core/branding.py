"""Validation helpers for generated application-branding resources."""
from __future__ import annotations

import hashlib
import json
import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BrandingManifestError(ValueError):
    """The branding manifest or one of its declared assets is invalid."""


@dataclass(frozen=True)
class BrandingManifest:
    path: Path
    document: dict[str, Any]

    @classmethod
    def load(cls, path: str | Path) -> 'BrandingManifest':
        manifest_path = Path(path)
        try:
            document = json.loads(manifest_path.read_text(encoding='utf-8'))
        except FileNotFoundError as error:
            raise BrandingManifestError(f'branding manifest is missing: {manifest_path}') from error
        except (OSError, json.JSONDecodeError) as error:
            raise BrandingManifestError(f'cannot read branding manifest: {error}') from error
        if not isinstance(document, dict) or document.get('schema_version') != 1:
            raise BrandingManifestError('branding manifest schema_version must be 1')
        if not isinstance(document.get('source'), dict) or not isinstance(
                document.get('outputs'), dict):
            raise BrandingManifestError('branding manifest requires source and outputs objects')
        return cls(manifest_path, document)

    def validate_files(self) -> tuple[Path, ...]:
        root = self.path.parent
        declarations = {
            self.document['source']['path']: self.document['source'],
            **self.document['outputs'],
        }
        validated: list[Path] = []
        for name, declaration in declarations.items():
            if Path(name).name != name:
                raise BrandingManifestError(f'branding asset path must be a file name: {name}')
            asset = root / name
            if not asset.is_file():
                raise BrandingManifestError(f'declared branding asset is missing: {asset}')
            expected = declaration.get('sha256')
            actual = hashlib.sha256(asset.read_bytes()).hexdigest()
            if expected != actual:
                raise BrandingManifestError(f'branding asset checksum mismatch: {asset}')
            validated.append(asset)
        return tuple(validated)


DIAGNOSTIC_UTILITIES = (
    'blockMesh', 'surfaceFeatures', 'snappyHexMesh', 'checkMesh',
    'transformPoints', 'foamFormatConvert', 'paraFoam',
)


@dataclass(frozen=True)
class RuntimeDiagnostics:
    """Copyable build/runtime identity shown in About and support reports."""

    product: str
    version: str
    build_id: str
    commit: str
    python: str
    pyside: str
    qt: str
    vtk: str
    platform: str
    openfoam_target: str
    capabilities: tuple[tuple[str, bool, str], ...]
    #: GPUPREF: plain lines on the graphics in use and the dedicated-GPU option.
    graphics: tuple[str, ...] = ()

    @classmethod
    def collect(cls, product: str, version: str, capability_registry=None,
                *, environment: dict[str, str] | None = None) -> 'RuntimeDiagnostics':
        environment = environment if environment is not None else os.environ
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion
        from vtkmodules.vtkCommonCore import vtkVersion
        from foammesh.core.case import DEFAULT_OPENFOAM_TARGET

        build_id, commit = _build_identity(environment)
        capabilities = []
        for utility in DIAGNOSTIC_UTILITIES:
            if capability_registry is None:
                capabilities.append((utility, False, 'not probed'))
                continue
            result = capability_registry.utility(utility)
            capabilities.append((utility, result.available,
                                 result.executable or result.reason))
        return cls(
            product=product,
            version=version,
            build_id=build_id,
            commit=commit,
            python=platform.python_version(),
            pyside=pyside_version,
            qt=qVersion(),
            vtk=vtkVersion.GetVTKVersion(),
            platform=f'{platform.system()} {platform.release()} ({platform.machine()})',
            openfoam_target=DEFAULT_OPENFOAM_TARGET,
            capabilities=tuple(capabilities),
            graphics=cls._graphics_lines(),
        )

    @staticmethod
    def _graphics_lines() -> tuple[str, ...]:
        try:
            from foammesh.app import app
            from foammesh.rendering import gpu_preference
            return tuple(gpu_preference.diagnostic_lines(app.settings))
        except Exception:                                  # noqa: BLE001
            return ()

    def as_text(self) -> str:
        available = ', '.join(name for name, present, _detail in self.capabilities if present) or 'none'
        missing = ', '.join(name for name, present, _detail in self.capabilities if not present) or 'none'
        lines = (
            f'Product: {self.product} {self.version}',
            f'Build: {self.build_id}',
            f'Commit: {self.commit}',
            f'Platform: {self.platform}',
            f'Python: {self.python}',
            f'PySide: {self.pyside}',
            f'Qt: {self.qt}',
            f'VTK: {self.vtk}',
            f'OpenFOAM target: {self.openfoam_target}',
            f'Utilities available: {available}',
            f'Utilities unavailable: {missing}',
        ) + tuple(self.graphics)
        return '\n'.join(lines)


def _build_identity(environment: dict[str, str]) -> tuple[str, str]:
    configured_id = environment.get('FOAMMESH_BUILD_ID')
    configured_commit = environment.get('FOAMMESH_GIT_COMMIT') or environment.get('GIT_COMMIT')
    if configured_id or configured_commit:
        return configured_id or 'development', configured_commit or 'unknown'
    try:
        from resources import resource
        document = json.loads(resource.file('branding/build_info.json').read_text(encoding='utf-8'))
        if document.get('schema_version') == 1:
            return str(document.get('build_id', 'development')), str(document.get('commit', 'unknown'))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return 'development', 'unknown'


#: Plan 44 MF15 / U5. The watermark is small, and always in the bottom-right
#: corner of what the user can see, at any aspect ratio.
WATERMARK_MAX = 40.0
WATERMARK_INSET = 12.0


def watermark_geometry(width: int, height: int, device_pixel_ratio: float = 1.0,
                       clear=None) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return a bottom-right, square-in-pixels VTK logo rectangle.

    ``width`` and ``height`` are the render window in device pixels.
    ``clear`` is the clear area ``(x0, y0, x1, y1)`` in LOGICAL pixels, y down
    (what the sheet and the console leave free); without it the whole view is
    clear. The logo is at most ``WATERMARK_MAX`` logical pixels, its
    bottom-right is ``WATERMARK_INSET`` from the clear area's bottom-right,
    and it is shrunk (never moved off the corner or clipped) when the clear
    area is smaller than it.
    """
    width = max(int(width), 1)
    height = max(int(height), 1)
    dpr = max(float(device_pixel_ratio), 1.0)
    logical_w, logical_h = width / dpr, height / dpr
    if clear is None:
        x0, y0, x1, y1 = 0.0, 0.0, logical_w, logical_h
    else:
        x0, y0, x1, y1 = (float(v) for v in clear)
        x0, x1 = max(0.0, min(x0, logical_w)), max(0.0, min(x1, logical_w))
        y0, y1 = max(0.0, min(y0, logical_h)), max(0.0, min(y1, logical_h))
    room = min(x1 - x0, y1 - y0) - WATERMARK_INSET
    size = max(min(WATERMARK_MAX, room), 1.0)
    left = max(x0, x1 - WATERMARK_INSET - size)
    bottom_from_top = max(y1 - WATERMARK_INSET, size)
    x = left * dpr / width
    y = (logical_h - bottom_from_top) * dpr / height
    return (x, y), (size * dpr / width, size * dpr / height)
