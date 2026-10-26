// foammesh-sizefield: command line.
//
//   foammesh-sizefield --surface name=path.stl [--surface ...] --out DIR
//       [--hmin H] [--hmax H] [--hbulk H] [--gradation G]
//       [--nodes-per-two-pi N] [--nodes-per-gap N] [--no-features]
//       [--no-smoothing] [--threads N] [--vtu] [--p4est] [--pos] [--aniso]
//       [--debug]
//
// Writes into DIR (version 1.0.0): leaves_ijk, leaves_level, leaves_size,
// nodes_xyz, nodes_size, hexes as .npy (with --aniso also leaves_metric),
// surface.vtp (curvature, normals, feature size, size at the nodes) and
// field.json. Opt-in: --vtu field.vtu (octree leaves), --p4est field.p4est +
// field.data (reusable octree), --pos field.pos (the Gmsh SH view a
// PostView + Octree field reads; one text line per leaf, so large).
//
// GPL-3.0-or-later, (C) FoamMesh contributors.

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "foammesh_sizefield.h"
#include "GmshMessage.h"

#include <p4est_to_p8est.h>
#include <p8est_extended.h>
#include <sc.h>

static void usage()
{
  std::fprintf(stderr,
    "usage: foammesh-sizefield --surface NAME=PATH.stl [--surface ...] --out DIR\n"
    "       [--hmin H] [--hmax H] [--hbulk H] [--gradation G]\n"
    "       [--nodes-per-two-pi N] [--nodes-per-gap N] [--no-features]\n"
    "       [--no-smoothing] [--threads N] [--vtu] [--p4est] [--pos]\n"
    "       [--aniso] [--debug] [--version]\n");
}

int main(int argc, char **argv)
{
  std::vector<SurfaceInput> surfaces;
  std::vector<std::string> inputs;
  SizeFieldSettings settings;
  std::string out;
  bool writePos = false, writeVtu = false, writeP4est = false;

  for(int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&](double *d, int *n, std::string *s) {
      if(i + 1 >= argc) {
        std::fprintf(stderr, "%s needs a value\n", a.c_str());
        std::exit(2);
      }
      const char *v = argv[++i];
      if(d) *d = std::atof(v);
      if(n) *n = std::atoi(v);
      if(s) *s = v;
    };
    if(a == "--version") {
      std::printf("foammesh-sizefield %s (gmsh-4.15.2 AutomaticMeshSizeField, p4est %s)\n",
                  FOAMMESH_SIZEFIELD_VERSION, P4EST_VERSION);
      return 0;
    }
    else if(a == "--surface") {
      std::string spec;
      next(nullptr, nullptr, &spec);
      size_t eq = spec.find('=');
      SurfaceInput s;
      s.name = eq == std::string::npos ? spec : spec.substr(0, eq);
      std::string path = eq == std::string::npos ? spec : spec.substr(eq + 1);
      std::string error;
      if(!readStl(path, s, error)) {
        Msg::Error("%s", error.c_str());
        return 1;
      }
      Msg::Info("Read %s: %zu vertices, %zu triangles", path.c_str(),
                s.xyz.size() / 3, s.tris.size() / 3);
      inputs.push_back(path);
      surfaces.push_back(std::move(s));
    }
    else if(a == "--out") next(nullptr, nullptr, &out);
    else if(a == "--hmin") next(&settings.hmin, nullptr, nullptr);
    else if(a == "--hmax") next(&settings.hmax, nullptr, nullptr);
    else if(a == "--hbulk") next(&settings.hbulk, nullptr, nullptr);
    else if(a == "--gradation") next(&settings.gradation, nullptr, nullptr);
    else if(a == "--nodes-per-two-pi") next(nullptr, &settings.nPointsPerCircle, nullptr);
    else if(a == "--nodes-per-gap") next(nullptr, &settings.nPointsPerGap, nullptr);
    else if(a == "--no-features") settings.nPointsPerGap = 0;
    else if(a == "--no-smoothing") settings.smoothing = false;
    else if(a == "--threads") next(nullptr, &settings.threads, nullptr);
    else if(a == "--pos") writePos = true;
    else if(a == "--vtu") writeVtu = true;
    else if(a == "--p4est") writeP4est = true;
    else if(a == "--aniso") settings.aniso = true;
    else if(a == "--debug") settings.debugPos = true;
    else {
      usage();
      return 2;
    }
  }
  if(surfaces.empty() || out.empty()) {
    usage();
    return 2;
  }
  settings.debugDir = out;

  // MPI once for the process (Ubuntu's p4est is an MPI build); quiet sc and
  // p4est so the tool's own lines are the log.
  int mpiret = sc_MPI_Init(&argc, &argv);
  SC_CHECK_MPI(mpiret);
  sc_init(sc_MPI_COMM_WORLD, 1, 1, nullptr, SC_LP_ERROR);
  p4est_init(nullptr, SC_LP_ERROR);

  int rc = 0;
  {
    FoamMeshSizeField field;
    HXTStatus s = field.compute(surfaces, settings);
    if(s != HXT_STATUS_OK) {
      Msg::Error("Something went wrong when computing the octree (status %d)", (int)s);
      rc = 1;
    }
    else {
      std::string base = out + "/field";
      if(writePos && field.writePos(base + ".pos") != HXT_STATUS_OK) rc = 1;
      if(writeP4est && field.writeP4est(base) != HXT_STATUS_OK) rc = 1;
      if(writeVtu && !field.writeVtu(base + ".vtu")) rc = 1;
      if(!field.writeNpy(out)) rc = 1;
      if(!field.writeSurfaceVtp(out + "/surface.vtp")) rc = 1;
      if(!field.writeJson(base + ".json", inputs)) rc = 1;
      if(rc == 0)
        Msg::Info("Wrote the .npy arrays, surface.vtp and field.json in %s (%.2f s)",
                  out.c_str(), field.times().total);
    }
  }
  sc_finalize();
  mpiret = sc_MPI_Finalize();
  SC_CHECK_MPI(mpiret);
  return rc;
}
