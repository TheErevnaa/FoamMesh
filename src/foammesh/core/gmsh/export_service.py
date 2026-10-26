"""Write the session mesh in any format, and say what was written (GN15).

:class:`MeshExporter` is the one place the Export page of the Gmsh route
goes through.  It asks the resident session for the facts of its mesh,
refuses a writer that cannot carry them with the reason
(:func:`export_writers.refusal`), writes through the session with the options
chosen, and reads the file back to compare counts.  OpenFOAM and Fluent are
entries of the same list: the session writes a hidden MSH 2.2 copy (the one
``gmshToFoam`` and the publisher of :mod:`publish` read) and the publisher
turns it into a polyMesh, from which ``foamMeshToFluent`` writes the Fluent
file.

Nothing here depends on the solver the mesh is meant for.

Plan 48 GM4 (output honesty).  The user's own ``.msh`` is written exactly as
chosen (any version, ASCII or binary, SaveAll, parametric, split), and
OpenFOAM never reads it: the polyMesh comes from the hidden copy (D1).  A mesh
the target matrix marks CONVERTED is written only through the conversion the
caller names, on a copy made for that write; the mesh in the session and the
user's files are never changed.
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

    @property
    def publisher_copy(self) -> dict:
        """The hidden MSH 2.2 copy OpenFOAM was built from (D1), when there was one."""
        return dict(self.extra.get('publisher_copy') or {})

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

    def __init__(self, session_getter, *, profile=None, overrides=None):
        self._session_getter = session_getter
        self._profile = profile
        #: ``() -> {option: value}``: the project's stored Gmsh options. The
        #: unit pins (D2) are read from it; without it they are locked.
        self._overrides = overrides

    def unit_scaling(self) -> float:
        """Metres in one unit of the model, from the unit pins (1 when locked)."""
        try:
            stored = dict(self._overrides() or {}) if self._overrides else {}
        except Exception:                                    # noqa: BLE001
            stored = {}
        factor = writers.metres_per_unit(stored)
        return float(factor) if factor else 1.0

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
                facts: writers.MeshFacts | None = None, convert=None) -> str:
        facts = facts or self.facts()
        return writers.refusal(writer_id, facts, options, convert=convert)

    def conversions(self, writer_id: str, options=None,
                    facts: writers.MeshFacts | None = None) -> list:
        """The conversions this write needs on the mesh in hand (§4.9.1)."""
        if writers.retired(writer_id):
            return []
        facts = facts or self.facts()
        return writers.conversions(writers.writer(writer_id), facts, options)

    def drops(self, writer_id: str, options=None,
              facts: writers.MeshFacts | None = None) -> list:
        """What this write loses on the mesh in hand, stated before it is written."""
        if writers.retired(writer_id):
            return []
        facts = facts or self.facts()
        return writers.stated_drops(writers.writer(writer_id), facts, options)

    def offered(self, facts: writers.MeshFacts | None = None) -> list:
        """``[(writer, reason or '')]`` for every entry the page lists."""
        facts = facts or self.facts()
        return [(item, writers.refusal(item.id, facts)) for item in
                writers.WRITERS]

    # -- writing ---------------------------------------------------------- #

    def write(self, writer_id: str, destination, options: dict | None = None,
              *, overwrite: bool = False, verify: bool = True,
              check_mesh: bool = False, convert=None,
              extrusion: dict | None = None) -> ExportResult:
        # DP-1437: a retired writer (Medit) is refused before anything else,
        # and so is a destination only a retired writer writes.
        sentence = writers.retired(writer_id) or writers.retired_path(
            destination)
        if sentence:
            raise ExportRefused(sentence)
        try:
            item = writers.writer(writer_id)
        except KeyError as error:
            raise ExportRefused(str(error)) from error
        facts = self.facts()
        chosen = ((convert,) if isinstance(convert, str)
                  else tuple(convert or ()))
        unknown = [name for name in chosen if name not in writers.CONVERSIONS]
        if unknown:
            raise ExportRefused(
                f'there is no export conversion called {unknown[0]!r}; the '
                f'choices are {", ".join(writers.CONVERSIONS)}')
        reason = writers.refusal(writer_id, facts, options, convert=chosen)
        if reason:
            raise ExportRefused(reason)
        stated = writers.stated_drops(item, facts, options)
        try:
            write_options, read_options = _split_options(
                writers.option_values(writer_id, options))
        except catalogue.OptionRefusal as error:
            raise ExportRefused(str(error)) from error
        if item.kind == writers.FOAM:
            result = self._write_openfoam(
                item, Path(destination), facts, overwrite,
                check_mesh=check_mesh, convert=chosen, extrusion=extrusion)
            result.notes += stated
            return result
        if item.kind == writers.FLUENT:
            result = self._write_fluent(item, Path(destination), facts,
                                        overwrite, convert=chosen,
                                        extrusion=extrusion)
            result.notes += stated
            return result
        path = self._named(item, Path(destination))
        outputs = writers.output_paths(item.id, path)
        existing = [Path(out) for out in outputs if Path(out).exists()]
        if existing and not overwrite:
            raise ExportRefused(f'{existing[0]} already exists; choose '
                                'another name or allow overwriting')
        path.parent.mkdir(parents=True, exist_ok=True)
        scale_note = ''
        if (item.id == 'su2' and 'Mesh.ScalingFactor' not in write_options
                and self.unit_scaling() != 1.0):
            write_options['Mesh.ScalingFactor'] = self.unit_scaling()
            scale_note = (f'the model is not in metres (unit pins): the file '
                          f'is scaled by {self.unit_scaling():g} to metres')
        try:
            answer = self._session_write(
                path, write_options, item, outputs, chosen)
        except ExportRefused:
            raise
        except Exception as error:                           # noqa: BLE001
            raise ExportRefused(plain_error(item, error)) from error
        if (verify and item.verify == 'gmsh' and path.is_file()
                and path.stat().st_size):
            answer['found'] = self.read_back(path, read_options)
        result = self._judge(item, path, outputs, answer, facts, verify)
        if 'first_order_copy' in chosen:
            result.notes.append(
                f'{item.label} holds the corner nodes of the order '
                f'{facts.order} mesh (a first-order copy); the mesh in the '
                'session is unchanged')
        if scale_note:
            result.notes.append(scale_note)
        result.notes += stated
        return result

    def _session_write(self, path, write_options, item, outputs, chosen,
                       timeout=None) -> dict:
        """Write through the session, on a first-order copy when chosen."""
        session = self.session()
        kind = 'geometry' if item.kind == writers.GEOMETRY else 'mesh'
        if 'first_order_copy' not in chosen:
            return session.export_file(path, write_options, kind=kind,
                                       outputs=outputs)
        result = session.call(
            'session.export_converted', session.runtime_path(path),
            options=dict(write_options), kind=kind,
            outputs=[session.runtime_path(out) for out in outputs],
            convert='first_order_copy', timeout=timeout)
        root = Path(path).parent
        for entry in result.get('files', ()):
            entry['host_path'] = str(root / Path(entry['path']).name)
        return result

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

    #: Plan 48 GM4 (D1). The hidden copy OpenFOAM is built from: the one
    #: version ``gmshToFoam`` and the publisher read, ASCII, the physical
    #: groups as they are (SaveAll only when there are none to name the
    #: patches). It is written beside the work, never as the user's file.
    PUBLISHER_COPY = {'Mesh.MshFileVersion': 2.2, 'Mesh.Binary': 0.0,
                      'Mesh.SaveParametric': 0.0,
                      'Mesh.PartitionSplitMeshFiles': 0.0}

    def _msh_for_openfoam(self, directory: Path, facts, chosen=()) -> Path:
        from . import publish

        path = directory / ('session' + publish.PUBLISHER_COPY_SUFFIX)
        options = dict(self.PUBLISHER_COPY)
        options['Mesh.SaveAll'] = 0.0 if facts.physical_groups else 1.0
        scale = self.unit_scaling()
        if scale != 1.0:
            options['Mesh.ScalingFactor'] = scale
        self._session_write(path, options, writers.writer('msh'), (path,),
                            chosen)
        if not path.is_file() or not path.stat().st_size:
            raise ExportRefused('Gmsh wrote no mesh to convert')
        return path

    def _publish_arguments(self, facts, chosen, extrusion) -> dict:
        """The conversions of a polyMesh write, as the publisher takes them."""
        from . import publish

        arguments: dict = {}
        if 'extrude_2d' in chosen:
            values = dict(extrusion or {})
            bounds = list(getattr(facts, 'bounds', ()) or ())
            extent = max((bounds[i + 3] - bounds[i] for i in range(3)),
                         default=0.0) if len(bounds) == 6 else 0.0
            values.setdefault('thickness', extent * 0.01 if extent else 0.01)
            arguments['extrusion'] = publish.SectionExtrusion(**values)
        if 'cyclic_patches' in chosen:
            arguments['periodic_pairs'] = [
                {'name': f'{pair["slave"]}_{pair["master"]}',
                 'masterSolverName': pair['master'],
                 'slaveSolverName': pair['slave'],
                 'transform': 'translation',
                 'translation': list(pair['translation']),
                 'matchTolerance': 1e-6}
                for pair in facts.periodic_pairs]
        # Plan 48 WG (DP-445, DP-4215): the build's declared empty/wedge pair.
        getter = getattr(self, '_session_getter', None)
        case_path = getattr(getter() if getter else None, 'case_path', None)
        if case_path is not None:
            from . import geometry_build
            roles = geometry_build.publish_roles(case_path)
            if roles['categories']:
                arguments['categories'] = roles['categories']
            if roles['declared']:
                arguments['declared'] = roles['declared']
        return arguments

    def _write_openfoam(self, item, destination: Path, facts, overwrite,
                        check_mesh=False, convert=(), extrusion=None
                        ) -> ExportResult:
        from . import publish

        if destination.exists() and any(destination.iterdir()) and not overwrite:
            raise ExportRefused(f'{destination} is not empty; choose another '
                                'folder or allow overwriting')
        if destination.exists() and overwrite:
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        with tempfile.TemporaryDirectory(prefix='foammesh-gmsh-',
                                         dir=destination) as work:
            msh = self._msh_for_openfoam(Path(work), facts, convert)
            copy_info = {'version': 2.2, 'binary': False, 'hidden': True,
                         'name': msh.name, 'bytes': msh.stat().st_size}
            try:
                report = publish.publish(
                    msh, destination / 'constant' / 'polyMesh',
                    **self._publish_arguments(facts, convert, extrusion))
            except publish.PublishError as error:
                raise ExportRefused(str(error)) from error
        write_case_files(destination)
        result = ExportResult(writer=item.id, path=str(destination))
        result.extra['publisher_copy'] = copy_info
        expected_cells = sum(
            count for name, count in facts_family_counts(self.session())
            .items() if name in writers.VOLUME_FAMILIES)
        if 'extrude_2d' in convert:
            # the extrusion makes the cells: one layer of the section's cells
            expected_cells = sum(
                count for name, count in facts_family_counts(self.session())
                .items() if name in writers.SURFACE_FAMILIES)
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
                      overwrite, convert=(), extrusion=None) -> ExportResult:
        path = self._named(item, destination)
        if path.exists() and not overwrite:
            raise ExportRefused(f'{path} already exists')
        with tempfile.TemporaryDirectory(prefix='foammesh-fluent-') as work:
            case = Path(work) / 'case'
            self._write_openfoam(writers.writer('openfoam'), case, facts,
                                 False, convert=convert, extrusion=extrusion)
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
        if writers.retired(writer_id):
            raise ExportRefused(writers.retired(writer_id))
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
        # Plan 45: no console window from a console-less build.
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=600, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
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
        # Plan 45: no console window from a console-less build.
        done = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
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
