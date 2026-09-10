#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""CAD unit mapping (pure).

STEP/IGES declare a model unit; map it to a FoamMesh length unit so the phase-04
unit/transform machinery can scale to SI metres.
"""
from __future__ import annotations

import re
from pathlib import Path

#: The unit an OCCT XSTEP read actually hands back, whatever the file says.
#:
#: R193. OCCT does not return a STEP or IGES model in the unit the file
#: declares: it converts every length into ``xstep.cascade.unit``, which
#: defaults to millimetres. MEASURED on ``tee.step``, which declares
#: ``SI_UNIT($,.METRE.)`` and spans 0.6 m: the shape OCCT returns spans 600.
#: Scaling that by the *declared* unit is a no-op, so a 0.6 m tee was imported,
#: drawn and sized as a 600 m tee -- and the Gmsh runner, which pins
#: ``Geometry.OCCTargetUnit='M'``, meshed the same file at 0.6.
#:
#: The importer pins the static rather than trusting the default, so this is a
#: statement about what FoamMesh asked for, not a guess about OCCT's mood.
#:
#: Plan 31 C31-06e. MEASURED for IGES as well, because CP-04 recorded that it
#: behaved differently and it does not. One IGES was rewritten three times
#: changing nothing but Global Section fields 14 and 15 -- the same parameter
#: section, byte for byte -- and read with the cascade unit pinned to MM: the
#: copy declaring M came back spanning 0.6, the copy declaring MM 0.0006, the
#: copy declaring INCH 0.01524. That is exactly the declared unit converted
#: into millimetres, so ``IGESControl_Reader`` honours the declaration and
#: obeys this contract the same way ``STEPControl_Reader`` does.
READER_UNIT = 'mm'

# OCCT/STEP unit names (lowercased) -> foammesh unit token (see core.geometry.units)
_CAD_UNIT_MAP = {
    'metre': 'm', 'meter': 'm', 'm': 'm',
    'millimetre': 'mm', 'millimeter': 'mm', 'mm': 'mm',
    'centimetre': 'cm', 'centimeter': 'cm', 'cm': 'cm',
    'micrometre': 'um', 'micron': 'um', 'um': 'um',
    'inch': 'inch', 'in': 'inch',
    'foot': 'ft', 'feet': 'ft', 'ft': 'ft',
}


def map_cad_unit(name: str | None, default: str = 'mm') -> str:
    if not name:
        return default
    return _CAD_UNIT_MAP.get(str(name).strip().lower(), default)


# STEP: `#n=(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.));` or
#       `#n=(CONVERSION_BASED_UNIT('INCH',#m)LENGTH_UNIT()NAMED_UNIT(#k));`
_STEP_CONVERSION_UNIT = re.compile(r"CONVERSION_BASED_UNIT\s*\(\s*'([^']+)'", re.I)
_STEP_SI_LENGTH = re.compile(r"SI_UNIT\s*\(\s*(\.[A-Z]+\.|\$)\s*,\s*\.METRE\.\s*\)", re.I)
_STEP_PREFIXES = {'$': 'm', '.MILLI.': 'mm', '.CENTI.': 'cm', '.MICRO.': 'um'}
# IGES global section, parameter 14: the units flag.
_IGES_UNIT_FLAGS = {1: 'inch', 2: 'mm', 4: 'ft', 6: 'm', 9: 'um', 10: 'cm'}
# Parameter 15 is the unit's name, `<n>H<name>`. Flag 3 means "the name is
# the only statement of the unit", and the flags this table does not carry
# (5 mile, 7 km, 8 mil, 11 microinch) still name themselves there, so the
# name is read whenever the flag does not settle it.
_IGES_HOLLERITH = re.compile(r'^\s*(\d+)H(.*)$', re.S)


def declared_unit(path, fmt: str | None, default: str = 'mm') -> str:
    """The length unit a STEP or IGES file says it is written in.

    Read from the file's own text rather than assumed: a model exported in
    inches or metres was imported as millimetres and every measurement was
    wrong by a factor the user had no way to see. A file that does not say,
    or a format that cannot (BREP), gets *default*.
    """
    try:
        text = Path(path).read_bytes().decode('latin-1', errors='replace')
    except OSError:
        return default
    fmt = (fmt or '').lower()
    if fmt == 'step':
        return _step_unit(text, default)
    if fmt == 'iges':
        return _iges_unit(text, default)
    return default


def _step_entity(text: str, position: int) -> str:
    start = text.rfind(';', 0, position) + 1
    end = text.find(';', position)
    return text[start:end if end >= 0 else None].upper()


def _step_unit(text: str, default: str) -> str:
    for match in _STEP_CONVERSION_UNIT.finditer(text):
        if 'LENGTH_UNIT' in _step_entity(text, match.start()):
            unit = map_cad_unit(match.group(1), '')
            if unit:
                return unit
    for match in _STEP_SI_LENGTH.finditer(text):
        if 'LENGTH_UNIT' in _step_entity(text, match.start()):
            return _STEP_PREFIXES.get(match.group(1).upper(), default)
    return default


def _iges_unit(text: str, default: str) -> str:
    global_section = ''.join(
        line[:72] for line in text.splitlines() if len(line) >= 73 and line[72] == 'G')
    if not global_section:
        return default
    delimiter = ','
    if global_section.startswith('1H'):
        delimiter = global_section[2]
        fields = ['1H' + delimiter, *global_section[4:].split(delimiter)]
    else:
        fields = global_section.split(delimiter)
    try:
        flag = int(fields[13].strip())
    except (IndexError, ValueError):
        flag = None
    if flag in _IGES_UNIT_FLAGS:
        return _IGES_UNIT_FLAGS[flag]
    # Flag 3 is "user-defined", and its unit is named in parameter 15 and
    # nowhere else. Reporting `mm` for a file that says INCH there put a unit
    # the file never claimed on the mesh report, which is the row a user
    # checks when a part looks the wrong size.
    return map_cad_unit(_iges_unit_name(fields), default)


def _iges_unit_name(fields: list[str]) -> str:
    try:
        match = _IGES_HOLLERITH.match(fields[14])
    except IndexError:
        return ''
    if not match:
        return ''
    return match.group(2)[:int(match.group(1))]
