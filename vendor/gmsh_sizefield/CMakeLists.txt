# foammesh-sizefield: Gmsh's AutomaticMeshSizeField (Arthur Bawin) as a
# stand-alone tool for FoamMesh's two meshing routes. Built inside the WSL
# runtime; see ../../scripts/build_sizefield.sh and UPSTREAM.md.
#
# GPL-3.0-or-later. The lifted sources keep their own licences (LICENSE.txt,
# hxt/LICENSE.txt).

cmake_minimum_required(VERSION 3.16)
project(foammesh_sizefield C CXX)

set(CMAKE_C_STANDARD 99)
set(CMAKE_CXX_STANDARD 14)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(MPI REQUIRED COMPONENTS C)
find_package(OpenMP)
# FOAMMESH_STATIC_P4EST=ON links libp4est.a and libsc.a into the tool, so it runs on
# a distribution that has OpenMPI but not libp4est/libsc (the stock OpenFOAM 13
# runtime). libsc needs LAPACK/BLAS, which stay shared (the runtime has them).
option(FOAMMESH_STATIC_P4EST "Link p4est and libsc statically" OFF)
if(FOAMMESH_STATIC_P4EST)
  find_library(P4EST_LIB NAMES libp4est.a REQUIRED)
  find_library(SC_LIB NAMES libsc.a REQUIRED)
  set(P4EST_EXTRA_LIBS lapack blas z)
else()
  find_library(P4EST_LIB p4est REQUIRED)
  find_library(SC_LIB sc REQUIRED)
endif()
find_path(P4EST_INCLUDE_DIR p4est.h REQUIRED)

# HXT core and predicates through their own CMake (object libraries).
add_subdirectory(hxt/core)
add_subdirectory(hxt/predicates)
# predicates.c includes hxt_tools.h from core (Gmsh adds it globally).
target_include_directories(hxt_predicates PRIVATE hxt/core/include)

# The Delaunay subset of HXT tetMesh: hxtEmptyMesh and what it calls. Not
# hxt_tetMesh.c (boundary recovery, tetBR) nor the optimisers.
add_library(hxt_delaunay STATIC
  hxt/tetMesh/src/hxt_tetRefine.c
  hxt/tetMesh/src/hxt_tetDelaunay.c
  hxt/tetMesh/src/hxt_tetDelaunayReshape.c
  hxt/tetMesh/src/hxt_tetFlag.c
  hxt/tetMesh/src/hxt_tetRepair.c
  hxt/tetMesh/src/hxt_tetUtils.c
  hxt/tetMesh/src/hxt_tetSync.c
  hxt/tetMesh/src/hxt_vertices.c
  hxt/tetMesh/src/hxt_tetNodalSize.c)
target_include_directories(hxt_delaunay
  PUBLIC hxt/tetMesh/include hxt/tetMesh/src
  PUBLIC hxt/core/include hxt/predicates/include)
target_link_libraries(hxt_delaunay PUBLIC hxt_core hxt_predicates m)
if(OpenMP_C_FOUND)
  target_link_libraries(hxt_delaunay PUBLIC OpenMP::OpenMP_C)
endif()

add_executable(foammesh-sizefield
  src/main.cpp
  src/foammesh_sizefield.cpp
  src/surface_io.cpp
  src/automaticMeshSizeField.cpp
  third_party/gmsh/curvature.cpp
  third_party/gmsh/robustPredicates.cpp)
# OMPI_SKIP_MPICXX: p4est's headers pull in mpi.h; the C++ bindings are not
# wanted (and not linked).
target_compile_definitions(foammesh-sizefield PRIVATE HAVE_HXT HAVE_P4EST
  OMPI_SKIP_MPICXX MPICH_SKIP_MPICXX)
target_include_directories(foammesh-sizefield PRIVATE
  src src/gmsh_shims third_party/gmsh ${P4EST_INCLUDE_DIR})
target_link_libraries(foammesh-sizefield PRIVATE
  hxt_delaunay hxt_core hxt_predicates ${P4EST_LIB} ${SC_LIB} ${P4EST_EXTRA_LIBS} MPI::MPI_C m)
if(OpenMP_CXX_FOUND)
  target_link_libraries(foammesh-sizefield PRIVATE OpenMP::OpenMP_CXX)
endif()
# Shewchuk's predicates must not be contracted or reassociated.
set_source_files_properties(third_party/gmsh/robustPredicates.cpp PROPERTIES
  COMPILE_OPTIONS "-fno-unsafe-math-optimizations;-ffp-contract=off")

install(TARGETS foammesh-sizefield RUNTIME DESTINATION bin)
