"""Run ``foammesh-sizefield`` in the WSL runtime as a job, with Reuse.

Plan 41 section 12.2 S2. The tool is a Linux binary built into the runtime
(``scripts/build_sizefield.sh``) that FoamMesh ships and copies into the
runtime on request (``tool_setup``); this module builds its command line, runs it
through the same ``JobManager`` the meshers use (progress lines, Cancel, run
record) and keeps the result under ``<case>/foammesh/sizing/field/``.

**Reuse.** The key is the SHA-256 of the staged geometry digest and the tool
inputs. ``run`` with ``reuse=True`` returns the saved field when the key
matches and the arrays are still on disk; any other input reruns the tool.
A missing tool is a measured refusal that names the set-up action, never
a fallback and never a silent copy.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath

from .field_model import SizeField
from .inputs import SizeFieldInputs
from .staging import StagedSurface, geometry_digest

from . import tool_setup

#: Where a developer's own build lives; the shipped tool is copied under the
#: distribution user's home by ``tool_setup`` and found first (RUN_SCRIPT).
TOOL_PATH = tool_setup.LEGACY_PATH
TOOL_MAJOR = 1
RUN_RECORD = 'run.json'
REQUIRED_ARRAYS = ('leaves_ijk', 'leaves_level', 'leaves_size', 'nodes_xyz',
                   'nodes_size', 'hexes')

MISSING_TOOL = tool_setup.MISSING_TOOL

#: The tool's progress lines, in order, and the fraction each one reaches.
STAGES = (('Refining octree', 0.35), ('Smoothing size gradient', 0.6),
          ('Octree leaves', 0.85), ('Wrote the .npy', 1.0))


class SizeFieldRunError(RuntimeError):
    pass


def sizing_dir(case_path) -> Path:
    return Path(case_path) / 'foammesh' / 'sizing'


def field_dir(case_path) -> Path:
    return sizing_dir(case_path) / 'field'


def staged_dir(case_path) -> Path:
    return sizing_dir(case_path) / 'staged'


def reuse_key(geometry: str, inputs) -> str:
    """The Reuse key: staged geometry digest + the inputs that change the field."""
    document = inputs.to_dict() if hasattr(inputs, 'to_dict') else dict(inputs)
    document.pop('threads', None)       # threads change the time, not the field
    text = json.dumps({'geometry': geometry, 'inputs': document},
                      sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode()).hexdigest()


def _linux_path(path, translate=None) -> str:
    if translate is not None:
        return translate(path)
    windows = PureWindowsPath(str(path))
    if not windows.drive:
        return str(PurePosixPath(str(path).replace('\\', '/')))
    parts = [p for p in windows.parts[1:] if p not in ('\\', '/')]
    return str(PurePosixPath('/mnt', windows.drive.rstrip(':').lower(), *parts))


def tool_arguments(staged, out_dir, inputs: SizeFieldInputs,
                   translate=None) -> tuple[str, ...]:
    """The tool's own arguments (no ``wsl.exe`` prefix)."""
    argv = []
    for item in staged:
        argv += ['--surface', f'{item.name}={_linux_path(item.path, translate)}']
    argv += ['--out', _linux_path(out_dir, translate),
             '--hmin', repr(float(inputs.hmin)),
             '--hmax', repr(float(inputs.hmax)),
             '--hbulk', repr(float(inputs.hbulk)),
             '--gradation', repr(float(inputs.gradation)),
             '--nodes-per-two-pi', str(int(inputs.nodes_per_two_pi)),
             '--nodes-per-gap', str(int(inputs.nodes_per_gap)),
             '--threads', str(max(1, int(inputs.threads)))]
    if int(inputs.nodes_per_gap) == 0:
        argv.append('--no-features')
    if not inputs.smoothing:
        argv.append('--no-smoothing')
    if inputs.aniso:
        argv.append('--aniso')
    return tuple(argv)


def build_argv(profile, staged, out_dir, inputs: SizeFieldInputs
               ) -> tuple[str, ...]:
    """``wsl.exe --distribution D [--user U] --exec bash -c RUN_SCRIPT _ ARGS``.

    The script runs the user copy ``tool_setup`` put under ``$HOME`` and then
    the developer's ``/opt/foammesh/bin`` one; neither is a guess about which
    user the distribution runs as.
    """
    prefix = profile._wsl_prefix()
    return (*prefix, 'bash', '-c', tool_setup.RUN_SCRIPT, '_',
            *tool_arguments(staged, out_dir, inputs,
                            profile.translate_host_path))


def progress_of(line: str) -> float | None:
    """Fraction reached when *line* is one of the tool's stage lines."""
    for marker, fraction in STAGES:
        if marker in line:
            return fraction
    return None


def refusal_for(output: str, returncode) -> str | None:
    """The measured refusal text when the tool is not in the runtime."""
    text = (output or '').lower()
    if returncode in (126, 127) or 'no such file' in text \
            or 'not found' in text and 'foammesh-sizefield' in text:
        return MISSING_TOOL
    return None


@dataclass(frozen=True)
class FieldRun:
    field: SizeField
    reused: bool
    key: str
    directory: Path

    def to_dict(self) -> dict:
        return {'reused': self.reused, 'key': self.key,
                'directory': str(self.directory),
                'leaves': self.field.leaf_count,
                'estimated_hex_cells': self.field.estimated_hex_cells()}


def saved_run(case_path) -> dict | None:
    try:
        return json.loads((field_dir(case_path) / RUN_RECORD).read_text('utf-8'))
    except (OSError, ValueError):
        return None


def reusable(case_path, key: str) -> bool:
    record = saved_run(case_path)
    folder = field_dir(case_path)
    return bool(record and record.get('key') == key and all(
        (folder / f'{name}.npy').is_file() for name in REQUIRED_ARRAYS)
        and (folder / 'field.json').is_file())


def status(case_path, key: str | None = None) -> dict:
    """What the card shows: no field, ready, or stale (geometry or inputs)."""
    record = saved_run(case_path)
    if record is None:
        return {'state': 'none'}
    state = 'ready' if key is None or record.get('key') == key else 'stale'
    return {'state': state, 'key': record.get('key'),
            'inputs': record.get('inputs'), 'leaves': record.get('leaves'),
            'message': '' if state == 'ready' else
            'stale: geometry or inputs changed'}


def write_record(case_path, key, geometry, inputs, staged, field) -> None:
    document = {'key': key, 'geometry_digest': geometry,
                'inputs': inputs.to_dict(), 'leaves': field.leaf_count,
                'surfaces': [item.to_dict() for item in staged],
                'tool_version': field.meta.get('version')}
    (field_dir(case_path) / RUN_RECORD).write_text(
        json.dumps(document, indent=1), 'utf-8')


async def run(manager, profile, case_path, staged: list[StagedSurface],
              inputs: SizeFieldInputs, *, reuse: bool = True, on_line=None,
              timeout: float | None = None) -> FieldRun:
    """Compute the field, or hand back the saved one when Reuse applies."""
    from foammesh.core.jobs.manager import JobRequest, JobStatus
    inputs.validate()
    geometry = geometry_digest(staged)
    key = reuse_key(geometry, inputs)
    folder = field_dir(case_path)
    if reuse and reusable(case_path, key):
        return FieldRun(SizeField.load(folder), True, key, folder)
    folder.mkdir(parents=True, exist_ok=True)
    argv = build_argv(profile, staged, folder, inputs)

    def watch(line):
        if on_line is not None:
            on_line(line, progress_of(line))

    request = JobRequest(
        name='sizing.field', argv=argv, cwd=Path(case_path),
        log_path=sizing_dir(case_path) / 'field.log', timeout=timeout,
        expected_outputs=(folder / 'field.json',),
        record_case=Path(case_path),
        record_fields={'operation': 'sizing.field.compute'})
    result = await manager.run(request, on_line=watch)
    if result.status is not JobStatus.DONE:
        refusal = refusal_for(result.output, result.returncode)
        raise SizeFieldRunError(
            refusal or result.error or result.reason
            or 'the size field run did not finish')
    field = SizeField.load(folder)
    write_record(case_path, key, geometry, inputs, staged, field)
    return FieldRun(field, False, key, folder)
