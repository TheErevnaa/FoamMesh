// FoamMesh shim for the handful of Gmsh mesh-element classes that
// featureSize() in the lifted automaticMeshSizeField.cpp uses: MVertex,
// MEdge (+ MEdgeLessThan), MFace, MTriangle, MTetrahedron. Only the members
// that function calls are provided; the geometry (edge/face numbering of the
// tetrahedron, circumcenter, circumradius, normals) follows Gmsh's
// MTetrahedron / MTriangle / MEdge / MFace exactly, because
// commonFaceTetFast() relies on Gmsh's face numbering.
//
// GPL-3.0-or-later, (C) FoamMesh contributors. Not upstream code.

#ifndef FOAMMESH_GMSH_MVERTEX_SHIM_H
#define FOAMMESH_GMSH_MVERTEX_SHIM_H

#include <cmath>
#include "SPoint3.h"
#include "SVector3.h"

class MVertex {
  double _x, _y, _z;
  int _num;
  static int &counter()
  {
    static int n = 0;
    return n;
  }

public:
  MVertex(double x, double y, double z) : _x(x), _y(y), _z(z)
  {
    _num = ++counter();
  }
  double &x() { return _x; }
  double &y() { return _y; }
  double &z() { return _z; }
  double x() const { return _x; }
  double y() const { return _y; }
  double z() const { return _z; }
  int getNum() const { return _num; }
  SPoint3 point() const { return SPoint3(_x, _y, _z); }
  double distance(const MVertex *v) const
  {
    double dx = _x - v->_x, dy = _y - v->_y, dz = _z - v->_z;
    return std::sqrt(dx * dx + dy * dy + dz * dz);
  }
};

class MEdge {
  MVertex *_v[2];
  // Gmsh orders the two vertices by number so that MEdge comparison is
  // orientation-free.
  MVertex *minV() const { return _v[0]->getNum() < _v[1]->getNum() ? _v[0] : _v[1]; }
  MVertex *maxV() const { return _v[0]->getNum() < _v[1]->getNum() ? _v[1] : _v[0]; }

public:
  MEdge() { _v[0] = _v[1] = nullptr; }
  MEdge(MVertex *v0, MVertex *v1)
  {
    _v[0] = v0;
    _v[1] = v1;
  }
  MVertex *getVertex(int i) const { return _v[i]; }
  MVertex *getMinVertex() const { return minV(); }
  MVertex *getMaxVertex() const { return maxV(); }
  double length() const { return _v[0]->distance(_v[1]); }
  SVector3 tangent() const
  {
    SVector3 t(_v[1]->x() - _v[0]->x(), _v[1]->y() - _v[0]->y(),
               _v[1]->z() - _v[0]->z());
    t.normalize();
    return t;
  }
  bool operator==(const MEdge &e) const
  {
    return minV() == e.minV() && maxV() == e.maxV();
  }
  bool operator!=(const MEdge &e) const { return !(*this == e); }
};

struct MEdgeLessThan {
  bool operator()(const MEdge &a, const MEdge &b) const
  {
    int a0 = a.getMinVertex()->getNum(), a1 = a.getMaxVertex()->getNum();
    int b0 = b.getMinVertex()->getNum(), b1 = b.getMaxVertex()->getNum();
    if(a0 != b0) return a0 < b0;
    return a1 < b1;
  }
};

class MFace {
  MVertex *_v[3];

public:
  MFace() { _v[0] = _v[1] = _v[2] = nullptr; }
  MFace(MVertex *v0, MVertex *v1, MVertex *v2)
  {
    _v[0] = v0;
    _v[1] = v1;
    _v[2] = v2;
  }
  MVertex *getVertex(int i) const { return _v[i]; }
  // Gmsh: normal3points(v0, v1, v2) = normalised (v1 - v0) x (v2 - v0).
  SVector3 normal() const
  {
    SVector3 a(_v[1]->x() - _v[0]->x(), _v[1]->y() - _v[0]->y(),
               _v[1]->z() - _v[0]->z());
    SVector3 b(_v[2]->x() - _v[0]->x(), _v[2]->y() - _v[0]->y(),
               _v[2]->z() - _v[0]->z());
    SVector3 n = crossprod(a, b);
    n.normalize();
    return n;
  }
};

class MTriangle {
  MVertex *_v[3];

public:
  MTriangle(MVertex *v0, MVertex *v1, MVertex *v2)
  {
    _v[0] = v0;
    _v[1] = v1;
    _v[2] = v2;
  }
  MVertex *getVertex(int i) const { return _v[i]; }
  // Gmsh MTriangle::getOuterRadius(): radius of the circumscribed circle,
  // a b c / (4 area).
  double getOuterRadius() const
  {
    double a = _v[0]->distance(_v[1]);
    double b = _v[1]->distance(_v[2]);
    double c = _v[2]->distance(_v[0]);
    double s = 0.5 * (a + b + c);
    double area2 = s * (s - a) * (s - b) * (s - c);
    double area = area2 > 0 ? std::sqrt(area2) : 0.0;
    if(area <= 0.0) return 1e300;
    return a * b * c / (4.0 * area);
  }
};

class MTetrahedron {
  MVertex *_v[4];

public:
  MTetrahedron(MVertex *v0, MVertex *v1, MVertex *v2, MVertex *v3)
  {
    _v[0] = v0;
    _v[1] = v1;
    _v[2] = v2;
    _v[3] = v3;
  }
  MVertex *getVertex(int i) const { return _v[i]; }
  // Gmsh MTetrahedron: edges_tetra and faces_tetra tables.
  MEdge getEdge(int num) const
  {
    static const int e[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};
    return MEdge(_v[e[num][0]], _v[e[num][1]]);
  }
  MFace getFace(int num) const
  {
    static const int f[4][3] = {{0, 2, 1}, {0, 1, 3}, {0, 3, 2}, {3, 1, 2}};
    return MFace(_v[f[num][0]], _v[f[num][1]], _v[f[num][2]]);
  }
  // Gmsh circumCenterXYZ: the centre equidistant from the four vertices. A
  // flat tetrahedron (which a Delaunay of a surface can contain) has none;
  // the centroid is returned then, as upstream's 3x3 inverse would be
  // meaningless as well.
  SPoint3 circumcenter() const
  {
    double ax = _v[1]->x() - _v[0]->x(), ay = _v[1]->y() - _v[0]->y(),
           az = _v[1]->z() - _v[0]->z();
    double bx = _v[2]->x() - _v[0]->x(), by = _v[2]->y() - _v[0]->y(),
           bz = _v[2]->z() - _v[0]->z();
    double cx = _v[3]->x() - _v[0]->x(), cy = _v[3]->y() - _v[0]->y(),
           cz = _v[3]->z() - _v[0]->z();
    double a2 = ax * ax + ay * ay + az * az;
    double b2 = bx * bx + by * by + bz * bz;
    double c2 = cx * cx + cy * cy + cz * cz;
    // b x c, c x a, a x b
    double bcx = by * cz - bz * cy, bcy = bz * cx - bx * cz,
           bcz = bx * cy - by * cx;
    double cax = cy * az - cz * ay, cay = cz * ax - cx * az,
           caz = cx * ay - cy * ax;
    double abx = ay * bz - az * by, aby = az * bx - ax * bz,
           abz = ax * by - ay * bx;
    double det = 2.0 * (ax * bcx + ay * bcy + az * bcz);
    if(std::fabs(det) < 1e-300) {
      return SPoint3(
        0.25 * (_v[0]->x() + _v[1]->x() + _v[2]->x() + _v[3]->x()),
        0.25 * (_v[0]->y() + _v[1]->y() + _v[2]->y() + _v[3]->y()),
        0.25 * (_v[0]->z() + _v[1]->z() + _v[2]->z() + _v[3]->z()));
    }
    double ox = (a2 * bcx + b2 * cax + c2 * abx) / det;
    double oy = (a2 * bcy + b2 * cay + c2 * aby) / det;
    double oz = (a2 * bcz + b2 * caz + c2 * abz) / det;
    return SPoint3(_v[0]->x() + ox, _v[0]->y() + oy, _v[0]->z() + oz);
  }
};

#endif
