"""Measure one Gmsh inside the distribution (Plan 48 GM16a). Runs in WSL.

    python3 verify_build.py identity            # version, BuildOptions, file, sha
    python3 verify_build.py cvt WORKDIR         # Plugin(CVTRemesh) on a small STL
    python3 verify_build.py offscreen WORKDIR   # can this Gmsh write PGF/TikZ headless

``measure_runtime.py`` (host side) starts these twice, once with the official
wheel and once with the build on PYTHONPATH, and writes
``plans/evidence/plan48/runtime.json``. Each mode prints one JSON object.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sys


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _libgmsh_path(gmsh):
    # The shared object this process really mapped.
    with open('/proc/self/maps', encoding='utf-8') as handle:
        for line in handle:
            if 'libgmsh' in line:
                return line.split()[-1]
    return ''


def identity():
    import gmsh
    gmsh.initialize()
    try:
        gmsh.option.setNumber('General.Terminal', 0)
        lib = _libgmsh_path(gmsh)
        return {
            'version': gmsh.option.getString('General.Version'),
            'build_options': gmsh.option.getString('General.BuildOptions'),
            'build_info': gmsh.option.getString('General.BuildInfo'),
            'gmsh_py': getattr(gmsh, '__file__', ''),
            'libgmsh_path': lib,
            'libgmsh_sha256': _sha256(lib) if lib else '',
            'python': sys.version.split()[0],
            'sys_path_head': sys.path[:4],
        }
    finally:
        gmsh.finalize()


def _small_stl(work):
    """A sphere surface mesh written as STL by a throw-away Gmsh session."""
    import gmsh
    path = os.path.join(work, 'sphere.stl')
    gmsh.initialize()
    try:
        gmsh.option.setNumber('General.Terminal', 0)
        gmsh.model.add('sphere')
        gmsh.model.occ.addSphere(0, 0, 0, 1.0)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber('Mesh.MeshSizeMax', 0.25)
        gmsh.model.mesh.generate(2)
        gmsh.write(path)
    finally:
        gmsh.finalize()
    return path


def cvt(work):
    import gmsh
    os.makedirs(work, exist_ok=True)
    stl = _small_stl(work)
    result = {'ran': False, 'input': stl}
    gmsh.initialize()
    try:
        gmsh.option.setNumber('General.Terminal', 0)
        gmsh.merge(stl)
        # CVTRemesh evaluates the surface (face->point), which a merged STL
        # only supports once it is classified and given a parametrisation.
        gmsh.model.mesh.classifySurfaces(math.pi, True, True)
        gmsh.model.mesh.createGeometry()
        before = gmsh.model.getEntities(2)
        in_tri = sum(len(gmsh.model.mesh.getElementsByType(2, tag=t[1])[0])
                     for t in before)
        result.update(input_surfaces=len(before), input_triangles=in_tri)
        try:
            gmsh.plugin.setNumber('CVTRemesh', 'Sites', 400)
            gmsh.plugin.setNumber('CVTRemesh', 'Iterations', 10)
            gmsh.plugin.run('CVTRemesh')
        except Exception as error:  # the wheel has no such plugin
            result.update(error=f'{type(error).__name__}: {error}')
            return result
        after = gmsh.model.getEntities(2)
        new = [e for e in after if e not in before]
        out_tri = sum(len(gmsh.model.mesh.getElementsByType(2, tag=t[1])[0])
                      for t in new)
        result.update(ran=True, new_surfaces=len(new), output_triangles=out_tri)
    finally:
        gmsh.finalize()
    return result


def offscreen(work, use_fltk=False):
    """Whether this Gmsh can write the GL2PS formats (tex, pgf, tikz, pdf, svg,
    eps) and PNG, with the graphics context of the given kind.

    use_fltk=False writes straight from the API (no graphics context);
    use_fltk=True first calls gmsh.fltk.initialize(), which needs an X server
    (the caller supplies a virtual one with xvfb-run, or none at all).
    """
    import gmsh
    os.makedirs(work, exist_ok=True)
    stl = _small_stl(work)
    result = {'display': os.environ.get('DISPLAY', ''), 'use_fltk': use_fltk,
              'formats': {}}
    gmsh.initialize()
    try:
        gmsh.option.setNumber('General.Terminal', 0)
        gmsh.merge(stl)
        gmsh.option.setNumber('Mesh.SurfaceEdges', 1)
        if use_fltk:
            try:
                gmsh.fltk.initialize()
                result['fltk_initialize'] = 'ok'
            except Exception as error:
                result['fltk_initialize'] = f'{type(error).__name__}: {error}'
                return result
        for ext in ('tex', 'pgf', 'tikz', 'pdf', 'svg', 'eps', 'png'):
            path = os.path.join(work, f'probe.{ext}')
            if os.path.exists(path):
                os.remove(path)
            try:
                gmsh.write(path)
                ok = os.path.isfile(path) and os.path.getsize(path) > 0
                result['formats'][ext] = {
                    'written': ok,
                    'bytes': os.path.getsize(path) if ok else 0}
            except Exception as error:
                result['formats'][ext] = {
                    'written': False, 'error': f'{type(error).__name__}: {error}'}
    finally:
        gmsh.finalize()
    return result


def main(argv):
    mode = argv[1] if len(argv) > 1 else 'identity'
    work = argv[2] if len(argv) > 2 else '/tmp/foammesh-gmsh-verify'
    data = {'identity': lambda: identity(),
            'cvt': lambda: cvt(work),
            'offscreen': lambda: offscreen(work),
            'offscreen-fltk': lambda: offscreen(work, use_fltk=True)}[mode]()
    print('__VERIFY__' + json.dumps(data))


if __name__ == '__main__':
    main(sys.argv)
