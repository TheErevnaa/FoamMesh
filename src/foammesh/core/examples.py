"""Example cases: geometry this project authored, ready to open (Plan 40 UXA8).

An example is a STEP file shipped with the application and a few words about
it. Opening one makes a new case, copies the geometry into it, imports it and
stands on Mesh setup, so a new user is one click from a model worth meshing.
Only geometry this repository created is listed (Plan 40 Q2): the catalogue
file records where each came from.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from resources import resource

CATALOGUE = 'examples/examples.json'
#: The folder inside a case that an example's file is copied into.
CASE_FOLDER = 'geometry_input'


@dataclass(frozen=True)
class Example:
    name: str
    title: str
    file: str
    summary: str

    @property
    def source(self) -> Path:
        return resource.file(f'examples/{self.file}')


def list_examples() -> list[Example]:
    """Every example whose geometry file is present, in catalogue order."""
    with open(resource.file(CATALOGUE), encoding='utf-8') as handle:
        records = json.load(handle)['examples']
    found = [Example(r['name'], r['title'], r['file'], r['summary'])
             for r in records]
    return [example for example in found if example.source.is_file()]


def find_example(name: str) -> Example:
    for example in list_examples():
        if example.name == name:
            return example
    raise ValueError(f'there is no example called {name!r}')


def stage_example(example: Example, case_path) -> Path:
    """Copy the example's geometry into the case; answers the copy's path."""
    target = Path(case_path) / CASE_FOLDER / example.file
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(example.source, target)
    return target
