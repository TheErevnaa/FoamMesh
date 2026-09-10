"""The independent surface a mesh is measured against.

Plan 23 §5.2 and §16.1. The reference must not be the tessellation handed to
the mesher: a defect present in the mesher's input would then certify itself.
So a *third* surface is generated, at a deflection tight enough that its own
error is a small, recorded fraction of the tolerance being judged.

§16.1 fixes that budget. For a CAD source, with ``tau_min`` the smallest
applicable tolerance among its rated sections and features:

    requested OCCT linear deflection = 0.05 * tau_min
    recorded epsilon_reference       = 0.10 * tau_min

The factor-of-two envelope is deliberate: until an exact OCC projection audit
replaces this, qualification does not claim the tessellator achieved more than
the conservative figure. ``epsilon_reference`` is then the third term of
§6.1.1's bound, so it is spent tolerance -- which is why the budget is capped
rather than made arbitrarily tight.

For a **discrete** source the imported surface *is* the truth available, so
``epsilon_reference`` is zero and the report says the check proves conformance
only to that discrete source (§5.2).

**Stored outside the prepared revision.** A revision is immutable and its
digest covers only groups, regions and declared sources, so a surface added
afterwards would be neither covered nor legal, and generating one inside
``materialize()`` would change ``revision_id`` for every case already on disk.
It is keyed by the revision instead:

    foammesh/quality/geometry/validation/<revision>/<chordal_tag>/
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

#: §16.1. Requested deflection and recorded uncertainty, as fractions of the
#: tightest applicable tolerance.
DEFLECTION_FRACTION = 0.05
UNCERTAINTY_FRACTION = 0.10
ANGULAR_DEFLECTION_DEG = 5.0

#: §16.1's numerical floor. Below this a deflection is meaningless and the
#: reference is ``unrated`` rather than silently coarsened.
ABSOLUTE_FLOOR = 1e-12
RELATIVE_FLOOR = 1e-9

REFERENCE_SCHEMA_VERSION = 1
MANIFEST_NAME = 'validation-reference.json'


class ValidationReferenceError(ValueError):
    pass


@dataclass(frozen=True)
class ReferenceBudget:
    """What §16.1 permits for one source, and whether it is achievable."""

    tau_min: float
    linear_deflection: float
    epsilon_reference: float
    floor: float
    rated: bool
    reason: str = ''

    def to_dict(self) -> dict:
        return {
            'tau_min': self.tau_min,
            'linear_deflection': self.linear_deflection,
            'epsilon_reference': self.epsilon_reference,
            'floor': self.floor, 'rated': self.rated, 'reason': self.reason,
        }


def budget_for(tau_min: float, *, characteristic_length: float = 0.0
               ) -> ReferenceBudget:
    """Apply §16.1 to one source.

    Deliberately per CAD source rather than per CFD domain: one tightly
    toleranced component must not force every unrelated source to the same
    deflection.
    """
    tau = float(tau_min)
    floor = max(ABSOLUTE_FLOOR,
                RELATIVE_FLOOR * abs(float(characteristic_length or 0.0)))
    if not tau > 0:
        return ReferenceBudget(
            tau, 0.0, 0.0, floor, False,
            'no applicable tolerance, so the reference cannot be sized')
    deflection = DEFLECTION_FRACTION * tau
    epsilon = UNCERTAINTY_FRACTION * tau
    if deflection < floor:
        # Coarsening silently would spend more of the tolerance than §16.1
        # allows, without saying so. Unrated is the honest outcome.
        return ReferenceBudget(
            tau, deflection, epsilon, floor, False,
            f'the required deflection {deflection:.3g} m is below the '
            f'numerical floor {floor:.3g} m; loosen the tolerance, split the '
            'CAD source, or use the exact-CAD projector')
    return ReferenceBudget(tau, deflection, epsilon, floor, True)


def chordal_tag(deflection: float) -> str:
    """A directory name that is content-addressed by the deflection asked for.

    Tightening the tolerance produces a sibling rather than mutating what an
    existing report was computed against.
    """
    return 'd' + hashlib.sha256(
        f'{float(deflection):.17g}'.encode('ascii')).hexdigest()[:16]


@dataclass(frozen=True)
class ValidationReference:
    """The surfaces one prepared revision is judged against."""

    prepared_revision_id: str
    reference_class: str          # 'cad_backed' | 'discrete'
    budget: ReferenceBudget
    sources: tuple[dict, ...] = ()
    #: True only when every source could be built inside its §16.1 budget.
    rated: bool = False
    reason: str = ''
    detection: dict = field(default_factory=dict)

    @property
    def epsilon_reference(self) -> float:
        """The third term of §6.1.1's bound. Zero for a discrete reference."""
        return 0.0 if self.reference_class == 'discrete' else \
            self.budget.epsilon_reference

    def fingerprint(self) -> str:
        payload = json.dumps(self.to_dict(), sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict:
        return {
            'schema_version': REFERENCE_SCHEMA_VERSION,
            'prepared_revision_id': self.prepared_revision_id,
            'reference_class': self.reference_class,
            'epsilon_reference': self.epsilon_reference,
            'budget': self.budget.to_dict(),
            'sources': [dict(item) for item in self.sources],
            'rated': self.rated, 'reason': self.reason,
            'detection': dict(self.detection),
        }

    @classmethod
    def from_dict(cls, value: Mapping) -> 'ValidationReference':
        if int(value.get('schema_version', 0)) != REFERENCE_SCHEMA_VERSION:
            raise ValidationReferenceError(
                'unsupported validation reference schema')
        budget = value.get('budget') or {}
        return cls(
            prepared_revision_id=str(value.get('prepared_revision_id') or ''),
            reference_class=str(value.get('reference_class') or 'discrete'),
            budget=ReferenceBudget(
                tau_min=float(budget.get('tau_min', 0.0)),
                linear_deflection=float(budget.get('linear_deflection', 0.0)),
                epsilon_reference=float(budget.get('epsilon_reference', 0.0)),
                floor=float(budget.get('floor', ABSOLUTE_FLOOR)),
                rated=bool(budget.get('rated', False)),
                reason=str(budget.get('reason') or '')),
            sources=tuple(dict(item) for item in value.get('sources', ())),
            rated=bool(value.get('rated', False)),
            reason=str(value.get('reason') or ''),
            detection=dict(value.get('detection') or {}))


class ValidationReferenceStore:
    """Publish and resolve validation references, keyed by prepared revision."""

    def __init__(self, case_path: str | Path):
        self.case_path = Path(case_path).resolve()
        self.root = (self.case_path / 'foammesh' / 'quality' / 'geometry'
                     / 'validation')

    def directory(self, prepared_revision_id: str, deflection: float) -> Path:
        revision = str(prepared_revision_id)
        if not revision or '/' in revision or '\\' in revision:
            raise ValidationReferenceError(
                f'invalid prepared revision id: {prepared_revision_id!r}')
        return self.root / revision / chordal_tag(deflection)

    def read(self, prepared_revision_id: str,
             deflection: float) -> ValidationReference | None:
        path = self.directory(prepared_revision_id, deflection) / MANIFEST_NAME
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationReferenceError(
                f'validation reference could not be read: {path}') from error
        return ValidationReference.from_dict(document)

    def write(self, reference: ValidationReference) -> Path:
        directory = self.directory(
            reference.prepared_revision_id, reference.budget.linear_deflection)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / MANIFEST_NAME
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(
            json.dumps(reference.to_dict(), indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        os.replace(temporary, path)
        return path


def materialize(prepared, *, tau_min: float, case_path: str | Path,
                characteristic_length: float = 0.0,
                detection: Mapping | None = None,
                tessellator=None) -> ValidationReference:
    """Build and record the validation reference for a prepared revision.

    The prepared revision is not touched: only the derived directory beside it
    is written, so ``revision_id`` and every stored revision stay valid.

    ``tessellator`` exists so the budget and provenance logic stays testable
    where OCCT is not installed; the default re-tessellates the prepared CAD at
    §16.1's deflection.
    """
    from .surface import ValidationSurfaceError, build as build_surface

    reference_of = getattr(prepared, 'reference', None)
    revision = str(getattr(reference_of, 'revision_id', '') or '')
    if not revision:
        raise ValidationReferenceError(
            'a validation reference needs a prepared revision to key on')

    manifest = getattr(prepared, 'manifest', {}) or {}
    records = list(manifest.get('sources', ()) or ())
    discrete = all(
        str(item.get('source_format', '')).lower() in
        {'stl', 'obj', 'vtk', 'vtp', ''}
        for item in records) if records else True
    reference_class = 'discrete' if discrete else 'cad_backed'

    budget = budget_for(tau_min, characteristic_length=characteristic_length)
    store = ValidationReferenceStore(case_path)
    directory = store.directory(revision, budget.linear_deflection)
    prepared_root = getattr(reference_of, 'root', None)

    sources: list[dict] = []
    failures: list[str] = []
    if budget.rated and prepared_root is not None:
        for record in records:
            try:
                built = build_surface(
                    record, destination_dir=directory,
                    deflection=budget.linear_deflection,
                    prepared_root=Path(prepared_root),
                    tessellator=tessellator)
            except (ValidationSurfaceError, OSError, ImportError,
                    RuntimeError) as error:
                # A reference that could not be built is unrated, never
                # silently absent: the alternative is a fidelity report with
                # nothing behind it.
                failures.append(f'{record.get("geometry_id")}: {error}')
                continue
            sources.append(built.to_dict())
    elif prepared_root is None:
        failures.append('the prepared revision has no materialized sources')

    rated = budget.rated and not failures and bool(sources)
    if not budget.rated:
        reason = budget.reason
    elif failures:
        reason = 'the reference surface could not be built — ' + '; '.join(failures)
    elif reference_class == 'discrete':
        reason = ('conformance is proved against the imported discrete source '
                  'only; detail lost before import cannot be detected')
    else:
        reason = ''

    built = ValidationReference(
        prepared_revision_id=revision, reference_class=reference_class,
        budget=budget, sources=tuple(sources), rated=rated, reason=reason,
        detection=dict(detection or {}))
    store.write(built)
    return built
