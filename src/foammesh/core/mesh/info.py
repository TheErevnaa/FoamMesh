"""Structured, read-only OpenFOAM ``polyMesh`` inspection.

The parser intentionally reports unknown values as ``None`` plus warnings.  It
never invents zero counts for binary or unfamiliar files, and it does not need
an OpenFOAM process, making it suitable for the GUI, API, and CLI.
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from foammesh.core.case import classify_case, fingerprint_poly_mesh


_UNIT_SCALE = {'m': 1.0, 'cm': 100.0, 'mm': 1000.0, 'um': 1_000_000.0}
_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'


@dataclass(frozen=True)
class FoamFileMetadata:
    name: str
    format: str | None = None
    file_class: str | None = None
    location: str | None = None
    object_name: str | None = None
    compression: str = 'none'


@dataclass(frozen=True)
class MeshBounds:
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]
    span: tuple[float, float, float]
    unit: str = 'm'


@dataclass(frozen=True)
class BoundaryPatch:
    name: str
    patch_type: str | None
    face_count: int | None
    start_face: int | None


@dataclass(frozen=True)
class MeshZone:
    name: str
    kind: str
    size: int | None


@dataclass(frozen=True)
class MeshInfo:
    case_path: Path
    poly_mesh_path: Path
    files: tuple[str, ...]
    total_bytes: int
    fingerprint: str
    points: int | None = None
    faces: int | None = None
    internal_faces: int | None = None
    cells: int | None = None
    bounds: MeshBounds | None = None
    patches: tuple[BoundaryPatch, ...] = ()
    zones: tuple[MeshZone, ...] = ()
    file_metadata: tuple[FoamFileMetadata, ...] = ()
    mesh_location: str = 'constant/polyMesh'
    mesh_time: str = 'constant'
    display_unit: str = 'm'
    last_quality: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> 'MeshInfo':
        """Rehydrate the transport DTO for desktop rendering only."""
        counts = data.get('counts') or {}
        bounds = data.get('bounds')
        return cls(
            case_path=Path(data['case_path']), poly_mesh_path=Path(data['poly_mesh_path']),
            files=tuple(data.get('files', ())), total_bytes=int(data.get('total_bytes', 0)),
            fingerprint=str(data.get('fingerprint', '')),
            points=counts.get('points'), faces=counts.get('faces'),
            internal_faces=counts.get('internal_faces'), cells=counts.get('cells'),
            bounds=MeshBounds(**bounds) if bounds else None,
            patches=tuple(BoundaryPatch(**item) for item in data.get('patches', ())),
            zones=tuple(MeshZone(**item) for item in data.get('zones', ())),
            file_metadata=tuple(FoamFileMetadata(**item) for item in data.get('file_metadata', ())),
            mesh_location=str(data.get('mesh_location', 'constant/polyMesh')),
            mesh_time=str(data.get('mesh_time', 'constant')),
            display_unit=str(data.get('display_unit', 'm')),
            last_quality=data.get('last_quality'), warnings=tuple(data.get('warnings', ())),
        )

    @property
    def boundary_patches(self) -> tuple[str, ...]:
        """Compatibility view used by existing shell and API callers."""
        return tuple(patch.name for patch in self.patches)

    def to_dict(self) -> dict[str, Any]:
        return {
            'case_path': str(self.case_path),
            'poly_mesh_path': str(self.poly_mesh_path),
            'mesh_location': self.mesh_location,
            'mesh_time': self.mesh_time,
            'files': list(self.files),
            'total_bytes': self.total_bytes,
            'fingerprint': self.fingerprint,
            'counts': {
                'points': self.points, 'faces': self.faces,
                'internal_faces': self.internal_faces, 'cells': self.cells,
                'boundary_patches': len(self.patches),
            },
            'bounds': asdict(self.bounds) if self.bounds else None,
            'patches': [asdict(item) for item in self.patches],
            'boundary_patches': list(self.boundary_patches),
            'zones': [asdict(item) for item in self.zones],
            'file_metadata': [asdict(item) for item in self.file_metadata],
            'display_unit': self.display_unit,
            'last_quality': self.last_quality,
            'warnings': list(self.warnings),
        }

    def to_text(self) -> str:
        def value(item):
            return 'unknown' if item is None else f'{item:,}'
        lines = [
            f'polyMesh: {self.poly_mesh_path}',
            f'Location/time: {self.mesh_location} ({self.mesh_time})',
            (f'Points: {value(self.points)}  Faces: {value(self.faces)}  '
             f'Internal faces: {value(self.internal_faces)}  Cells: {value(self.cells)}'),
            f'Size: {self.total_bytes:,} bytes',
            f'Fingerprint: {self.fingerprint}',
        ]
        if self.bounds:
            lines.append(
                f'Bounds ({self.bounds.unit}): min={self.bounds.minimum} '
                f'max={self.bounds.maximum} span={self.bounds.span}')
        lines.append('Patches: ' + (', '.join(self.boundary_patches) or 'none'))
        if self.zones:
            lines.append('Zones: ' + ', '.join(f'{z.name} ({z.kind})' for z in self.zones))
        if self.last_quality:
            stale = 'stale' if self.last_quality.get('stale') else self.last_quality.get('severity', 'unknown')
            lines.append(f'Last Mesh check: {stale} at {self.last_quality.get("checked_at", "unknown")}')
        if self.warnings:
            lines.append('Warnings: ' + '; '.join(self.warnings))
        return '\n'.join(lines)

    def to_csv(self) -> str:
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(('section', 'name', 'type', 'count', 'start', 'value'))
        for name, count in (
                ('points', self.points), ('faces', self.faces),
                ('internal_faces', self.internal_faces), ('cells', self.cells)):
            writer.writerow(('counts', name, '', count if count is not None else '', '', ''))
        if self.bounds:
            for name, vector in (
                    ('minimum', self.bounds.minimum), ('maximum', self.bounds.maximum),
                    ('span', self.bounds.span)):
                writer.writerow(('bounds', name, self.bounds.unit, '', '', ' '.join(map(str, vector))))
        for patch in self.patches:
            writer.writerow(('patch', patch.name, patch.patch_type or '',
                             patch.face_count if patch.face_count is not None else '',
                             patch.start_face if patch.start_face is not None else '', ''))
        for zone in self.zones:
            writer.writerow(('zone', zone.name, zone.kind,
                             zone.size if zone.size is not None else '', '', ''))
        return output.getvalue()


class MeshInfoService:
    """Inspect and export a complete mesh without modifying case files."""

    def inspect(self, case_path: str | Path, *, display_unit: str = 'm') -> MeshInfo:
        if display_unit not in _UNIT_SCALE:
            raise ValueError(f'unsupported display unit: {display_unit}')
        classification = classify_case(case_path)
        if not classification.has_mesh or classification.poly_mesh_path is None:
            raise ValueError('no complete constant/polyMesh was found')
        root = classification.poly_mesh_path
        fingerprint = fingerprint_poly_mesh(root)
        files = tuple(sorted(path.name for path in root.iterdir() if path.is_file()))
        total = sum(path.stat().st_size for path in root.iterdir() if path.is_file())
        warnings: list[str] = []

        texts: dict[str, str | None] = {}
        metadata = []
        for name in files:
            path = root / name
            text = _read_text(path, warnings)
            texts[_logical_name(name)] = text
            metadata.append(_metadata(path, text))

        point_count = _declared_count(texts.get('points'))
        face_count = _declared_count(texts.get('faces'))
        internal_faces = _declared_count(texts.get('neighbour'))
        owner_labels = _label_values(texts.get('owner'))
        neighbour_labels = _label_values(texts.get('neighbour'))
        labels = owner_labels + neighbour_labels
        cells = max(labels) + 1 if labels else None
        points = _point_values(texts.get('points'))
        if point_count is None:
            warnings.append('point count could not be read')
        if face_count is None:
            warnings.append('face count could not be read')
        if cells is None:
            warnings.append('cell count could not be derived from owner/neighbour')
        bounds = _bounds(points, display_unit) if points else None
        if bounds is None:
            warnings.append('bounding box is unavailable (binary or unsupported points file)')

        patches = _parse_named_blocks(texts.get('boundary'), boundary=True)
        zones = []
        for kind in ('cellZones', 'faceZones', 'pointZones'):
            zones.extend(_parse_zones(texts.get(kind), kind))

        try:
            relative = root.relative_to(classification.path).as_posix()
        except ValueError:
            relative = str(root)
        mesh_time = root.parent.name if root.name == 'polyMesh' else 'constant'
        quality = _quality_summary(classification.path, fingerprint.digest)
        return MeshInfo(
            case_path=classification.path, poly_mesh_path=root, files=files,
            total_bytes=total, fingerprint=fingerprint.digest,
            points=point_count, faces=face_count, internal_faces=internal_faces,
            cells=cells, bounds=bounds, patches=tuple(patches), zones=tuple(zones),
            file_metadata=tuple(metadata), mesh_location=relative,
            mesh_time=mesh_time, display_unit=display_unit,
            last_quality=quality, warnings=tuple(dict.fromkeys(warnings)),
        )

    @staticmethod
    def save_report(info: MeshInfo, destination: str | Path) -> Path:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix.lower() == '.json':
            text = json.dumps(info.to_dict(), indent=2, sort_keys=True) + '\n'
        elif path.suffix.lower() == '.csv':
            text = info.to_csv()
        else:
            raise ValueError('mesh report destination must end in .json or .csv')
        path.write_text(text, encoding='utf-8', newline='')
        return path


def _logical_name(name: str) -> str:
    return name[:-3] if name.endswith('.gz') else name


def _read_text(path: Path, warnings: list[str]) -> str | None:
    try:
        if path.suffix == '.gz':
            with gzip.open(path, 'rt', encoding='utf-8', errors='replace') as source:
                return source.read()
        return path.read_text(encoding='utf-8', errors='replace')
    except (OSError, EOFError) as error:
        warnings.append(f'{path.name}: {error}')
        return None


def _without_comments(text: str) -> str:
    text = re.sub(r'/\*.*?\*/', ' ', text, flags=re.S)
    return re.sub(r'//[^\n]*', ' ', text)


def _header(text: str | None) -> dict[str, str]:
    if not text:
        return {}
    match = re.search(r'\bFoamFile\s*\{(.*?)\}', _without_comments(text), flags=re.S)
    if not match:
        return {}
    return {key: value.strip('"') for key, value in re.findall(
        r'([A-Za-z][A-Za-z0-9_]*)\s+("[^"]*"|[^;\s]+)\s*;', match.group(1))}


def _metadata(path: Path, text: str | None) -> FoamFileMetadata:
    header = _header(text)
    return FoamFileMetadata(
        name=_logical_name(path.name), format=header.get('format'),
        file_class=header.get('class'), location=header.get('location'),
        object_name=header.get('object'),
        compression='gzip' if path.suffix == '.gz' else header.get('compression', 'none'))


def _body(text: str | None) -> str:
    if not text:
        return ''
    cleaned = _without_comments(text)
    return re.sub(r'\bFoamFile\s*\{.*?\}', ' ', cleaned, count=1, flags=re.S)


def _declared_count(text: str | None) -> int | None:
    match = re.search(r'(^|\s)(\d+)\s*\(', _body(text))
    return int(match.group(2)) if match else None


def _list_content(text: str | None) -> str:
    body = _body(text)
    match = re.search(r'\d+\s*\(', body)
    if not match:
        return ''
    start = match.end()
    end = body.rfind(')')
    return body[start:end] if end >= start else ''


def _label_values(text: str | None) -> list[int]:
    if _header(text).get('format', 'ascii').lower() == 'binary':
        return []
    return [int(value) for value in re.findall(r'(?<![.eE])\b\d+\b', _list_content(text))]


def _point_values(text: str | None) -> list[tuple[float, float, float]]:
    if _header(text).get('format', 'ascii').lower() == 'binary':
        return []
    return [tuple(float(value) for value in match) for match in re.findall(
        rf'\(\s*({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s*\)', _list_content(text))]


def _bounds(points: list[tuple[float, float, float]], unit: str) -> MeshBounds:
    factor = _UNIT_SCALE[unit]
    minimum = tuple(min(point[index] for point in points) * factor for index in range(3))
    maximum = tuple(max(point[index] for point in points) * factor for index in range(3))
    span = tuple(maximum[index] - minimum[index] for index in range(3))
    return MeshBounds(minimum, maximum, span, unit)


def _parse_named_blocks(text: str | None, *, boundary=False) -> list[BoundaryPatch]:
    blocks = []
    for name, content in re.findall(r'(?m)^\s*([^\s(){}]+)\s*\{([^{}]*)\}', _list_content(text), re.S):
        patch_type = _field(content, 'type')
        count = _integer_field(content, 'nFaces')
        start = _integer_field(content, 'startFace')
        if boundary or patch_type or count is not None:
            blocks.append(BoundaryPatch(name, patch_type, count, start))
    return blocks


def _parse_zones(text: str | None, kind: str) -> list[MeshZone]:
    zones = []
    for name, content in re.findall(r'(?m)^\s*([^\s(){}]+)\s*\{([^{}]*)\}', _list_content(text), re.S):
        size_match = re.search(r'\b(?:cellLabels|faceLabels|pointLabels|labels)\s+(\d+)\s*\(', content)
        zones.append(MeshZone(name, kind, int(size_match.group(1)) if size_match else None))
    return zones


def _field(content: str, name: str) -> str | None:
    match = re.search(rf'\b{re.escape(name)}\s+([^;\s]+)\s*;', content)
    return match.group(1).strip('"') if match else None


def _integer_field(content: str, name: str) -> int | None:
    value = _field(content, name)
    return int(value) if value is not None and value.isdigit() else None


def _quality_summary(case_path: Path, fingerprint: str) -> dict[str, Any] | None:
    try:
        from foammesh.core.quality import MeshCheckService
        report = MeshCheckService.load_report(case_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if report is None:
        return None
    return {
        'checked_at': report.checked_at, 'severity': report.result.severity,
        'mesh_ok': report.result.mesh_ok,
        'stale': report.mesh_fingerprint != fingerprint,
        'mesh_fingerprint': report.mesh_fingerprint,
    }
