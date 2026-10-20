# API & CLI Reference

## REST API (`foammesh[api]`)

Run `foammesh serve --token T --allow-root DIR`. The token `T` only pairs a
client (see Pairing below); the session token it earns is sent as
`Authorization: Bearer` on every other request, and cases are created or opened only inside an
allowed folder. The OpenAPI document is at `GET /api/v1/openapi.json`; there
is no `/docs` page. The only public domain surface is versioned under `/api/v1`.

### Local API of the desktop app

Off unless switched on: Settings > Preferences > Connections, or
`FoamMesh.exe [case] --api-attach [--api-discovery FILE] [--api-allow-root DIR]`
for one run. The open window then serves the same routes on 127.0.0.1 with a
new token each start, over the case open in it (no case create or open; a trusted client may close it).
The discovery file (default `~/.foammesh/local-api.json`) holds
`{endpoint, token, pid}`. The status bar shows while it is on; a click there
turns it off.
Clients use semantic field and operation IDs, never database paths or arbitrary
executable names.

### Pairing (signed handshake)

The discovery token (or `serve --token`) is accepted only by
`GET /api/v1/handshake` and `POST /api/v1/handshake`; every other route and
the event WebSocket answer 403 `client_not_trusted` until the client pairs:

1. `GET /api/v1/handshake` returns a challenge, valid for 60 s and usable once.
2. The client signs `foammesh-local-api-handshake-v1
<challenge>` with its
   Ed25519 key.
3. `POST /api/v1/handshake` with `{challenge, key_id, signature}` (base64).
4. The answer carries a `session_token`, sent as `Authorization: Bearer` on
   every other call.

A stock build trusts only the public keys it ships with.

### Discovery and lifecycle

- `GET /api/v1/system/capabilities`
- `GET /api/v1/fields`, `GET /api/v1/operations`, `GET /api/v1/openapi.json`
- `POST /api/v1/cases:create`, `POST /api/v1/cases:open`
- `POST /api/v1/cases/{case_id}:save`, `POST /api/v1/cases/{case_id}:close` (on the window's API, a trusted client closes its case with `{"save": true|false}`)

### State, operations, and artifacts

- `GET /api/v1/cases/{case_id}/snapshot`
- `GET /api/v1/cases/{case_id}/fields/{field_id}`
- `POST /api/v1/cases/{case_id}/operations/{operation_id}:execute`
- `GET /api/v1/cases/{case_id}/history`, `/artifacts`, and `/events`
- `GET /api/v1/cases/{case_id}/jobs/{job_id}`
- `POST /api/v1/cases/{case_id}/jobs/{job_id}:cancel`
- `GET /api/v1/cases/{case_id}/jobs/{job_id}/log?tail_lines&offset`
- `GET /api/v1/cases/{case_id}/console?after_line&limit&min_level`
- `GET /api/v1/cases/{case_id}/status?after` (the status line)

Geometry, meshing, QA, transforms, repairs, import, and export all use the
operation execution endpoint. Parameter and capability metadata comes from
`/api/v1/operations`.

### Plans and presentation

- `POST /api/v1/cases/{case_id}/operations/{operation_id}:plan`
- `POST /api/v1/cases/{case_id}/plans:validate`
- `POST /api/v1/cases/{case_id}/plans/{plan_id}:confirm|execute|cancel`
- `GET /api/v1/cases/{case_id}/presentation/snapshot`
- `POST /api/v1/cases/{case_id}/presentation/operations/{operation_id}:execute`

## CLI

```text
foammesh version
foammesh generate <surface> -o <case_dir>
foammesh mesh <surface> -o <case_dir>
foammesh checkmesh <log>
foammesh info <case> [--json|--report F]
foammesh check <case> [--quiet]
foammesh transform scale|translate <case> "X Y Z"
foammesh transform rotate <case> <axis> <deg> [--pivot "X Y Z"]
foammesh restore <case>
foammesh import-mesh <case> <src> [--format fluent|gmsh|... | --from-case]
foammesh export <case> <format> [dest]
foammesh formats
foammesh history <case>
foammesh serve --token T --allow-root DIR [--host H --port P]
```

Workflow, mesh, import, export, and history commands execute the same
in-process facade operations as REST and the desktop.
