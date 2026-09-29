"""Processes FoamMesh starts to do work the window must not do (Plan 35 CR2).

A worker is ``python -m foammesh.workers.mesh_worker <op> <args.json>`` from a
source tree and ``FoamMesh.exe --worker <op> <args.json>`` from the packaged
build. Nothing here may import Qt.
"""
