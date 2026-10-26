// FoamMesh shim for Gmsh's Numeric.h. The lifted size-field code only uses
// angle(), crossprod() and dot() on SVector3, which SVector3.h itself
// provides, plus M_PI. Nothing from Gmsh's Numeric.cpp is needed.
//
// GPL-3.0-or-later, (C) FoamMesh contributors. Not upstream code.

#ifndef FOAMMESH_GMSH_NUMERIC_SHIM_H
#define FOAMMESH_GMSH_NUMERIC_SHIM_H

#include <cmath>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#endif
