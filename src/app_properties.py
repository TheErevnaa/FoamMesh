#!/usr/bin/env python
# -*- coding: utf-8 -*-

from dataclasses import dataclass
import sys

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication

from resources import resource


APP_VERSION = '1.1.0'


@dataclass
class AppProperties:
    name: str
    fullName: str
    iconResource: str
    logoResource: str
    watermarkResources: dict[str, str]
    platformIconResources: dict[str, str]
    version: str            = APP_VERSION
    projectSuffix: str      = None
    analyticsEnabled: bool  = True  # OEM variants can disable analytics entirely

    def icon(self):
        return QIcon(str(self.iconPath()))

    def iconPath(self, platform_name: str | None = None):
        platform_name = platform_name or sys.platform
        resource_name = self.platformIconResources.get(platform_name, self.iconResource)
        return resource.file(resource_name)

    def logo(self):
        return QPixmap(str(resource.file(self.logoResource)))

    def watermark(self, theme: str = 'light'):
        selected = self.watermarkResources.get(theme, self.watermarkResources['light'])
        return resource.file(selected)


# Single application: the standalone FoamMesh meshing workbench.
meshAppProperties = AppProperties(
    name='FoamMesh',
    fullName=QApplication.translate('AppProperties', 'FoamMesh'),
    iconResource='branding/foammesh.ico',
    logoResource='branding/foammesh_icon_256.png',
    watermarkResources={
        'light': 'branding/foammesh_watermark_light.png',
        'dark': 'branding/foammesh_watermark_dark.png',
    },
    platformIconResources={
        'win32': 'branding/foammesh.ico',
        'darwin': 'branding/foammesh.icns',
        'linux': 'branding/foammesh_icon_256.png',
    },
    projectSuffix='.fm'
)
