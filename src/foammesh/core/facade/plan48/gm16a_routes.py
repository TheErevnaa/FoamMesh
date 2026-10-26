"""Plan 48 GM16a local-API routes: FoamMesh's own Gmsh build.

A plain remote control for the "Set up FoamMesh's Gmsh" action and the status
line the window shows next to it. It controls only what the window controls.

* ``gmsh.build.status``  where the build is in the OpenFOAM 13 runtime, whether
  Python uses it, and (with ``probe: true``) what the running Gmsh reports:
  version, ``General.BuildOptions`` and whether the wheel is standing in.
* ``gmsh.build.setup``   copy the shipped build under the runtime user's home
  and switch to it (one file, sha256-checked, no download, no root).
* ``gmsh.build.disable`` stop using the build; the official wheel answers again.
"""
from __future__ import annotations

import asyncio

from foammesh.core.facade.errors import CapabilityUnavailableError
from foammesh.core.facade.operations import OperationDescriptor
from foammesh.core.facade.policy import ImpactClass
from foammesh.core.facade.results import OperationResult

OPERATIONS = (
    OperationDescriptor(
        'gmsh.build.status', "FoamMesh's Gmsh build status", 'case',
        ImpactClass.READ,
        summary="Where FoamMesh's own Gmsh 4.15.2 build (Revoropt, "
                'CVTRemesh) is in the OpenFOAM 13 runtime, whether it is the '
                'shipped one and in use. With probe=true also the version and '
                'General.BuildOptions of the Gmsh that answers, and the '
                'notice shown while the official wheel stands in.',
        parameters_schema={
            'type': 'object', 'additionalProperties': False,
            'properties': {'probe': {'type': 'boolean', 'default': False}}}),
    OperationDescriptor(
        'gmsh.build.setup', "Set up FoamMesh's Gmsh", 'case',
        ImpactClass.FILE_PRODUCING,
        summary="Copy the shipped FoamMesh Gmsh build into the home folder of "
                'the runtime user, check its sha256 and that it imports, and '
                'use it. No download, no administrator rights.'),
    OperationDescriptor(
        'gmsh.build.disable', "Stop using FoamMesh's Gmsh", 'case',
        ImpactClass.FILE_PRODUCING,
        summary='Switch back to the official Gmsh wheel (the build files '
                'stay). The wheel notice is shown until the build is set up '
                'again.'),
)

_NO_RUNTIME = ('No Gmsh runtime is configured; Gmsh runs in a WSL '
               'distribution and this host has none.')


def _profile():
    from foammesh.core.gmsh.launch_profiles import configured_profiles
    profiles = configured_profiles()
    if not profiles:
        raise CapabilityUnavailableError(_NO_RUNTIME)
    return profiles[0]


def _bundle_state(runtime) -> dict:
    """Whether this FoamMesh contains the build, and its sha256."""
    try:
        path = runtime.bundle_path()
        present = path.is_file()
    except Exception:  # a frozen app without the resource tree
        return {'shipped': False, 'sha256': ''}
    return {'shipped': bool(present),
            'sha256': runtime._file_sha256(path) if present else ''}


def _status(profile, params) -> dict:
    from foammesh.core.gmsh import runtime
    bundle = _bundle_state(runtime)
    found = runtime.build_status(
        profile, expected_bundle_sha=bundle['sha256'] or None)
    found['bundle'] = bundle
    if params.get('probe'):
        reports = runtime.probe_configured(refresh=True)
        found['runtime'] = reports[0].to_dict() if reports else {}
    return found


def handlers():
    from foammesh.core.gmsh import runtime

    async def status(session, command):
        params = dict(command.parameters or {})
        found = await asyncio.to_thread(_status, _profile(), params)
        return OperationResult('accepted', command.operation,
                               session.revisions, payload=found)

    async def setup(session, command):
        profile = _profile()
        try:
            found = await asyncio.to_thread(runtime.install_build, profile)
        except runtime.GmshBuildSetupError as error:
            return OperationResult(
                'failed', command.operation, session.revisions,
                warnings=(str(error),), payload={})
        found['message'] = (
            f"{runtime.BUILD_ID} is in {found['distribution']} at "
            f"{found['path']}.")
        return OperationResult('accepted', command.operation,
                               session.revisions, payload=found)

    async def disable(session, command):
        profile = _profile()
        try:
            found = await asyncio.to_thread(runtime.disable_build, profile)
        except runtime.GmshBuildSetupError as error:
            return OperationResult(
                'failed', command.operation, session.revisions,
                warnings=(str(error),), payload={})
        return OperationResult('accepted', command.operation,
                               session.revisions, payload=found)

    return {
        'gmsh.build.status': status,
        'gmsh.build.setup': setup,
        'gmsh.build.disable': disable,
    }
