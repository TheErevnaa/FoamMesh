"""AF7 schema publishing (§7): emit the facade's OpenAPI + tool schemas.

Any agent framework can consume these generated artifacts to build tools; no
FoamMesh-specific SDK is required. The tool schema is a flat list derived from
the operation registry so a framework can register each operation as a callable
tool with its impact and confirmation class.
"""
from __future__ import annotations

from pathlib import Path

def openapi_document(facade) -> dict:
    return facade.openapi()


def tool_schemas(facade) -> list[dict]:
    """One tool descriptor per registered operation, agent-framework agnostic."""
    tools = []
    for descriptor in facade.describe_operations()['operations']:
        tools.append({
            'name': descriptor['operation'],
            'description': descriptor.get('summary') or descriptor['title'],
            'scope': descriptor['scope'],
            'impact': descriptor['impact'],
            'confirmation': descriptor['confirmation'],
            'capabilities': descriptor['capabilities'],
            'parameters_schema': descriptor.get('parameters_schema') or {'type': 'object'},
        })
    return tools


def publish_schemas(facade, out_dir: str | Path) -> dict:
    """Write ``openapi.json``, ``fields.json``, ``operations.json``, and
    ``tools.json`` to *out_dir*; return the written paths."""
    artifacts = {
        'openapi.json': openapi_document(facade),
        'fields.json': facade.describe_fields(),
        'operations.json': facade.describe_operations(),
        'tools.json': {'tools': tool_schemas(facade)},
    }
    from foammesh.core.facade.schema_publisher import publish_documents
    return publish_documents(out_dir, artifacts)
