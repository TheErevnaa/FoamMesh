"""The parameters an operation reads, found from its handler.

DP-6081. 257 of the 430 operations carried an empty ``parameters_schema``, so a
client could not learn what to send. A handler reads its parameters as
``command.parameters['key']`` (required) or ``.get('key')`` (optional), often
through a local alias (``p = _parameters(command)``). This reads those keys from
the handler's source and offers them as a JSON schema marked ``x-inferred``.
Keys read only inside a helper the handler calls are not seen, so the schema
never forbids additional properties; a hand-written schema always wins.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

#: False while the generator reads the source itself.
USE_DATA = True


def _is_parameters(node, aliases: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    if isinstance(node, ast.Attribute):
        return node.attr == 'parameters'
    return False


def _aliases(tree) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Await):
                value = value.value
            if (isinstance(value, ast.Attribute) and value.attr == 'parameters') or (
                    isinstance(value, ast.Call) and any(
                        isinstance(a, ast.Attribute) and a.attr == 'parameters'
                        or isinstance(a, ast.Name) and a.id == 'command'
                        for a in value.args) and 'param' in ast.unparse(value.func)):
                names.add(node.targets[0].id)
    return names


def infer_parameters(handler) -> dict:
    """``{}`` when nothing could be read, else a schema of the keys seen."""
    try:
        source = textwrap.dedent(inspect.getsource(inspect.unwrap(handler)))
        tree = ast.parse(source)
    except (OSError, TypeError, SyntaxError):
        return {}
    aliases = _aliases(tree)
    required: dict[str, None] = {}
    optional: dict[str, None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_parameters(node.value, aliases):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                required[key.value] = None
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr in ('get', 'pop') and _is_parameters(node.func.value, aliases)
              and node.args and isinstance(node.args[0], ast.Constant)
              and isinstance(node.args[0].value, str)):
            optional[node.args[0].value] = None
    for key in required:
        optional.pop(key, None)
    if not required and not optional:
        return {}
    schema = {'type': 'object', 'x-inferred': True,
              'properties': {key: {} for key in (*required, *optional)}}
    if required:
        schema['required'] = list(required)
    return schema


def schema_for(operation: str, handler) -> dict:
    """The shipped schema (a frozen build has no source), else read from the handler."""
    if USE_DATA:
        try:
            from .operation_parameters_data import SCHEMAS
        except ImportError:
            SCHEMAS = {}
        if operation in SCHEMAS:
            return dict(SCHEMAS[operation])
    return infer_parameters(handler)
