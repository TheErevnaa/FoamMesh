// FoamMesh shim for Gmsh's STensor3.h, reduced to the SMetric3 members the
// lifted size-field code touches on its compiled paths (the anisotropic
// search keeps a metric per leaf; the metric-building code is under `#if 0`
// upstream). Gmsh's SMetric3 needs fullMatrix (eigen solves); none of that
// is reachable here, so this is a plain symmetric 3x3 store.
//
// GPL-3.0-or-later, (C) FoamMesh contributors. Not upstream code.

#ifndef FOAMMESH_GMSH_STENSOR3_SHIM_H
#define FOAMMESH_GMSH_STENSOR3_SHIM_H

#include <cstdio>
#include "SVector3.h"

class SMetric3 {
  // xx, xy, xz, yy, yz, zz (Gmsh's _val layout: 0:xx 1:xy 2:yy 3:xz 4:yz 5:zz
  // differs; nothing here indexes it directly)
  double _m[6];

public:
  SMetric3(double v = 1.0)
  {
    _m[0] = _m[3] = _m[5] = v;
    _m[1] = _m[2] = _m[4] = 0.0;
  }
  SMetric3(double l1, double l2, double l3, const SVector3 &t1,
           const SVector3 &t2, const SVector3 &t3)
  {
    // M = sum_i l_i t_i t_i^T
    const SVector3 *t[3] = {&t1, &t2, &t3};
    double l[3] = {l1, l2, l3};
    for(int k = 0; k < 6; ++k) _m[k] = 0.0;
    for(int i = 0; i < 3; ++i) {
      const SVector3 &v = *t[i];
      _m[0] += l[i] * v(0) * v(0);
      _m[1] += l[i] * v(0) * v(1);
      _m[2] += l[i] * v(0) * v(2);
      _m[3] += l[i] * v(1) * v(1);
      _m[4] += l[i] * v(1) * v(2);
      _m[5] += l[i] * v(2) * v(2);
    }
  }
  double operator()(int i, int j) const
  {
    static const int idx[3][3] = {{0, 1, 2}, {1, 3, 4}, {2, 4, 5}};
    return _m[idx[i][j]];
  }
  double determinant() const
  {
    double a = _m[0], b = _m[1], c = _m[2], d = _m[3], e = _m[4], f = _m[5];
    return a * (d * f - e * e) - b * (b * f - e * c) + c * (b * e - d * c);
  }
  void print(const char *name) const
  {
    std::printf("SMetric3 %s = [%g %g %g; %g %g %g; %g %g %g]\n", name, _m[0],
                _m[1], _m[2], _m[1], _m[3], _m[4], _m[2], _m[4], _m[5]);
  }
};

#endif
