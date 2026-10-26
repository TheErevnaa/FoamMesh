// foammesh-sizefield: surface input. Reads the STL files the shared FoamMesh
// pipeline writes after Preparation (one file per named surface), welds
// coincident vertices, and fills the HXTMesh the lifted size-field code
// expects (the job Gmsh2Hxt* did from a GModel upstream).
//
// GPL-3.0-or-later, (C) FoamMesh contributors.

#ifndef FOAMMESH_SIZEFIELD_SURFACE_IO_H
#define FOAMMESH_SIZEFIELD_SURFACE_IO_H

#include <cstdint>
#include <string>
#include <vector>

extern "C" {
#include "hxt_mesh.h"
}

// One named surface: a welded triangle soup.
struct SurfaceInput {
  std::string name;
  std::string path;
  std::vector<double> xyz; // 3 per vertex, welded within the surface
  std::vector<uint32_t> tris; // 3 local vertex indices per triangle
};

// The assembled model: every surface welded into one vertex set.
struct AssembledSurface {
  std::vector<double> xyz; // 3 per global vertex
  std::vector<uint32_t> tris; // 3 global indices per triangle
  std::vector<uint32_t> color; // surface index per triangle
  std::vector<std::vector<uint32_t> > localToGlobal; // per surface
  double bbox[6]; // xmin ymin zmin xmax ymax zmax
};

// Reads an ASCII or binary STL. Returns false with `error` set on failure.
bool readStl(const std::string &path, SurfaceInput &surface, std::string &error);

// Welds every surface into one model (exact coordinate match, as the
// pipeline's STLs come from one tessellation).
void assembleSurfaces(const std::vector<SurfaceInput> &surfaces,
                      AssembledSurface &model);

// Fills an HXTMesh (vertices + triangles with colours, no lines) from the
// assembled model, with HXT's aligned allocations.
HXTStatus assembledToHxt(const AssembledSurface &model, HXTMesh *mesh);

#endif
