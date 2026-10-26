// foammesh-sizefield: the driver that replaces Gmsh's automaticMeshSizeField
// Field subclass. It runs upstream's forest functions on an STL tessellation
// and keeps every intermediate the GUI wants (curvature, normals, feature
// size, leaves, estimate) for export.
//
// GPL-3.0-or-later, (C) FoamMesh contributors.

#ifndef FOAMMESH_SIZEFIELD_DRIVER_H
#define FOAMMESH_SIZEFIELD_DRIVER_H

#include <string>
#include <vector>

#include "automaticMeshSizeField.h"
#include "surface_io.h"

#define FOAMMESH_SIZEFIELD_VERSION "1.0.0"

struct SizeFieldSettings {
  double hmin = -1.0; // < 0: L / 1000 as upstream
  double hmax = -1.0; // < 0: hbulk
  double hbulk = -1.0; // < 0: L / 20 as upstream
  double gradation = 1.1;
  int nPointsPerCircle = 20; // curvature: nodes per 2 pi
  int nPointsPerGap = 4; // thin regions: layers per gap; 0 disables
  bool smoothing = true;
  bool aniso = false; // --aniso: leaves_metric (experimental, MMG3D only)
  int threads = 0; // 0: OpenMP default
  bool debugPos = false;
  std::string debugDir;
};

struct StageTimes {
  double read = 0, curvature = 0, delaunay = 0, normals = 0, rtree = 0,
         features = 0, refine = 0, smoothing = 0, aniso = 0, total = 0;
};

class FoamMeshSizeField {
public:
  FoamMeshSizeField();
  ~FoamMeshSizeField();

  // Computes the field. `surfaces` are the welded inputs (readStl).
  HXTStatus compute(const std::vector<SurfaceInput> &surfaces,
                    const SizeFieldSettings &settings);

  // Size at a point (linear interpolation inside the leaf, as Gmsh's
  // operator() did).
  double size(double x, double y, double z) const;

  // Exports.
  HXTStatus writePos(const std::string &path) const; // Gmsh SH view
  HXTStatus writeP4est(const std::string &prefix) const; // prefix.p4est + .data
  bool writeVtu(const std::string &path) const; // octree leaves
  // The version 1.0.0 outputs: leaves_ijk, leaves_level, leaves_size, nodes_xyz,
  // nodes_size, hexes (and leaves_metric with aniso) as .npy files in `dir`.
  bool writeNpy(const std::string &dir) const;
  bool writeSurfaceVtp(const std::string &path) const; // curvature etc.
  bool writeJson(const std::string &path,
                 const std::vector<std::string> &inputs) const;

  // Results.
  double estimatedTets() const { return _estimate; }
  const SizeFieldSettings &settings() const { return _settings; }
  const StageTimes &times() const { return _times; }
  const std::vector<forest_leaf_t> &leaves() const { return _leaves; }
  const AssembledSurface &model() const { return _model; }

private:
  void computeMetrics();
  SizeFieldSettings _settings;
  AssembledSurface _model;
  std::vector<std::string> _names;
  HXTMesh *_mesh = nullptr;
  double *_nodalCurvature = nullptr;
  std::vector<double> _nodeNormals;
  std::vector<double> _featureSize;
  RTree<uint64_t, double, 3> _triRTree;
  double _bbox[6];
  ForestOptions *_forestOptions = nullptr;
  Forest *_forest = nullptr;
  double _estimate = 0.0;
  StageTimes _times;
  std::vector<forest_leaf_t> _leaves;
  double _rootOrigin[3] = {0, 0, 0};
  double _rootLength = 0;
  size_t _anisoCrossed = 0;
  std::vector<double> _leafPoints; // shared corner points of the leaves
};

#endif
