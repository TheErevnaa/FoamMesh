"""Plan 48 PA3 local-API routes: the Structure panel, the Fields panel and the
Compute / Sizing / CVT / Build panels, driven as the window drives them.

A plain remote control for what a person does in those panels, nothing
agent-specific. Three kinds of route live here:

* **Rows** (``gmsh.fields.rename_row`` / ``remove_row`` / ``graph`` /
  ``use_view``, ``gmsh.structure.catalogue`` / ``add_row`` / ``check``): the
  same edits the panels make to their rows, on the stored rows (or on
  ``rows`` the caller sends), with the references to a renamed or removed
  size field fixed exactly as the table does. The rows come back; keep them
  with ``configuration.patch``, as for ``gmsh.fields.add_row``.
* **Operations** (``gmsh.structure.apply`` / ``preview_grid`` / ``measure`` /
  ``write_grid``, ``gmsh.fields.preview``): the buttons of the panels. They
  work on the project's Gmsh session; when a window shows the panel the
  result is also put in the panel (its status line, its picture), as after a
  click.
* **Window state** (``gmsh.fields.select_row`` / ``overlay``,
  ``gmsh.compute.preflight``, ``gmsh.mesh.show_session``,
  ``gmsh.global_sizing.notes``, ``gmsh.cvt.show``,
  ``gmsh.geometry.show_model``): what only the open window holds. Each panel
  registers a small API object on the case session
  (:func:`register_panel`); without a window these answer
  ``precondition_failed``.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import (
    PreconditionFailedError, ValidationFailedError,
)
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

ATTRIBUTE = 'pa3_window_panels'

_ROWS = {'type': 'array', 'items': {'type': 'object'}}
_ROWS_ONLY = {'type': 'object', 'properties': {'rows': _ROWS}}


def _schema(**properties) -> dict:
    return {'type': 'object', 'additionalProperties': False,
            'properties': properties}


OPERATIONS = (
    # -- size fields ---------------------------------------------------- #
    OperationDescriptor(
        'gmsh.fields.rename_row', 'Rename a size field', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['name', 'new_name'],
                           'properties': {'name': {'type': 'string'},
                                          'new_name': {'type': 'string'},
                                          'rows': _ROWS}},
        summary='The size-field rows with one field renamed; every other '
                'field that read the old name now reads the new one, as '
                'the table does. Send the rows back with '
                'configuration.patch on gmsh.native_fields.rows to keep '
                'them. problem is what the Fields page shows under the '
                'table ("" when the rows can be built).'),
    OperationDescriptor(
        'gmsh.fields.remove_row', 'Remove a size field', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['name'],
                           'properties': {'name': {'type': 'string'},
                                          'rows': _ROWS}},
        summary='The size-field rows without one field; a field that read '
                'it no longer names it, as the Remove button does. Send '
                'the rows back with configuration.patch on '
                'gmsh.native_fields.rows to keep them.'),
    OperationDescriptor(
        'gmsh.fields.graph', 'Map of the size fields', 'case',
        ImpactClass.READ,
        parameters_schema=_ROWS_ONLY,
        summary='The field graph of the Fields page: one node per field '
                'with its column and row, the links (reader, read), the '
                'number of columns and the one-line description.'),
    OperationDescriptor(
        'gmsh.fields.use_view', 'Use a view as a size field', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['tag'],
                           'properties': {'tag': {'type': 'integer'},
                                          'rows': _ROWS}},
        summary='"Use as size field" of the Plugins panel: the rows with a '
                'background PostView field that reads the view with this '
                'tag. Send the rows back with configuration.patch on '
                'gmsh.native_fields.rows to keep them.'),
    OperationDescriptor(
        'gmsh.fields.select_row', 'Select a size field row', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'properties': {
            'name': {'type': 'string'}}},
        summary='Show the form of one size field in the Fields page (the '
                'row the table has selected). Without a name: which row '
                'is selected and the page state. Needs the open window.'),
    OperationDescriptor(
        'gmsh.fields.preview', 'Preview all size fields', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema=_schema(dim={'type': 'integer', 'enum': [1, 2, 3]},
                                  limit={'type': 'integer', 'minimum': 1}),
        summary='"Preview field": sample ALL stored size fields together '
                'on the model and return the points, the sizes, how many '
                'were asked and the read-out the page shows. The Fields '
                'page draws the scatter when it is open. Refused while the '
                'model has a mesh.'),
    OperationDescriptor(
        'gmsh.fields.overlay', 'Show a size field in the viewport', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema=_schema(shown={'type': 'boolean'},
                                  name={'type': 'string'},
                                  dim={'type': 'integer', 'enum': [1, 2, 3]},
                                  limit={'type': 'integer', 'minimum': 1}),
        summary='Show (shown=true, with the field name) or hide '
                '(shown=false) the sizes of one size field in the '
                'viewport, as the Show / Hide button of the Fields page '
                'does. Answers which field is shown. Refused while the '
                'model has a mesh.'),
    # -- structure ------------------------------------------------------ #
    OperationDescriptor(
        'gmsh.structure.catalogue', 'Structured meshing kinds', 'case',
        ImpactClass.READ,
        summary='The kinds of structured row the Structure panel can add '
                '(transfinite surface, transfinite volume, automatic '
                'transfinite): the Gmsh call, the dimensions, the help, the '
                'corner counts and every parameter with its kind, default, '
                'range, choices and help.'),
    OperationDescriptor(
        'gmsh.structure.add_row', 'Add a structured row', 'case',
        ImpactClass.READ,
        parameters_schema={'type': 'object', 'required': ['kind'],
                           'properties': {'kind': {'type': 'string'},
                                          'dim': {'type': 'integer',
                                                  'enum': [2, 3]},
                                          'tags': {'type': 'array'},
                                          'rows': _ROWS}},
        summary='The structure rows with a new row of the kind, named and '
                'filled with the defaults as the Add button does. Send '
                'the rows back with configuration.patch on '
                'gmsh.structure.rows to keep them.'),
    OperationDescriptor(
        'gmsh.structure.check', 'Check the structure rows', 'case',
        ImpactClass.READ,
        parameters_schema=_ROWS_ONLY,
        summary='The reason the structure rows cannot be applied (the line '
                'under the Structure table), or "" with the operations '
                'they become. Rows: the ones sent, else the open '
                'Structure panel\'s, else the stored ones.'),
    OperationDescriptor(
        'gmsh.structure.apply', 'Apply the structure rows to the model',
        'case', ImpactClass.MESH_MUTATION,
        parameters_schema=_ROWS_ONLY,
        summary='"Apply to model": set the transfinite and recombine '
                'constraints of the enabled rows on the Gmsh session. '
                'Answers the count and the status line.'),
    OperationDescriptor(
        'gmsh.structure.preview_grid', 'Preview the block grid', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema=_ROWS_ONLY,
        summary='"Preview block grid": the lines of the structured block '
                'grid of the enabled rows, drawn from the 1D mesh of the '
                'session; the mesh is cleared again. The Structure panel '
                'draws them when it is open. Refused while the model has a '
                'mesh.'),
    OperationDescriptor(
        'gmsh.structure.measure', 'Measure the structured result', 'case',
        ImpactClass.MESH_MUTATION,
        parameters_schema=_ROWS_ONLY,
        summary='"Measure result": what Gmsh made of each enabled row, '
                'one note per row.'),
    OperationDescriptor(
        'gmsh.structure.write_grid', 'Write the Structured size grid',
        'case', ImpactClass.FILE_PRODUCING,
        parameters_schema={'type': 'object', 'required': ['bounds'],
                           'properties': {
                               'bounds': {'type': 'array', 'minItems': 6,
                                          'maxItems': 6,
                                          'items': {'type': 'number'}},
                               'step': {'type': 'number',
                                        'exclusiveMinimum': 0},
                               'expression': {'type': 'string'},
                               'text': {'type': 'boolean'},
                               'rows': _ROWS}},
        summary='"Write grid and add field": write the grid file of the '
                'Structured size field next to the case (bounds in '
                'metres: xmin ymin zmin xmax ymax zmax; step, a size or an '
                'expression in x, y, z; ASCII text or binary) and return '
                'the size-field rows with the Structured field added. '
                'Send them back with configuration.patch on '
                'gmsh.native_fields.rows to keep them.'),
    # -- compute / sizing / viewport ----------------------------------- #
    OperationDescriptor(
        'gmsh.compute.preflight', 'Pre-run note of the Compute page', 'case',
        ImpactClass.READ,
        summary='What the Compute page says before a run: the assembly '
                'refusal and the layer-gate conflict, each line, whether '
                'the note is shown and its severity (warning or error). '
                'Needs the open window.'),
    OperationDescriptor(
        'gmsh.mesh.show_session', 'Show the session mesh in the viewport',
        'case', ImpactClass.READ,
        summary='Put the mesh the Gmsh session holds in the viewport and '
                'refresh the statistics, as a finished Generate stage '
                'does; with no mesh in the session the picture of it comes '
                'down. Needs the open window.'),
    OperationDescriptor(
        'gmsh.global_sizing.notes', 'Notes of the Global sizing page',
        'case', ImpactClass.READ,
        summary='The warnings the Gmsh Global sizing page shows: the '
                'single-threaded volume algorithm note, the quad-surface '
                'algorithm note and the text of the Threads box (Mesh '
                'setup n threads). Needs the open window.'),
    OperationDescriptor(
        'gmsh.global_sizing.sizing_view', 'Automatic sizing views', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(
            layer={'type': 'string', 'enum': ['field', 'conformity']},
            shown={'type': 'boolean'}),
        summary='The viewport toggles of the Automatic sizing card: field '
                '(the Preview button, the proposed size field) and '
                'conformity ("Show on the mesh", snappy route only). With '
                'layer and shown it sets the toggle as a click does; '
                'without, it reads both. Needs the open window.'),
    OperationDescriptor(
        'gmsh.cvt.show', 'Show before and after of a CVT remesh', 'case',
        ImpactClass.READ,
        parameters_schema=_schema(shown={'type': 'boolean'},
                                  run_id={'type': 'string'}),
        summary='Show (default) or hide the before and after surfaces of '
                'a CVT run in the viewport, as the "Show before and '
                'after" box does. The run is the last one of the panel '
                'unless run_id is given. Needs the open window.'),
    OperationDescriptor(
        'gmsh.geometry.show_model', 'Show the model for picking', 'case',
        ImpactClass.READ,
        summary='"Show model" of the Geometry build panel: draw the model '
                'entities in the viewport so they can be picked. Needs the '
                'open window.'),
)


# --------------------------------------------------------------------------- #
# The panels a window registers on the case session
# --------------------------------------------------------------------------- #

def register_panel(session, name: str, api) -> None:
    """A panel of the window tells the API it is showing this case."""
    held = getattr(session, ATTRIBUTE, None)
    if held is None:
        held = {}
        setattr(session, ATTRIBUTE, held)
    held[name] = api


def unregister_panel(session, name: str, api=None) -> None:
    """The panel closed; a newer panel of the same name stays registered."""
    held = getattr(session, ATTRIBUTE, None)
    if held and (api is None or held.get(name) is api):
        held.pop(name, None)


def announce(client, name: str, api):
    """Register *api* on the case session of the window's project client.

    Returns the session, or ``None`` when there is no case session (a panel
    built without a project, as in a test)."""
    try:
        session = client.session()
    except Exception:                                    # noqa: BLE001
        return None
    if session is None:
        return None
    register_panel(session, name, api)
    return session


def announce_from_app(name: str, api):
    """:func:`announce` for a panel that holds no project client: the one
    the application's window uses."""
    try:
        from foammesh.app import app
        client = getattr(app, 'facadeClient', None)
    except Exception:                                    # noqa: BLE001
        return None
    return announce(client, name, api) if client is not None else None


def window_panel(session, name: str):
    """The registered panel API, or ``None`` with no window."""
    return (getattr(session, ATTRIBUTE, None) or {}).get(name)


def _need_panel(session, name: str, what: str):
    api = window_panel(session, name)
    if api is None:
        raise PreconditionFailedError(
            f'no window is showing this case, and {what} lives in it: open '
            'the case in the FoamMesh window', details={'needs': name})
    return api


def _call(session, name, what, method, *args, **kwargs) -> dict:
    api = _need_panel(session, name, what)
    try:
        return dict(getattr(api, method)(*args, **kwargs) or {})
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error


def _result(session, command, payload) -> OperationResult:
    return OperationResult('accepted', command.operation, session.revisions,
                           payload=payload)


# --------------------------------------------------------------------------- #
# Row edits shared with the panels (so the table and the API cannot differ)
# --------------------------------------------------------------------------- #

def next_name(rows, base: str) -> str:
    """``base1``, ``base2``, ...: the first the rows do not use yet."""
    names = {r.get('name') for r in rows}
    number = 1
    while f'{base}{number}' in names:
        number += 1
    return f'{base}{number}'


def rename_references(rows, old: str, new: str) -> None:
    """Every row that read the field *old* reads *new* now (in place)."""
    for other in rows:
        for key, value in list((other.get('params') or {}).items()):
            if value == old:
                other['params'][key] = new
            elif isinstance(value, list) and old in value:
                other['params'][key] = [new if v == old else v for v in value]


def drop_references(rows, gone: str) -> None:
    """A field that read the removed one no longer names it (in place)."""
    for row in rows:
        for key, value in list((row.get('params') or {}).items()):
            if value == gone:
                del row['params'][key]
            elif isinstance(value, list) and gone in value:
                row['params'][key] = [v for v in value if v != gone]


def copy_rows(rows) -> list:
    return [dict(r, params=dict(r.get('params') or {})) for r in rows
            if isinstance(r, dict)]


def structured_field_rows(rows, file_name: str, text: bool) -> list:
    """*rows* with the ``structured_grid`` Structured field reading the grid."""
    from foammesh.core.gmsh import field_catalogue as fields

    rows = [r for r in copy_rows(rows) if r.get('name') != 'structured_grid']
    row = fields.new_row('Structured', 'structured_grid')
    row['params'] = {'FileName': file_name, 'TextFormat': 1 if text else 0}
    rows.append(row)
    return rows


def use_view_rows(rows, tag: int) -> tuple:
    """*rows* and the background ``PostView`` row that reads view *tag*."""
    from foammesh.core.gmsh import field_catalogue as fields

    rows = copy_rows(rows)
    new = fields.new_row('PostView', next_name(rows, 'postView'))
    new['params'] = {'ViewTag': int(tag)}
    new['background'] = True
    rows.append(new)
    return rows, new


# --------------------------------------------------------------------------- #
# Stored values
# --------------------------------------------------------------------------- #

def _stored_plain(session, ids) -> dict:
    from foammesh.core.facade import FIELD_REGISTRY
    from foammesh.core.facade.field_adapters import read_value

    configuration = session.configuration()
    plain = {}
    for field_id in ids:
        try:
            value = read_value(configuration,
                               FIELD_REGISTRY.get(field_id).storage_path)
        except Exception:                                # noqa: BLE001
            value = None
        plain[field_id] = getattr(value, 'value', value)
    return plain


_ALGORITHMS = ('gmsh.global_sizing.surface', 'gmsh.global_sizing.volume')


def _field_rows(session, command) -> list:
    from foammesh.core.gmsh import field_catalogue as fc

    given = (command.parameters or {}).get('rows')
    if given is not None:
        if not isinstance(given, list):
            raise ValidationFailedError('rows must be a list')
        return copy_rows(given)
    stored = _stored_plain(session, (fc.FIELD_ID,))
    try:
        return copy_rows(fc.rows_from_text(stored.get(fc.FIELD_ID)))
    except fc.FieldRefusal as error:
        raise ValidationFailedError(str(error)) from error


def field_problem(session, rows) -> str:
    """The line under the Fields table: why the rows cannot be built."""
    from foammesh.core.gmsh import field_catalogue as fc

    stored = _stored_plain(session, _ALGORITHMS)
    try:
        fc.validate(rows, surface_algorithm=str(stored[_ALGORITHMS[0]] or ''),
                    volume_algorithm=str(stored[_ALGORITHMS[1]] or ''))
    except fc.FieldRefusal as error:
        return str(error)
    return ''


def _rows_payload(session, rows, **extra) -> dict:
    from foammesh.core.gmsh import field_catalogue as fc

    return {'rows': rows, 'rows_text': fc.rows_to_text(rows),
            'problem': field_problem(session, rows), **extra}


def _named(command, key='name') -> str:
    value = (command.parameters or {}).get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailedError(f'{key} is required')
    return value.strip()


def _controller(session):
    from foammesh.core.gmsh.session_generate import (
        controller_for_case_session)
    return controller_for_case_session(session)


# --------------------------------------------------------------------------- #
# size fields
# --------------------------------------------------------------------------- #

def _rename_row(session, command) -> OperationResult:
    name, new = _named(command), _named(command, 'new_name')
    rows = _field_rows(session, command)
    row = next((r for r in rows if r.get('name') == name), None)
    if row is None:
        raise ValidationFailedError(f'there is no size field {name!r}',
                                    details={'names': [r.get('name')
                                                       for r in rows]})
    if new != name:
        row['name'] = new
        rename_references(rows, name, new)
    return _result(session, command, _rows_payload(session, rows, row=row))


def _remove_row(session, command) -> OperationResult:
    name = _named(command)
    rows = _field_rows(session, command)
    index = next((i for i, r in enumerate(rows) if r.get('name') == name),
                 None)
    if index is None:
        raise ValidationFailedError(f'there is no size field {name!r}',
                                    details={'names': [r.get('name')
                                                       for r in rows]})
    gone = rows.pop(index)['name']
    drop_references(rows, gone)
    return _result(session, command, _rows_payload(session, rows,
                                                   removed=gone))


def graph_payload(rows) -> dict:
    """The field graph as the view draws it (``catalogue.graph_layout``)."""
    from foammesh.core.gmsh import field_catalogue as fc
    from foammesh.core.quantities import count_text

    layout = fc.graph_layout(rows)
    edges = [list(e) for e in layout['edges']]
    description = ('No size fields.' if not rows else
                   count_text(len(rows), 'size field') + ', '
                   + count_text(len(edges), 'link') + '.')
    return {'nodes': {name: {'column': int(col), 'row': int(row)}
                      for name, (col, row) in layout['nodes'].items()},
            'edges': edges, 'columns': int(layout['columns']),
            'description': description}


def _graph(session, command) -> OperationResult:
    return _result(session, command, graph_payload(_field_rows(session,
                                                               command)))


def _use_view(session, command) -> OperationResult:
    try:
        tag = int((command.parameters or {}).get('tag'))
    except (TypeError, ValueError):
        raise ValidationFailedError('tag must be a view tag') from None
    rows, row = use_view_rows(_field_rows(session, command), tag)
    return _result(session, command, _rows_payload(session, rows, row=row))


async def _select_row(session, command) -> OperationResult:
    name = str((command.parameters or {}).get('name') or '')
    api = _need_panel(session, 'fields', 'the Fields page')
    if name:
        if not await asyncio.to_thread(api.select_row, name):
            raise ValidationFailedError(f'there is no size field {name!r}')
    return _result(session, command, await asyncio.to_thread(api.state))


def _sample_arguments(command) -> tuple:
    parameters = command.parameters or {}
    return int(parameters.get('dim') or 2), int(parameters.get('limit')
                                                or 20000)


async def _preview(session, command) -> OperationResult:
    from foammesh.core.gmsh.generate_actions import ActionRefusal
    from foammesh.core.gmsh import field_catalogue as fc

    dim, limit = _sample_arguments(command)
    api = window_panel(session, 'fields')
    refusal = (api.preview_refusal() if api is not None else '')
    if not refusal:
        stored = _stored_plain(session, (fc.FIELD_ID, *_ALGORITHMS))
        try:
            refusal = field_problem(session, fc.rows_from_text(
                stored.get(fc.FIELD_ID)))
        except fc.FieldRefusal as error:
            refusal = str(error)
    if refusal:
        raise PreconditionFailedError(refusal)
    controller = _controller(session)
    try:
        answer = await asyncio.to_thread(controller.preview_fields, dim,
                                         limit)
    except (ValueError, ActionRefusal) as error:
        if api is not None:
            api.show_preview(None, str(error))
        raise PreconditionFailedError(str(error)) from error
    note = (api.show_preview(answer, '') if api is not None
            else None)
    return _result(session, command, {**dict(answer), 'note': note})


async def _overlay(session, command) -> OperationResult:
    from foammesh.core.gmsh.generate_actions import ActionRefusal

    parameters = command.parameters or {}
    api = window_panel(session, 'fields')
    if parameters.get('shown') is False:
        if api is None:
            return _result(session, command, {'shown_field': ''})
        return _result(session, command,
                       await asyncio.to_thread(api.hide_overlay))
    name = str(parameters.get('name') or '')
    if api is not None and not name:
        name = api.state().get('current') or ''
    if not name:
        raise ValidationFailedError('name is required')
    dim, limit = _sample_arguments(command)
    if api is not None:
        refusal = api.overlay_refusal(name)
    else:
        refusal = ''
        from foammesh.core.gmsh import field_catalogue as fc
        rows = fc.rebase_size_data(_field_rows(session, command),
                                   session.case_path)
        row = next((r for r in rows if r.get('name') == name), None)
        if row is None:
            refusal = f'there is no size field {name!r}'
        else:
            kind = fc.TYPES[row['type']]
            if kind.gmsh == 'BoundaryLayer' or kind.anisotropic:
                refusal = f'a {kind.gmsh} field has no single size to show'
            elif not row.get('enabled', True):
                refusal = f'{name!r} is switched off'
    if refusal:
        raise PreconditionFailedError(refusal)
    controller = _controller(session)
    try:
        answer = await asyncio.to_thread(controller.sample_field, name, dim,
                                         limit)
    except (ValueError, ActionRefusal) as error:
        if api is not None:
            api.show_overlay(None, str(error), name)
        raise PreconditionFailedError(str(error)) from error
    shown = (await asyncio.to_thread(api.show_overlay, answer, '', name)
             if api is not None else {'shown_field': '', 'note': None})
    return _result(session, command, {**dict(answer), **shown})


# --------------------------------------------------------------------------- #
# structure
# --------------------------------------------------------------------------- #

def kind_payload(kind) -> dict:
    return {'id': kind.id, 'title': kind.title, 'gmsh': kind.gmsh,
            'dims': list(kind.dims), 'help': kind.help,
            'min_tags': kind.min_tags, 'corners': list(kind.corners),
            'params': [{'name': p.name, 'kind': p.kind, 'default': p.default,
                        'help': p.help, 'low': p.low, 'high': p.high,
                        'choices': [list(c) for c in p.choices]}
                       for p in kind.params]}


def _structure_catalogue(session, command) -> OperationResult:
    from foammesh.core.gmsh import structure as st

    return _result(session, command, {
        'kinds': [kind_payload(k) for k in st.KINDS.values()],
        'default_corner_angle': st.DEFAULT_CORNER_ANGLE,
        'field': st.FIELD_ID})


def _structure_rows(session, command) -> list:
    from foammesh.core.gmsh import structure as st

    given = (command.parameters or {}).get('rows')
    if given is not None:
        if not isinstance(given, list):
            raise ValidationFailedError('rows must be a list')
        return [dict(r) for r in given if isinstance(r, dict)]
    api = window_panel(session, 'structure')
    if api is not None:
        return api.rows()
    stored = _stored_plain(session, (st.FIELD_ID,))
    try:
        return st.rows_from_text(stored.get(st.FIELD_ID))
    except st.StructureRefusal as error:
        raise ValidationFailedError(str(error)) from error


def _structure_add_row(session, command) -> OperationResult:
    from foammesh.core.gmsh import structure as st

    parameters = command.parameters or {}
    kind = str(parameters.get('kind') or '')
    if kind not in st.KINDS:
        raise ValidationFailedError(
            f'{kind!r} is not a kind of structured row',
            details={'kinds': list(st.KINDS)})
    rows = _structure_rows(session, command)
    dim = parameters.get('dim')
    if dim is not None and int(dim) not in st.KINDS[kind].dims:
        raise ValidationFailedError(
            f'{st.KINDS[kind].title} acts on dimension '
            + ' or '.join(str(d) for d in st.KINDS[kind].dims))
    row = st.new_row(kind, next_name(rows, kind), dim)
    if parameters.get('tags'):
        try:
            row['tags'] = st.parse_tags(parameters['tags'])
        except st.StructureRefusal as error:
            raise ValidationFailedError(str(error)) from error
    rows.append(row)
    return _result(session, command, {
        'row': row, 'rows': rows, 'rows_text': st.rows_to_text(rows),
        'problem': _structure_problem(rows)})


def _structure_problem(rows) -> str:
    from foammesh.core.gmsh import structure as st

    try:
        st.validate(rows)
    except st.StructureRefusal as error:
        return str(error)
    return ''


def _structure_check(session, command) -> OperationResult:
    from foammesh.core.gmsh import structure as st

    rows = _structure_rows(session, command)
    reason = _structure_problem(rows)
    return _result(session, command, {
        'ok': not reason, 'reason': reason,
        'ops': [] if reason else st.validate(rows).to_list()})


def _structure_ops(session, command) -> list:
    """The operations of the rows, or the refusal the panel puts in its
    status line."""
    from foammesh.core.gmsh import structure as st

    rows = _structure_rows(session, command)
    reason = _structure_problem(rows)
    if reason:
        api = window_panel(session, 'structure')
        if api is not None:
            api.show_failure(reason)
        raise ValidationFailedError(reason)
    return st.validate(rows).to_list()


def _structure_work(what):
    async def run(session, command) -> OperationResult:
        from foammesh.core.facade.plan48 import gm6_routes
        from foammesh.core.gmsh import structure as st

        ops = _structure_ops(session, command)
        api = window_panel(session, 'structure')

        def work(call):
            if what == 'apply':
                return st.apply_to_session(call, ops)
            if what == 'preview':
                return st.preview_on_session(call, ops)
            return st.runtime_module().measure(st.RemoteGmsh(call), ops)

        try:
            result = await asyncio.to_thread(
                gm6_routes._with_session, session, work)    # noqa: SLF001
        except Exception as error:                       # noqa: BLE001
            if api is not None:
                api.show_failure(str(error))
            raise gm6_routes._refusal(error) from error     # noqa: SLF001
        status = api.show_result(what, result) if api is not None else None
        payload = ({'rows': result} if what == 'measure' else dict(result))
        payload['status'] = status
        return _result(session, command, payload)
    return run


def write_grid_files(case_path, translate, bounds, step, expression,
                     text) -> dict:
    """Write the grid file the Structured size field reads; the paths."""
    from pathlib import Path

    from foammesh.core.gmsh import structured_grid as grids

    if len(bounds) != 6:
        raise grids.GridRefusal(
            'the bounds are six numbers: xmin ymin zmin xmax ymax zmax')
    spec = grids.spec_covering(bounds, float(step), str(expression or '')
                               .strip(), bool(text))
    path = Path(case_path) / ('structured_size_grid.dat' if spec.text
                              else 'structured_size_grid.bin')
    written = grids.write(path, spec)
    name = str(path)
    if translate is not None:
        try:
            name = translate(path)
        except Exception:                                # noqa: BLE001
            pass
    return {'path': str(path), 'file_name': name, **written,
            'text': spec.text}


async def _write_grid(session, command) -> OperationResult:
    from foammesh.core.gmsh import structured_grid as grids

    parameters = command.parameters or {}
    bounds = [float(b) for b in parameters.get('bounds') or ()]
    step = parameters.get('step', 0.1)
    expression = str(parameters.get('expression') or '')
    text = bool(parameters.get('text', True))
    api = window_panel(session, 'structure')
    try:
        if api is not None:
            done = await asyncio.to_thread(
                api.write_grid, bounds, step, expression, text)
        else:
            controller = None
            try:
                controller = _controller(session)
            except Exception:                            # noqa: BLE001
                controller = None
            translate = (controller.profile().translate_host_path
                         if controller is not None else None)
            done = await asyncio.to_thread(
                write_grid_files, session.case_path, translate, bounds,
                step, expression, text)
    except (grids.GridRefusal, ValueError) as error:
        raise ValidationFailedError(str(error)) from error
    rows = done.pop('rows', None)
    if rows is None:
        rows = structured_field_rows(_field_rows(session, command),
                                     done['file_name'], done['text'])
    return _result(session, command, {**done, **_rows_payload(session, rows)})


# --------------------------------------------------------------------------- #
# compute / sizing / viewport
# --------------------------------------------------------------------------- #

async def _preflight(session, command) -> OperationResult:
    api = _need_panel(session, 'compute', 'the Compute page')
    return _result(session, command, await asyncio.to_thread(api.preflight))


async def _show_session(session, command) -> OperationResult:
    api = _need_panel(session, 'compute', 'the Compute page')
    try:
        done = await asyncio.to_thread(api.show_session_mesh)
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, dict(done))


async def _sizing_notes(session, command) -> OperationResult:
    api = _need_panel(session, 'global_sizing', 'the Global sizing page')
    return _result(session, command, await asyncio.to_thread(api.notes))


async def _sizing_view(session, command) -> OperationResult:
    parameters = command.parameters or {}
    api = _need_panel(session, 'global_sizing', 'the Global sizing page')
    shown = parameters.get('shown')
    try:
        done = await asyncio.to_thread(
            api.sizing_view, str(parameters.get('layer') or ''),
            None if shown is None else bool(shown))
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, dict(done))


async def _cvt_show(session, command) -> OperationResult:
    parameters = command.parameters or {}
    api = _need_panel(session, 'cvt', 'the CVT panel')
    try:
        done = await asyncio.to_thread(
            api.show, parameters.get('shown', True) is not False,
            str(parameters.get('run_id') or ''))
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, dict(done))


async def _show_model(session, command) -> OperationResult:
    api = _need_panel(session, 'build', 'the Geometry build panel')
    try:
        done = await asyncio.to_thread(api.show_model)
    except ValueError as error:
        raise PreconditionFailedError(str(error)) from error
    return _result(session, command, done)


def _asynchronous(function):
    """A plain handler as the coroutine the dispatcher awaits."""
    async def handle(session, command):
        return function(session, command)
    return handle


def handlers() -> dict:
    return {
        'gmsh.fields.rename_row': _asynchronous(_rename_row),
        'gmsh.fields.remove_row': _asynchronous(_remove_row),
        'gmsh.fields.graph': _asynchronous(_graph),
        'gmsh.fields.use_view': _asynchronous(_use_view),
        'gmsh.fields.select_row': _select_row,
        'gmsh.fields.preview': _preview,
        'gmsh.fields.overlay': _overlay,
        'gmsh.structure.catalogue': _asynchronous(_structure_catalogue),
        'gmsh.structure.add_row': _asynchronous(_structure_add_row),
        'gmsh.structure.check': _asynchronous(_structure_check),
        'gmsh.structure.apply': _structure_work('apply'),
        'gmsh.structure.preview_grid': _structure_work('preview'),
        'gmsh.structure.measure': _structure_work('measure'),
        'gmsh.structure.write_grid': _write_grid,
        'gmsh.compute.preflight': _preflight,
        'gmsh.mesh.show_session': _show_session,
        'gmsh.global_sizing.notes': _sizing_notes,
        'gmsh.global_sizing.sizing_view': _sizing_view,
        'gmsh.cvt.show': _cvt_show,
        'gmsh.geometry.show_model': _show_model,
    }
