#!/usr/bin/env python
# -*- coding: utf-8 -*-

from enum import Enum
import os
import re
from pathlib import Path
from datetime import datetime, timezone

import yaml
from PySide6.QtCore import QLocale, QRect



FORMAT_VERSION = 1
RECENT_PROJECTS_NUMBER = 50


class SettingKey(Enum):
    FORMAT_VERSION = 'format_version'
    SCALE = 'display_scale'
    LOCALE = 'default_language'
    RECENT_DIRECTORY = 'recent_directory'
    RECENT_CASES = 'recent_cases'
    RECENT_IMPORT_DIRECTORY = 'recent_import_directory'
    RECENT_IMPORT_UNIT = 'recent_import_unit'
    LAST_START_WINDOW_GEOMETRY = 'LAST_START_WINDOW_GEOMETRY'
    LAST_MAIN_WINDOW_GEOMETRY = 'LAST_MAIN_WINDOW_GEOMETRY'
    THEME = 'theme'
    OPENFOAM_PROFILE_ID = 'openfoam_profile_id'
    OPENFOAM_WSL_DISTRO = 'openfoam_wsl_distro'
    OPENFOAM_WSL_USER = 'openfoam_wsl_user'
    OPENFOAM_BASHRC = 'openfoam_bashrc'
    OPENFOAM_STAGE_TIMEOUT = 'openfoam_stage_timeout'
    THREE_REGION_LAYOUT = 'three_region_layout'
    # How surface diagnostics behave when a check overruns. See
    # foammesh.core.geometry.diagnostics.budget.
    DIAGNOSTIC_BUDGET_POLICY = 'diagnostic_budget_policy'
    DIAGNOSTIC_BUDGET_MAX_SECONDS = 'diagnostic_budget_max_seconds'
    # Whether geometry-fidelity and resolution verdicts gate anything, or only
    # report. See foammesh.core.quality.qualification.
    GEOMETRY_QUALIFICATION_MODE = 'geometry_qualification_mode'
    # DP-710. How interior mesh lines are drawn in the viewport.
    MESH_LINE_STYLE = 'mesh_line_style'
    # DP-701. The viewport gradient colours the user picked, if any.
    VIEWPORT_BACKGROUND = 'viewport_background'
    # DP-726. The viewport's Render quality preset.
    RENDER_QUALITY = 'render_quality'


#: The store every surface shares, as the GUI bootstrap names it.
#:
#: Only ``app.py`` used to call :meth:`AppSettings.load`, so a CLI, API or
#: headless process read an unloaded store and every ``_get`` raised. Callers
#: that wrapped the read in ``except Exception`` -- the diagnostic budget, and
#: then the qualification mode -- silently got their defaults instead of the
#: user's settings, which made those preferences GUI-only in practice and
#: quietly violated the parity rule they were created to satisfy.
DEFAULT_APP_NAME = 'FoamMesh'


class AppSettings:
    def __init__(self):
        self._settings = None

        self._settingsPath = None
        self._settingsFile = None
        # self._lockFile = None
        self._lock = None

    def _ensureLoaded(self):
        """Load the shared store on first access outside the GUI bootstrap."""
        if self._settings is None:
            self.load(DEFAULT_APP_NAME)

    def load(self, name=DEFAULT_APP_NAME):
        self._settingsPath = Path.home() / f'.{name}'
        self._settingsFile = self._settingsPath / 'foammesh.settings.yaml'

        if self._settingsFile.is_file():
            with open(self._settingsFile) as file:
                self._settings = yaml.load(file, Loader=yaml.FullLoader)
        else:
            self._settingsPath.mkdir(exist_ok=True)
            self._settings = {SettingKey.FORMAT_VERSION.value: FORMAT_VERSION}

    def settingsPath(self) -> Path:
        assert self._settingsPath is not None, 'AppSettings.load() must be called first'
        return self._settingsPath

    def getRecentLocation(self):
        return self._get(SettingKey.RECENT_DIRECTORY, str(Path.home()))

    def getRecentProjects(self):
        return [item['path'] for item in self.getRecentCases()]

    def getRecentCases(self):
        """Return validated structured recent-project records."""
        records = []
        for item in self._get(SettingKey.RECENT_CASES, []):
            if isinstance(item, dict) and isinstance(item.get('path'), str):
                path = item['path']
                opened = item.get('last_opened')
            else:
                continue
            records.append({
                'path': path,
                'display_name': str(item.get('display_name') or '')
                                or Path(path).name or path,
                'last_opened': opened,
            })
        return records[:RECENT_PROJECTS_NUMBER]

    def updateRecents(self, path, new=False):
        path = Path(path).expanduser().resolve()
        if new:
            self._settings[SettingKey.RECENT_DIRECTORY.value] = str(path.parent)

        p = str(path)
        recentCases = [item for item in self.getRecentCases()
                       if str(Path(item['path']).expanduser().resolve()).casefold() != p.casefold()]
        recentCases.insert(0, {
            'path': p,
            'display_name': path.name or p,
            'last_opened': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        })
        self._settings[SettingKey.RECENT_CASES.value] = recentCases[:RECENT_PROJECTS_NUMBER]
        self._save()

    def clearRecents(self):
        self._settings[SettingKey.RECENT_CASES.value] = []
        self._save()

    def removeRecent(self, path):
        target = str(Path(path).expanduser().resolve()).casefold()
        self._settings[SettingKey.RECENT_CASES.value] = [
            item for item in self.getRecentCases()
            if str(Path(item['path']).expanduser().resolve()).casefold() != target]
        self._save()

    def getRecentImportDirectory(self):
        return self._get(SettingKey.RECENT_IMPORT_DIRECTORY, str(Path.home()))

    def updateRecentImportDirectory(self, path):
        self._set(SettingKey.RECENT_IMPORT_DIRECTORY, str(path))

    def getRecentImportUnit(self):
        """Default import unit. Millimetres, because most CAD exports are.

        The suggestion heuristic overrides this as soon as a file is chosen;
        this is only what the combo reads before there is anything to measure.
        """
        return self._get(SettingKey.RECENT_IMPORT_UNIT, 'mm')

    def updateRecentImportUnit(self, unit):
        self._set(SettingKey.RECENT_IMPORT_UNIT, str(unit))

    def getLastStartWindowGeometry(self) -> QRect:
        x, y, width, height = self._get(SettingKey.LAST_START_WINDOW_GEOMETRY, [200, 100, 400, 300])
        return QRect(x, y, width, height)

    def updateLastStartWindowGeometry(self, geometry: QRect):
        self._set(SettingKey.LAST_START_WINDOW_GEOMETRY, [geometry.x(), geometry.y(), geometry.width(), geometry.height()])

    def getLastMainWindowGeometry(self) -> QRect:
        x, y, width, height = self._get(SettingKey.LAST_MAIN_WINDOW_GEOMETRY, [200, 100, 1280, 770])
        return QRect(x, y, width, height)

    def updateLastMainWindowGeometry(self, geometry: QRect):
        self._set(SettingKey.LAST_MAIN_WINDOW_GEOMETRY, [geometry.x(), geometry.y(), geometry.width(), geometry.height()]
                  )

    def getThreeRegionLayout(self):
        """Return only a valid current-schema three-region layout record.

        FoamMesh is a new-build application.  Obsolete sidebar and dock keys
        are deliberately not interpreted or migrated.

        DP-134.  Schema 1 wrote the splitter's sizes on every close, whether or
        not anyone had touched the handle, so a width the clamp merely happened
        to produce came back looking like a width someone had chosen -- and,
        being honoured, could never widen again.  Schema 2 records sizes only
        for a handle that was actually dragged.  A schema 1 record is therefore
        dropped rather than migrated: its sizes carry no intent to preserve.
        """
        value = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        if not isinstance(value, dict) or value.get('version') != 2:
            return {'version': 2, 'sizes': None, 'active_output': 'mesh'}
        sizes = value.get('sizes')
        if (not isinstance(sizes, (list, tuple)) or len(sizes) != 3
                or any(not isinstance(item, int) or item < 0
                       for item in sizes)):
            sizes = None
        active_output = str(value.get('active_output') or 'mesh')
        return {
            'version': 2,
            'sizes': list(sizes) if sizes is not None else None,
            'active_output': active_output,
        }

    def updateThreeRegionLayout(self, *, sizes, active_output: str):
        """Record the layout.  ``sizes`` is ``None`` unless a drag chose them.

        DP-134.  Handing this the splitter's current sizes unconditionally is
        what froze Region A at whatever the clamp last produced, so the caller
        now has to say that a person moved the handle.
        """
        if sizes is None:
            values = None
        else:
            values = [int(value) for value in sizes]
            if len(values) != 3 or any(value < 0 for value in values):
                raise ValueError('three-region layout requires three non-negative sizes')
        return self._set(SettingKey.THREE_REGION_LAYOUT, {
            'version': 2,
            'sizes': values,
            'active_output': str(active_output or 'mesh'),
        })

    def getMeshLineStyle(self) -> dict:
        """DP-710. The Mesh lines opacity, colour and width the user chose.

        Anything unreadable is dropped key by key, so one bad value cannot
        take the other two with it; the viewport clamps what it is given.
        """
        value = self._get(SettingKey.MESH_LINE_STYLE, {})
        if not isinstance(value, dict):
            return {}
        style = {}
        for key in ('opacity', 'width'):
            try:
                style[key] = float(value[key])
            except (KeyError, TypeError, ValueError):
                pass
        if isinstance(value.get('color'), str):
            style['color'] = value['color']
        return style

    def updateMeshLineStyle(self, *, opacity: float, color: str, width: float):
        return self._set(SettingKey.MESH_LINE_STYLE, {
            'opacity': float(opacity), 'color': str(color),
            'width': float(width)})

    def getViewportBackground(self) -> dict:
        """DP-701. The gradient ends the user picked, as ``#rrggbb``.

        Keys are ``bottom`` and ``top``; an end never picked, or stored in a
        form this cannot read, is left out so the theme's colour shows.
        """
        value = self._get(SettingKey.VIEWPORT_BACKGROUND, {})
        if not isinstance(value, dict):
            return {}
        return {key: value[key] for key in ('bottom', 'top')
                if isinstance(value.get(key), str)
                and re.fullmatch(r'#[0-9a-fA-F]{6}', value[key])}

    def updateViewportBackground(self, *, bottom: str | None = None,
                                 top: str | None = None):
        value = dict(self.getViewportBackground())
        if bottom is not None:
            value['bottom'] = str(bottom)
        if top is not None:
            value['top'] = str(top)
        return self._set(SettingKey.VIEWPORT_BACKGROUND, value)

    def clearViewportBackground(self):
        """DP-739. Forget the picked gradient; the theme's shows again."""
        return self._set(SettingKey.VIEWPORT_BACKGROUND, {})

    def getRenderQuality(self) -> str:
        """DP-726. ``performance``, ``balanced`` or ``quality``."""
        from foammesh.rendering import render_style
        value = self._get(SettingKey.RENDER_QUALITY, render_style.DEFAULT_PRESET)
        return value if value in render_style.PRESETS else render_style.DEFAULT_PRESET

    def updateRenderQuality(self, name: str):
        from foammesh.rendering import render_style
        return self._set(SettingKey.RENDER_QUALITY,
                         render_style.quality(name).name)

    def getOpenFoamRuntime(self):
        return {
            'profile_id': self._get(
                SettingKey.OPENFOAM_PROFILE_ID,
                'wsl-ubuntu22-openfoam-13' if os.name == 'nt'
                else 'native-path-openfoam'),
            'wsl_distro': self._get(
                SettingKey.OPENFOAM_WSL_DISTRO, 'OpenFOAM13Runtime'),
            'wsl_user': self._get(
                SettingKey.OPENFOAM_WSL_USER, 'foamuser'),
            'bashrc': self._get(
                SettingKey.OPENFOAM_BASHRC, '/opt/openfoam13/etc/bashrc'),
            'stage_timeout': int(self._get(
                SettingKey.OPENFOAM_STAGE_TIMEOUT, 3600)),
        }

    def hasOpenFoamRuntime(self) -> bool:
        """Whether a runtime has been stored, by Preferences or by detection."""
        return self._get(SettingKey.OPENFOAM_WSL_DISTRO) is not None

    def updateOpenFoamRuntime(self, *, profile_id: str, wsl_distro: str,
                              wsl_user: str, bashrc: str,
                              stage_timeout: int = 3600):
        # Plan 37 #7. 0 is "no limit": a far field of millions of cells can
        # castellate for longer than any fixed limit, and the product never
        # refuses on size -- so the user may take the limit away.
        if int(stage_timeout) < 0:
            raise ValueError(
                'OpenFOAM stage timeout must be positive, or 0 for no limit')
        self._settings.update({
            SettingKey.OPENFOAM_PROFILE_ID.value: str(profile_id),
            SettingKey.OPENFOAM_WSL_DISTRO.value: str(wsl_distro),
            SettingKey.OPENFOAM_WSL_USER.value: str(wsl_user),
            SettingKey.OPENFOAM_BASHRC.value: str(bashrc),
            SettingKey.OPENFOAM_STAGE_TIMEOUT.value: int(stage_timeout),
        })
        self._save()

    def getDiagnosticBudget(self):
        """How long surface checks may run, and what happens when they overrun.

        ``abort`` is the default because an unbounded check can wedge the
        application indefinitely; ``warn_only`` and ``never_limit`` exist for
        anyone who would rather wait than have an STL go unchecked.
        """
        from foammesh.core.geometry.diagnostics.budget import (
            BudgetPolicy, DEFAULT_MAXIMUM_SECONDS,
        )

        stored = self._get(SettingKey.DIAGNOSTIC_BUDGET_POLICY,
                           BudgetPolicy.ABORT.value)
        try:
            policy = BudgetPolicy(str(stored))
        except ValueError:
            policy = BudgetPolicy.ABORT
        seconds = self._get(SettingKey.DIAGNOSTIC_BUDGET_MAX_SECONDS,
                            DEFAULT_MAXIMUM_SECONDS)
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            seconds = DEFAULT_MAXIMUM_SECONDS
        return policy, seconds

    def getQualificationMode(self):
        """Whether geometry verdicts gate anything, or only report.

        ``report_only`` is the default and the state the capability ships in:
        the checks run and persist, but nothing they say can block a pipeline
        or an export until WP8 has calibrated thresholds against the corpus.
        """
        from foammesh.core.quality.qualification import DEFAULT_MODE, coerce

        return coerce(self._get(SettingKey.GEOMETRY_QUALIFICATION_MODE,
                                DEFAULT_MODE.value))

    def setQualificationMode(self, mode) -> None:
        from foammesh.core.quality.qualification import coerce

        self._set(SettingKey.GEOMETRY_QUALIFICATION_MODE, coerce(mode).value)
        self._save()

    def updateDiagnosticBudget(self, policy, maximum_seconds):
        self._set(SettingKey.DIAGNOSTIC_BUDGET_POLICY,
                  getattr(policy, 'value', str(policy)))
        self._set(SettingKey.DIAGNOSTIC_BUDGET_MAX_SECONDS,
                  float(maximum_seconds))
        self._save()

    def getScale(self):
        return self._get(SettingKey.SCALE, '1.0')

    def setScale(self, scale):
        return self._set(SettingKey.SCALE, scale)

    def getTheme(self):
        return self._get(SettingKey.THEME, 'system')

    def setTheme(self, theme):
        return self._set(SettingKey.THEME, theme)

    # Territory is not considered for now
    def getLocale(self) -> QLocale:
        return QLocale('en')

    def getLanguage(self):
        return 'en'

    def setLanguage(self, language):
        # FoamMesh currently supports one UI language: English.
        return False

    def _save(self):
        with open(self._settingsFile, 'w') as file:
            yaml.dump(self._settings, file)

    def _get(self, key, default=None):
        self._ensureLoaded()
        return self._settings[key.value] if key.value in self._settings else default

    def _set(self, key, value):
        self._ensureLoaded()
        if key.value in self._settings and self._settings[key.value] == value:
            return False

        self._settings[key.value] = value
        self._save()

        return True

    def removeProject(self, num):
        project = self.getRecentProjects()
        if 0 <= num < len(project):
            self.removeRecent(project[num])


appSettings = AppSettings()
