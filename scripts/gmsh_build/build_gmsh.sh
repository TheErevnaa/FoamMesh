#!/bin/bash
# Builds FoamMesh's own Gmsh (Plan 48 GM16a, section 4.15) inside the WSL
# distribution OpenFOAM13Runtime, from /opt/foammesh-src, with Revoropt on and
# every other option the official wheel has.
#
#   wsl -d OpenFOAM13Runtime -u root -- bash /mnt/d/FoamMesh/scripts/gmsh_build/build_gmsh.sh
#
# Stages (each is skipped when its marker exists; delete $WORK/.done-<stage>):
#   apt     system packages (pins.sh APT_PACKAGES)
#   occt    OpenCASCADE, static, from source
#   mmg     Mmg, static, from source
#   petsc   PETSc (no MPI), static, from source
#   hdf5 med cgns fltk   serial static builds (the distribution's are MPI/Cairo)
#   gmsh    configure, build, install
#   bundle  bundle.sh: the installable folder and its tarball
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pins.sh
source "$HERE/pins.sh"
JOBS="${JOBS:-$(nproc)}"
mkdir -p "$WORK/src" "$WORK/logs" "$DEPS"
cd "$WORK"

stage() { # name; runs the function of the same name once
  local n="$1" mark="$WORK/.done-$1"
  # Gmsh itself and its bundle are redone for every BUILD_ID (pins.sh).
  case "$n" in gmsh|bundle) mark="$mark-$BUILD_ID" ;; occt) mark="$mark-$OCCT_BUILD_ID" ;; esac
  if [ -e "$mark" ]; then echo "[skip] $n"; return; fi
  echo "[run ] $n  $(date +%T)"
  # A subshell run on its own line keeps `set -e` alive inside the stage
  # (it is ignored for anything run on the left of `||`).
  set +e
  ( set -euo pipefail; "$n" ) > "$WORK/logs/$n.log" 2>&1
  local rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    echo "[FAIL] $n rc=$rc (see $WORK/logs/$n.log)"; tail -n 30 "$WORK/logs/$n.log"; exit 1
  fi
  touch "$mark"
  echo "[done] $n  $(date +%T)"
}

fetch() { # url sha256-or-empty dest ; prints the sha256 it got
  local url="$1" want="$2" dest="$3"
  # Download to a part file: an interrupted run must not leave a truncated
  # tarball that a later run would trust.
  if [ ! -f "$dest" ]; then
    curl -fL --retry 5 --retry-delay 3 -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
  fi
  local got; got="$(sha256sum "$dest" | cut -d' ' -f1)"
  echo "sha256 $dest $got"
  if [ -n "$want" ] && [ "$want" != "$got" ]; then
    echo "sha256 mismatch for $dest: wanted $want" >&2; return 1
  fi
}

apt() {
  export DEBIAN_FRONTEND=noninteractive
  # A previous apt run that was cut off leaves dpkg half configured.
  dpkg --configure -a || true
  for attempt in 1 2 3; do
    command apt-get update && break || sleep 5
  done
  # force-unsafe-io: no fsync per file; the distribution's disk is shared and
  # an fsync-per-file install of ~100 packages took over an hour under load.
  command apt-get install -y --no-install-recommends \
    -o Dpkg::Options::=--force-unsafe-io "${APT_PACKAGES[@]}"
  dpkg-query -W -f='${Package} ${Version}\n' "${APT_PACKAGES[@]}" > "$WORK/apt-installed.txt"
}

occt() {
  fetch "$OCCT_URL" "$OCCT_SHA256" "$WORK/src/occt.tar.gz"
  rm -rf "$WORK/src/occt" && mkdir -p "$WORK/src/occt" && tar -xzf "$WORK/src/occt.tar.gz" -C "$WORK/src/occt" --strip-components=1
  for p in "${OCCT_PATCHES[@]}"; do
    ( cd "$WORK/src/occt" && patch -p1 -s < "$HERE/patches/$p" )
    echo "applied $p"
  done
  cmake -S "$WORK/src/occt" -B "$WORK/build/occt" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$DEPS/occt" -DBUILD_LIBRARY_TYPE=Static \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_MODULE_Draw=OFF \
    -DBUILD_MODULE_DETools=OFF -DBUILD_DOC_Overview=OFF -DBUILD_Inspector=OFF \
    -DUSE_TK=OFF -DUSE_TBB=OFF -DUSE_FREETYPE=ON -DUSE_RAPIDJSON=OFF \
    -DUSE_OPENVR=OFF -DUSE_FFMPEG=OFF -DUSE_FREEIMAGE=OFF -DUSE_VTK=OFF
  cmake --build "$WORK/build/occt" -j "$JOBS"
  cmake --install "$WORK/build/occt"
}

mmg() {
  fetch "$MMG_URL" "$MMG_SHA256" "$WORK/src/mmg.tar.gz"
  rm -rf "$WORK/src/mmg" && mkdir -p "$WORK/src/mmg" && tar -xzf "$WORK/src/mmg.tar.gz" -C "$WORK/src/mmg" --strip-components=1
  cmake -S "$WORK/src/mmg" -B "$WORK/build/mmg" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$DEPS/mmg" -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DUSE_SCOTCH=OFF -DUSE_VTK=OFF \
    -DUSE_ELAS=OFF -DBUILD=MMG -DBUILD_TESTING=OFF
  cmake --build "$WORK/build/mmg" -j "$JOBS"
  cmake --install "$WORK/build/mmg"
}

petsc() {
  fetch "$PETSC_URL" "$PETSC_SHA256" "$WORK/src/petsc.tar.gz"
  rm -rf "$WORK/src/petsc" && mkdir -p "$WORK/src/petsc" && tar -xzf "$WORK/src/petsc.tar.gz" -C "$WORK/src/petsc" --strip-components=1
  ( cd "$WORK/src/petsc"
    unset PETSC_DIR PETSC_ARCH
    python3 ./configure --prefix="$DEPS/petsc" --with-cc=gcc --with-cxx=0 --with-fc=0 \
      --with-mpi=0 --with-shared-libraries=0 --with-pic=0 --with-debugging=0 \
      --with-x=0 CFLAGS=-fPIC --COPTFLAGS=-O2 --download-f2cblaslapack=1 --with-ssl=0
    make PETSC_DIR="$PWD" PETSC_ARCH=arch-linux-c-opt -j "$JOBS" all
    make PETSC_DIR="$PWD" PETSC_ARCH=arch-linux-c-opt install )
}

unpack() { # name url sha256 ; downloads and unpacks into $WORK/src/<name>
  local name="$1" url="$2" sha="$3" tarball="$WORK/src/$1.tar.gz"
  fetch "$url" "$sha" "$tarball"
  rm -rf "$WORK/src/$name" && mkdir -p "$WORK/src/$name"
  tar -xzf "$tarball" -C "$WORK/src/$name" --strip-components=1
}

# Serial, static, position independent: what the wheel's SDK links, and no MPI
# (the distribution's HDF5/MED/CGNS packages are the OpenMPI variants).
hdf5() {
  unpack hdf5 "$HDF5_URL" "$HDF5_SHA256"
  cmake -S "$WORK/src/hdf5" -B "$WORK/build/hdf5" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$DEPS/hdf5" -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DBUILD_TESTING=OFF \
    -DHDF5_BUILD_TOOLS=OFF -DHDF5_BUILD_EXAMPLES=OFF -DHDF5_BUILD_HL_LIB=OFF \
    -DHDF5_BUILD_CPP_LIB=OFF -DHDF5_BUILD_FORTRAN=OFF -DHDF5_ENABLE_PARALLEL=OFF \
    -DHDF5_ENABLE_Z_LIB_SUPPORT=ON -DHDF5_ENABLE_SZIP_SUPPORT=OFF
  cmake --build "$WORK/build/hdf5" -j "$JOBS"
  cmake --install "$WORK/build/hdf5"
}

med() {
  [ -f "$WORK/src/med/configure" ] || unpack med "$MED_URL" "$MED_SHA256"
  ( cd "$WORK/src/med"
    # The version probe greps H5public.h without -m1 and so also matches the
    # H5_VERS_* uses further down the header; HDF5 1.10.11 is in range.
    for k in MAJOR MINOR RELEASE; do sed -i "s/grep '#define \*H5_VERS_$k'/grep -m1 '#define *H5_VERS_$k'/" configure; done
    # ...and the shipped configure keeps m4's doubled brackets in its sed.
    sed -i '/H5_VER_\(MAJOR\|MINOR\|RELEASE\)=/ s/\[\[ \\t\]\]/[ \\t]/g' configure
    grep -n 'H5_VER_MAJOR=' configure
    # MED's configure insists on an h5dump; it is only used by MED's own
    # tests, which are not built. A stub keeps the HDF5 build tool-free.
    mkdir -p "$DEPS/hdf5/bin"
    printf '#!/bin/sh\nexit 0\n' > "$DEPS/hdf5/bin/h5dump"; chmod +x "$DEPS/hdf5/bin/h5dump"
    ./configure --prefix="$DEPS/med" --with-hdf5="$DEPS/hdf5" \
      --disable-shared --enable-static --disable-fortran --disable-python \
      --with-pic CFLAGS="-O2 -fPIC" CXXFLAGS="-O2 -fPIC" LIBS="-lz -ldl -lm"
    # Only the library and headers: MED's command line tools clash when linked
    # against the static library, and Gmsh does not use them.
    make -C include install
    make -j "$JOBS" -C src
    make -C src install )
}

cgns() {
  unpack cgns "$CGNS_URL" "$CGNS_SHA256"
  cmake -S "$WORK/src/cgns" -B "$WORK/build/cgns" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$DEPS/cgns" -DCGNS_BUILD_SHARED=OFF \
    -DCGNS_ENABLE_HDF5=ON -DHDF5_ROOT="$DEPS/hdf5" -DHDF5_PREFER_PARALLEL=OFF \
    -DCGNS_ENABLE_TESTS=OFF -DCGNS_BUILD_TESTING=OFF -DCGNS_BUILD_CGNSTOOLS=OFF \
    -DCGNS_ENABLE_FORTRAN=OFF -DCGNS_ENABLE_LEGACY=OFF \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON -DCMAKE_PREFIX_PATH="$DEPS/hdf5"
  cmake --build "$WORK/build/cgns" -j "$JOBS"
  cmake --install "$WORK/build/cgns"
}

fltk() {
  unpack fltk "$FLTK_URL" "$FLTK_SHA256"
  cmake -S "$WORK/src/fltk" -B "$WORK/build/fltk" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX="$DEPS/fltk" -DOPTION_BUILD_SHARED_LIBS=OFF \
    -DOPTION_USE_GL=ON -DOPTION_CAIRO=OFF -DOPTION_CAIROEXT=OFF \
    -DOPTION_USE_SYSTEM_LIBPNG=ON -DOPTION_USE_SYSTEM_LIBJPEG=ON \
    -DOPTION_USE_SYSTEM_ZLIB=ON -DOPTION_BUILD_EXAMPLES=OFF \
    -DCMAKE_C_FLAGS=-fPIC -DCMAKE_CXX_FLAGS=-fPIC
  cmake --build "$WORK/build/fltk" -j "$JOBS"
  cmake --install "$WORK/build/fltk"
}

gmsh() {
  export CASROOT="$DEPS/occt"
  export PETSC_DIR="$DEPS/petsc" PETSC_ARCH=""
  export MED3HOME="$DEPS/med" CGNS_ROOT="$DEPS/cgns" HDF5_ROOT="$DEPS/hdf5"
  export PATH="$DEPS/fltk/bin:$PATH"
  export CMAKE_PREFIX_PATH="$DEPS/occt:$DEPS/mmg:$DEPS/petsc:$DEPS/hdf5:$DEPS/med:$DEPS/cgns:$DEPS/fltk"
  # Re-running keeps the CMake cache (configuring on a loaded disk takes long);
  # delete $WORK/build/gmsh by hand for a clean configure.
  # Gmsh 4.15.2 compiles src/plugin/CVTRemesh.cpp only with Revoropt on, and
  # that file calls GFace::setAllElementsVisible, which does not exist in the
  # 4.15.2 sources (the flag is GEntity::setOnlySomeElementsVisible). The wheel
  # is built without Revoropt, so it never compiles this file. The one-line
  # patch below is the whole local change to the sources and is idempotent.
  sed -i 's/res_face->setAllElementsVisible(true);/res_face->setOnlySomeElementsVisible(false);/' \
    "$GMSH_SRC/src/plugin/CVTRemesh.cpp"
  # The Plan 48 G15 patches (pins.sh GMSH_PATCHES). Idempotent: a patch that
  # reverses cleanly is already in the tree.
  for p in "${GMSH_PATCHES[@]}"; do
    if ( cd "$GMSH_SRC" && patch -p1 -R --dry-run -s -f < "$HERE/patches/$p" >/dev/null 2>&1 ); then
      echo "patch $p already applied"
    else
      ( cd "$GMSH_SRC" && patch -p1 -s < "$HERE/patches/$p" )
      echo "applied $p"
    fi
  done
  # PETSc was built C-only (no C++ compiler pass), so its C++ macros are set here.
  cmake -S "$GMSH_SRC" -B "$WORK/build/gmsh" "${GMSH_CMAKE_FLAGS[@]}" \
    -DCMAKE_CXX_FLAGS="-DPETSC_FUNCTION_NAME_CXX=__func__ -DPETSC_CXX_RESTRICT=__restrict__" \
    -DCMAKE_INSTALL_PREFIX="$WORK/install/$BUILD_ID"
  cmake --build "$WORK/build/gmsh" -j "$JOBS"
  cmake --install "$WORK/build/gmsh"
}

bundle() { bash "$HERE/bundle.sh"; }

for s in ${STAGES:-apt occt mmg petsc hdf5 med cgns fltk gmsh bundle}; do stage "$s"; done
echo "all stages done"
