"""Readers part of the batch runner (Plan 48 GM10).

A job's geometry may name a gzip file (``part.stl.gz``). Gmsh itself would run
the system ``gunzip``; the distro may not have it on the path the runner uses,
so the file is decompressed here, before the import, under its inner name, and
the scratch copy is removed when the job reaches its export stage. Everything
else in the job passes through untouched.
"""
from __future__ import annotations

import gzip
import os
import shutil
import tempfile


def _is_gz(item) -> bool:
    return str(item).lower().endswith('.gz')


def decompress_gz_sources(run, **_context):
    sources = run.job.get('geometry')
    if not sources:
        return
    listed = [sources] if isinstance(sources, str) else list(sources)
    if not any(_is_gz(item) for item in listed):
        return
    scratch = tempfile.mkdtemp(prefix='foammesh-gz-')
    run._gm10_scratch = scratch
    plain = []
    for index, item in enumerate(listed):
        if not _is_gz(item):
            plain.append(item)
            continue
        folder = os.path.join(scratch, str(index))
        os.makedirs(folder)
        target = os.path.join(folder, os.path.basename(str(item))[:-3])
        with gzip.open(str(item), 'rb') as packed, open(target, 'wb') as out:
            shutil.copyfileobj(packed, out)
        plain.append(target)
    run.job['geometry'] = plain[0] if isinstance(sources, str) else plain


def remove_scratch(run, **_context):
    scratch = getattr(run, '_gm10_scratch', None)
    if scratch:
        shutil.rmtree(scratch, ignore_errors=True)
        run._gm10_scratch = None


def register(add_hook):
    add_hook('before_import', decompress_gz_sources)
    add_hook('export', remove_scratch)
