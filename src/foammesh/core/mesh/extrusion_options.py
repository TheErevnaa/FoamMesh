"""Transport-neutral authored extrusion options shared by UI and writers."""
from dataclasses import dataclass
from enum import Enum


class ExtrudeModel(Enum):
    PLANE = 'plane'
    WEDGE = 'wedge'


@dataclass
class ExtrudeOptions:
    model: ExtrudeModel
    thickness: str | None = None
    point: list | None = None
    axis: list | None = None
    angle: str | None = None
