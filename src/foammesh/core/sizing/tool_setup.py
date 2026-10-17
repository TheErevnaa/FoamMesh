"""Put ``foammesh-sizefield`` into the user's OpenFOAM 13 WSL distribution.

Plan 41 GN25e (rework). The tool is a small self-contained Linux binary shipped
with FoamMesh (``src/resources/sizefield/linux-x86_64``; p4est and libsc are
linked into it). It is not part of a distribution image and nothing is
downloaded: on the user's say-so (the "Set up automatic sizing" action) the
app copies the shipped file into the distribution it already uses for
OpenFOAM, under the user's home (no root, no sudo):

    ``$HOME/.local/share/foammesh/sizefield/foammesh-sizefield``

The copy is checked by SHA-256 and by running ``--version``. A copy that is
not the shipped one (an older FoamMesh) is refused as stale until the action
copies it again. ``/opt/foammesh/bin/foammesh-sizefield`` (a developer's own
build) is looked at second, so a machine that already has it keeps working.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from foammesh.core.openfoam_runtime.detect import _run, _text

#: The tool as the app ships it, relative to ``src/resources``.
BUNDLE_RELATIVE = 'sizefield/linux-x86_64/foammesh-sizefield'
TOOL_NAME = 'foammesh-sizefield'
TOOL_MAJOR = 1
VERSION_PREFIX = f'{TOOL_NAME} {TOOL_MAJOR}.'

#: Under the distribution user's home (no root needed).
USER_DIRECTORY = '.local/share/foammesh/sizefield'
USER_PATH = f'$HOME/{USER_DIRECTORY}/{TOOL_NAME}'
#: A developer's own build; looked at after the user copy.
LEGACY_PATH = f'/opt/foammesh/bin/{TOOL_NAME}'
RECORD_NAME = 'installed.json'

#: The refusal text. The card shows the action when it sees this one.
SETUP_ACTION = 'Set up automatic sizing'
MISSING_TOOL = (
    'Automatic sizing needs the size-field tool in the OpenFOAM 13 runtime, '
    'and it is not there yet. Use "' + SETUP_ACTION + '" to copy it from '
    'FoamMesh (about 1 MB, into your home folder in the runtime; nothing is '
    'downloaded and nothing needs administrator rights).')

#: DP-1442. The tool is single-process but p4est/libsc call ``MPI_Init``. Run as
#: a bare singleton, OpenMPI 4.x starts ``orted`` helper processes that were seen
#: spinning at 100 % CPU and wedging the distribution. An *isolated* singleton
#: starts none (``ess_singleton_isolated``), and the shared-memory/network
#: transports are not needed for one rank (``btl=self``).
SINGLETON_ENV = (
    'export OMPI_MCA_ess_singleton_isolated=1 OMPI_MCA_btl=self '
    'OMPI_MCA_plm=isolated OMPI_MCA_rmaps_base_oversubscribe=1; ')
#: Seconds a run may take before the watchdog ends it, and the grace before
#: SIGKILL. ``timeout`` makes the tool a process-group leader and signals the
#: whole group, so a helper the tool started cannot outlive it.
RUN_TIMEOUT_SECONDS = 3600
VERSION_TIMEOUT_SECONDS = 30
KILL_GRACE_SECONDS = 5
WATCHDOG = f'timeout -k {KILL_GRACE_SECONDS} '

#: ``bash -c`` script: the first copy that starts. ``$@`` are the tool's own
#: arguments. Used by ``field_runner.build_argv``.
RUN_SCRIPT = (
    SINGLETON_ENV
    + f'for p in "{USER_PATH}" "{LEGACY_PATH}"; do '
    f'[ -x "$p" ] && exec {WATCHDOG}"${{FOAMMESH_SIZEFIELD_TIMEOUT:-{RUN_TIMEOUT_SECONDS}}}" "$p" "$@"; done; '
    f'echo "{TOOL_NAME}: not found" >&2; exit 127')

#: ``--version`` under the same environment and a short watchdog.
VERSION_COMMAND = f'{WATCHDOG}{VERSION_TIMEOUT_SECONDS} "$p" --version 2>/dev/null'

_STATUS_SCRIPT = (
    SINGLETON_ENV
    + 'for tag in user legacy; do '
    f'if [ "$tag" = user ]; then p="{USER_PATH}"; else p="{LEGACY_PATH}"; fi; '
    'if [ -f "$p" ]; then '
    'echo "$tag.path=$p"; '
    'echo "$tag.sha=$(sha256sum "$p" | cut -d" " -f1)"; '
    'echo "$tag.version=$(' + VERSION_COMMAND + ' | head -n 1)"; fi; done; '
    'echo "user.name=$(id -un)"; true')

#: ``$1`` is the shipped file as the distribution sees it.
_COPY_SCRIPT = (
    SINGLETON_ENV
    + 'set -e; umask 022; '
    f'D="$HOME/{USER_DIRECTORY}"; mkdir -p "$D"; '
    f'cp -f "$1" "$D/{TOOL_NAME}.new"; chmod 755 "$D/{TOOL_NAME}.new"; '
    f'mv -f "$D/{TOOL_NAME}.new" "$D/{TOOL_NAME}"; '
    f'printf \'{{"sha256": "%s", "version": "%s", "path": "%s"}}\\n\' '
    f'"$(sha256sum "$D/{TOOL_NAME}" | cut -d" " -f1)" '
    f'"$({WATCHDOG}{VERSION_TIMEOUT_SECONDS} "$D/{TOOL_NAME}" --version | head -n 1)" "$D/{TOOL_NAME}" '
    f'> "$D/{RECORD_NAME}"; '
    f'echo "done=$D/{TOOL_NAME}"')


class ToolSetupError(RuntimeError):
    pass


def bundle_path() -> Path:
    from resources import resource
    return resource.file(BUNDLE_RELATIVE)


def file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def bundle_sha256(path=None) -> str:
    return file_sha256(path or bundle_path())


def _wsl(profile, script: str, *arguments: str) -> tuple:
    return (*profile._wsl_prefix(), 'bash', '-c', script, '_', *arguments)


def _values(output: bytes) -> dict:
    found = {}
    for line in _text(output).splitlines():
        key, separator, value = line.strip().partition('=')
        if separator:
            found.setdefault(key, value.strip())
    return found


def _usable(version: str) -> bool:
    return version.startswith(VERSION_PREFIX)


def status(profile, *, runner=_run, timeout: float = 60.0,
           expected_sha: str | None = None) -> dict:
    """Where the tool is in *profile*'s distribution, and whether it is usable.

    ``state``: ``ok`` (a usable copy: the shipped one, or the developer's),
    ``stale`` (the user copy is not the shipped file), ``broken`` (a copy is
    there and does not start), ``missing``, or ``unreachable`` (the
    distribution would not answer).
    """
    expected = expected_sha if expected_sha is not None else bundle_sha256()
    code, output = runner(_wsl(profile, _STATUS_SCRIPT), timeout)
    values = _values(output)
    result = {'distribution': str(getattr(profile, 'distribution', '') or ''),
              'user': values.get('user.name', ''), 'bundle_sha256': expected,
              'path': '', 'version': '', 'sha256': '', 'source': '',
              'state': 'missing', 'message': MISSING_TOOL}
    if code != 0 or not values.get('user.name'):
        result.update(state='unreachable', message=(
            'The OpenFOAM 13 runtime did not answer, so the size-field tool '
            'could not be looked for.'))
        return result
    user_version = values.get('user.version', '')
    if 'user.path' in values:
        result.update(path=values['user.path'], sha256=values.get('user.sha', ''),
                      version=user_version, source='user')
        if _usable(user_version) and result['sha256'] == expected:
            result.update(state='ok', message='')
            return result
    legacy_version = values.get('legacy.version', '')
    if 'legacy.path' in values and _usable(legacy_version):
        result.update(path=values['legacy.path'], sha256=values.get('legacy.sha', ''),
                      version=legacy_version, source='legacy', state='ok',
                      message='')
        return result
    if 'user.path' in values:
        if _usable(user_version):
            result.update(state='stale', message=(
                'The size-field tool in the runtime is not the one this '
                'FoamMesh ships. Use "' + SETUP_ACTION + '" to copy it again.'))
        else:
            result.update(state='broken', message=(
                f'The size-field tool at {values["user.path"]} does not start '
                f'in {result["distribution"]}. Use "' + SETUP_ACTION
                + '" to copy it again.'))
    return result


def set_up(profile, *, runner=_run, timeout: float = 120.0,
           bundle=None) -> dict:
    """Copy the shipped tool into the runtime and check it; never silent.

    Returns the new ``status``; raises :class:`ToolSetupError` with the
    measured reason when the copy or the check fails. Nothing is installed
    and nothing is downloaded: it is one ``cp`` of a file FoamMesh ships.
    """
    source = Path(bundle) if bundle is not None else bundle_path()
    if not source.is_file():
        raise ToolSetupError(
            f'This FoamMesh does not contain the size-field tool ({source}).')
    expected = file_sha256(source)
    before = status(profile, runner=runner, timeout=timeout,
                    expected_sha=expected)
    if before['state'] == 'unreachable':
        raise ToolSetupError(before['message'])
    if before['state'] == 'ok' and before['source'] == 'user':
        return before
    code, output = runner(_wsl(profile, _COPY_SCRIPT + ' 2>&1',
                               profile.translate_host_path(source)), timeout)
    if code != 0 or 'done=' not in _text(output):
        raise ToolSetupError(
            'Copying the size-field tool into the runtime failed: '
            + (_text(output).strip()[-400:] or f'exit code {code}'))
    after = status(profile, runner=runner, timeout=timeout,
                   expected_sha=expected)
    if after['state'] != 'ok' or after['source'] != 'user':
        raise ToolSetupError(
            'The copied size-field tool did not pass its check: '
            + (after['message'] or f'it printed {after["version"]!r}'))
    after['copied'] = True
    return after


def record(status_payload: dict) -> str:
    """The one line the card shows after a successful set-up."""
    return (f'{status_payload["version"]} is in '
            f'{status_payload["distribution"]} at {status_payload["path"]}.')

