// foammesh-sizefield: driver (see foammesh_sizefield.h). The sequence is
// upstream's automaticMeshSizeField::updateHXT() — per-surface curvature,
// Delaunay of the surface nodes, global normals, R-tree of triangles,
// forest create / feature size / refine / smoothing / estimate — with the
// GModel walk replaced by the assembled STL model.
//
// GPL-3.0-or-later, (C) FoamMesh contributors.

#include "foammesh_sizefield.h"

#include <chrono>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>
#include <algorithm>
#include <cstdint>
#include <string>
#include <array>
#include <limits>
#include <sys/resource.h>

#include "GmshMessage.h"
#include "curvature.h"
#include "robustPredicates.h"

extern "C" {
#include "hxt_tools.h"
#include "hxt_bbox.h"
#include "hxt_tetDelaunay.h"
#include "hxt_tetRefine.h"
}

#ifdef _OPENMP
#include <omp.h>
#endif

namespace {
double now()
{
  using namespace std::chrono;
  return duration<double>(steady_clock::now().time_since_epoch()).count();
}
} // namespace

FoamMeshSizeField::FoamMeshSizeField()
{
  for(int i = 0; i < 6; ++i) _bbox[i] = 0.0;
}

FoamMeshSizeField::~FoamMeshSizeField()
{
  if(_forest) forestDelete(&_forest);
  if(_forestOptions) forestOptionsDelete(&_forestOptions);
  if(_nodalCurvature) hxtFree(&_nodalCurvature);
  if(_mesh) hxtMeshDelete(&_mesh);
}

HXTStatus FoamMeshSizeField::compute(const std::vector<SurfaceInput> &surfaces,
                                     const SizeFieldSettings &settings)
{
  _settings = settings;
  double t0 = now(), t;

#ifdef _OPENMP
  if(settings.threads > 0) omp_set_num_threads(settings.threads);
#endif

  // --- assemble the model (Gmsh2Hxt's job)
  _names.clear();
  for(const SurfaceInput &s : surfaces) _names.push_back(s.name);
  assembleSurfaces(surfaces, _model);
  HXT_CHECK(hxtMeshCreate(&_mesh));
  HXT_CHECK(assembledToHxt(_model, _mesh));
  if(_mesh->vertices.num == 0) {
    Msg::Error("Surface mesh is empty");
    return HXT_STATUS_FAILED;
  }
  for(int i = 0; i < 6; ++i) _bbox[i] = _model.bbox[i];
  _times.read = now() - t0;

  Msg::Info("Gradation = %f", settings.gradation);
  Msg::Info("Node density = %d", settings.nPointsPerCircle);
  if(settings.nPointsPerGap > 0)
    Msg::Info("Layers per gap = %d", settings.nPointsPerGap);
  else
    Msg::Info("Layers per gap = %d : not detecting features.",
              settings.nPointsPerGap);

  // Shewchuk's predicates (orient3d in featureSize) need their error bounds
  // sized to the model; Gmsh does this once at start-up from the model box.
  robustPredicates::exactinit(_bbox[3] - _bbox[0], _bbox[4] - _bbox[1],
                              _bbox[5] - _bbox[2]);

  // --- curvature per surface (upstream: per GFace, so a sharp edge between
  // two surfaces does not smear the curvature across it)
  t = now();
  size_t numVertices = _mesh->vertices.num;
  HXT_CHECK(hxtMalloc(&_nodalCurvature, 6 * numVertices * sizeof(double)));
  for(size_t i = 0; i < 6 * numVertices; ++i) _nodalCurvature[i] = NAN;

  for(size_t s = 0; s < surfaces.size(); ++s) {
    const SurfaceInput &in = surfaces[s];
    if(in.tris.empty()) {
      Msg::Info("Skipping curvature computation on surface %d with 0 element",
                (int)s);
      continue;
    }
    std::vector<SPoint3> nodes(in.xyz.size() / 3);
    for(size_t i = 0; i < nodes.size(); ++i)
      nodes[i] = SPoint3(in.xyz[3 * i], in.xyz[3 * i + 1], in.xyz[3 * i + 2]);
    std::vector<int> tris(in.tris.begin(), in.tris.end());
    std::vector<std::pair<SVector3, SVector3> > curv;
    CurvatureRusinkiewicz(tris, nodes, curv);
    const std::vector<uint32_t> &map = _model.localToGlobal[s];
    for(size_t i = 0; i < nodes.size() && i < curv.size(); ++i) {
      uint64_t g = map[i];
      _nodalCurvature[6 * g + 0] = curv[i].first[0];
      _nodalCurvature[6 * g + 1] = curv[i].first[1];
      _nodalCurvature[6 * g + 2] = curv[i].first[2];
      _nodalCurvature[6 * g + 3] = curv[i].second[0];
      _nodalCurvature[6 * g + 4] = curv[i].second[1];
      _nodalCurvature[6 * g + 5] = curv[i].second[2];
    }
  }
  // A node the curvature never reached (degenerate surface) counts as flat.
  for(size_t i = 0; i < 6 * numVertices; ++i)
    if(std::isnan(_nodalCurvature[i])) _nodalCurvature[i] = 0.0;
  _times.curvature = now() - t;

  // --- Delaunay tetrahedrization of the (empty) surface mesh
  t = now();
  HXTDelaunayOptions delaunayOptions = {nullptr, nullptr, 0, 0, 0, 2, 1, 0, 0, 0};
  delaunayOptions.delaunayThreads = settings.threads;
  HXT_CHECK(hxtEmptyMesh(_mesh, &delaunayOptions));
  _times.delaunay = now() - t;

  // --- normals on the whole surface
  t = now();
  {
    std::vector<int> tris(3 * _mesh->triangles.num, 0);
    for(size_t i = 0; i < _mesh->triangles.num; ++i) {
      tris[3 * i + 0] = _mesh->triangles.node[3 * i + 0];
      tris[3 * i + 1] = _mesh->triangles.node[3 * i + 1];
      tris[3 * i + 2] = _mesh->triangles.node[3 * i + 2];
    }
    std::vector<SPoint3> nodes(_mesh->vertices.num);
    for(size_t i = 0; i < _mesh->vertices.num; ++i)
      nodes[i] = SPoint3(_mesh->vertices.coord[4 * i + 0],
                         _mesh->vertices.coord[4 * i + 1],
                         _mesh->vertices.coord[4 * i + 2]);
    std::vector<std::pair<SVector3, SVector3> > curv;
    _nodeNormals.clear();
    _nodeNormals.assign(3 * _mesh->vertices.num, 0.0); // the 4-argument CurvatureRusinkiewicz accumulates into it
    CurvatureRusinkiewicz(tris, nodes, curv, _nodeNormals);
    if(_nodeNormals.size() < 3 * _mesh->vertices.num)
      _nodeNormals.resize(3 * _mesh->vertices.num, 0.0);
  }
  _times.normals = now() - t;

  // --- R-tree of the triangle boxes
  t = now();
  {
    HXTBbox bbox_triangle;
    for(uint64_t i = 0; i < _mesh->triangles.num; ++i) {
      hxtBboxInit(&bbox_triangle);
      for(uint64_t j = 0; j < 3; ++j) {
        double coord[3];
        uint32_t node = _mesh->triangles.node[3 * i + j];
        for(uint32_t k = 0; k < 3; ++k)
          coord[k] = _mesh->vertices.coord[(size_t)4 * node + k];
        hxtBboxAddOne(&bbox_triangle, coord);
      }
      _triRTree.Insert(bbox_triangle.min, bbox_triangle.max, i);
    }
  }
  _times.rtree = now() - t;

  // --- defaults from the model size, as upstream
  double hbulk = settings.hbulk, hmin = settings.hmin, hmax = settings.hmax;
  if(hbulk < 0 || hmin < 0) {
    double L = -1.0;
    for(int i = 0; i < 3; ++i) L = fmax(L, _bbox[i + 3] - _bbox[i]);
    if(hbulk < 0) hbulk = L / 20.;
    if(hmin < 0) hmin = L / 1000.;
    Msg::Info("Bulk size is set to %f", hbulk);
    Msg::Info("Min  size is set to %f", hmin);
  }
  if(hmax < 0) hmax = hbulk;
  _settings.hbulk = hbulk;
  _settings.hmin = hmin;
  _settings.hmax = hmax;

  _featureSize.assign(_mesh->vertices.num, DBL_MAX);

  HXT_CHECK(forestOptionsCreate(&_forestOptions));
  std::memset(_forestOptions, 0, sizeof(ForestOptions));
  _forestOptions->dim = 3;
  _forestOptions->hmax = hmax;
  _forestOptions->hmin = hmin;
  _forestOptions->hbulk = hbulk;
  _forestOptions->gradation = settings.gradation;
  _forestOptions->nodePerTwoPi = settings.nPointsPerCircle;
  _forestOptions->nodePerGap = settings.nPointsPerGap;
  _forestOptions->bbox = _bbox;
  _forestOptions->sizeFunction = nullptr;
  _forestOptions->nodalCurvature = _nodalCurvature;
  _forestOptions->nodeNormals = _nodeNormals.data();
  _forestOptions->featureSizeAtVertices = &_featureSize;
  _forestOptions->triRTree = &_triRTree;
  _forestOptions->mesh = _mesh;
  _forestOptions->debugPos = settings.debugPos ? 1 : 0;
  _forestOptions->debugDir = settings.debugDir.empty() ? nullptr :
                                                        settings.debugDir.c_str();

  HXT_CHECK(forestCreate(0, nullptr, &_forest, nullptr, _forestOptions));

  if(settings.nPointsPerGap > 0) {
    t = now();
    Msg::Info("Detecting features...");
    HXT_CHECK(featureSize(_forest));
    _times.features = now() - t;
  }
  if(settings.nPointsPerCircle > 0) {
    t = now();
    Msg::Info("Refining octree...");
    HXT_CHECK(forestRefine(_forest));
    _times.refine = now() - t;
  }
  if(settings.smoothing) {
    t = now();
    Msg::Info("Smoothing size gradient...");
    HXT_CHECK(forestSizeSmoothing(_forest));
    _times.smoothing = now() - t;
  }

  _estimate = 0.0;
  HXT_CHECK(hxtOctreeElementEstimation(_forest, &_estimate));
  Msg::Info("Estimated number of tetrahedra in the bounding box : %ld",
            (long)ceil(_estimate));

  _leaves.clear();
  _leafPoints.clear();
  HXT_CHECK(forestCollectLeaves(_forest, &_leaves, &_leafPoints));
  Msg::Info("Octree leaves : %ld (%ld corner points)", (long)_leaves.size(),
            (long)(_leafPoints.size() / 3));

  HXT_CHECK(forestRootCube(_forest, _rootOrigin, &_rootLength));
  if(settings.aniso) {
    t = now();
    Msg::Info("Computing the anisotropic metric of the leaves...");
    computeMetrics();
    _times.aniso = now() - t;
  }

  _times.total = now() - t0;
  return HXT_STATUS_OK;
}

namespace {
// Triangle / axis-aligned box overlap (separating axis test, Akenine-Moller).
bool triBoxOverlap(const double c[3], const double h[3], const double v0[3],
                   const double v1[3], const double v2[3])
{
  double v[3][3];
  for(int k = 0; k < 3; ++k) {
    v[0][k] = v0[k] - c[k];
    v[1][k] = v1[k] - c[k];
    v[2][k] = v2[k] - c[k];
  }
  for(int a = 0; a < 3; ++a) { // box face normals
    double mn = std::min(std::min(v[0][a], v[1][a]), v[2][a]);
    double mx = std::max(std::max(v[0][a], v[1][a]), v[2][a]);
    if(mn > h[a] || mx < -h[a]) return false;
  }
  double e[3][3];
  for(int k = 0; k < 3; ++k) {
    e[0][k] = v[1][k] - v[0][k];
    e[1][k] = v[2][k] - v[1][k];
    e[2][k] = v[0][k] - v[2][k];
  }
  for(int i = 0; i < 3; ++i) // nine edge cross products
    for(int a = 0; a < 3; ++a) {
      double ax[3] = {0, 0, 0};
      ax[a] = 1;
      double n[3] = {ax[1] * e[i][2] - ax[2] * e[i][1],
                     ax[2] * e[i][0] - ax[0] * e[i][2],
                     ax[0] * e[i][1] - ax[1] * e[i][0]};
      double p0 = n[0] * v[0][0] + n[1] * v[0][1] + n[2] * v[0][2];
      double p1 = n[0] * v[1][0] + n[1] * v[1][1] + n[2] * v[1][2];
      double p2 = n[0] * v[2][0] + n[1] * v[2][1] + n[2] * v[2][2];
      double r = h[0] * std::fabs(n[0]) + h[1] * std::fabs(n[1]) +
                 h[2] * std::fabs(n[2]);
      if(std::min(std::min(p0, p1), p2) > r ||
         std::max(std::max(p0, p1), p2) < -r)
        return false;
    }
  double nrm[3] = {e[0][1] * e[1][2] - e[0][2] * e[1][1],
                   e[0][2] * e[1][0] - e[0][0] * e[1][2],
                   e[0][0] * e[1][1] - e[0][1] * e[1][0]};
  double d = nrm[0] * v[0][0] + nrm[1] * v[0][1] + nrm[2] * v[0][2];
  double r = h[0] * std::fabs(nrm[0]) + h[1] * std::fabs(nrm[1]) +
             h[2] * std::fabs(nrm[2]);
  return std::fabs(d) <= r;
}

struct MetricSearch {
  const HXTMesh *mesh;
  const double *c, *h;
  int best = -1;
  double bestDist = DBL_MAX;
};

bool metricCallback(uint64_t tri, void *ctx)
{
  MetricSearch *m = (MetricSearch *)ctx;
  const uint32_t *nd = &m->mesh->triangles.node[3 * tri];
  double p[3][3];
  for(int j = 0; j < 3; ++j)
    for(int k = 0; k < 3; ++k)
      p[j][k] = m->mesh->vertices.coord[4 * (size_t)nd[j] + k];
  if(!triBoxOverlap(m->c, m->h, p[0], p[1], p[2])) return true;
  double g[3];
  for(int k = 0; k < 3; ++k)
    g[k] = (p[0][k] + p[1][k] + p[2][k]) / 3.0 - m->c[k];
  double dist = g[0] * g[0] + g[1] * g[1] + g[2] * g[2];
  if(dist < m->bestDist) {
    m->bestDist = dist;
    m->best = (int)tri;
  }
  return true;
}

double vlen(const double *v)
{
  return std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
}
} // namespace

// The FoamMesh anisotropy rule (upstream's assignSizeAfterRefinementAniso is
// under #if 0): in a leaf that a surface triangle crosses, the size along the
// surface normal is the leaf's smoothed isotropic size; along the two
// principal curvature directions k1 >= k2 it is max(size, 2 pi / (n k1)) and
// max(size, 2 pi / ((n / 4) k2)) with n the nodes per 2 pi, never above hmax.
// The curvature, normal and directions come from the vertex of the nearest
// crossing triangle that is closest to the leaf centre. Other leaves get the
// isotropic metric 1 / size^2 on the identity.
void FoamMeshSizeField::computeMetrics()
{
  const double twoPi = 2.0 * M_PI;
  double n = std::max(1, _settings.nPointsPerCircle);
  RTree<uint64_t, double, 3> &tree =
    const_cast<RTree<uint64_t, double, 3> &>(_triRTree);
#pragma omp parallel for schedule(dynamic, 1024)
  for(long i = 0; i < (long)_leaves.size(); ++i) {
    forest_leaf_t &l = _leaves[i];
    double iso = 1.0 / (l.size * l.size);
    double c[3] = {l.center[0], l.center[1], l.center[2]};
    double half = 0.5 * _rootLength / (double)(1 << l.level);
    double h[3] = {half, half, half};
    double lo[3] = {c[0] - h[0], c[1] - h[1], c[2] - h[2]};
    double hi[3] = {c[0] + h[0], c[1] + h[1], c[2] + h[2]};
    MetricSearch ms;
    ms.mesh = _mesh;
    ms.c = c;
    ms.h = h;
    tree.Search(lo, hi, metricCallback, &ms);
    double M[6] = {iso, 0, 0, iso, 0, iso};
    if(ms.best >= 0) {
      const uint32_t *nd = &_mesh->triangles.node[3 * (size_t)ms.best];
      uint32_t g = nd[0];
      double bd = DBL_MAX;
      for(int j = 0; j < 3; ++j) {
        double d = 0;
        for(int k = 0; k < 3; ++k) {
          double x = _mesh->vertices.coord[4 * (size_t)nd[j] + k] - c[k];
          d += x * x;
        }
        if(d < bd) {
          bd = d;
          g = nd[j];
        }
      }
      const double *c1 = _nodalCurvature + 6 * (size_t)g, *c2 = c1 + 3;
      double k1 = vlen(c1), k2 = vlen(c2);
      const double *d1 = c1;
      if(k2 > k1) {
        std::swap(k1, k2);
        d1 = c2;
      }
      double nrm[3] = {_nodeNormals[3 * (size_t)g], _nodeNormals[3 * (size_t)g + 1],
                       _nodeNormals[3 * (size_t)g + 2]};
      double nl = vlen(nrm);
      double t1[3] = {0, 0, 0}, t2[3] = {0, 0, 0};
      bool ok = nl > 1e-12 && k1 > 1e-12;
      if(ok) {
        for(int k = 0; k < 3; ++k) nrm[k] /= nl;
        double dot = d1[0] * nrm[0] + d1[1] * nrm[1] + d1[2] * nrm[2];
        for(int k = 0; k < 3; ++k) t1[k] = d1[k] - dot * nrm[k];
        double tl = vlen(t1);
        ok = tl > 1e-12;
        if(ok) {
          for(int k = 0; k < 3; ++k) t1[k] /= tl;
          t2[0] = nrm[1] * t1[2] - nrm[2] * t1[1];
          t2[1] = nrm[2] * t1[0] - nrm[0] * t1[2];
          t2[2] = nrm[0] * t1[1] - nrm[1] * t1[0];
        }
      }
      if(ok) {
        double hmax = _settings.hmax;
        double s1 = std::min(hmax, std::max(l.size, twoPi / (n * k1)));
        double s2 = k2 > 1e-12 ?
                      std::min(hmax, std::max(l.size, twoPi / ((n / 4.0) * k2))) :
                      hmax;
        s1 = std::max(s1, l.size);
        s2 = std::max(s2, l.size);
        double w[3] = {iso, 1.0 / (s1 * s1), 1.0 / (s2 * s2)};
        const double *ax[3] = {nrm, t1, t2};
        double xx = 0, xy = 0, xz = 0, yy = 0, yz = 0, zz = 0;
        for(int a = 0; a < 3; ++a) {
          xx += w[a] * ax[a][0] * ax[a][0];
          xy += w[a] * ax[a][0] * ax[a][1];
          xz += w[a] * ax[a][0] * ax[a][2];
          yy += w[a] * ax[a][1] * ax[a][1];
          yz += w[a] * ax[a][1] * ax[a][2];
          zz += w[a] * ax[a][2] * ax[a][2];
        }
        M[0] = xx;
        M[1] = xy;
        M[2] = xz;
        M[3] = yy;
        M[4] = yz;
        M[5] = zz;
      }
    }
    for(int k = 0; k < 6; ++k) l.m[k] = M[k];
    if(ms.best >= 0) {
#pragma omp atomic
      _anisoCrossed++;
    }
  }
}

double FoamMeshSizeField::size(double x, double y, double z) const
{
  double val = 1.e17;
  if(!_forest) return val;
  forestSearchOne(_forest, x, y, z, &val, true);
  return val;
}

HXTStatus FoamMeshSizeField::writePos(const std::string &path) const
{
  return forestExport(_forest, path.c_str());
}

HXTStatus FoamMeshSizeField::writeP4est(const std::string &prefix) const
{
  std::string forestFile = prefix + ".p4est", dataFile = prefix + ".data";
  return forestSave(_forest, forestFile.c_str(), dataFile.c_str());
}

namespace {
// One appended-binary DataArray: the header line goes in the XML, the bytes
// (UInt64 byte count, then the raw values) go after the underscore.
struct VtuArrays {
  std::vector<std::string> xml;
  std::vector<std::vector<char> > blobs;
  uint64_t offset = 0;
  template <class T>
  void add(const char *type, const char *name, int comps, const std::vector<T> &v)
  {
    char line[256];
    std::snprintf(line, sizeof line,
                  "<DataArray type=\"%s\" Name=\"%s\" NumberOfComponents=\"%d\" "
                  "format=\"appended\" offset=\"%llu\"/>\n",
                  type, name, comps, (unsigned long long)offset);
    xml.push_back(line);
    uint64_t bytes = (uint64_t)v.size() * sizeof(T);
    std::vector<char> blob(sizeof(uint64_t) + bytes);
    std::memcpy(blob.data(), &bytes, sizeof(uint64_t));
    if(bytes) std::memcpy(blob.data() + sizeof(uint64_t), v.data(), bytes);
    offset += blob.size();
    blobs.push_back(std::move(blob));
  }
};
} // namespace

bool FoamMeshSizeField::writeVtu(const std::string &path) const
{
  FILE *f = std::fopen(path.c_str(), "wb");
  if(!f) return false;
  size_t n = _leaves.size(), np = _leafPoints.size() / 3;
  VtuArrays pointsA, cellsA, dataA;
  pointsA.add("Float64", "Points", 3, _leafPoints);
  std::vector<int32_t> conn(8 * n), offs(n);
  std::vector<uint8_t> types(n, 12);
  std::vector<double> size(n), edge(n);
  std::vector<int32_t> level(n);
  for(size_t i = 0; i < n; ++i) {
    for(int c = 0; c < 8; ++c) conn[8 * i + c] = _leaves[i].corner[c];
    offs[i] = 8 * (int32_t)(i + 1);
    size[i] = _leaves[i].size;
    edge[i] = _leaves[i].h;
    level[i] = _leaves[i].level;
  }
  cellsA.offset = pointsA.offset;
  cellsA.add("Int32", "connectivity", 1, conn);
  cellsA.add("Int32", "offsets", 1, offs);
  cellsA.add("UInt8", "types", 1, types);
  dataA.offset = cellsA.offset;
  dataA.add("Float64", "size", 1, size);
  dataA.add("Float64", "leaf_edge", 1, edge);
  dataA.add("Int32", "level", 1, level);

  std::fprintf(f, "<?xml version=\"1.0\"?>\n<VTKFile type=\"UnstructuredGrid\" "
                  "version=\"1.0\" byte_order=\"LittleEndian\" header_type=\"UInt64\">\n"
                  "<UnstructuredGrid>\n");
  std::fprintf(f, "<Piece NumberOfPoints=\"%zu\" NumberOfCells=\"%zu\">\n", np, n);
  std::fprintf(f, "<Points>\n%s</Points>\n", pointsA.xml[0].c_str());
  std::fprintf(f, "<Cells>\n%s%s%s</Cells>\n", cellsA.xml[0].c_str(),
               cellsA.xml[1].c_str(), cellsA.xml[2].c_str());
  std::fprintf(f, "<CellData Scalars=\"size\">\n%s%s%s</CellData>\n",
               dataA.xml[0].c_str(), dataA.xml[1].c_str(), dataA.xml[2].c_str());
  std::fprintf(f, "</Piece>\n</UnstructuredGrid>\n<AppendedData encoding=\"raw\">\n_");
  for(const VtuArrays *a : {&pointsA, &cellsA, &dataA})
    for(const std::vector<char> &blob : a->blobs)
      std::fwrite(blob.data(), 1, blob.size(), f);
  std::fprintf(f, "\n</AppendedData>\n</VTKFile>\n");
  std::fclose(f);
  return true;
}

namespace {
// NumPy .npy version 1.0: magic, 2-byte header length, an ASCII dict padded
// with spaces to a multiple of 64 bytes and ending in a newline, then the raw
// little-endian values in C order.
bool writeNpy1(const std::string &path, const char *descr,
               const std::vector<size_t> &shape, const void *data, size_t bytes)
{
  FILE *f = std::fopen(path.c_str(), "wb");
  if(!f) return false;
  std::string shp = "(";
  for(size_t i = 0; i < shape.size(); ++i) {
    shp += std::to_string(shape[i]);
    if(i + 1 < shape.size()) shp += ", ";
  }
  shp += shape.size() == 1 ? ",)" : ")";
  std::string dict = std::string("{'descr': '") + descr +
                     "', 'fortran_order': False, 'shape': " + shp + ", }";
  size_t total = 10 + dict.size() + 1;
  size_t pad = (64 - total % 64) % 64;
  dict += std::string(pad, ' ') + "\n";
  const unsigned char magic[8] = {0x93, 'N', 'U', 'M', 'P', 'Y', 1, 0};
  std::fwrite(magic, 1, 8, f);
  uint16_t hl = (uint16_t)dict.size();
  std::fwrite(&hl, 2, 1, f);
  std::fwrite(dict.data(), 1, dict.size(), f);
  if(bytes) std::fwrite(data, 1, bytes, f);
  bool ok = std::ferror(f) == 0;
  std::fclose(f);
  return ok;
}
template <class T>
bool npy(const std::string &dir, const char *name, const char *descr,
         const std::vector<size_t> &shape, const std::vector<T> &v)
{
  return writeNpy1(dir + "/" + name + ".npy", descr, shape, v.data(),
                   v.size() * sizeof(T));
}
} // namespace

bool FoamMeshSizeField::writeNpy(const std::string &dir) const
{
  size_t n = _leaves.size(), np = _leafPoints.size() / 3;
  std::vector<uint32_t> ijk(3 * n);
  std::vector<uint8_t> level(n);
  std::vector<float> size(n), nsize(np, std::numeric_limits<float>::max());
  std::vector<int32_t> hex(8 * n);
  for(size_t i = 0; i < n; ++i) {
    const forest_leaf_t &l = _leaves[i];
    for(int k = 0; k < 3; ++k) ijk[3 * i + k] = (uint32_t)l.xyz[k];
    level[i] = (uint8_t)l.level;
    size[i] = (float)l.size;
    for(int c = 0; c < 8; ++c) {
      hex[8 * i + c] = (int32_t)l.corner[c];
      nsize[l.corner[c]] = std::min(nsize[l.corner[c]], size[i]);
    }
  }
  bool ok = true;
  ok &= npy(dir, "leaves_ijk", "<u4", {n, 3}, ijk);
  ok &= npy(dir, "leaves_level", "|u1", {n}, level);
  ok &= npy(dir, "leaves_size", "<f4", {n}, size);
  ok &= npy(dir, "nodes_xyz", "<f8", {np, 3}, _leafPoints);
  ok &= npy(dir, "nodes_size", "<f4", {np}, nsize);
  ok &= npy(dir, "hexes", "<i4", {n, 8}, hex);
  if(_settings.aniso) {
    std::vector<float> m(6 * n);
    for(size_t i = 0; i < n; ++i)
      for(int k = 0; k < 6; ++k) m[6 * i + k] = (float)_leaves[i].m[k];
    ok &= npy(dir, "leaves_metric", "<f4", {n, 6}, m);
  }
  return ok;
}

bool FoamMeshSizeField::writeSurfaceVtp(const std::string &path) const
{
  FILE *f = std::fopen(path.c_str(), "w");
  if(!f) return false;
  size_t nv = _mesh->vertices.num, nt = _mesh->triangles.num;
  std::fprintf(f, "<?xml version=\"1.0\"?>\n<VTKFile type=\"PolyData\" version=\"0.1\" "
                  "byte_order=\"LittleEndian\">\n<PolyData>\n");
  std::fprintf(f, "<Piece NumberOfPoints=\"%zu\" NumberOfPolys=\"%zu\">\n", nv, nt);
  std::fprintf(f, "<Points><DataArray type=\"Float64\" NumberOfComponents=\"3\" "
                  "format=\"ascii\">\n");
  for(size_t i = 0; i < nv; ++i)
    std::fprintf(f, "%.10g %.10g %.10g\n", _mesh->vertices.coord[4 * i],
                 _mesh->vertices.coord[4 * i + 1], _mesh->vertices.coord[4 * i + 2]);
  std::fprintf(f, "</DataArray></Points>\n<PointData Scalars=\"size\">\n");

  auto scalar = [&](const char *name, auto value) {
    std::fprintf(f, "<DataArray type=\"Float64\" Name=\"%s\" format=\"ascii\">\n", name);
    for(size_t i = 0; i < nv; ++i) std::fprintf(f, "%.8g\n", value(i));
    std::fprintf(f, "</DataArray>\n");
  };
  auto vec = [&](const char *name, auto value) {
    std::fprintf(f, "<DataArray type=\"Float64\" Name=\"%s\" NumberOfComponents=\"3\" "
                    "format=\"ascii\">\n", name);
    for(size_t i = 0; i < nv; ++i) {
      double v[3];
      value(i, v);
      std::fprintf(f, "%.8g %.8g %.8g\n", v[0], v[1], v[2]);
    }
    std::fprintf(f, "</DataArray>\n");
  };
  auto norm3 = [](const double *v) {
    return std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
  };
  scalar("size", [&](size_t i) {
    return size(_mesh->vertices.coord[4 * i], _mesh->vertices.coord[4 * i + 1],
                _mesh->vertices.coord[4 * i + 2]);
  });
  scalar("curvature_max", [&](size_t i) { return norm3(_nodalCurvature + 6 * i); });
  scalar("curvature_min", [&](size_t i) { return norm3(_nodalCurvature + 6 * i + 3); });
  scalar("feature_size", [&](size_t i) {
    double v = _featureSize[i];
    return v == DBL_MAX ? -1.0 : v;
  });
  vec("dir_max", [&](size_t i, double *v) {
    for(int k = 0; k < 3; ++k) v[k] = _nodalCurvature[6 * i + k];
  });
  vec("dir_min", [&](size_t i, double *v) {
    for(int k = 0; k < 3; ++k) v[k] = _nodalCurvature[6 * i + 3 + k];
  });
  vec("normal", [&](size_t i, double *v) {
    for(int k = 0; k < 3; ++k) v[k] = _nodeNormals[3 * i + k];
  });
  std::fprintf(f, "</PointData>\n<CellData Scalars=\"surface\">\n<DataArray "
                  "type=\"Int32\" Name=\"surface\" format=\"ascii\">\n");
  for(size_t t = 0; t < nt; ++t) std::fprintf(f, "%u\n", _mesh->triangles.color[t]);
  std::fprintf(f, "</DataArray>\n</CellData>\n<Polys>\n<DataArray type=\"Int64\" "
                  "Name=\"connectivity\" format=\"ascii\">\n");
  for(size_t t = 0; t < nt; ++t)
    std::fprintf(f, "%u %u %u\n", _mesh->triangles.node[3 * t],
                 _mesh->triangles.node[3 * t + 1], _mesh->triangles.node[3 * t + 2]);
  std::fprintf(f, "</DataArray>\n<DataArray type=\"Int64\" Name=\"offsets\" "
                  "format=\"ascii\">\n");
  for(size_t t = 0; t < nt; ++t) std::fprintf(f, "%zu\n", 3 * (t + 1));
  std::fprintf(f, "</DataArray>\n</Polys>\n</Piece>\n</PolyData>\n</VTKFile>\n");
  std::fclose(f);
  return true;
}

bool FoamMeshSizeField::writeJson(const std::string &path,
                                  const std::vector<std::string> &inputs) const
{
  FILE *f = std::fopen(path.c_str(), "w");
  if(!f) return false;
  const SizeFieldSettings &s = _settings;
  std::fprintf(f, "{\n  \"tool\": \"foammesh-sizefield\",\n  \"version\": "
                  "\"%s\",\n  \"upstream\": "
                  "\"gmsh-4.15.2 AutomaticMeshSizeField\",\n",
               FOAMMESH_SIZEFIELD_VERSION);
  std::fprintf(f, "  \"settings\": {\"hmin\": %.10g, \"hmax\": %.10g, \"hbulk\": %.10g, "
                  "\"gradation\": %.10g, \"nPointsPerCircle\": %d, \"nPointsPerGap\": %d, "
                  "\"smoothing\": %s, \"threads\": %d, \"aniso\": %s},\n",
               s.hmin, s.hmax, s.hbulk, s.gradation, s.nPointsPerCircle,
               s.nPointsPerGap, s.smoothing ? "true" : "false", s.threads,
               s.aniso ? "true" : "false");
  std::fprintf(f, "  \"bbox\": [%.10g, %.10g, %.10g, %.10g, %.10g, %.10g],\n", _bbox[0],
               _bbox[1], _bbox[2], _bbox[3], _bbox[4], _bbox[5]);
  std::fprintf(f, "  \"vertices\": %u,\n  \"triangles\": %lu,\n  \"leaves\": %zu,\n",
               _mesh->vertices.num, (unsigned long)_mesh->triangles.num, _leaves.size());
  std::fprintf(f, "  \"root\": {\"origin\": [%.10g, %.10g, %.10g], \"length\": %.10g, "
                  "\"max_level\": %d},\n",
               _rootOrigin[0], _rootOrigin[1], _rootOrigin[2], _rootLength,
               (int)P4EST_MAXLEVEL);
  {
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    std::fprintf(f, "  \"peak_rss_mb\": %.1f,\n", ru.ru_maxrss / 1024.0);
    std::map<int, size_t> byLevel;
    for(const forest_leaf_t &l : _leaves) byLevel[l.level]++;
    std::fprintf(f, "  \"leaves_by_level\": {");
    bool first = true;
    for(const auto &kv : byLevel) {
      std::fprintf(f, "%s\"%d\": %zu", first ? "" : ", ", kv.first, kv.second);
      first = false;
    }
    std::fprintf(f, "},\n");
  }
  if(_settings.aniso)
    std::fprintf(f, "  \"aniso_leaves_crossed_by_surface\": %zu,\n", _anisoCrossed);
  std::fprintf(f, "  \"estimated_tets_in_octree_box\": %.0f,\n", std::ceil(_estimate));
  // Hex-cell equivalent of the same volume at the same size (a cube of edge
  // size holds 6 sqrt(2) tets of that edge): what a hex mesher would need.
  std::fprintf(f, "  \"estimated_hex_cells_in_octree_box\": %.0f,\n",
               std::ceil(_estimate / (6.0 * std::sqrt(2.0))));
  const StageTimes &t = _times;
  std::fprintf(f, "  \"seconds\": {\"read\": %.3f, \"curvature\": %.3f, \"delaunay\": %.3f, "
                  "\"normals\": %.3f, \"rtree\": %.3f, \"features\": %.3f, \"refine\": "
                  "%.3f, \"smoothing\": %.3f, \"aniso\": %.3f, \"total\": %.3f},\n",
               t.read, t.curvature, t.delaunay, t.normals, t.rtree, t.features,
               t.refine, t.smoothing, t.aniso, t.total);
  std::fprintf(f, "  \"inputs\": [");
  for(size_t i = 0; i < inputs.size(); ++i)
    std::fprintf(f, "%s\"%s\"", i ? ", " : "", inputs[i].c_str());
  std::fprintf(f, "],\n  \"per_surface\": [\n");
  // Size, feature-size and curvature quantiles at each surface's own vertices.
  auto quant = [](std::vector<double> &v, double p) {
    if(v.empty()) return -1.0;
    std::sort(v.begin(), v.end());
    return v[(size_t)std::floor(p * (v.size() - 1))];
  };
  for(size_t si = 0; si < _names.size(); ++si) {
    std::vector<double> v, fs, kc;
    double fcount = 0;
    for(uint32_t g : _model.localToGlobal[si]) {
      v.push_back(size(_mesh->vertices.coord[4 * g], _mesh->vertices.coord[4 * g + 1],
                       _mesh->vertices.coord[4 * g + 2]));
      if(_featureSize[g] != DBL_MAX) {
        fs.push_back(_featureSize[g]);
        fcount += 1;
      }
      kc.push_back(std::max(vlen(_nodalCurvature + 6 * (size_t)g),
                            vlen(_nodalCurvature + 6 * (size_t)g + 3)));
    }
    std::fprintf(f, "    {\"name\": \"%s\", \"vertices\": %zu, \"size_min\": %.8g, "
                    "\"size_p10\": %.8g, \"size_p50\": %.8g, \"size_p90\": %.8g, "
                    "\"size_max\": %.8g, \"feature_size_min\": %.8g, "
                    "\"feature_size_p10\": %.8g, \"feature_size_p50\": %.8g, "
                    "\"curvature_p50\": %.8g, \"curvature_p90\": %.8g, "
                    "\"curvature_max\": %.8g, \"feature_vertices\": %.0f}%s\n",
                 _names[si].c_str(), v.size(), quant(v, 0.0), quant(v, 0.1),
                 quant(v, 0.5), quant(v, 0.9), quant(v, 1.0), quant(fs, 0.0),
                 quant(fs, 0.1), quant(fs, 0.5), quant(kc, 0.5), quant(kc, 0.9),
                 quant(kc, 1.0), fcount, si + 1 < _names.size() ? "," : "");
  }
  std::fprintf(f, "  ]\n}\n");
  std::fclose(f);
  return true;
}
