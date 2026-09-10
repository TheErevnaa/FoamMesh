"""Stable identity for the features a fidelity tolerance can be attached to.

Plan 23 §7.1. Tolerance precedence rule 1 is a per-feature override, and before
this there was nothing to hang it on: no feature identity, no manifest, no
producer. A "critical feature" that cannot be named the same way twice is not
implementable and not reproducible.

CAD edge numbers cannot serve as that name. Healing renumbers them --
``cad/healing_pipeline.py`` exists precisely because face IDs do not survive
OCCT operations, and it already maps old IDs to surviving ``patch_uuid``s for
the same reason. So a ``feature_uuid`` is *minted* once and carried forward by
an explicit old-to-new map, exactly as ``patch_uuid`` is.

**The manifest lives outside the prepared revision.** A prepared revision is
immutable and content-verified, and its digest covers only groups, regions and
declared sources. Adding a file to it afterwards would be neither covered nor
legal, and generating one inside ``materialize()`` would change ``revision_id``
for every case that already exists on disk. It is derived data with an explicit
provenance key, so it is stored as such:

    foammesh/geometry/features/<prepared_revision_id>/feature-manifest.json
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Iterable, Mapping
import uuid as _uuid

FEATURE_SCHEMA_VERSION = 1
MANIFEST_NAME = 'feature-manifest.json'

#: Where a feature came from. Detected and authored features differ in origin
#: and in whether they default to critical -- not in schema, so one code path
#: serves both (§7.1 rule 2).
ORIGINS = ('cad_edge', 'crease', 'corner', 'gap', 'thickness', 'user')

#: §16.6 defaults. The crease threshold decides which features *exist*, so it
#: is manifest-level detection rather than a per-feature tolerance: changing it
#: changes the feature set and invalidates GF0 and everything downstream.
DEFAULT_FEATURE_ANGLE_DEG = 30.0
DEFAULT_PAIRING_ANGLE_DEG = 15.0


class FeatureManifestError(ValueError):
    pass


@dataclass(frozen=True)
class FeaturePolicy:
    """Per-feature tolerance, read by precedence rule 1."""

    max_distance_m: float | None = None
    max_tangent_angle_deg: float | None = None
    min_length_coverage: float | None = None
    min_elements_along: int | None = None
    #: §16.2: no universal hard default. ``None`` means measured but not gated.
    min_cells_across: int | None = None

    def to_dict(self) -> dict:
        return {key: value for key, value in {
            'max_distance_m': self.max_distance_m,
            'max_tangent_angle_deg': self.max_tangent_angle_deg,
            'min_length_coverage': self.min_length_coverage,
            'min_elements_along': self.min_elements_along,
            'min_cells_across': self.min_cells_across,
        }.items() if value is not None}

    @classmethod
    def from_dict(cls, value: Mapping | None) -> 'FeaturePolicy':
        value = value or {}
        return cls(
            max_distance_m=value.get('max_distance_m'),
            max_tangent_angle_deg=value.get('max_tangent_angle_deg'),
            min_length_coverage=value.get('min_length_coverage'),
            min_elements_along=value.get('min_elements_along'),
            min_cells_across=value.get('min_cells_across'))


@dataclass(frozen=True)
class Feature:
    """One named feature, and the sections it belongs to."""

    feature_uuid: str
    origin: str
    #: What lets a failed feature fail *its* section rather than the model.
    owner_patch_uuids: tuple[str, ...] = ()
    owner_region_uuids: tuple[str, ...] = ()
    critical: bool = False
    #: ``{'kind': 'polyline'|'point'|'surface_pair', ...}``
    geometry: dict = field(default_factory=dict)
    policy: FeaturePolicy = field(default_factory=FeaturePolicy)
    #: A stable description of the shape, used to relocate this feature in a
    #: later revision. Not an identity -- geometry moves, identity must not.
    signature: str = ''

    def __post_init__(self):
        if self.origin not in ORIGINS:
            raise FeatureManifestError(f'unknown feature origin: {self.origin}')
        if not str(self.feature_uuid).strip():
            raise FeatureManifestError('a feature requires a stable UUID')

    def to_dict(self) -> dict:
        return {
            'feature_uuid': self.feature_uuid, 'origin': self.origin,
            'owner_patch_uuids': list(self.owner_patch_uuids),
            'owner_region_uuids': list(self.owner_region_uuids),
            'critical': self.critical, 'geometry': dict(self.geometry),
            'policy': self.policy.to_dict(), 'signature': self.signature,
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> 'Feature':
        return cls(
            feature_uuid=str(value['feature_uuid']),
            origin=str(value['origin']),
            owner_patch_uuids=tuple(value.get('owner_patch_uuids', ())),
            owner_region_uuids=tuple(value.get('owner_region_uuids', ())),
            critical=bool(value.get('critical', False)),
            geometry=dict(value.get('geometry') or {}),
            policy=FeaturePolicy.from_dict(value.get('policy')),
            signature=str(value.get('signature') or ''))


@dataclass(frozen=True)
class FeatureManifest:
    """Every feature of one prepared revision."""

    prepared_revision_id: str
    features: tuple[Feature, ...] = ()
    detection: dict = field(default_factory=dict)
    #: Features a previous revision had that could not be relocated here.
    #: Reported as a GF0 finding rather than silently dropped (§7.1 rule 1).
    lost: tuple[dict, ...] = ()

    def critical(self) -> tuple[Feature, ...]:
        return tuple(item for item in self.features if item.critical)

    def for_patch(self, patch_uuid: str) -> tuple[Feature, ...]:
        return tuple(item for item in self.features
                     if patch_uuid in item.owner_patch_uuids)

    def get(self, feature_uuid: str) -> Feature | None:
        for item in self.features:
            if item.feature_uuid == feature_uuid:
                return item
        return None

    def fingerprint(self) -> str:
        """Digest of the feature set, for the report's staleness check."""
        payload = json.dumps(self.to_dict(), sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict:
        return {
            'schema_version': FEATURE_SCHEMA_VERSION,
            'prepared_revision_id': self.prepared_revision_id,
            'detection': dict(self.detection),
            'features': [item.to_dict() for item in self.features],
            'lost': [dict(item) for item in self.lost],
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> 'FeatureManifest':
        if int(value.get('schema_version', 0)) != FEATURE_SCHEMA_VERSION:
            raise FeatureManifestError('unsupported feature manifest schema')
        return cls(
            prepared_revision_id=str(value.get('prepared_revision_id') or ''),
            features=tuple(Feature.from_dict(item)
                           for item in value.get('features', ())),
            detection=dict(value.get('detection') or {}),
            lost=tuple(dict(item) for item in value.get('lost', ())))


def default_detection() -> dict:
    return {
        'feature_angle_deg': DEFAULT_FEATURE_ANGLE_DEG,
        'pairing_angle_deg': DEFAULT_PAIRING_ANGLE_DEG,
        'calculation_version': 1,
    }


def mint() -> str:
    """A new feature identity. Assigned once, never derived from geometry."""
    return str(_uuid.uuid4())


def carry_forward(previous: FeatureManifest | None,
                  candidates: Iterable[Feature],
                  *, prepared_revision_id: str,
                  detection: Mapping | None = None) -> FeatureManifest:
    """Give each candidate the identity it had before, where it still exists.

    This is what "carried forward" means, and without it the stability
    guarantee is a wish: a candidate whose signature matches a previous
    feature inherits that feature's ``feature_uuid`` and its authored policy,
    so a tolerance a user attached survives healing and re-preparation.

    A previous feature that no candidate matches is recorded as ``lost``.
    Dropping it silently is the failure mode this exists to prevent -- a
    critical feature would stop being checked and nothing would say so.
    """
    candidates = list(candidates)
    prior = {item.signature: item
             for item in (previous.features if previous else ())
             if item.signature}
    claimed: set[str] = set()
    carried: list[Feature] = []

    for candidate in candidates:
        match = prior.get(candidate.signature)
        if match is None or match.feature_uuid in claimed:
            carried.append(candidate)
            continue
        claimed.add(match.feature_uuid)
        carried.append(Feature(
            feature_uuid=match.feature_uuid,
            origin=candidate.origin,
            owner_patch_uuids=candidate.owner_patch_uuids,
            owner_region_uuids=candidate.owner_region_uuids,
            # Authored intent outranks a fresh detection default: a user who
            # marked a feature critical, or set a tolerance on it, must not
            # have that undone by re-running detection.
            critical=match.critical or candidate.critical,
            geometry=candidate.geometry,
            policy=match.policy if match.policy.to_dict() else candidate.policy,
            signature=candidate.signature))

    lost = tuple(
        {'feature_uuid': item.feature_uuid, 'origin': item.origin,
         'critical': item.critical, 'signature': item.signature,
         'reason': 'no matching feature in the new prepared revision'}
        for item in (previous.features if previous else ())
        if item.feature_uuid not in claimed)

    return FeatureManifest(
        prepared_revision_id=str(prepared_revision_id),
        features=tuple(carried),
        detection=dict(detection or (previous.detection if previous else None)
                       or default_detection()),
        lost=lost)


class FeatureManifestStore:
    """Read and atomically publish the feature manifest of one revision."""

    def __init__(self, case_path: str | Path):
        self.case_path = Path(case_path).resolve()
        self.root = self.case_path / 'foammesh' / 'geometry' / 'features'

    def path_for(self, prepared_revision_id: str) -> Path:
        revision = str(prepared_revision_id)
        if not revision or '/' in revision or '\\' in revision:
            raise FeatureManifestError(
                f'invalid prepared revision id: {prepared_revision_id!r}')
        return self.root / revision / MANIFEST_NAME

    def read(self, prepared_revision_id: str) -> FeatureManifest | None:
        path = self.path_for(prepared_revision_id)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise FeatureManifestError(
                f'feature manifest could not be read: {path}') from error
        return FeatureManifest.from_dict(document)

    def write(self, manifest: FeatureManifest) -> Path:
        path = self.path_for(manifest.prepared_revision_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(
            json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, path)
        return path
