"""Gmsh's keyboard shortcuts, one row each, and what the Gmsh route does with it.

Plan 41 GN18 (I-05). The list is the one Gmsh 4.15.2 prints for
``Help > Keyboard and Mouse Usage`` (``doc/texinfo/shortcuts.texi`` of the
source the size-field tool is built from, read on 2026-10-10).  Every key of it
is a row here, either bound to what the same act is on this route, or left
unbound with the reason, so a key is never missing by omission:

* ``generate`` -- runs a Generate action on the resident session;
* ``tier`` -- opens a row's page and unfolds the tier that holds the control;
* ``page`` -- opens the row;
* ``quality`` -- raises the Mesh quality tab (the statistics are its items);
* ``view`` -- turns the viewport to a standard direction;
* a reason string -- the key is not bound, and why.

Reasons come in three kinds.  *taken*: the window already owns the key (the
rule is that a Gmsh key never shadows one of ours, and a test checks it
against every action the window has).  *typed*: a bare letter or digit whose
Gmsh meaning is a mode FoamMesh does not have, which would only steal a
character from a text box.  *absent*: the thing it toggles does not exist here
(post-processing views, the window manager's own windows).
"""
from __future__ import annotations

GENERATE = 'generate'
TIER = 'tier'
PAGE = 'page'
VIEW = 'view'
QUALITY = 'quality'
COLOUR = 'colour'

_GEN = 'gmsh.compute'
_GEO = 'gmsh.geometry'
_OPT = 'gmshOptionsHeader'
#: The Display popover of the side rail (Plan 43 LY13 D11); the name is the
#: tier header it replaced, which a page without a window still has.
DISPLAY = 'gmshDisplayHeader'
_DISP = DISPLAY
_NO_POST = 'absent: no post-processing views'
_NO_MODE = 'typed: no creation mode, points are typed'

#: ``(key, what Gmsh does, binding)``; *binding* is a tuple for a bound key
#: and a reason string for an unbound one.
KEYS = (
    ('Left', 'Previous time step', 'absent: there are no time steps'),
    ('Right', 'Next time step', 'absent: there are no time steps'),
    ('Up', 'Make previous view visible', _NO_POST),
    ('Down', 'Make next view visible', _NO_POST),
    ('0', 'Reload geometry', 'typed: a digit goes to a focused field'),
    ('Ctrl+0', 'Reload full project', 'taken: the window reopens projects'),
    ('9', 'Reload full project', 'typed: a digit goes to a focused field'),
    ('1', 'Mesh lines', (GENERATE, 'generate1d')),
    ('F1', 'Mesh lines', 'taken: F1 is the viewport cheat sheet'),
    ('2', 'Mesh surfaces', (GENERATE, 'generate2d')),
    ('F2', 'Mesh surfaces', (GENERATE, 'generate2d')),
    ('3', 'Mesh volumes', (GENERATE, 'generate3d')),
    ('F3', 'Mesh volumes', (GENERATE, 'generate3d')),
    ('Escape', 'Cancel selection',
     'taken: Escape leaves focus mode and closes an open editor; '
     'while a Generate run is going it aborts the run (Plan 41 I-06)'),
    ('e', 'Accept selection in geometry creation mode',
     'typed: Enter accepts a pick here'),
    ('g', 'Go to geometry module', 'typed: the rows are the modules'),
    ('m', 'Go to mesh module', 'typed: the rows are the modules'),
    ('p', 'Go to post-processing module', _NO_POST),
    ('q', 'Abort selection', 'typed: Escape aborts a pick here'),
    ('s', 'Go to solver module', 'absent: no solver module'),
    ('x', 'Freeze x in geometry creation mode', _NO_MODE),
    ('y', 'Freeze y in geometry creation mode', _NO_MODE),
    ('z', 'Freeze z in geometry creation mode', _NO_MODE),
    ('Shift+A', 'Bring all windows to front', 'absent: one window'),
    ('Shift+G', 'Show geometry options', (TIER, _GEO, _OPT)),
    ('Shift+M', 'Show mesh options', (TIER, 'gmsh.global_sizing', _OPT)),
    ('Shift+O', 'Show general options', (TIER, _GEN, _OPT)),
    ('Shift+P', 'Show post-processing options', _NO_POST),
    ('Shift+S', 'Show solver options', 'absent: no solver module'),
    ('Shift+U', 'Show post-processing view plugins',
     (TIER, _GEN, 'gmshPluginsHeader')),
    ('Shift+W', 'Show post-processing view options', _NO_POST),
    ('Shift+X', 'Move only along x', _NO_MODE),
    ('Shift+Y', 'Move only along y', _NO_MODE),
    ('Shift+Z', 'Move only along z', _NO_MODE),
    ('Shift+Escape', 'Enable full mouse selection',
     'taken: Escape is the window\'s'),
    ('Ctrl+D', 'Attach/detach menu', 'absent: no floating menu'),
    ('Ctrl+E', 'Export project',
     'taken: Ctrl+E closes the project; Export is a row of the outline'),
    ('Ctrl+F', 'Enter full screen',
     'absent: F11 is focus mode, and Ctrl+F searches the console'),
    ('Ctrl+I', 'Show statistics window', (QUALITY,)),
    ('Ctrl+J', 'Save model options', 'taken: Ctrl+J is the output band'),
    ('Ctrl+L', 'Show message console',
     'absent: Ctrl+J opens the output band, Ctrl+Shift+C the console'),
    ('Ctrl+M', 'Minimise window', 'absent: the window manager\'s'),
    ('Ctrl+N', 'Create new project file', 'taken: Ctrl+N is New project'),
    ('Ctrl+O', 'Open project file', 'taken: Ctrl+O is Open project'),
    ('Ctrl+Q', 'Quit', 'taken: Ctrl+Q is Exit'),
    ('Ctrl+R', 'Rename project file',
     'absent: Save project as (Ctrl+Shift+S) renames the project'),
    ('Ctrl+S', 'Save mesh in default format',
     'taken: Ctrl+S saves the project; the mesh is written on the Export row'),
    ('Shift+Ctrl+C', 'Show clipping plane window',
     'taken: Ctrl+Shift+C is the console'),
    ('Shift+Ctrl+H', 'Show current options', (TIER, _GEN, _OPT)),
    ('Shift+Ctrl+J', 'Save options as default',
     'absent: options are kept with the project'),
    ('Shift+Ctrl+M', 'Show manipulator window', 'absent: no manipulator'),
    ('Shift+Ctrl+N', 'Show option window', (TIER, _GEN, _OPT)),
    ('Shift+Ctrl+O', 'Merge files', (TIER, _GEO, 'gmshImportHeader')),
    ('Shift+Ctrl+R', 'Open next-to-last opened file',
     'absent: no recent-file list on the Gmsh route'),
    ('Shift+Ctrl+U', 'Show plugin window', (TIER, _GEN, 'gmshPluginsHeader')),
    ('Shift+Ctrl+V', 'Show visibility window', (TIER, _GEN, _DISP)),
    ('Alt+A', 'Loop through axes modes',
     'absent: the axes are the corner triad and the Origin button'),
    ('Alt+B', 'Hide/show bounding boxes', 'absent: no bounding boxes'),
    ('Alt+C', 'Loop through colour schemes',
     'absent: the theme is chosen in Preferences'),
    ('Alt+E', 'Hide/show element outlines of views', _NO_POST),
    ('Alt+F', 'Change redraw mode', 'absent: the viewport is GPU-drawn'),
    ('Alt+H', 'Hide/show post-processing views', _NO_POST),
    ('Alt+I', 'Hide/show view scales', _NO_POST),
    ('Alt+L', 'Hide/show geometry lines', (TIER, _GEN, _DISP)),
    ('Alt+M', 'Toggle visibility of all mesh entities', (TIER, _GEN, _DISP)),
    ('Alt+N', 'Hide/show view annotations', _NO_POST),
    ('Alt+O', 'Change projection mode', (TIER, _GEN, _DISP)),
    ('Alt+P', 'Hide/show geometry points', (TIER, _GEN, _DISP)),
    ('Alt+R', 'Loop through range modes', _NO_POST),
    ('Alt+S', 'Hide/show geometry surfaces', (TIER, _GEN, _DISP)),
    ('Alt+T', 'Loop through interval modes', _NO_POST),
    ('Alt+V', 'Hide/show geometry volumes', (TIER, _GEN, _DISP)),
    ('Alt+W', 'Enable/disable all lighting', (TIER, _GEN, _DISP)),
    ('Alt+X', 'Set X view', (VIEW, '+x')),
    ('Alt+Y', 'Set Y view', (VIEW, '+y')),
    ('Alt+Z', 'Set Z view', (VIEW, '+z')),
    ('Alt+1', 'Set 1:1 view', 'taken: Alt+1 is View +X'),
    ('Alt+Shift+A', 'Hide/show small axes', (TIER, _GEN, _DISP)),
    ('Alt+Shift+B', 'Hide/show mesh volume faces', (TIER, _GEN, _DISP)),
    ('Alt+Shift+C', 'Loop through colour maps', (COLOUR, 'next')),
    ('Alt+Shift+D', 'Hide/show mesh surface faces', (TIER, _GEN, _DISP)),
    ('Alt+Shift+L', 'Hide/show mesh lines', (TIER, _GEN, _DISP)),
    ('Alt+Shift+P', 'Hide/show mesh nodes', (TIER, _GEN, _DISP)),
    ('Alt+Shift+S', 'Hide/show mesh surface edges', (TIER, _GEN, _DISP)),
    ('Alt+Shift+T', 'Interval modes with numeric', _NO_POST),
    ('Alt+Shift+V', 'Hide/show mesh volume edges', (TIER, _GEN, _DISP)),
    ('Alt+Shift+X', 'Set -X view', (VIEW, '-x')),
    ('Alt+Shift+Y', 'Set -Y view', (VIEW, '-y')),
    ('Alt+Shift+Z', 'Set -Z view', (VIEW, '-z')),
    ('Alt+Shift+1', 'Reset bounding box around visible entities',
     'absent: no bounding boxes'),
    ('Alt+Ctrl++1', 'Sync scale between viewports', 'absent: one viewport'),
)


def bound() -> dict:
    """``{key: binding}`` for the keys that do something."""
    return {key: binding for key, _what, binding in KEYS
            if isinstance(binding, tuple)}


def unbound() -> dict:
    """``{key: reason}`` for the keys left alone."""
    return {key: binding for key, _what, binding in KEYS
            if not isinstance(binding, tuple)}
