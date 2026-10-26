// Gmsh - Copyright (C) 1997-2025 C. Geuzaine, J.-F. Remacle
//
// See the LICENSE.txt file in the Gmsh root directory for license information.
// Please report all issues on https://gitlab.onelab.info/gmsh/gmsh/issues.
//
// Contributed by Arthur Bawin
//
// FOAMMESH: lifted from gmsh-4.15.2-source/src/mesh/automaticMeshSizeField.h.
// The ForestOptions / Forest / size_data / size_point / interpolation_data
// structs and the forest function declarations are upstream's. The Gmsh
// `Field` subclass is gone: FoamMesh drives the forest from
// foammesh_sizefield.cpp on a surface tessellation read from STL files, so
// nothing here needs GModel, Field, Context or GmshConfig. Every change is
// marked FOAMMESH and listed in ../UPSTREAM.md.

#ifndef AUTOMATIC_MESH_SIZE_FIELD_H
#define AUTOMATIC_MESH_SIZE_FIELD_H

#include <vector>
#include <cstdio>
#include <cstdint>

#include "STensor3.h"
#include "curvature.h"

#include "rtree.h"

#if defined(HAVE_P4EST)
// P4EST INCLUDES
#include <p4est_to_p8est.h>
#include <p8est_extended.h>
#endif

#if defined(HAVE_HXT)
// HXT INCLUDES
extern "C" {
#include "hxt_tools.h"
#include "hxt_mesh.h"
#include "hxt_bbox.h"
}
#endif

// Information needed to create and compute a forest of octrees
typedef struct ForestOptions {
  int dim;
  double hmax;
  double hmin;
  double hbulk;
  double gradation;
  int nodePerTwoPi;
  int nodePerGap;
  double *bbox;
  double (*sizeFunction)(double, double, double, double);
  std::vector<double> *featureSizeAtVertices;
  RTree<uint64_t, double, 3> *triRTree;
#ifdef HAVE_HXT
  HXTMesh *mesh;
#endif
  double *nodalCurvature;
  double *nodeNormals;
  // std::vector<std::function<double(double)>> *curvFunctions;
  // std::vector<std::function<double(double)>> *xFunctions;
  // std::vector<std::function<double(double)>> *yFunctions;
  FILE *file1;
  FILE *file2;
  FILE *file3;
  // FOAMMESH: upstream wrote its debug .pos files into the working directory
  // unconditionally; they are now written only when debugPos is set, into
  // debugDir.
  int debugPos;
  const char *debugDir;
} ForestOptions;

// The structure containing the size field information (forest)
typedef struct Forest {
#ifdef HAVE_P4EST
  p4est_t *p4est;
#endif
  ForestOptions *forestOptions;
} Forest;

// Data available on each tree cell
typedef struct size_data {
  double size;
#ifdef HAVE_P4EST
  double ds[3]; // Size gradient
#endif
  double h; // Isotropic cell size
  SMetric3 M; // Anisotropic size

  // Données pour l'interpolation des directions de courbure
  bool hasIntersection;
  SVector3 t1, t2, n;

} size_data_t;

// A node to search in the tree
typedef struct size_point {
  double x;
  double y;
  double z;
  double size;
  SMetric3 m;
  bool isFound;
  int parcourus;
} size_point_t;

typedef struct interpolation_data {
  double center[3];
  SVector3 t1;
  SVector3 t2;
  double denom;
} interpolation_data_t;

// FOAMMESH: one octree leaf, as collected by forestCollectLeaves for the
// .vtu export and the element estimate per region.
typedef struct forest_leaf {
  double center[3];
  double h; // edge length of the leaf
  double size; // prescribed mesh size in the leaf
  int level;
  uint32_t corner[8]; // indices into the shared corner-point list (VTK hex order)
  int32_t xyz[3]; // quadrant coordinates (0 .. P4EST_ROOT_LEN) of the low corner
  double m[6]; // metric xx xy xz yy yz zz; filled by the driver with --aniso
} forest_leaf_t;

#if defined(HAVE_HXT) && defined(HAVE_P4EST)
HXTStatus forestOptionsCreate(ForestOptions **forestOptions);
HXTStatus forestOptionsDelete(ForestOptions **forestOptions);

HXTStatus forestCreate(int argc, char **argv, Forest **forest,
                       const char *filename, ForestOptions *forestOptions);
HXTStatus forestDelete(Forest **forest);

HXTStatus forestSave(Forest *forest, const char *forestFile,
                     const char *dataFile);
HXTStatus forestExport(Forest *forest, const char *forestFile);
HXTStatus forestExport2D(Forest *forest, const char *forestFile);
HXTStatus forestLoad(Forest **forest, const char *forestFile,
                     const char *dataFile, ForestOptions *forestOptions);

HXTStatus forestSearchOne(Forest *forest, double x, double y, double z,
                          double *size, int linear);

// FOAMMESH: these were file-local to the upstream .cpp (the Field subclass
// called them from the same translation unit); the FoamMesh driver lives in
// another file, so they are declared here.
HXTStatus featureSize(Forest *forest);
HXTStatus forestRefine(Forest *forest);
HXTStatus forestSizeSmoothing(Forest *forest);
HXTStatus hxtOctreeElementEstimation(Forest *forest, double *elemEstimate);
HXTStatus forestSearchOneAniso(Forest *forest, double x, double y, double z,
                               SMetric3 &m, int linear);
HXTStatus forestInterpolateDirections(Forest *forest);
HXTStatus hxtForestSearch(Forest *forest, std::vector<double> *x,
                          std::vector<double> *y, std::vector<double> *z,
                          std::vector<double> *size);
// FOAMMESH: added, see the .cpp.
HXTStatus forestCollectLeaves(Forest *forest,
                              std::vector<forest_leaf_t> *leaves,
                              std::vector<double> *points);
// FOAMMESH: the root cube of the single octree (origin = the corner with
// quadrant coordinates 0,0,0; length = its edge); the quadrant coordinates
// run 0 .. P4EST_ROOT_LEN = 2^19 along it.
HXTStatus forestRootCube(Forest *forest, double origin[3], double *length);
#endif

#endif
