"""Size data of the batch runner (Plan 48 GM8, decision D6).

A PostView field reads a ``.pos`` / ``.msh`` the user loaded. The runner
builds it with the other native fields (``native_fields_v1.build``); this
part, run at the ``fields`` stage right after, writes down what was loaded so
a run says which data drove which size: the file, the dataset, the time step,
the unit, the number of values and the smallest and largest size in metres.
"""
from __future__ import annotations


def record_size_data(run, **_context):
    try:
        import native_fields_v1
    except ImportError:                               # pragma: no cover
        return
    loaded = native_fields_v1.last_data()
    if not loaded:
        return
    summary = {}
    for name, facts in sorted(loaded.items()):
        summary[name] = {
            'mode': facts.get('mode'), 'values': facts.get('values'),
            'sizeMin': facts.get('min'), 'sizeMax': facts.get('max')}
        run.ledger.record(
            f'nativeField:{name}/sizeData', facts.get('mode'),
            facts.get('mode'),
            note=f"{facts.get('values')} values, sizes "
                 f"{facts.get('min'):g} to {facts.get('max'):g} m")
    run.statistics['sizeData'] = summary


def register(add_hook):
    add_hook('fields', record_size_data)
