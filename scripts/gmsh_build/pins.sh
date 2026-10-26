# Pinned inputs and flags for FoamMesh's own Gmsh build (Plan 48 GM16a).
# Sourced by build_gmsh.sh and bundle.sh. Change a value here and nowhere else;
# the next Gmsh release is rebuilt the same way by editing GMSH_VERSION and the
# source path.

# What is built -------------------------------------------------------------
GMSH_VERSION=4.15.2
GMSH_SRC=${GMSH_SRC:-/opt/foammesh-src/gmsh-4.15.2-source}
# The id of the installed folder in the distribution (bump with any rebuild).
BUILD_ID=gmsh-4.15.2-revoropt-6

# Source patches applied to the unpacked 4.15.2 tree before configuring, in
# this order (scripts/gmsh_build/patches/). Plan 48 G15:
#   highorder-options: the six High-order tools fields of the native window
#     as Mesh.HighOrder* options read by optimize("HighOrder") (DP-5707).
#   crossfield-extra-connexions: bound check in assembleExtraConnexions, so
#     a curved model ends in a Gmsh error, not a segfault (DP-5705).
#   patch-adaptation: the patch adaptation loop of the one-by-one strategy
#     grows the patch after a failed try (4.15.2 retried the same patch and
#     never used the grown distance), so MaxAdaptBlob, AdaptBlobLayerFactor
#     and AdaptBlobDistFactor take effect (DP-5707).
#   bl-curving: optimize("HighOrderFastCurving") reads Gmsh's own option
#     Mesh.HighOrderFastCurvingNewAlgo, which 4.15.2 declared and never read,
#     so the window's "Boundary layer curving" is reachable (DP-4257).
#   ordered-optimizer-sets: the optimizer's element and node sets and maps
#     are ordered by element and node number, not by memory address, so the
#     one-by-one strategy gives the same mesh on every run; 4.15.2 walked
#     them in address order and a run's result depended on where the
#     allocator put the elements (DP-5735).
GMSH_PATCHES=(
  gmsh-4.15.2-highorder-options.patch
  gmsh-4.15.2-crossfield-extra-connexions.patch
  gmsh-4.15.2-patch-adaptation.patch
  gmsh-4.15.2-bl-curving.patch
  gmsh-4.15.2-ordered-optimizer-sets.patch
)

# Where it is built and where the result is staged (inside the distribution,
# on its own disk: drvfs is far too slow for object files).
WORK=${WORK:-/root/foammesh-gmsh-build}
DEPS=$WORK/deps
STAGE=$WORK/stage/$BUILD_ID

# Dependencies the official wheel carries inside itself (its SDK) and the
# distribution does not have. Built here from source, static and position
# independent, so the result needs nothing the wheel did not need.
OCCT_VERSION=7.8.1
OCCT_URL=https://github.com/Open-Cascade-SAS/OCCT/archive/refs/tags/V7_8_1.tar.gz
OCCT_SHA256=7321af48c34dc253bf8aae3f0430e8cb10976961d534d8509e72516978aa82f5
# Source patches applied to the unpacked OCCT tree (scripts/gmsh_build/patches/).
#   type-registry-never-destroyed: linked statically into libgmsh, other
#     files' static type handles (the STEP protocol's map, the IGES protocols'
#     atype handles) outlived OCCT's type registry, and ~Standard_Type erased
#     from the destroyed map, so every process that read or wrote STEP, or
#     wrote IGES, died with SIGSEGV in exit() after finishing its work.
# OCCT_BUILD_ID names this set: change it with the list and OCCT is rebuilt.
OCCT_PATCHES=(
  occt-7.8.1-type-registry-never-destroyed.patch
)
OCCT_BUILD_ID=occt-7.8.1-p2

MMG_VERSION=5.7.3
MMG_URL=https://github.com/MmgTools/mmg/archive/refs/tags/v5.7.3.tar.gz
MMG_SHA256=b0a9c5ad6789df369a68f94295df5b324b6348020b73bcc395d32fdd44abe706

PETSC_VERSION=3.22.0
PETSC_URL=https://web.cels.anl.gov/projects/petsc/download/release-snapshots/petsc-3.22.0.tar.gz
PETSC_SHA256=2c03f7c0f7ad2649240d4989355cf7fb7f211b75156cd7d424e1d9dd7dfb290b

HDF5_VERSION=1.10.11
HDF5_URL=https://github.com/HDFGroup/hdf5/archive/refs/tags/hdf5-1_10_11.tar.gz
HDF5_SHA256=4ef6375fc7d8c54dcd66e9bc35a7a3580d33cd8878bdf21ad1eb388a43863159

# MED 4.1.0 as repacked by Debian/Ubuntu (upstream med-fichier 4.1.0).
MED_VERSION=4.1.0
MED_URL=http://archive.ubuntu.com/ubuntu/pool/universe/m/med-fichier/med-fichier_4.1.0+repack.orig.tar.gz
MED_SHA256=ef5420a8b9aa2db4477661814ce355939fbdbe5f540f943508defc292f0c3b1b

CGNS_VERSION=4.4.0
CGNS_URL=https://github.com/CGNS/CGNS/archive/refs/tags/v4.4.0.tar.gz
CGNS_SHA256=3b0615d1e6b566aa8772616ba5fd9ca4eca1a600720e36eadd914be348925fe2

FLTK_VERSION=1.3.8
FLTK_URL=https://github.com/FLTK/fltk/archive/refs/tags/release-1.3.8.tar.gz
FLTK_SHA256=abddf697bf1b0a59462a0d2ec57557f4bfb9c634627e6fa50d8c664112642811

# Packages installed in the distribution with apt (recorded in runtime.json).
APT_PACKAGES=(
  build-essential cmake patchelf pkg-config curl autoconf automake libtool
  libfreetype-dev libfontconfig1-dev
  libgl1-mesa-dev libglu1-mesa-dev
  libxft-dev libxinerama-dev libxcursor-dev libxfixes-dev libxext-dev
  libxrender-dev libx11-dev
  libpng-dev libjpeg-dev zlib1g-dev
)

# Gmsh configuration. Everything not named here is left at Gmsh's own default,
# exactly as the official build does; the options named OFF are the ones the
# official wheel's General.BuildOptions does not list, written out so a library
# that happens to be installed can never switch one on.
GMSH_CMAKE_FLAGS=(
  -DCMAKE_BUILD_TYPE=Release
  -DENABLE_BUILD_SHARED=ON
  -DENABLE_BUILD_DYNAMIC=ON
  -DGMSH_RELEASE=1
  -DENABLE_REVOROPT=ON
  -DENABLE_OCC=ON -DENABLE_OCC_CAF=ON
  -DENABLE_MMG=ON
  -DENABLE_PETSC=ON
  -DENABLE_MED=ON -DENABLE_CGNS=ON
  -DENABLE_FLTK=ON
  -DENABLE_OPENMP=ON
  -DENABLE_TESTS=OFF
  -DENABLE_GMP=OFF -DENABLE_CAIRO=OFF -DENABLE_POPPLER=OFF
  -DENABLE_MPI=OFF -DENABLE_P4EST=OFF -DENABLE_GETDP=OFF -DENABLE_SLEPC=OFF
  -DENABLE_MUMPS=OFF -DENABLE_OSMESA=OFF -DENABLE_ZIPPER=OFF
  -DENABLE_GEOMETRYCENTRAL=OFF -DENABLE_MESQUITE=OFF -DENABLE_TCMALLOC=OFF
  -DENABLE_3M=OFF -DENABLE_PRIVATE_API=OFF -DENABLE_BLAS_LAPACK=OFF
)
