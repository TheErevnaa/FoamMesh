"""Per-case viewport state: named views, and the frame plans for animations.

Presentation-neutral, like `core.capture`: the CLI and the report writer read
these and neither has Qt.

Rule 5 of Plan 27 says everything is reversible and nothing is sticky by
accident, so only the user's own choices are persisted here -- named views they
saved deliberately -- and never incidental camera drift.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

VIEWS_FILENAME = Path('foammesh') / 'views.json'
VIEWS_VERSION = 1

#: Frame counts chosen so a sequence is watchable but does not take minutes to
#: render on a large mesh. Arbitrary keyframing is deliberately out of scope.
ORBIT_FRAMES = 48
SWEEP_FRAMES = 32


class ViewStateError(RuntimeError):
    """Named views could not be read or written."""


def views_path(case_root: Path | str) -> Path:
    return Path(case_root) / VIEWS_FILENAME


def load_views(case_root: Path | str) -> dict[str, dict]:
    """Every named view in the case. A broken file reads as none, not as a crash."""
    path = views_path(case_root)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    views = document.get('views') if isinstance(document, dict) else None
    if not isinstance(views, dict):
        return {}
    return {name: camera for name, camera in views.items()
            if isinstance(camera, dict)}


def save_view(case_root: Path | str, name: str, camera: dict) -> dict[str, dict]:
    """Add or replace one named view. Returns the full set."""
    name = (name or '').strip()
    if not name:
        raise ViewStateError('a named view needs a name')
    views = load_views(case_root)
    views[name] = dict(camera)
    _write(case_root, views)
    return views


def delete_view(case_root: Path | str, name: str) -> dict[str, dict]:
    views = load_views(case_root)
    views.pop(name, None)
    _write(case_root, views)
    return views


def _write(case_root: Path | str, views: dict[str, dict]) -> None:
    path = views_path(case_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({'version': VIEWS_VERSION, 'views': views},
                   indent=2, sort_keys=True),
        encoding='utf-8')


# -- animation frame plans -------------------------------------------------- #


@dataclass(frozen=True)
class OrbitFrame:
    """One camera position on a circle about the scene's up axis."""
    index: int
    azimuth: float


@dataclass(frozen=True)
class SweepFrame:
    """One section-plane origin on a march through the model."""
    index: int
    origin: tuple


def orbit_frames(count: int = ORBIT_FRAMES) -> list[OrbitFrame]:
    """A full revolution, as per-frame azimuth *increments*.

    Increments rather than absolute angles because that is what a camera's
    ``Azimuth`` takes, and accumulating absolute angles would double-rotate.
    """
    if count < 2:
        raise ViewStateError('an orbit needs at least two frames')
    step = 360.0 / count
    return [OrbitFrame(index=index, azimuth=step) for index in range(count)]


def sweep_frames(bounds, normal, count: int = SWEEP_FRAMES) -> list[SweepFrame]:
    """March a plane origin across ``bounds`` along ``normal``.

    The first and last frames sit just inside the extremes: a plane exactly on
    the boundary produces an empty or a whole mesh, and two frames of nothing
    at the ends of every sweep look like a bug.
    """
    if count < 2:
        raise ViewStateError('a sweep needs at least two frames')

    length = math.sqrt(sum(value * value for value in normal))
    if not length:
        raise ViewStateError('a sweep needs a non-zero normal')
    unit = [value / length for value in normal]

    centre = bounds.center()
    size = bounds.size()
    span = sum(abs(unit[axis]) * size[axis] for axis in range(3))
    if span <= 0:
        raise ViewStateError('the model has no extent along that normal')

    frames = []
    for index in range(count):
        # 0.02..0.98 of the span, centred on the model.
        fraction = 0.02 + (index / (count - 1)) * 0.96
        offset = (fraction - 0.5) * span
        frames.append(SweepFrame(
            index=index,
            origin=tuple(centre[axis] + offset * unit[axis]
                         for axis in range(3))))
    return frames


def frame_name(sequence: str, index: int, total: int) -> str:
    """Zero-padded so the frames sort in playback order in any file browser."""
    width = max(3, len(str(total - 1)))
    return f'{sequence}-{index:0{width}d}'
