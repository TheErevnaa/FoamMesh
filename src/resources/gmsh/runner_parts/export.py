"""Output honesty of the batch runner (Plan 48 GM4, D1 and D2).

``write_outputs`` of the runner always writes ``mesh.msh`` as MSH 2.2, ASCII,
``SaveAll`` off: the one file OpenFOAM's publisher reads. That made the user's
Export choices (version, binary, SaveAll, SaveParametric, partition split)
unreachable from a run, because the file was the publisher's before it was the
user's.

When the intent carries ``export.mshWrite`` this part separates them:

* the hidden publisher copy, ``<stem>.publisher.msh`` (MSH 2.2 ASCII, written
  by the runner itself), is what ``publish.publish`` reads whenever it sits
  beside the ``.msh`` (``publish.publisher_source``);
* ``mesh.msh`` is then written again exactly as chosen, and never replaced.

With no ``mshWrite`` the job passes through as before, and a copy left by an
earlier run is removed so it can never stand in for a newer ``mesh.msh``.

``export.unitPins`` is recorded beside the outputs together with what Gmsh
held for the two pins when the mesh was written, so the run says which unit
its numbers are in.
"""
from __future__ import annotations

import os

#: Mirrors ``AUTO_FORMAT_CODE`` of ``runner_v1`` (a part cannot import it).
AUTO_FORMAT_CODE = 10
PUBLISHER_SUFFIX = '.publisher.msh'
#: The options a user ``.msh`` may set, with the values the runner pins.
MSH_OPTIONS = ('Mesh.MshFileVersion', 'Mesh.Binary', 'Mesh.SaveAll',
               'Mesh.SaveParametric', 'Mesh.PartitionSplitMeshFiles')
PINS = ('Geometry.OCCTargetUnit', 'Geometry.OCCScaling')


def publisher_path(msh_path) -> str:
    base, _suffix = os.path.splitext(str(msh_path))
    return base + PUBLISHER_SUFFIX


def _remove(path) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _user_options(export) -> dict:
    chosen = export.get('mshWrite')
    if not isinstance(chosen, dict) or not chosen:
        return {}
    return {name: chosen[name] for name in MSH_OPTIONS if name in chosen}


#: Metres in one model unit (mirrors ``export_writers.UNIT_METRES``; a part
#: cannot import the application).
UNIT_METRES = {'M': 1.0, 'MM': 1e-3, 'CM': 1e-2, 'DM': 1e-1, 'KM': 1e3,
               'UM': 1e-6, 'IN': 0.0254, 'FT': 0.3048, 'YD': 0.9144,
               'MI': 1609.344}


def _scale_to_metres(export):
    """Metres per model unit when the user unlocked a non-metre pin, else None.

    DP-4111. A model imported in millimetres is meshed in millimetres; the
    files OpenFOAM and SU2 read are in metres, so they are written with
    ``Mesh.ScalingFactor`` = metres per unit (the Export page does the same
    for its own writes). The user's chosen ``.msh`` is never scaled.
    """
    pins = export.get('unitPins')
    if not isinstance(pins, dict):
        return None
    factor = UNIT_METRES.get(str(pins.get('targetUnit', '')).strip().upper())
    if factor is None or factor == 1.0:
        return None
    scaling = pins.get('scaling')
    try:
        factor *= float(scaling) if scaling else 1.0
    except (TypeError, ValueError):
        pass
    return factor if factor != 1.0 else None


def _write_scaled(run, export, write):
    """Call *write* with ``Mesh.ScalingFactor`` at the metre factor."""
    factor = _scale_to_metres(export)
    if factor is None or (str(export.get('meshFormat', '')) != 'su2'
                          and not _user_options(export)):
        write()
        return
    option = run.gmsh.option
    before = option.getNumber('Mesh.ScalingFactor')
    option.setNumber('Mesh.ScalingFactor', factor)
    try:
        write()
    finally:
        option.setNumber('Mesh.ScalingFactor', before)
    run.statistics['unitScaling'] = {'metresPerUnit': factor}


def install(run, **_context):
    """Wrap ``write_outputs`` of this run so it writes both files."""
    original = run.write_outputs
    export = run.intent.get('export') or {}
    chosen = _user_options(export)

    def write_outputs():
        outputs = dict(run.job['output'])
        msh = outputs.get('msh')
        if not msh:
            _write_scaled(run, export, original)
            _record_pins(run, export)
            return
        copy = publisher_path(msh)
        if not chosen:
            _remove(copy)
            _write_scaled(run, export, original)
            _record_pins(run, export)
            return
        # 1. everything else, and the publisher's copy, as the runner writes it
        run.job['output'] = dict(outputs, msh=copy)
        try:
            _write_scaled(run, export, original)
        finally:
            run.job['output'] = outputs
        written = run.statistics.setdefault('outputs', {})
        written['publisher_msh'] = dict(
            written.pop('msh', {}) or {'path': copy}, hidden=True)
        # 2. the user's file, as chosen
        written['msh'] = _write_user_msh(run, msh, chosen)
        _record_pins(run, export)

    run.write_outputs = write_outputs


def _write_user_msh(run, msh, chosen) -> dict:
    refusal = run.refuse_unsafe_write(msh) \
        if hasattr(run, 'refuse_unsafe_write') else ''
    if refusal:
        run.warnings.append(f'msh export refused: {refusal}')
        return {'path': str(msh), 'error': refusal}
    option = run.gmsh.option
    for name, value in chosen.items():
        option.setNumber(name, float(value))
    option.setNumber('Mesh.Format', AUTO_FORMAT_CODE)
    try:
        os.makedirs(os.path.dirname(str(msh)) or '.', exist_ok=True)
        run.gmsh.write(str(msh))
    except Exception as error:                               # noqa: BLE001
        run.warnings.append(f'msh export failed: {error}')
        return {'path': str(msh), 'error': str(error)[:300]}
    version = float(option.getNumber('Mesh.MshFileVersion'))
    binary = bool(option.getNumber('Mesh.Binary'))
    return {'path': str(msh), 'bytes': os.path.getsize(msh),
            'format': 'msh%g' % version, 'binary': binary,
            'options': {name: float(value) for name, value in chosen.items()},
            'hidden': False}


def _record_pins(run, export) -> None:
    option = run.gmsh.option
    try:
        held = {'targetUnit': option.getString(PINS[0]),
                'scaling': option.getNumber(PINS[1])}
    except Exception:                                        # noqa: BLE001
        held = {}
    pins = export.get('unitPins')
    run.statistics['unitPins'] = {
        'held': held, 'requested': dict(pins) if isinstance(pins, dict) else {}}


def register(add_hook):
    # The export hooks run just before write_outputs, which is the call wrapped.
    add_hook('export', install)
