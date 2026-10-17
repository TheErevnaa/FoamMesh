"""Write the session mesh in any format, and say what was written (GN15).

:class:`MeshExporter` is the one place the Export page of the Gmsh route
goes through.  It asks the resident session for the facts of its mesh,
refuses a writer that cannot carry them with the reason
(:func:`export_writers.refusal`), writes through the session with the options
chosen, and reads the file back to compare counts.  OpenFOAM and Fluent are
entries of the same list: the session writes MSH 4.1 (binary) and the
publisher of :mod:`publish` turns it into a polyMesh, from which
``foamMeshToFluent`` writes the Fluent file.

Nothing here depends on the solver the mesh is meant for.
"""
from __future__ import annotations

import shutil
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import export_verify
from . import export_writers as writers
from . import option_catalogue as catalogue


class ExportRefused(ValueError):
    """The writer cannot take this mesh, or the destination cannot be used."""


@dataclass
class ExportResult:
    writer: str
    path: str
    files: list = field(default_factory=list)
    bytes: int = 0
    seconds: float = 0.0
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    expected: dict = field(default_factory=dict)
    found: dict = field(default_factory=dict)
    verified: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems and self.bytes > 0

    def to_dict(self) -> dict:
        return {'writer': self.writer, 'path': self.path,
                'files': list(self.files), 'bytes': self.bytes,
                'seconds': self.seconds, 'problems': list(self.problems),
                'notes': list(self.notes), 'expected': dict(self.expected),
                'found': dict(self.found), 'verified': self.verified,
                'ok': self.ok, **self.extra}


def plain_error(item, error) -> str:
    """A raw Gmsh exception as a sentence (WALK-2 SG15)."""
    text = str(error).strip()
    if 'OpenCASCADE' in text and 'CAD' in text:
        return f'{item.label}: {writers.NO_CAD}'
    if 'segmentation' in text.lower() or 'crashed' in text.lower():
        return (f'{item.label}: Gmsh crashed while writing this file. '
                'Choose another format.')
    return f'{item.label}: Gmsh could not write the file ({text or "no reason given"}).'


def _split_options(raw: dict | None) -> tuple[dict, dict]:
    write, read = {}, {}
    for name, value in dict(raw or {}).items():
        (read if name in writers.READ_OPTIONS else write)[name] = value
    return write, read


class MeshExporter:
    """Exports the mesh a :class:`GmshSession` holds."""

    def __init__(self, session_getter, *, profile=None):
        self._session_getter = session_getter
        self._profile = profile

    # -- the mesh in hand ------------------------------------------------ #

    def session(self):
        session = self._session_getter()
        if session is None or not session.alive:
            raise ExportRefused('there is no Gmsh session with a mesh yet: '
                                'generate one first')
        return session

    def facts(self) -> writers.MeshFacts:
        return writers.MeshFacts.from_dict(self.session().export_facts())

    def refusal(self, writer_id: str, options=None,
                facts: writers.MeshFacts | None = None) -> str:
        facts = facts or self.facts()
        return writers.refusal(writer_id, facts, options)

    def offered(self, facts: writers.MeshFacts | None = None) -> list:
        """``[(writer, reason or '')]`` for every entry the page lists."""
        facts = facts or self.facts()
        return [(item, writers.refusal(item.id, facts)) for item in
                writers.WRITERS]

    # -- writing ---------------------------------------------------------- #

    def write(self, writer_id: str, destination, options: dict | None = None,
              *, overwrite: bool = False, verify: bool = True,
              check_mesh: bool = False) -> ExportResult:
        item = writers.writer(writer_id)
        facts = self.facts()
        reason = writers.refusal(writer_id, facts, options)
        if reason:
            raise ExportRefused(reason)
        try:
            write_options, read_options = _split_options(
                writers.option_values(writer_id, options))
        except catalogue.OptionRefusal as error:
            raise ExportRefused(str(error)) from error
        if item.kind == writers.FOAM:
            return self._write_openfoam(item, Path(destination), facts,
                                        overwrite, check_mesh=check_mesh)
        if item.kind == writers.FLUENT:
            return self._write_fluent(item, Path(destination), facts,
                                      overwrite)
        path = self._named(item, Path(destination))
        outputs = writers.output_paths(item.id, path)
        existing = [Path(out) for out in outputs if Path(out).exists()]
        if existing and not overwrite:
            raise ExportRefused(f'{existing[0]} already exists; choose '
                                'another name or allow overwriting')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            answer = self.session().export_file(
                path, write_options,
                kind='geometry' if item.kind == writers.GEOMETRY else 'mesh',
                outputs=outputs)
        except ExportRefused:
            raise
        except Exception as error:                           # noqa: BLE001
            raise ExportRefused(plain_error(item, error)) from error
        if (verify and item.verify == 'gmsh' and path.is_file()
                and path.stat().st_size):
            answer['found'] = self.read_back(path, read_options)
        return self._judge(item, path, outputs, answer, facts, verify)

    def read_back(self, path, read_options: dict | None = None) -> dict:
        """The census of *path* read by a Gmsh of its own (never the session's)."""
        return read_back(path, read_options, self._profile)

    @staticmethod
    def _named(item: writers.Writer, destination: Path) -> Path:
        if not item.suffix:
            return destination
        if destination.suffix.lower() == item.suffix:
            return destination
        if destination.suffix and destination.suffix.lower() != item.suffix:
            raise ExportRefused(
                f'{item.label} files end in {item.suffix}, not '
                f'{destination.suffix}')
        return destination.with_name(destination.name + item.suffix)

    def _judge(self, item, path, outputs, answer, facts, verify) -> ExportResult:
        result = ExportResult(writer=item.id, path=str(path),
                              seconds=float(answer.get('seconds') or 0.0))
        result.files = [dict(entry) for entry in answer.get('files', ())]
        named = {Path(out).name for out in outputs}
        for entry in result.files:
            result.bytes += int(entry.get('bytes') or 0)
        missing = [name for name in sorted(named) if not any(
            Path(entry['path']).name == name and entry.get('bytes')
            for entry in result.files)]
        if missing:
            result.problems.append(
                f'{item.label}: Gmsh did not create {", ".join(missing)} '
                'or wrote it empty')
        expected = dict(answer.get('expected') or {})
        result.expected = expected
        found = dict(answer.get('found') or {})
        if item.verify == 'text' and verify and not missing:
            found = export_verify.text_census(path) or {}
            found['elements'] = found.get('elements')
        result.found = found
        if found.get('error'):
            result.problems.append(f'{item.label}: {found["error"]}')
        elif verify and not missing and item.verify != 'file':
            if item.kind == writers.SURFACE:
                # A surface writer stores the boundary: compare the surface
                # elements Gmsh reads back with those the model gave it.
                expected = dict(expected)
            result.problems += writers.compare(item.id, expected, found)
            result.verified = not result.problems
        elif item.verify == 'file':
            result.verified = not missing
        if item.kind == writers.SURFACE and facts.order > 1:
            result.notes.append(
                f'{item.label} stores a first order surface: the order '
                f'{facts.order} mesh was written as its corner elements')
        if len(result.files) > 1 and item.id == 'msh':
            result.notes.append(f'{len(result.files)} files were written, '
                                'one per partition')
        return result

    # -- OpenFOAM and Fluent --------------------------------------------- #

    def _msh_for_openfoam(self, directory: Path, facts) -> Path:
        path = directory / 'session.msh'
        session = self.session()
        options = {'Mesh.MshFileVersion': 4.1, 'Mesh.Binary': 1.0}
        if not facts.physical_groups:
            options['Mesh.SaveAll'] = 1.0
        session.export_file(path, options, outputs=(path,))
        if not path.is_file() or not path.stat().st_size:
            raise ExportRefused('Gmsh wrote no mesh to convert')
        return path

    def _write_openfoam(self, item, destination: Path, facts, overwrite,
                        check_mesh=False) -> ExportResult:
        from . import publish

        if destination.exists() and any(destination.iterdir()) and not overwrite:
            raise ExportRefused(f'{destination} is not empty; choose another '
                                'folder or allow overwriting')
        if destination.exists() and overwrite:
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix='foammesh-gmsh-',
                                         dir=destination) as work:
            msh = self._msh_for_openfoam(Path(work), facts)
            try:
                report = publish.publish(msh, destination / 'constant' /
                                         'polyMesh')
            except publish.PublishError as error:
                raise ExportRefused(str(error)) from error
        write_case_files(destination)
        result = ExportResult(writer=item.id, path=str(destination))
        expected_cells = sum(
            count for name, count in facts_family_counts(self.session())
            .items() if name in writers.VOLUME_FAMILIES)
        result.expected = {'cells': expected_cells}
        result.found = {'cells': report.cells, 'points': report.points,
                        'patches': list(report.patches),
                        'cells_by_type': dict(report.cells_by_type)}
        result.files = [{'path': str(destination / 'constant' / 'polyMesh' / name)}
                        for name in ('points', 'faces', 'owner', 'neighbour',
                                     'boundary')]
        result.bytes = sum((destination / 'constant' / 'polyMesh' / name
                            ).stat().st_size for name in
                           ('points', 'faces', 'owner', 'neighbour'))
        if expected_cells and report.cells != expected_cells:
            result.problems.append(
                f'{report.cells} cells in the polyMesh, {expected_cells} in '
                'the Gmsh mesh')
        result.verified = not result.problems
        result.notes += list(report.warnings)
        if check_mesh:
            outcome = run_check_mesh(destination, self._profile)
            result.extra['check_mesh'] = outcome
            if not outcome.get('ok'):
                result.problems.append('checkMesh: ' +
                                       str(outcome.get('summary')))
        return result

    def _write_fluent(self, item, destination: Path, facts,
                      overwrite) -> ExportResult:
        path = self._named(item, destination)
        if path.exists() and not overwrite:
            raise ExportRefused(f'{path} already exists')
        with tempfile.TemporaryDirectory(prefix='foammesh-fluent-') as work:
            case = Path(work) / 'case'
            self._write_openfoam(writers.writer('openfoam'), case, facts,
                                 False)
            outcome = run_foam_utility(case, 'foamMeshToFluent', self._profile)
            produced = case / 'fluentInterface' / f'{case.name}.msh'
            if not outcome['ok'] or not produced.is_file():
                raise ExportRefused('foamMeshToFluent failed: ' +
                                    outcome.get('tail', ''))
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(produced, path)
        result = ExportResult(writer=item.id, path=str(path),
                              bytes=path.stat().st_size,
                              files=[{'path': str(path),
                                      'bytes': path.stat().st_size}])
        result.verified = result.bytes > 0
        return result

    # -- the options file ------------------------------------------------- #

    @staticmethod
    def write_options_file(path, writer_id: str, options: dict) -> Path:
        """Save the options of one export as a Gmsh ``.opt`` file (F-15)."""
        values = writers.option_values(writer_id, options)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        catalogue.write_opt_file(target, values)
        return target


def read_back(path, read_options=None, profile=None) -> dict:
    """Count what Gmsh reads from *path*, in a process started for the purpose."""
    import json
    from resources import resource

    if profile is None:
        from .launch_profiles import configured_profiles
        profile = configured_profiles({})[0]
    script = profile.translate_host_path(
        Path(resource.file('gmsh/readback_v1.py')).resolve())
    argv = (*profile._wsl_prefix(), profile.python, '-u', script,
            profile.translate_host_path(path),
            json.dumps(dict(read_options or {})))
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'error': f'the read-back could not run: {error}'}
    for line in reversed((done.stdout or '').splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {'error': 'the read-back printed no census: ' +
            ((done.stderr or done.stdout or '').strip()[-200:])}


def facts_family_counts(session) -> dict:
    return dict(session.export_facts().get('family_counts') or {})


_CONTROL_DICT = '''FoamFile
{
    format      ascii;
    class       dictionary;
    object      controlDict;
}
application     checkMesh;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         1;
deltaT          1;
writeControl    timeStep;
writeInterval   1;
'''

_FV_SCHEMES = '''FoamFile
{
    format      ascii;
    class       dictionary;
    object      fvSchemes;
}
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; }
laplacianSchemes { default Gauss linear corrected; }
interpolationSchemes { default linear; }
snGradSchemes { default corrected; }
'''

_FV_SOLUTION = '''FoamFile
{
    format      ascii;
    class       dictionary;
    object      fvSolution;
}
solvers {}
'''


def write_case_files(case: Path) -> None:
    """The three system files an OpenFOAM utility needs to open the case."""
    system = case / 'system'
    system.mkdir(parents=True, exist_ok=True)
    for name, text in (('controlDict', _CONTROL_DICT),
                       ('fvSchemes', _FV_SCHEMES),
                       ('fvSolution', _FV_SOLUTION)):
        file = system / name
        if not file.exists():
            file.write_text(text, encoding='utf-8')


def run_foam_utility(case: Path, utility: str, profile=None, *,
                     arguments: str = '', timeout: float = 600.0) -> dict:
    """Run one OpenFOAM utility on *case* in the WSL runtime."""
    if profile is None:
        from .launch_profiles import configured_profiles
        profile = configured_profiles({})[0]
    runtime_case = profile.translate_host_path(case)
    script = (f'source /opt/openfoam13/etc/bashrc >/dev/null 2>&1; '
              f'cd {shlex.quote(runtime_case)} && {utility} {arguments}')
    argv = (*profile._wsl_prefix(), 'bash', '-c', script)
    try:
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'ok': False, 'tail': str(error), 'returncode': None}
    output = (done.stdout or '') + (done.stderr or '')
    return {'ok': done.returncode == 0, 'returncode': done.returncode,
            'tail': '\n'.join(output.strip().splitlines()[-12:]),
            'output': output}


def run_check_mesh(case: Path, profile=None) -> dict:
    outcome = run_foam_utility(case, 'checkMesh', profile)
    output = outcome.get('output', '')
    ok = outcome['ok'] and 'Mesh OK' in output
    failed = [line.strip() for line in output.splitlines()
              if line.strip().startswith('***')]
    return {'ok': ok, 'summary': 'Mesh OK' if ok else (
        '; '.join(failed) or outcome.get('tail', '')), 'tail': outcome['tail']}
