#!/usr/bin/env python
# -*- coding: utf-8 -*-
import math

from matplotlib import colormaps

from vtkmodules.vtkCommonCore import vtkLookupTable


STEP = 256


def getLookupTable(name: str):
    lut = vtkLookupTable()
    lut.SetNumberOfTableValues(STEP)

    cmap = colormaps[name]

    for i in range(0, STEP):
        rgb = cmap(i/(STEP-1))[:3]  # Extract RGB values excluding Alpha
        lut.SetTableValue(i, *rgb)

    return lut


sequentialRedLut = vtkLookupTable()
# Use a color series to create a transfer function
for i in range(0, STEP):
    o = 1 - math.pow(i/(STEP-1), 2)
    sequentialRedLut.SetTableValue(i, 1, o, o)


#: DP-713. Unmeasured faces: a neutral grey, never a colour of the scale.
NAN_GREY = (0.62, 0.62, 0.62, 1.0)


def deviationLut(valueRange=None):
    """Blue inside, white on the surface, red outside; unmeasured is grey.

    DP-713 (viewport audit 0925 F8). Deviation is signed, and a scale that
    ran one hue from low to high painted "short of the surface" and "past
    it" as opposite ends of the same colour -- and painted an unmeasured
    face in whatever colour VTK chose for nan.
    """
    lut = getLookupTable('RdBu_r')
    lut.SetNanColor(*NAN_GREY)
    if valueRange is not None:
        lut.SetTableRange(float(valueRange[0]), float(valueRange[1]))
    return lut


def qualityBandLut(low, high, worstIsHigh=True):
    """DP-714. The poor-cell scale, on the band it is colouring.

    The quality colouring used `sequentialRedLut` as is, on its 0-1 table
    range, with the mapper told to take the table's range: every value above
    1 -- every aspect ratio, every angle in degrees -- came out the same full
    red, and the legend read 0 to 1 whatever the metric. This is the same
    white-to-red scale stretched over the band, turned round when the bad
    end of the metric is the low one, so red is always the worst cell.
    """
    lut = vtkLookupTable()
    lut.DeepCopy(sequentialRedLut)
    if not worstIsHigh:
        count = lut.GetNumberOfTableValues()
        values = [lut.GetTableValue(i) for i in range(count)]
        for i, value in enumerate(reversed(values)):
            lut.SetTableValue(i, *value)
    low, high = float(low), float(high)
    if high <= low:
        high = low + (abs(low) * 1e-6 or 1e-12)
    lut.SetTableRange(low, high)
    return lut
