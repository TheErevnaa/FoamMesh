"""What a running mesher is doing, read from the log the app already keeps.

Plan 40 UXD1 (section 4.5.7). A snappyHexMesh stage prints its progress as
text and the Gmsh runner prints ``FOAMMESH_PROGRESS`` records; the console
shows them as lines. This module turns the same lines into numbers a chart can
draw, so the chart and the log can never disagree: there is no second source.

snappy
    * a stage timeline: castellated, snap, layers;
    * cells per refinement level after every refinement iteration;
    * the snap displacement (average and largest) per morph iteration;
    * the layer coverage per patch (the share of the wanted thickness the
      layers reached) and the share of faces extruded per layer iteration;
    * an ETA from the rate of the iterations already done, or, before the
      first one of a phase is done, from the previous phase's rate.

Gmsh
    * the runner's stages with their progress fraction and the element count
      the mesh stage reports.

The parser is pure: it takes lines and a clock reading and holds no Qt object,
so it can be fed a recorded log and compared with the numbers in that log.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from foammesh.core.naming import humanise_option

__all__ = ['is_mesher_job', 'RunProgress', 'RefineStep', 'SnapStep', 'LayerPatch', 'Stage',
           'SNAPPY_STAGES', 'PROGRESS_PREFIX']

PROGRESS_PREFIX = 'FOAMMESH_PROGRESS '

#: The three phases one snappyHexMesh log can hold, in the order it runs them.
SNAPPY_STAGES = (('castellated', 'Castellated'), ('snap', 'Snap'),
                 ('layers', 'Layers'))

_CELLS = re.compile(r'cells:\s*(\d+)\s+faces:\s*(\d+)\s+points:\s*(\d+)')
_AFTER_REFINEMENT = re.compile(
    r'^After refinement (?P<what>.+?) iteration (?P<n>\d+) : ')
_LEVEL_ROW = re.compile(r'^\s+(\d+)\s+(\d+)\s*$')
_SNAP_TOTAL = re.compile(r'^Snapping to features in (\d+) iterations')
_MORPH = re.compile(r'^Morph iteration (\d+)')
_DISPLACEMENT = re.compile(
    r'^Wanted displacement : average:(\S+) min:(\S+) max:(\S+)')
_LAYER_ITERATION = re.compile(r'^Layer addition iteration (\d+)')
_EXTRUDING = re.compile(
    r'^Extruding (\d+) out of (\d+) faces \(([\d.]+)%\)')
_ADDED_CELLS = re.compile(r'^Added (\d+) out of (\d+) cells \(([\d.]+)%\)')
_LAYER_ROW = re.compile(
    r'^(?P<patch>\S+)\s+(?P<faces>\d+)\s+(?P<layers>[\d.]+)\s+'
    r'(?P<thickness>[\d.eE+-]+)\s+(?P<percent>[\d.]+)\s*$')

def is_mesher_job(argv) -> bool:
    """True for a job that runs snappyHexMesh, the Gmsh runner or a Gmsh
    session action (Plan 42 SG16)."""
    text = ' '.join(str(part) for part in (argv or ()))
    return ('snappyHexMesh' in text or 'runner_v1' in text
            or 'gmsh-session' in text)


@dataclass
class Stage:
    key: str
    label: str
    #: pending, running, done, failed, cancelled
    status: str = 'pending'
    started: float | None = None
    ended: float | None = None
    #: Iterations finished / the total when the log says it.
    iterations: int = 0
    total: int | None = None

    def seconds(self, now: float) -> float | None:
        if self.started is None:
            return None
        return max(0.0, (self.ended if self.ended is not None else now)
                   - self.started)


@dataclass
class RefineStep:
    label: str
    cells: int
    faces: int
    points: int
    #: ``{refinement level: cells}``, as the log lists it under the line.
    levels: dict[int, int] = field(default_factory=dict)


@dataclass
class SnapStep:
    iteration: int
    average: float
    minimum: float
    maximum: float


@dataclass
class LayerPatch:
    patch: str
    faces: int
    layers: float
    thickness: float
    percent: float


class RunProgress:
    """The numbers of one mesher run, built up line by line.

    ``feed`` takes the lines of a batch and the clock reading they arrived at;
    ``revision`` rises whenever anything a chart shows changed, so a view can
    redraw only when it must.
    """

    def __init__(self):
        self.reset()

    # ---------------------------------------------------------------- state
    def reset(self) -> None:
        self.engine = ''
        #: idle, running, done, failed, cancelled
        self.state = 'idle'
        self.revision = 0
        self.started: float | None = None
        self.ended: float | None = None
        self.stages: list[Stage] = []
        self.refinement: list[RefineStep] = []
        self.snap: list[SnapStep] = []
        self.extrusion: list[tuple[int, float]] = []
        self.layer_patches: list[LayerPatch] = []
        self.layer_cells_percent: float | None = None
        self.final_cells: int | None = None
        # Gmsh
        self.fraction = 0.0
        self.message = ''
        self.element_count: int | None = None
        self.min_quality: float | None = None
        self._phase = ''
        self._levels_target: RefineStep | None = None
        self._table = ''
        self._iteration_marks: list[float] = []
        self._previous_rate: float | None = None
        self._snap_iteration = -1
        self._layer_iteration = -1
        self._bump()

    def _bump(self) -> None:
        self.revision += 1

    @property
    def running(self) -> bool:
        return self.state == 'running'

    # ----------------------------------------------------------- job events
    def job_started(self, now: float) -> None:
        """A mesher job began: the panel shows it as running."""
        if self.state == 'done' and self.engine == 'snappy':
            # The next stage of the same pipeline: the timeline and the
            # charts of the stages before it stay, and this stage adds to
            # them. A stage that runs again replaces its own entries
            # (``_enter``).
            self.ended = None
            self._transient()
        elif self.state != 'running':
            self.reset()
        self.state = 'running'
        if self.started is None:
            self.started = now
        self._bump()

    def _transient(self) -> None:
        """Forget what only one snappyHexMesh process knows."""
        self._phase = ''
        self._levels_target = None
        self._table = ''
        self._iteration_marks = []
        self._snap_iteration = -1
        self._layer_iteration = -1

    def job_ended(self, now: float, outcome: str = 'done') -> None:
        """``outcome`` is ``done``, ``failed`` or ``cancelled``."""
        if self.state != 'running':
            return
        if outcome == 'done':
            self._close_phase(now)
        self.state = outcome
        self.ended = now
        for stage in self.stages:
            if stage.status == 'running':
                stage.status = 'done' if outcome == 'done' else outcome
                stage.ended = now
        if outcome == 'done' and self.engine == 'gmsh':
            self.fraction = 1.0
        self._bump()

    # ----------------------------------------------------------------- feed
    def feed(self, lines, now: float) -> bool:
        """Read ``lines``; True when a chart value changed."""
        before = self.revision
        if isinstance(lines, str):
            lines = lines.split('\n')
        for raw in lines:
            line = raw.rstrip('\r\n')
            if not line:
                continue
            if line.startswith(PROGRESS_PREFIX):
                self._gmsh(line, now)
            else:
                self._snappy(line, now)
        return self.revision != before

    # ---------------------------------------------------------------- gmsh
    def _gmsh(self, line: str, now: float) -> None:
        try:
            record = json.loads(line[len(PROGRESS_PREFIX):])
        except ValueError:
            return
        if not isinstance(record, dict):
            return
        if self.engine != 'gmsh':
            if self.engine == 'snappy' or self.state != 'running':
                self.reset()
            self.engine = 'gmsh'
            self.state = 'running'
            self.started = self.started if self.started is not None else now
        if record.get('kind') == 'started' and self.stages:
            started = self.started
            self.reset()
            self.engine = 'gmsh'
            self.state = 'running'
            self.started = started if started is not None else now
        stage_id = str(record.get('stage_id') or '')
        message = str(record.get('message') or '')
        try:
            fraction = float(record.get('fraction'))
        except (TypeError, ValueError):
            fraction = self.fraction
        kind = str(record.get('kind') or '')
        if kind == 'finished' or stage_id == 'gmsh':
            # The runner's own umbrella stage: not a row of the timeline.
            if kind == 'finished':
                for stage in self.stages:
                    if stage.status == 'running':
                        stage.status = 'done'
                        stage.ended = now
            self.fraction = max(self.fraction, min(1.0, fraction))
            self.message = message or self.message
            self._bump()
            return
        stage = self.stages[-1] if self.stages else None
        if stage is None or stage.key != stage_id:
            if stage is not None and stage.status == 'running':
                stage.status = 'done'
                stage.ended = now
            # A stage the runner comes back to (the mesh count is reported
            # after the optimiser) is the same row, running again.
            stage = next((item for item in self.stages
                          if item.key == stage_id), None)
            if stage is None:
                stage = Stage(stage_id,
                              humanise_option(stage_id),
                              'running', now)
                self.stages.append(stage)
            else:
                stage.status = 'running'
                stage.ended = None
        stage.iterations += 1
        details = record.get('details')
        if isinstance(details, dict):
            if 'cells' in details:
                try:
                    self.element_count = int(details['cells'])
                except (TypeError, ValueError):
                    pass
            if 'minQuality' in details:
                try:
                    self.min_quality = float(details['minQuality'])
                except (TypeError, ValueError):
                    pass
        self.fraction = max(self.fraction, min(1.0, fraction))
        self.message = message
        self._bump()

    # -------------------------------------------------------------- snappy
    def _stage(self, key: str) -> Stage:
        for stage in self.stages:
            if stage.key == key:
                return stage
        stage = Stage(key, dict(SNAPPY_STAGES)[key])
        # Keep the order of the run, not of arrival.
        order = [name for name, _ in SNAPPY_STAGES]
        self.stages.append(stage)
        self.stages.sort(key=lambda item: order.index(item.key))
        return stage

    def _enter(self, key: str, now: float) -> Stage:
        if self.engine != 'snappy':
            if self.engine == 'gmsh':
                self.reset()
            self.engine = 'snappy'
            if self.state != 'running':
                self.state = 'running'
            if self.started is None:
                self.started = now
        stage = self._stage(key)
        if stage.status == 'done' and self._phase != key:
            # The stage runs again: its old entries and everything after it
            # belong to the run that is being replaced.
            self._drop_from(key)
            stage = self._stage(key)
        if self._phase != key:
            self._close_phase(now)
            self._phase = key
            self._iteration_marks = []
        if stage.status == 'pending':
            stage.status = 'running'
            stage.started = now
            self._bump()
        return stage

    def _drop_from(self, key: str) -> None:
        order = [name for name, _ in SNAPPY_STAGES]
        first = order.index(key)
        self.stages = [item for item in self.stages
                       if order.index(item.key) < first]
        if first <= 0:
            self.refinement = []
        if first <= 1:
            self.snap = []
        self.extrusion = []
        self.layer_patches = []
        self.layer_cells_percent = None
        self.final_cells = None
        self._bump()

    def _close_phase(self, now: float) -> None:
        stage = next((item for item in self.stages
                      if item.key == self._phase), None)
        if stage is not None and stage.status == 'running':
            stage.status = 'done'
            stage.ended = now
            # The iteration in progress when the phase closes is complete.
            last = {'snap': self._snap_iteration,
                    'layers': self._layer_iteration}.get(stage.key, -1)
            stage.iterations = max(stage.iterations, last + 1)
            rate = self._phase_rate(stage, now)
            if rate is not None:
                self._previous_rate = rate
            self._bump()

    def _phase_rate(self, stage: Stage, now: float) -> float | None:
        """Seconds per iteration of ``stage`` so far."""
        if not stage.iterations:
            return None
        seconds = stage.seconds(now)
        if not seconds:
            return None
        return seconds / stage.iterations

    def _iteration_done(self, stage: Stage, now: float) -> None:
        stage.iterations += 1
        self._iteration_marks.append(now)

    def _snappy(self, line: str, now: float) -> None:
        if self._table:
            if self._layer_row(line):
                return
        # The Exec header: a new snappyHexMesh process begins, a new run.
        if line.startswith('Exec   : snappyHexMesh'):
            if self.engine == 'gmsh':
                self.reset()
            self.engine = 'snappy'
            self.state = 'running'
            self.ended = None
            if self.started is None:
                self.started = now
            self._transient()
            self._bump()
            return
        head = line[:1]
        if head in (' ', '\t'):
            match = _LEVEL_ROW.match(line)
            if match and self._levels_target is not None:
                self._levels_target.levels[int(match[1])] = int(match[2])
                self._bump()
            return
        if head == 'I':
            if line.startswith('Initial mesh :'):
                self._refine_row('start', line)
            return
        if head == 'A':
            match = _AFTER_REFINEMENT.match(line)
            if match:
                stage = self._enter('castellated', now)
                self._refine_row(
                    f"{match['what']} {int(match['n']) + 1}", line)
                self._iteration_done(stage, now)
                self._bump()
                return
            if line.startswith(('After balancing', 'After introducing')):
                self._levels_target = None
                return
            match = _ADDED_CELLS.match(line)
            if match and self._phase == 'layers':
                self.layer_cells_percent = float(match[3])
                self._bump()
            return
        if head == 'S':
            if line.startswith(('Surface refinement iteration',
                                'Shell refinement iteration',
                                'Selecting all cells')):
                self._enter('castellated', now)
            elif line.startswith('Snapping to features'):
                stage = self._enter('snap', now)
                match = _SNAP_TOTAL.match(line)
                if match:
                    stage.total = int(match[1])
                    self._bump()
            elif line.startswith('Smoothing patch points'):
                self._enter('snap', now)
            elif line.startswith('Snapped mesh :'):
                self._enter('snap', now)
                self._levels_target = None
                self._finish_phase(now)
            elif line.startswith('Shrinking and layer addition phase'):
                self._enter('layers', now)
            return
        if head == 'M':
            match = _MORPH.match(line)
            if match:
                stage = self._enter('snap', now)
                if self._snap_iteration >= 0:
                    self._iteration_done(stage, now)
                self._snap_iteration = int(match[1])
                self._bump()
            return
        if head == 'W':
            match = _DISPLACEMENT.match(line)
            if match and self._phase == 'snap':
                try:
                    step = SnapStep(self._snap_iteration, float(match[1]),
                                    float(match[2]), float(match[3]))
                except ValueError:
                    return
                self.snap.append(step)
                self._bump()
            return
        if head == 'R':
            if line.startswith('Refined mesh :'):
                self._enter('castellated', now)
                self._refine_row('final', line)
                self._finish_phase(now)
            return
        if head == 'L':
            match = _LAYER_ITERATION.match(line)
            if match:
                stage = self._enter('layers', now)
                if self._layer_iteration >= 0:
                    self._iteration_done(stage, now)
                self._layer_iteration = int(match[1])
                self._bump()
            elif line.startswith('Layer mesh :'):
                self._enter('layers', now)
                cells = _CELLS.search(line)
                if cells:
                    self.final_cells = int(cells[1])
                self._finish_phase(now)
            return
        if head == 'N':
            if line.startswith('No layers to generate'):
                self._enter('layers', now)
            return
        if head == 'E':
            match = _EXTRUDING.match(line)
            if match and self._phase == 'layers':
                self.extrusion.append(
                    (max(self._layer_iteration, 0), float(match[3])))
                self._bump()
            return
        if head == 'p' and line.startswith('patch'):
            # Two tables open this way. The one that says "overall thickness"
            # is the achieved one; the "avg thickness" one is the target.
            self._table = 'header' if 'overall thickness' in line else ''

    def _finish_phase(self, now: float) -> None:
        self._close_phase(now)
        self._phase = ''

    def _layer_row(self, line: str) -> bool:
        """Read the achieved-thickness table; True when the line was part."""
        if self._table == 'header':
            if line.startswith('-----'):
                self._table = 'rows'
            return True
        match = _LAYER_ROW.match(line)
        if match is None:
            self._table = ''
            return False
        row = LayerPatch(match['patch'], int(match['faces']),
                         float(match['layers']), float(match['thickness']),
                         float(match['percent']))
        for index, existing in enumerate(self.layer_patches):
            if existing.patch == row.patch:
                self.layer_patches[index] = row
                break
        else:
            self.layer_patches.append(row)
        self._bump()
        return True

    def _refine_row(self, label: str, line: str) -> None:
        cells = _CELLS.search(line)
        if cells is None:
            self._levels_target = None
            return
        if label == 'start' and self.refinement:
            # The mesh this stage starts from is the one the stage before it
            # ended on, which the chart already shows.
            self._levels_target = None
            return
        step = RefineStep(label, int(cells[1]), int(cells[2]), int(cells[3]))
        if label == 'final' and self.refinement \
                and self.refinement[-1].cells == step.cells:
            # The last iteration already says it; keep one bar for it.
            self._levels_target = self.refinement[-1]
            return
        self.refinement.append(step)
        self._levels_target = step
        self._bump()

    # -------------------------------------------------------------- readout
    def elapsed(self, now: float) -> float:
        if self.started is None:
            return 0.0
        return max(0.0, (self.ended if self.ended is not None else now)
                   - self.started)

    def eta(self, now: float) -> float | None:
        """Seconds left in the running phase, or ``None`` when unknown.

        snappy: the iterations left times the seconds one takes -- this
        phase's mean once it has finished one, the previous phase's before
        that. Only a phase whose total the log states (the snap) has an ETA.
        Gmsh: the elapsed time scaled by the fraction left.
        """
        if self.state != 'running':
            return None
        if self.engine == 'gmsh':
            if self.fraction < 0.05 or self.fraction >= 1.0:
                return None
            return self.elapsed(now) * (1.0 - self.fraction) / self.fraction
        stage = next((item for item in self.stages
                      if item.status == 'running'), None)
        if stage is None or not stage.total:
            return None
        done = len(self.snap) if stage.key == 'snap' else stage.iterations
        left = max(0, stage.total - done)
        rate = None
        if len(self._iteration_marks) >= 2:
            rate = ((self._iteration_marks[-1] - self._iteration_marks[0])
                    / (len(self._iteration_marks) - 1))
        elif stage.iterations:
            rate = self._phase_rate(stage, now)
        if rate is None:
            rate = self._previous_rate
        if rate is None:
            return None
        return left * rate

    def current_cells(self) -> int | None:
        """The cell count the log last stated, for either engine."""
        if self.engine == 'gmsh':
            return self.element_count
        if self.final_cells is not None:
            return self.final_cells
        return self.refinement[-1].cells if self.refinement else None

    def summary(self, now: float) -> str:
        """The same numbers in words, for the accessible description."""
        parts = []
        for stage in self.stages:
            parts.append(f'{stage.label} {stage.status}')
        if self.refinement:
            parts.append(f'{self.refinement[-1].cells} cells after '
                         f'{len(self.refinement)} refinement steps')
        if self.snap:
            parts.append(f'snap iteration {self.snap[-1].iteration}, largest '
                         f'displacement {self.snap[-1].maximum:g}')
        for patch in self.layer_patches:
            parts.append(f'{patch.patch} layers reached {patch.percent:g} '
                         f'percent of the wanted thickness')
        if self.engine == 'gmsh':
            parts.append(f'{round(self.fraction * 100)} percent')
            if self.element_count is not None:
                parts.append(f'{self.element_count} elements')
        return '; '.join(parts)
