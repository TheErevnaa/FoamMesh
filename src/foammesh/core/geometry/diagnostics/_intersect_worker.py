#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Intersect two surface shells in a child process, so it can be abandoned.

``vtkIntersectionPolyDataFilter`` is one opaque call that never yields. A
budget can only stop it by giving up on it -- and giving up on a *thread* is
unsafe: the abandoned thread stays inside VTK, mutating objects the parent then
keeps using, which segfaults the application rather than merely wasting a core.

A child process can be killed outright. It costs two temporary files per pair,
which is nothing beside the alternative.

Invoked as ``python -m ..._intersect_worker left.vtp right.vtp result.json``.
"""
from __future__ import annotations

import json
import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print('usage: _intersect_worker LEFT.vtp RIGHT.vtp RESULT.json',
              file=sys.stderr)
        return 2
    left_path, right_path, result_path = argv

    from vtkmodules.vtkFiltersGeneral import vtkIntersectionPolyDataFilter
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader

    def read(path):
        reader = vtkXMLPolyDataReader()
        reader.SetFileName(path)
        reader.Update()
        return reader.GetOutput()

    test = vtkIntersectionPolyDataFilter()
    test.SetInputData(0, read(left_path))
    test.SetInputData(1, read(right_path))
    test.Update()
    curves = test.GetOutput()

    points = []
    total = curves.GetNumberOfPoints()
    for index in range(min(total, 50)):
        points.append(list(curves.GetPoint(index)))
    with open(result_path, 'w', encoding='utf-8') as stream:
        json.dump({'cells': int(curves.GetNumberOfCells()),
                   'points': points}, stream)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
