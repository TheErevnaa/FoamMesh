"""Settings > Set up FoamMesh's Gmsh (Plan 48 GM11b; DP-2903).

FoamMesh's own Gmsh 4.15.2 build (Revoropt, CVTRemesh) lives in the OpenFOAM
13 runtime.  Until it is set up, the official wheel answers and says so.  The
person gets one action that shows where the build is and sets it up (or
switches it off); the local API has the same three operations
(``gmsh.build.status``, ``setup``, ``disable``).  The work is
:mod:`foammesh.core.gmsh.runtime`; this module only calls it and words the
answer, so the dialog and the API cannot differ.
"""
from __future__ import annotations

ACTION_ID = 'gmsh.build.setup'
MENU_ID = 'gmsh.build.setup.menu'


def profile():
    """The configured Gmsh launch profile, or ``None`` (no runtime)."""
    from foammesh.core.gmsh.launch_profiles import configured_profiles

    found = configured_profiles()
    return found[0] if found else None


def status(chosen=None, runner=None) -> dict:
    """``build_status`` with the shipped sha known; ``state`` is ``none`` when
    no runtime is configured."""
    from foammesh.core.gmsh import runtime

    chosen = chosen or profile()
    if chosen is None:
        return {'state': 'none', 'in_use': False, 'message': (
            'No Gmsh runtime is configured; Gmsh runs in a WSL distribution '
            'and this host has none.')}
    try:
        path = runtime.bundle_path()
        sha = runtime._file_sha256(path) if path.is_file() else ''
    except Exception:                                 # noqa: BLE001
        sha = ''
    return runtime.build_status(chosen, runner=runner,
                                expected_bundle_sha=sha or None)


def can_set_up(found: dict) -> bool:
    return found.get('state') in ('missing', 'stale', 'not_in_use')


def set_up(chosen=None, runner=None) -> dict:
    """Copy the shipped build in and use it; ``{'message'}`` or raises
    ``ValueError`` with the reason."""
    from foammesh.core.gmsh import runtime

    chosen = chosen or profile()
    if chosen is None:
        raise ValueError('No Gmsh runtime is configured')
    try:
        found = runtime.install_build(chosen, runner=runner)
    except runtime.GmshBuildSetupError as error:
        raise ValueError(str(error)) from error
    found['message'] = (f"{runtime.BUILD_ID} is in {found.get('distribution')}"
                        f" at {found.get('path')}.")
    return found


def switch_off(chosen=None, runner=None) -> dict:
    from foammesh.core.gmsh import runtime

    chosen = chosen or profile()
    if chosen is None:
        raise ValueError('No Gmsh runtime is configured')
    try:
        return runtime.disable_build(chosen, runner=runner)
    except runtime.GmshBuildSetupError as error:
        raise ValueError(str(error)) from error


def run_dialog(window):
    """The action: show the state, offer Set up (a background job) or Stop.

    Returns the dialog (shown without blocking); its ``look`` and ``act``
    attributes are the two jobs, for a test to drive.
    """
    import threading

    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import (
        QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)

    box = QDialog(window)
    box.setObjectName('gmshBuildSetupDialog')
    box.setWindowTitle(box.tr("FoamMesh's Gmsh"))
    layout = QVBoxLayout(box)
    text = QLabel(box.tr('Looking for the build…'), box)
    text.setObjectName('gmshBuildSetupText')
    text.setWordWrap(True)
    layout.addWidget(text)
    row = QHBoxLayout()
    setup = QPushButton(box.tr('Set up'), box)
    setup.setObjectName('gmshBuildSetupGo')
    stop = QPushButton(box.tr('Stop using it'), box)
    stop.setObjectName('gmshBuildSetupStop')
    close = QPushButton(box.tr('Close'), box)
    for button in (setup, stop, close):
        row.addWidget(button)
    layout.addLayout(row)
    setup.setEnabled(False)
    stop.setEnabled(False)
    close.clicked.connect(box.close)

    class _Bridge(QObject):
        said = Signal(dict)

    bridge = _Bridge(box)

    def show(found):
        text.setText('%s\n\n%s' % (found.get('message') or '',
                                    box.tr('State: %s.')
                                    % found.get('state', '')))
        setup.setEnabled(can_set_up(found))
        stop.setEnabled(bool(found.get('in_use')))

    bridge.said.connect(show)

    def look():
        try:
            found = status()
        except Exception as error:                    # noqa: BLE001
            found = {'state': 'unreachable', 'message': str(error)}
        bridge.said.emit(found)

    def act(function):
        def run():
            try:
                found = function()
                if not found.get('message'):
                    found = status()
            except ValueError as error:
                found = {'state': 'failed', 'message': str(error)}
            bridge.said.emit(found)
        setup.setEnabled(False)
        stop.setEnabled(False)
        text.setText(box.tr('Working…'))
        threading.Thread(target=run, daemon=True).start()

    setup.clicked.connect(lambda: act(set_up))
    stop.clicked.connect(lambda: act(switch_off))
    box.look = lambda: threading.Thread(target=look, daemon=True).start()
    box.act = act
    box.look()
    box.show()
    return box
