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
#: Plan 43 LY3. Raised from 1 (1.1) when the settings column became a sheet.
#: A layout saved under an older version is not applied: it is kept under
#: ``layout_backup`` for one release and the standard layout opens.
LAYOUT_VERSION = 2
SHEET_MINIMUM = 280
SHEET_MAXIMUM = 560
RECENT_PROJECTS_NUMBER = 50


class SettingKey(Enum):
    FORMAT_VERSION = 'format_version'
    SCALE = 'display_scale'
    DENSITY = 'ui_density'
    UI_FONT = 'ui_font'
    HIDPI_POLICY = 'hidpi_rounding_policy'
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
    DOCK_LAYOUT = 'dock_layout'
    # Plan 43 LY3. The settings sheet (floating or pinned, side, widths), the
    # version of the saved layout, and the layout 1.1 saved, kept for one release.
    SETTINGS_SHEET = 'settings_sheet'
    LAYOUT_VERSION = 'layout_version'
    LAYOUT_BACKUP = 'layout_backup'
    DESKTOP_NOTIFICATIONS = 'desktop_notifications'
    # Plan 40 UXE2. Customise on the viewport rail: labels, rail shown.
    VIEWPORT_RAIL = 'viewport_rail'
    # Plan 39 VI13. View > Show resource usage (the CPU / RAM panel).
    RESOURCE_USAGE = 'resource_usage'
    # Plan 40 UXE1. Keys the user rebound: {command id: [keys]}.
    SHORTCUTS = 'shortcuts'
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
    # GPUPREF. Use the dedicated GPU when available (default on) and what
    # FoamMesh wrote to Windows' per-app graphics preference.
    USE_DEDICATED_GPU = 'use_dedicated_gpu'
    GPU_PREFERENCE_STATE = 'gpu_preference_state'
    # Plan 43 LY2. The step ribbon: numbers only, colour by state, show locks.
    STEP_RIBBON = 'step_ribbon'
    # Plan 43 LY12. The colour standard: scale, reverse, bands, range, legend,
    # label font and size. Absent means the app standard (Rainbow, Jet).
    COLOUR_STANDARD = 'colour_standard'


from types import SimpleNamespace  # noqa: E402

#: Plan 43 §4. The colour standard; LY15 keeps the labels font and size in it.
COLOUR_STANDARD_KEY = SimpleNamespace(value='colour_standard')

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
        record = {
            'version': 2,
            'sizes': values,
            'active_output': str(active_output or 'mesh'),
        }
        # Plan 43 LY4: the console's choices live beside the sizes and
        # survive a layout update.
        previous = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        if isinstance(previous, dict) and isinstance(
                previous.get('console'), dict):
            record['console'] = previous['console']
        # Plan 43 LY10: and so do the quality views.
        if isinstance(previous, dict) and isinstance(
                previous.get('quality_views'), dict):
            record['quality_views'] = previous['quality_views']
        if isinstance(previous, dict) and isinstance(
                previous.get('view_menu'), dict):
            record['view_menu'] = previous['view_menu']
        return self._set(SettingKey.THREE_REGION_LAYOUT, record)

    def getConsoleLayout(self) -> dict:
        """Plan 43 LY4. The reader's console choices, kept in the
        ``console`` member of the three-region record (per user, never in a
        project). A missing or bad record is the standard layout."""
        value = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        console = value.get('console') if isinstance(value, dict) else None
        return dict(console) if isinstance(console, dict) else {}

    def updateConsoleLayout(self, console: dict):
        value = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        record = dict(value) if isinstance(value, dict) else {}
        record.setdefault('version', 2)
        record.setdefault('sizes', None)
        record.setdefault('active_output', 'mesh')
        record['console'] = dict(console)
        return self._set(SettingKey.THREE_REGION_LAYOUT, record)

    def getDesktopNotifications(self) -> bool:
        """Plan 40 UXC4. Opt-in: a desktop notification when a run ends unfocused."""
        return bool(self._get(SettingKey.DESKTOP_NOTIFICATIONS, False))

    def updateDesktopNotifications(self, enabled):
        return self._set(SettingKey.DESKTOP_NOTIFICATIONS, bool(enabled))

    def getMeshQualityViews(self) -> dict:
        """Plan 43 LY10. ``{'layout': 'B', 'worst_values': True, 'caption':
        True}``, kept in the ``quality_views`` member of the three-region
        record (per user, never in a project). A missing or damaged record
        gives the defaults: layout B, the worst values and the caption on."""
        whole = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        value = whole.get('quality_views') if isinstance(whole, dict) else None
        value = value if isinstance(value, dict) else {}
        layout = str(value.get('layout') or 'B').upper()
        return {
            'layout': layout if layout in ('A', 'B', 'C') else 'B',
            'worst_values': bool(value.get('worst_values', True)),
            'caption': bool(value.get('caption', True)),
        }

    def updateMeshQualityViews(self, **changes):
        whole = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        record = dict(whole) if isinstance(whole, dict) else {}
        record.setdefault('version', 2)
        record.setdefault('sizes', None)
        record.setdefault('active_output', 'mesh')
        current = self.getMeshQualityViews()
        current.update(changes)
        record['quality_views'] = current
        return self._set(SettingKey.THREE_REGION_LAYOUT, record)

    def getDockLayout(self):
        """Plan 40 UXC3. The saved dock arrangement, or ``None``.

        ``{'state': <base64 of the Qt-ADS state>, 'screens': {dock: screen}}``.
        Anything unreadable is dropped, so a damaged record opens the default
        layout rather than an error. Plan 43 LY3: only a record of the
        current ``LAYOUT_VERSION`` is applied.
        """
        value = self._get(SettingKey.DOCK_LAYOUT, None)
        if (not isinstance(value, dict)
                or value.get('version') != LAYOUT_VERSION
                or not isinstance(value.get('state'), str)
                or not value['state']):
            return None
        screens = value.get('screens')
        if not isinstance(screens, dict):
            screens = {}
        return {'version': LAYOUT_VERSION, 'state': value['state'],
                'screens': {str(k): str(v) for k, v in screens.items()}}

    def updateDockLayout(self, record):
        """Store the dock arrangement; ``None`` forgets it (Reset layout)."""
        if record is None:
            return self._set(SettingKey.DOCK_LAYOUT, None)
        return self._set(SettingKey.DOCK_LAYOUT, {
            'version': LAYOUT_VERSION,
            'state': str(record['state']),
            'screens': {str(k): str(v)
                        for k, v in dict(record.get('screens') or {}).items()},
        })

    def migrateLayout(self) -> bool:
        """Plan 43 LY3. Raise the saved-layout version; True when a note is due.

        A layout saved by 1.1 (dock arrangement, region sizes) belongs to the
        old three-column window. It is moved to ``layout_backup`` (kept for one
        release) and forgotten, so the standard layout opens. The note is due
        only for someone who had a layout: a first start has nothing to
        explain. Called once at start; the second call returns False.
        """
        self._ensureLoaded()
        if self._settings.get(SettingKey.LAYOUT_VERSION.value) == LAYOUT_VERSION:
            return False
        old = {key.value: self._settings[key.value]
               for key in (SettingKey.DOCK_LAYOUT,
                           SettingKey.THREE_REGION_LAYOUT)
               if self._settings.get(key.value) is not None}
        due = bool(old)
        if due:
            self._settings[SettingKey.LAYOUT_BACKUP.value] = old
            for name in old:
                self._settings.pop(name, None)
        self._settings[SettingKey.LAYOUT_VERSION.value] = LAYOUT_VERSION
        self._save()
        return due

    def getSettingsSheet(self) -> dict:
        """Plan 43 LY3. How the settings sheet is shown.

        ``{'mode': 'floating'|'pinned', 'side': 'left'|'right', 'width': int
        or None, 'memory': {page: width}, 'minimised': bool}``. Anything
        unreadable falls back to the standard (floating, left, no chosen
        width), field by field.
        """
        value = self._get(SettingKey.SETTINGS_SHEET, None)
        record = {'mode': 'floating', 'side': 'left', 'width': None,
                  'memory': {}, 'minimised': False}
        if not isinstance(value, dict):
            return record
        if value.get('mode') in ('floating', 'pinned'):
            record['mode'] = value['mode']
        if value.get('side') in ('left', 'right'):
            record['side'] = value['side']
        width = value.get('width')
        if isinstance(width, int) and not isinstance(width, bool):
            record['width'] = min(max(width, SHEET_MINIMUM), SHEET_MAXIMUM)
        memory = value.get('memory')
        if isinstance(memory, dict):
            record['memory'] = {
                str(k): min(max(v, SHEET_MINIMUM), SHEET_MAXIMUM)
                for k, v in memory.items()
                if isinstance(v, int) and not isinstance(v, bool)}
        if isinstance(value.get('minimised'), bool):
            record['minimised'] = value['minimised']
        return record

    def updateSettingsSheet(self, record):
        """Store the settings sheet's choices (see ``getSettingsSheet``)."""
        if record is None:
            return self._set(SettingKey.SETTINGS_SHEET, None)
        return self._set(SettingKey.SETTINGS_SHEET, {
            'mode': str(record.get('mode', 'floating')),
            'side': str(record.get('side', 'left')),
            'width': record.get('width'),
            'memory': {str(k): int(v)
                       for k, v in dict(record.get('memory') or {}).items()},
            'minimised': bool(record.get('minimised', False)),
        })

    def getViewportRail(self) -> dict:
        """Plan 40 UXE2. What *Customise* set on the viewport rail.

        ``{'rail': bool, 'labels': bool}``; a missing or damaged record gives
        the defaults (rail shown, icons only).
        """
        value = self._get(SettingKey.VIEWPORT_RAIL, None)
        record = {'rail': True, 'labels': False}
        if isinstance(value, dict):
            for key in record:
                if isinstance(value.get(key), bool):
                    record[key] = value[key]
        return record

    def updateViewportRail(self, record):
        """Store the rail's *Customise* choices."""
        return self._set(SettingKey.VIEWPORT_RAIL, {
            'rail': bool(record.get('rail', True)),
            'labels': bool(record.get('labels', False))})

    def getResourceUsage(self) -> bool:
        """Plan 39 VI13. Whether the CPU / RAM panel is shown (default off)."""
        return self._get(SettingKey.RESOURCE_USAGE, False) is True

    def updateResourceUsage(self, shown):
        return self._set(SettingKey.RESOURCE_USAGE, bool(shown))

    def getShortcuts(self) -> dict:
        """Plan 40 UXE1. The keys the user rebound, ``{command id: [keys]}``.

        A damaged record gives ``{}`` and a damaged entry is dropped alone.
        """
        value = self._get(SettingKey.SHORTCUTS, None)
        if not isinstance(value, dict):
            return {}
        return {str(key): [str(k) for k in keys if isinstance(k, str)]
                for key, keys in value.items() if isinstance(keys, list)}

    def updateShortcuts(self, record):
        """Store the rebound keys; commands left at their default are absent."""
        return self._set(SettingKey.SHORTCUTS, {
            str(key): [str(k) for k in keys]
            for key, keys in dict(record).items()})

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

    def getColourStandard(self):
        """Plan 43 LY12. The user's saved colour standard, or ``None``."""
        value = self._get(SettingKey.COLOUR_STANDARD, None)
        if not isinstance(value, dict):
            return None
        # The labels font and size (LY15) share the record but are not part
        # of the colour standard.
        record = {k: v for k, v in value.items() if k != 'labels'}
        return record or None

    def updateColourStandard(self, record):
        from foammesh.support import colour_standard
        stored = self._get(SettingKey.COLOUR_STANDARD, None)
        merged = colour_standard.normalise(record).to_record()
        if isinstance(stored, dict) and 'labels' in stored:
            merged['labels'] = stored['labels']
        return self._set(SettingKey.COLOUR_STANDARD, merged)

    def clearColourStandard(self):
        """Remove the user's entry: the app standard shows again."""
        self._ensureLoaded()
        stored = self._settings.get(SettingKey.COLOUR_STANDARD.value)
        if stored is None:
            return False
        if isinstance(stored, dict) and 'labels' in stored:
            self._settings[SettingKey.COLOUR_STANDARD.value] = {
                'labels': stored['labels']}
        else:
            del self._settings[SettingKey.COLOUR_STANDARD.value]
        self._save()
        return True

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

    def getDensity(self):
        return self._get(SettingKey.DENSITY, 'compact')

    def setDensity(self, density):
        return self._set(SettingKey.DENSITY, str(density))

    def getUiFont(self):
        return self._get(SettingKey.UI_FONT, 'pretendard')

    def setUiFont(self, font):
        return self._set(SettingKey.UI_FONT, str(font))

    def getLabelStyle(self) -> dict:
        """Plan 43 LY15. The labels font and size kept in the colour standard.

        ``{'font': ..., 'size': ...}`` with only valid names; anything missing
        or unreadable is left out so the app default shows through.
        """
        from foammesh.rendering.label_style import FONTS, SIZES

        standard = self._get(COLOUR_STANDARD_KEY, {})
        labels = standard.get('labels') if isinstance(standard, dict) else None
        if not isinstance(labels, dict):
            return {}
        style = {}
        if labels.get('font') in FONTS:
            style['font'] = labels['font']
        if labels.get('size') in SIZES:
            style['size'] = labels['size']
        return style

    def updateLabelStyle(self, *, font: str, size: str) -> bool:
        """Write the labels font and size without touching the rest of the standard."""
        standard = self._get(COLOUR_STANDARD_KEY, {})
        standard = dict(standard) if isinstance(standard, dict) else {}
        standard['labels'] = {'font': str(font), 'size': str(size)}
        return self._set(COLOUR_STANDARD_KEY, standard)

    def clearLabelStyle(self) -> bool:
        standard = self._get(COLOUR_STANDARD_KEY, {})
        if not isinstance(standard, dict) or 'labels' not in standard:
            return False
        standard = {k: v for k, v in standard.items() if k != 'labels'}
        return self._set(COLOUR_STANDARD_KEY, standard)

    def getHiDpiPolicy(self):
        return self._get(SettingKey.HIDPI_POLICY, 'pass_through')

    def setHiDpiPolicy(self, policy):
        return self._set(SettingKey.HIDPI_POLICY, str(policy))

    def getUseDedicatedGpu(self) -> bool:
        return bool(self._get(SettingKey.USE_DEDICATED_GPU, True))

    def setUseDedicatedGpu(self, enabled) -> bool:
        return self._set(SettingKey.USE_DEDICATED_GPU, bool(enabled))

    def getGpuPreferenceState(self) -> dict:
        state = self._get(SettingKey.GPU_PREFERENCE_STATE, {})
        return dict(state) if isinstance(state, dict) else {}

    def setGpuPreferenceState(self, state) -> bool:
        return self._set(SettingKey.GPU_PREFERENCE_STATE, dict(state or {}))

    def getViewMenu(self) -> dict:
        """Plan 43 LY8. The View menu's remembered choices (per user), kept
        in the ``view_menu`` member of the three-region record beside the
        console's."""
        value = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        record = value.get('view_menu') if isinstance(value, dict) else None
        return dict(record) if isinstance(record, dict) else {}

    def updateViewMenu(self, values) -> bool:
        value = self._get(SettingKey.THREE_REGION_LAYOUT, {})
        record = dict(value) if isinstance(value, dict) else {}
        record.setdefault('version', 2)
        record.setdefault('sizes', None)
        record.setdefault('active_output', 'mesh')
        record['view_menu'] = dict(values or {})
        return self._set(SettingKey.THREE_REGION_LAYOUT, record)

    def getStepRibbon(self) -> dict:
        """Plan 43 LY2. The ribbon's three choices, always a complete record."""
        value = self._get(SettingKey.STEP_RIBBON, {})
        value = value if isinstance(value, dict) else {}
        return {
            'numbers_only': bool(value.get('numbers_only', False)),
            'colour_by_state': bool(value.get('colour_by_state', True)),
            'show_locks': bool(value.get('show_locks', True)),
        }

    def setStepRibbon(self, record) -> bool:
        record = dict(record or {})
        return self._set(SettingKey.STEP_RIBBON, {
            'numbers_only': bool(record.get('numbers_only', False)),
            'colour_by_state': bool(record.get('colour_by_state', True)),
            'show_locks': bool(record.get('show_locks', True)),
        })

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
