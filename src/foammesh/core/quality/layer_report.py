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

from foammesh.core.quantities import agreeing, count_text

#: DP-782. What to change when snappy rolls every layer back. MEASURED on
#: mesh campaign 0925: S8 asked for 3 layers with a 0.6 mm first layer on
#: 10 mm wall cells and grew none on elbow (0 of 1197 faces); S8B, the same
#: case and the same quality limits with a 2 mm first layer, grew 2.66 of 3
#: layers over 81.5% of the wall. The record said the layers were missing
#: and nothing about why or what to change.
NOT_GROWN_REMEDY = (
    'A first layer much thinner than the wall cells is the usual cause: '
    'thicken the first layer or use relative sizes, refine the wall so its '
    'cells come closer to the layer, or ask for fewer layers.')


#: Header of the achieved-layer table.
ACHIEVED_HEADER = 'layers   overall thickness'

#: DP-112. What ``addLayers`` prints when the ``layers`` dictionary selects no
#: patch at all. It is not an error and the run exits 0, so this sentence is
#: the only difference between a layered mesh and an untouched one.
NO_LAYERS_MARKER = 'No layers to generate'

#: A patch counts as under-covered below this share of what was requested.
DEFAULT_COVERAGE_FLOOR = 50.0

#: DP-461. A layer count and a layer thickness are separate columns of the
#: achieved table, and snappy will print a fractional count beside a thickness
#: of zero. MEASURED on the six multiregion snappy legs of 21 September 2026:
#: `coaxial_ducts_core` came back `3888 faces, 2.82 layers, 0 m, 0%` and
#: `shell_and_tube_tube` `764 faces, 2.53 layers, 0 m, 0%`, and both were
#: graded `ok` with `failed_patches: []`, because the pass test reads the count
#: against the request and never looks at the thickness. A prism layer of zero
#: total thickness is a degenerate cell, not a boundary layer -- nothing
#: resolves a gradient across a length of zero -- so the count alone cannot
#: settle it. Compared with `<=` rather than against a tolerance: snappy prints
#: the column as `0`, and a thickness that is merely small is a thin layer
#: (which the share already grades) rather than an absent one.
ZERO_THICKNESS = 0.0


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
        # DP-461. The count says how many layers were counted and the
        # thickness says whether they occupy any space. A patch with 2.53
        # layers over zero metres has no boundary layer on it, and grading it
        # on the count alone is this module's own defect one column across.
        if float(self.thickness or 0.0) <= ZERO_THICKNESS:
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
                    'could not be extruded. ' + NOT_GROWN_REMEDY)
            elif float(item.thickness or 0.0) <= ZERO_THICKNESS:
                # DP-461, and it is said in its own words rather than folded
                # into the share below: "2.53 of 3 requested layers" describes
                # a mesh that has layers, and this one has none.
                messages.append(
                    f'{item.patch}: {item.layers:g} '
                    f'{agreeing(item.layers, "layer")} were counted across '
                    f'{count_text(item.faces, "face")} at zero total '
                    'thickness, so no prism layer exists on that patch.')
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



#: The two figures a layer table carries are not the same kind of number, and
#: Plan 32 section 4.5 says so explicitly: ``thickness`` is a length in
#: metres, ``coverage_pct`` is a share of the thickness that was asked for.
#: Printed side by side without their units they read as one measurement
#: taken twice, and 0.00393 beside 83.9 invites the reader to believe the
#: layers are two orders of magnitude thinner than requested.
THICKNESS_UNIT = 'm'
COVERAGE_UNIT = '%'

#: What a row is allowed to say about a patch. `frozen` is a decision, not a
#: failure, and is kept distinct from `not grown` for that reason.
VERDICTS = ('complete', 'partial', 'not grown', 'frozen')


def _figure(value) -> str:
    """A number as a reader would write it, or an em-free dash for absence."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return '-'
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return f'{number:g}'


def coverage_verdict(patch) -> str:
    """One word for what happened to this patch's layers.

    The thresholds are the ones :class:`PatchLayerCoverage` already uses, so
    the word on the page and the pass/fail the report recorded cannot
    disagree about the same patch.
    """
    if patch.get('frozen'):
        return 'frozen'
    try:
        achieved = float(patch.get('layers') or 0.0)
    except (TypeError, ValueError):
        achieved = 0.0
    if achieved <= 0:
        return 'not grown'
    # DP-461. Counted is not grown: snappy prints a fractional count beside a
    # thickness of zero, and the word on the page must match the pass/fail the
    # report recorded for the same patch.
    # A thickness that is absent was never measured, and unmeasured is not
    # zero: snappy always prints the column, so a row that carries no
    # thickness at all came from a producer that never recorded one. Grading
    # that as an absent layer would be this fault inverted. Only a recorded
    # zero is a zero.
    if patch.get('thickness') is not None:
        try:
            if float(patch['thickness']) <= ZERO_THICKNESS:
                return 'not grown'
        except (TypeError, ValueError):
            pass
    requested = patch.get('requested_layers')
    try:
        share = 100.0 * achieved / float(requested) if requested else None
    except (TypeError, ValueError, ZeroDivisionError):
        share = None
    if share is None:
        # Nothing recorded a request, so the achieved count cannot be
        # measured against one; the thickness share is what is left.
        share = float(patch.get('coverage_pct') or 0.0)
    if share >= 100.0:
        return 'complete'
    if share >= DEFAULT_COVERAGE_FLOOR:
        return 'partial'
    return 'not grown'


def coverage_rows(document) -> list:
    """Requested against achieved, per patch, ready to be read.

    Plan 32 check 5. The numbers have been parsed since Plan 26 and persisted
    to ``foammesh/quality/layer-coverage.json`` ever since; the only thing
    that read them back was a viewport colouring mode. This is the projection
    the Quality page and the HTML report share, so the page and the document
    cannot describe the same run differently.

    *document* is a ``LayerReport.to_dict()`` or the ``mesh.layer_coverage``
    payload -- the same mapping either way. Keys may be missing: the artifact
    written by a run that recorded no request has no ``requested_layers`` and
    the one written before freezing was tracked has no ``frozen``.
    """
    rows = []
    for patch in (document or {}).get('patches') or ():
        if not isinstance(patch, dict):
            continue
        requested = patch.get('requested_layers')
        rows.append({
            'patch': str(patch.get('patch') or '(unnamed)'),
            'faces': int(patch.get('faces') or 0),
            'requested': requested,
            'achieved': patch.get('layers'),
            'verdict': coverage_verdict(patch),
            # Zero is an answer. `nSurfaceLayers 0` is how a case freezes a
            # patch, so `requested_layer_counts` records nought for it, and a
            # row reading `not recorded` in its request beside `frozen` in its
            # verdict told the reader nobody had asked for the nought that was
            # asked for. Only a missing key is unrecorded.
            'requested_text': ('not recorded' if requested is None
                               else f'{_figure(requested)} layers'),
            'achieved_text': f'{_figure(patch.get("layers"))} layers',
            # A length.
            'thickness_text': (f'{_figure(patch.get("thickness"))} '
                               f'{THICKNESS_UNIT}'
                               if patch.get('thickness') is not None else '-'),
            'thickness_unit': THICKNESS_UNIT,
            # A share of a request. Not the same kind of number as above.
            'coverage_text': (f'{_figure(patch.get("coverage_pct"))}'
                              f'{COVERAGE_UNIT} of requested thickness'),
            'coverage_unit': COVERAGE_UNIT,
        })
    return rows


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
