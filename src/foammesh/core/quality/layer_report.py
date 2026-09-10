#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Parse the snappyHexMesh add-layers log into per-patch coverage.

snappy prints **two** tables. First what was requested::

    patch         faces    layers avg thickness[m]
                                  near-wall overall
    wall          2150     5      0.000675  0.00455

and, after the shrink and mesh-quality loops have removed or reduced
extrusion, what was actually achieved::

    patch         faces    layers   overall thickness
                                    [m]       [%]
    wall          1884     2.41     0.0419    62.2

Only the second table describes the mesh that exists. Reading the first one
reports layers that are not there, so this parser locates the achieved table
and reads it. Achieved layer counts are per-patch *averages* and therefore
fractional -- parsing them as integers silently drops every real row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


#: Header of the achieved-layer table.
ACHIEVED_HEADER = 'layers   overall thickness'

#: A patch counts as under-covered below this share of what was requested.
DEFAULT_COVERAGE_FLOOR = 50.0


@dataclass
class PatchLayerCoverage:
    patch: str
    faces: int
    layers: float
    thickness: float
    coverage_pct: float
    requested_layers: int | None = None
    #: The dictionary asked for ``nSurfaceLayers 0`` on this patch.
    frozen: bool = False

    @property
    def layer_fraction(self) -> float | None:
        """Achieved layers as a share of the request, when the request is known."""
        if not self.requested_layers:
            return None
        return self.layers / float(self.requested_layers)

    @property
    def ok(self) -> bool:
        # A frozen patch was asked to gain nothing and to move nowhere, so no
        # layers on it is the requested outcome, not a failed extrusion.
        if self.frozen:
            return True
        if self.layers <= 0:
            return False
        share = self.layer_fraction
        if share is not None:
            return share * 100.0 >= DEFAULT_COVERAGE_FLOOR
        return self.coverage_pct >= DEFAULT_COVERAGE_FLOOR


@dataclass
class LayerReport:
    patches: list[PatchLayerCoverage] = field(default_factory=list)

    @property
    def failed_patches(self) -> list[str]:
        return [p.patch for p in self.patches if not p.ok]

    @property
    def frozen_patches(self) -> list[str]:
        """Patches the case deliberately froze (``nSurfaceLayers 0``).

        Reported rather than merely excluded from the failures: a reader
        looking at a patch with no layers has to be able to tell a decision
        from an accident.
        """
        return [p.patch for p in self.patches if p.frozen]

    @property
    def notes(self) -> list[str]:
        """Statements of fact that are not complaints."""
        return [f'{name}: frozen by request (nSurfaceLayers 0); no layers were '
                'added and the patch did not slide.'
                for name in self.frozen_patches]

    @property
    def warnings(self) -> list[str]:
        """Human-readable notes for patches that did not get their layers.

        Staying silent here would reproduce the defect this module exists to
        prevent: a layer run that added nothing reported as a success.
        """
        messages = []
        for item in self.patches:
            if item.frozen:
                continue
            if item.layers <= 0:
                messages.append(
                    f'{item.patch}: no prism layers were added across '
                    f'{item.faces} faces; the requested layer specification '
                    'could not be extruded.')
            elif not item.ok:
                if item.requested_layers:
                    messages.append(
                        f'{item.patch}: {item.layers:g} of '
                        f'{item.requested_layers} requested layers on average '
                        f'({item.layer_fraction:.0%} of the request).')
                else:
                    messages.append(
                        f'{item.patch}: layers reached {item.coverage_pct:g}% '
                        'of the requested thickness.')
        return messages

    def to_dict(self) -> dict:
        return {
            'patches': [vars(p) for p in self.patches],
            'failed_patches': self.failed_patches,
            'frozen_patches': self.frozen_patches,
            'notes': self.notes,
            'warnings': self.warnings,
        }


# patch faces layers overallThickness coverage%   (layers may be fractional)
_ROW = re.compile(
    r'^\s*([A-Za-z0-9_.\-]+)\s+([0-9]+)\s+([0-9.eE+\-]+)\s+'
    r'([0-9.eE+\-]+)\s+([0-9.eE+\-]+)\s*$')


def parse_layer_log(log: str,
                    requested: dict[str, int] | None = None,
                    frozen: set[str] | None = None) -> LayerReport:
    """Read achieved per-patch layer coverage from a snappyHexMesh log."""
    report = LayerReport()
    requested = requested or {}
    frozen = {str(name) for name in (frozen or ())}
    # Restrict to the final achieved table when snappy emitted one; a bare
    # table (tests, trimmed logs) is still read as-is.
    body = log.rsplit(ACHIEVED_HEADER, 1)[-1] if ACHIEVED_HEADER in log else log
    for line in body.splitlines():
        m = _ROW.match(line)
        if not m:
            continue
        name = m.group(1)
        if name.lower() in ('patch', 'faces', 'layers'):   # header tokens
            continue
        try:
            report.patches.append(PatchLayerCoverage(
                patch=name,
                faces=int(m.group(2)),
                layers=float(m.group(3)),
                thickness=float(m.group(4)),
                coverage_pct=float(m.group(5)),
                requested_layers=requested.get(name),
                frozen=name in frozen,
            ))
        except ValueError:
            continue
    return report
