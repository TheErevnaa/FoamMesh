"""Viewport captures and the record that makes one worth keeping.

There has never been a working path to a picture of a mesh in this app. The
only trigger was a keyboard shortcut that Qt refused to fire because it was
bound twice, it appeared in no menu and on no button, and users fell back to OS
screen capture -- losing resolution, and losing any record of *which* mesh the
picture shows.

A bare PNG has the second problem too. Each capture is therefore written with a
small JSON sidecar holding the camera, what was visible, the active section and
the mesh fingerprint. That sidecar is what lets the gallery restore the view,
and what lets a stale picture be labelled as one instead of being mistaken for
the current mesh.

Presentation-neutral on purpose: the CLI and the report writer read these
records, and neither has Qt.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

#: Where captures live inside a case. Beside the other FoamMesh sidecars, so a
#: case stays one directory a user can copy.
CAPTURES_DIRNAME = Path('foammesh') / 'captures'
SIDECAR_SUFFIX = '.json'
RECORD_VERSION = 1


class CaptureError(RuntimeError):
    """A capture could not be written or read."""


@dataclass
class CaptureRecord:
    """Everything needed to explain and to restore one picture."""

    image: str
    created: str
    label: str = ''
    camera: dict = field(default_factory=dict)
    visible_actors: list = field(default_factory=list)
    section: dict = field(default_factory=dict)
    scalar: str = ''
    scalar_band: list = field(default_factory=list)
    mesh_fingerprint: str = ''
    #: CP-09 item 8. Which run and which artifact the picture is of. The
    #: fingerprint says whether the mesh has moved on; it does not say *which*
    #: mesh, so a shot of a refused Gmsh candidate and a shot of the accepted
    #: polyMesh sat in the gallery as two unlabelled pictures of a duct.
    run_id: str = ''
    artifact_id: str = ''
    #: The run's own one-line description, kept verbatim so the caption does
    #: not have to be reconstructed from a run that may since have been
    #: discarded.
    result: str = ''
    version: int = RECORD_VERSION

    def is_stale(self, fingerprint: str | None) -> bool:
        """Whether the mesh has moved on since this picture was taken.

        Unknown on either side is *not* stale: claiming staleness without
        evidence is the same class of dishonesty as hiding it.
        """
        if not self.mesh_fingerprint or not fingerprint:
            return False
        return self.mesh_fingerprint != fingerprint

    def identity(self) -> str:
        """A one-line "this is the exact run this shows", or ''.

        Empty when the picture predates the identity or was taken with no
        result loaded -- a caption invented for such a shot would be the
        dishonesty this field exists to remove.
        """
        if self.result:
            return self.result
        if self.run_id:
            return self.run_id
        return ''


def captures_dir(case_root: Path | str) -> Path:
    return Path(case_root) / CAPTURES_DIRNAME


def ensure_captures_dir(case_root: Path | str) -> Path:
    """Create the capture directory. Owning this here keeps the view read-only.

    The view layer is not allowed to mutate the filesystem -- an architecture
    gate enforces it -- and that rule is the reason every artefact in this app
    has a single writer.
    """
    directory = captures_dir(case_root)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def capture_paths(case_root: Path | str, stamp: str) -> tuple[Path, Path]:
    """(image, sidecar) for a capture named ``stamp``."""
    directory = captures_dir(case_root)
    return directory / f'{stamp}.png', directory / f'{stamp}{SIDECAR_SUFFIX}'


def new_stamp(moment: datetime | None = None) -> str:
    moment = moment or datetime.now(timezone.utc)
    return moment.strftime('%Y%m%d-%H%M%S-%f')[:-3]


def write_record(sidecar: Path, record: CaptureRecord) -> Path:
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(
        json.dumps(asdict(record), indent=2, sort_keys=True), encoding='utf-8')
    return sidecar


def read_record(sidecar: Path) -> CaptureRecord | None:
    try:
        document = json.loads(Path(sidecar).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    known = {name: document[name] for name in CaptureRecord.__annotations__
             if name in document}
    try:
        return CaptureRecord(**known)
    except TypeError:
        return None


def list_captures(case_root: Path | str) -> list[CaptureRecord]:
    """Every capture in the case, newest first.

    A PNG whose sidecar is missing or unreadable is still listed -- the picture
    exists and dropping it silently would be worse than listing it without a
    view to restore.
    """
    directory = captures_dir(case_root)
    if not directory.is_dir():
        return []

    records = []
    for image in sorted(directory.glob('*.png')):
        sidecar = image.with_suffix(SIDECAR_SUFFIX)
        record = read_record(sidecar) if sidecar.is_file() else None
        if record is None:
            record = CaptureRecord(
                image=image.name,
                created=datetime.fromtimestamp(
                    image.stat().st_mtime, timezone.utc).isoformat())
        else:
            record.image = image.name
        records.append(record)

    records.sort(key=lambda item: item.created, reverse=True)
    return records


def delete_capture(case_root: Path | str, image_name: str) -> bool:
    directory = captures_dir(case_root)
    image = directory / Path(image_name).name
    if not image.is_file():
        return False
    sidecar = image.with_suffix(SIDECAR_SUFFIX)
    image.unlink()
    if sidecar.is_file():
        sidecar.unlink()
    return True
