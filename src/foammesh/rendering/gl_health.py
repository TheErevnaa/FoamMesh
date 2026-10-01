"""Plan 35 CR8: what the OpenGL driver says about itself, and how much to ask of it.

* The GL strings (vendor, renderer, version) a render window reports once it
  has a context: logged, kept for the crash bundle (``gl_info.json`` in the
  logs folder, which the bundle takes whole), and judged -- Windows' "GDI
  Generic" software renderer or OpenGL older than 3.2 cannot draw this
  viewport reliably, and the start after one is a safe-mode start.
* ``glGetError`` straight from ``opengl32.dll``: VTK's Python wrapping has no
  way to ask, and a context the driver reset reports GL_CONTEXT_LOST there.
* The face budget: depth peeling redraws the scene once per peel, so above
  the GPU's peeling budget (``gpu_profile``: a million faces on an integrated
  GPU or before the GPU is known, scaled with a discrete card's memory)
  translucency is blended in draw order instead.

No Qt here; the rendering widget owns the reactions.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

GL_INFO_FILE = 'gl_info.json'
#: The oldest OpenGL VTK's OpenGL2 backend is written for.
MINIMUM_VERSION = (3, 2)
#: Visible faces above which translucent parts are no longer depth-peeled,
#: while the GPU is not known; :func:`face_budget` scales it to the GPU.
FACE_BUDGET = 1_000_000

GL_OUT_OF_MEMORY = 0x0505
GL_CONTEXT_LOST = 0x0507
#: glGetError values that mean the context cannot be trusted to draw again.
LOST_ERRORS = {GL_CONTEXT_LOST: 'GL_CONTEXT_LOST',
               GL_OUT_OF_MEMORY: 'GL_OUT_OF_MEMORY'}

PEELING_NOTE = ('Large scene ({0:,} faces): see-through parts are drawn '
                'without depth peeling to keep the view responsive.')

_recorded: dict = {}


# --------------------------------------------------------------------------
# The GL strings
# --------------------------------------------------------------------------

def gl_strings(window) -> dict:
    """``{'vendor', 'renderer', 'version'}`` from a window that has a context.

    ``vtkOpenGLState`` caches the strings when the context is made; the
    capability report is the fallback. Empty when there is no context yet --
    asking a window that never rendered would create one.
    """
    info = {}
    try:
        if not window.GetInitialized():
            return {}
    except Exception:                                      # noqa: BLE001
        return {}
    try:
        state = window.GetState()
        info = {'vendor': state.GetVendor() or '',
                'renderer': state.GetRenderer() or '',
                'version': state.GetVersion() or ''}
    except Exception:                                      # noqa: BLE001
        info = {}
    if not any(info.values()):
        try:
            info = parse_capabilities(window.ReportCapabilities() or '')
        except Exception:                                  # noqa: BLE001
            info = {}
    return {key: value for key, value in info.items() if value}


def parse_capabilities(report: str) -> dict:
    info = {}
    for line in str(report or '').splitlines():
        key, _, value = line.partition(':')
        key = key.strip().lower()
        for name in ('vendor', 'renderer', 'version'):
            if key == f'opengl {name} string':
                info[name] = value.strip()
    return info


def parse_version(text) -> tuple[int, int] | None:
    match = re.match(r'\s*(?:OpenGL(?: ES)?\s*)?(\d+)\.(\d+)', str(text or ''))
    return (int(match.group(1)), int(match.group(2))) if match else None


def weak_reason(info: dict) -> str | None:
    """Why this driver should not draw the full viewport, or None."""
    renderer = str(info.get('renderer') or '')
    if 'gdi generic' in renderer.lower():
        return ('Windows is drawing with its basic "GDI Generic" OpenGL, which '
                'means no graphics driver is installed for this display.')
    version = parse_version(info.get('version'))
    if version is not None and version < MINIMUM_VERSION:
        return ('The graphics driver offers OpenGL {0}.{1}; the viewport needs '
                '{2}.{3} or newer.'.format(*version, *MINIMUM_VERSION))
    return None


def record(info: dict, log_directory=None) -> None:
    """Log the strings once per change and keep them for the crash bundle."""
    global _recorded
    info = {key: str(value) for key, value in (info or {}).items() if value}
    if not info or info == _recorded:
        return
    _recorded = dict(info)
    logger.info('OpenGL vendor: %s; renderer: %s; version: %s',
                info.get('vendor', 'unknown'), info.get('renderer', 'unknown'),
                info.get('version', 'unknown'))
    if log_directory is None:
        try:
            from foammesh.support import lifecycle
            log_directory = lifecycle.log_directory()
        except Exception:                                  # noqa: BLE001
            log_directory = None
    if log_directory is None:
        return
    try:
        (Path(log_directory) / GL_INFO_FILE).write_text(
            json.dumps(info, indent=1), encoding='utf-8')
    except OSError:
        pass


def recorded(log_directory=None) -> dict:
    """The GL strings this process recorded, else the last ones on disk."""
    if _recorded:
        return dict(_recorded)
    if log_directory is None:
        return {}
    try:
        info = json.loads((Path(log_directory) / GL_INFO_FILE).read_text(
            encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def reset() -> None:
    """Tests only."""
    global _recorded
    _recorded = {}


# --------------------------------------------------------------------------
# Context state
# --------------------------------------------------------------------------

def gl_error() -> int:
    """``glGetError()`` on the calling thread's current context; 0 elsewhere."""
    if sys.platform != 'win32':
        return 0
    try:
        import ctypes
        return int(ctypes.windll.opengl32.glGetError()) & 0xFFFF
    except Exception:                                      # noqa: BLE001
        return 0


def lost_context(window, was_initialized: bool = True) -> str | None:
    """Why ``window`` can no longer draw after a render, or None.

    A finalised window (``GetInitialized`` false although it had a context
    before the render -- ``was_initialized``), or a current context whose
    error flag says it was lost or ran out of memory. The error is read
    only while this window's context is current, since with no context
    bound ``glGetError`` answers GL_INVALID_OPERATION.
    """
    try:
        initialized = window.GetInitialized()
    except Exception:                                      # noqa: BLE001
        return None
    if not initialized:
        return ('the render window lost its OpenGL context'
                if was_initialized else None)
    try:
        current = window.IsCurrent()
    except Exception:                                      # noqa: BLE001
        current = False
    if current:
        error = gl_error()
        if error in LOST_ERRORS:
            return f'OpenGL reported {LOST_ERRORS[error]}'
    return None


# --------------------------------------------------------------------------
# The face budget
# --------------------------------------------------------------------------

def visible_faces(renderer) -> int:
    """Cells drawn by the visible actors of ``renderer`` (inputs as built)."""
    total = 0
    try:
        actors = renderer.GetActors()
        actors.InitTraversal()
        actor = actors.GetNextActor()
    except Exception:                                      # noqa: BLE001
        return 0
    while actor is not None:
        try:
            if actor.GetVisibility():
                mapper = actor.GetMapper()
                data = mapper.GetInput() if mapper is not None else None
                if data is not None:
                    total += int(data.GetNumberOfCells())
        except Exception:                                  # noqa: BLE001
            pass
        actor = actors.GetNextActor()
    return total


def face_budget() -> int:
    """Visible faces the GPU in use depth-peels (``gpu_profile``)."""
    try:
        from foammesh.rendering import gpu_profile
        return int(gpu_profile.display_budget().peeling_faces)
    except Exception:                                      # noqa: BLE001
        return FACE_BUDGET


def peeling_decision(translucent: bool, faces: int, preset,
                     budget: int | None = None) -> tuple[bool, str]:
    """``(peel, note)``: whether to depth-peel, and what to tell the user."""
    from foammesh.rendering import render_style

    if not translucent or not render_style.quality(preset).peeling:
        return False, ''
    if faces > (face_budget() if budget is None else int(budget)):
        return False, PEELING_NOTE.format(int(faces))
    return True, ''
