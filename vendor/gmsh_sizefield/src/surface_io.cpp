// foammesh-sizefield: surface input (see surface_io.h).
//
// GPL-3.0-or-later, (C) FoamMesh contributors.

#include "surface_io.h"

#include <cfloat>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <functional>
#include <unordered_map>

extern "C" {
#include "hxt_tools.h"
}

namespace {

struct Key3 {
  double x, y, z;
  bool operator==(const Key3 &o) const
  {
    return x == o.x && y == o.y && z == o.z;
  }
};

struct Key3Hash {
  size_t operator()(const Key3 &k) const
  {
    std::hash<double> h;
    size_t a = h(k.x), b = h(k.y), c = h(k.z);
    return a ^ (b * 0x9e3779b97f4a7c15ULL) ^ (c * 0xc2b2ae3d27d4eb4fULL);
  }
};

inline double clean(double v) { return v == 0.0 ? 0.0 : v; } // -0 -> 0

class Welder {
  std::unordered_map<Key3, uint32_t, Key3Hash> _index;
  std::vector<double> &_xyz;

public:
  explicit Welder(std::vector<double> &xyz) : _xyz(xyz) {}
  uint32_t add(double x, double y, double z)
  {
    Key3 k = {clean(x), clean(y), clean(z)};
    auto it = _index.find(k);
    if(it != _index.end()) return it->second;
    uint32_t id = (uint32_t)(_xyz.size() / 3);
    _xyz.push_back(k.x);
    _xyz.push_back(k.y);
    _xyz.push_back(k.z);
    _index.emplace(k, id);
    return id;
  }
};

bool looksBinary(FILE *f, long size)
{
  // Binary: 80-byte header, uint32 count, 50 bytes per facet.
  if(size < 84) return false;
  unsigned char head[84];
  if(std::fread(head, 1, 84, f) != 84) return false;
  uint32_t n;
  std::memcpy(&n, head + 80, 4);
  if((long)84 + (long)50 * (long)n == size) return true;
  // ASCII files start with "solid"; anything else of a plausible size is
  // binary with a wrong count (rare), treat as ASCII only when it says so.
  return std::strncmp((const char *)head, "solid", 5) != 0;
}

bool readBinary(FILE *f, SurfaceInput &s, std::string &error)
{
  std::fseek(f, 80, SEEK_SET);
  uint32_t n;
  if(std::fread(&n, 4, 1, f) != 1) {
    error = "truncated binary STL header";
    return false;
  }
  Welder w(s.xyz);
  unsigned char rec[50];
  float v[12];
  for(uint32_t i = 0; i < n; ++i) {
    if(std::fread(rec, 1, 50, f) != 50) {
      error = "truncated binary STL facet";
      return false;
    }
    std::memcpy(v, rec, 48);
    for(int k = 0; k < 3; ++k) {
      s.tris.push_back(w.add(v[3 + 3 * k], v[4 + 3 * k], v[5 + 3 * k]));
    }
  }
  return true;
}

bool readAscii(FILE *f, SurfaceInput &s, std::string &error)
{
  std::fseek(f, 0, SEEK_SET);
  Welder w(s.xyz);
  char line[1024];
  int inFacet = 0;
  uint32_t ids[3];
  while(std::fgets(line, sizeof line, f)) {
    const char *p = line;
    while(*p == ' ' || *p == '\t') ++p;
    if(std::strncmp(p, "vertex", 6) == 0) {
      double x, y, z;
      if(std::sscanf(p + 6, "%lf %lf %lf", &x, &y, &z) != 3) {
        error = "bad vertex line in ASCII STL";
        return false;
      }
      if(inFacet < 3) ids[inFacet] = w.add(x, y, z);
      ++inFacet;
    }
    else if(std::strncmp(p, "endfacet", 8) == 0) {
      if(inFacet == 3) {
        s.tris.push_back(ids[0]);
        s.tris.push_back(ids[1]);
        s.tris.push_back(ids[2]);
      }
      inFacet = 0;
    }
  }
  return true;
}

} // namespace

bool readStl(const std::string &path, SurfaceInput &surface, std::string &error)
{
  FILE *f = std::fopen(path.c_str(), "rb");
  if(!f) {
    error = "cannot open " + path;
    return false;
  }
  std::fseek(f, 0, SEEK_END);
  long size = std::ftell(f);
  std::fseek(f, 0, SEEK_SET);
  bool ok = looksBinary(f, size) ? readBinary(f, surface, error) :
                                   readAscii(f, surface, error);
  std::fclose(f);
  if(ok && surface.tris.empty()) {
    error = "no triangles in " + path;
    return false;
  }
  surface.path = path;
  return ok;
}

void assembleSurfaces(const std::vector<SurfaceInput> &surfaces,
                      AssembledSurface &model)
{
  model.xyz.clear();
  model.tris.clear();
  model.color.clear();
  model.localToGlobal.assign(surfaces.size(), std::vector<uint32_t>());
  Welder w(model.xyz);
  for(size_t s = 0; s < surfaces.size(); ++s) {
    const SurfaceInput &in = surfaces[s];
    std::vector<uint32_t> &map = model.localToGlobal[s];
    map.resize(in.xyz.size() / 3);
    for(size_t i = 0; i < map.size(); ++i) {
      map[i] = w.add(in.xyz[3 * i], in.xyz[3 * i + 1], in.xyz[3 * i + 2]);
    }
    for(size_t t = 0; t < in.tris.size() / 3; ++t) {
      model.tris.push_back(map[in.tris[3 * t]]);
      model.tris.push_back(map[in.tris[3 * t + 1]]);
      model.tris.push_back(map[in.tris[3 * t + 2]]);
      model.color.push_back((uint32_t)s);
    }
  }
  for(int k = 0; k < 3; ++k) {
    model.bbox[k] = DBL_MAX;
    model.bbox[k + 3] = -DBL_MAX;
  }
  for(size_t i = 0; i < model.xyz.size() / 3; ++i) {
    for(int k = 0; k < 3; ++k) {
      double v = model.xyz[3 * i + k];
      if(v < model.bbox[k]) model.bbox[k] = v;
      if(v > model.bbox[k + 3]) model.bbox[k + 3] = v;
    }
  }
}

HXTStatus assembledToHxt(const AssembledSurface &model, HXTMesh *m)
{
  // Same layout Gmsh2Hxt produced: 4 doubles per vertex (the 4th is HXT's
  // per-node scratch, zero), triangles with a colour each, no lines.
  m->vertices.num = m->vertices.size = (uint32_t)(model.xyz.size() / 3);
  HXT_CHECK(
    hxtAlignedMalloc(&m->vertices.coord, 4 * m->vertices.num * sizeof(double)));
  for(uint32_t i = 0; i < m->vertices.num; ++i) {
    m->vertices.coord[4 * i + 0] = model.xyz[3 * i + 0];
    m->vertices.coord[4 * i + 1] = model.xyz[3 * i + 1];
    m->vertices.coord[4 * i + 2] = model.xyz[3 * i + 2];
    m->vertices.coord[4 * i + 3] = 0.0;
  }
  m->lines.num = m->lines.size = 0;
  m->triangles.num = m->triangles.size = model.tris.size() / 3;
  HXT_CHECK(hxtAlignedMalloc(&m->triangles.node,
                             m->triangles.num * 3 * sizeof(uint32_t)));
  HXT_CHECK(
    hxtAlignedMalloc(&m->triangles.color, m->triangles.num * sizeof(uint32_t)));
  for(uint64_t t = 0; t < m->triangles.num; ++t) {
    m->triangles.node[3 * t + 0] = model.tris[3 * t + 0];
    m->triangles.node[3 * t + 1] = model.tris[3 * t + 1];
    m->triangles.node[3 * t + 2] = model.tris[3 * t + 2];
    m->triangles.color[t] = model.color[t];
  }
  return HXT_STATUS_OK;
}
