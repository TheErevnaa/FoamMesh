# API & CLI Reference

## REST API (`foammesh[api]`)

Run `foammesh serve`, then open `/docs` for runtime-generated OpenAPI
documentation. The only public domain surface is versioned under `/api/v1`.
Clients use semantic field and operation IDs, never database paths or arbitrary
executable names.

### Discovery and lifecycle

- `GET /api/v1/system/capabilities`
- `GET /api/v1/fields`, `GET /api/v1/operations`, `GET /api/v1/openapi.json`
- `POST /api/v1/cases:create`, `POST /api/v1/cases:open`
- `POST /api/v1/cases/{case_id}:save`, `POST /api/v1/cases/{case_id}:close`

### State, operations, and artifacts

- `GET /api/v1/cases/{case_id}/snapshot`
- `GET /api/v1/cases/{case_id}/fields/{field_id}`
- `POST /api/v1/cases/{case_id}/operations/{operation_id}:execute`
- `GET /api/v1/cases/{case_id}/history`, `/artifacts`, and `/events`
- `GET /api/v1/cases/{case_id}/jobs/{job_id}`
- `POST /api/v1/cases/{case_id}/jobs/{job_id}:cancel`

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
foammesh serve [--host H --port P]
```

Workflow, mesh, import, export, and history commands execute the same
in-process facade operations as REST and the desktop.
