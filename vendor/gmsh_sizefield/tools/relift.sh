#!/bin/bash
# Re-lift of the Gmsh sources behind foammesh-sizefield (Plan 41, GN25 S0).
#
#   tools/relift.sh [TARBALL] [OUTDIR]
#
# Unpacks gmsh-4.15.2-source.tgz (sha256 checked), copies every lifted file
# from its upstream path (table below), applies tools/foammesh.patch to the
# two modified size-field sources, and fails on any rejected hunk or missing
# upstream file. With the default OUTDIR it then compares the result to the
# vendored tree byte for byte and fails on any difference. Run it inside the
# WSL runtime (or any Linux with patch and tar).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$(dirname "$HERE")"
TARBALL="${1:-/opt/foammesh-src/gmsh-4.15.2-source.tgz}"
OUT="${2:-}"
SHA=be3f66f225d27ba9fa014f07e83169285da8a051b0e8ab7103d88066b39bdd3e
CHECK=0
if [ -z "$OUT" ]; then OUT="$(mktemp -d /tmp/relift.XXXXXX)"; CHECK=1; fi

[ "$(sha256sum "$TARBALL" | cut -d' ' -f1)" = "$SHA" ] || { echo "tarball sha256 mismatch" >&2; exit 1; }
WORK="$(mktemp -d /tmp/relift-src.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
tar -xzf "$TARBALL" -C "$WORK"
UP="$WORK/gmsh-4.15.2-source"
[ -d "$UP" ] || UP="$(find "$WORK" -maxdepth 1 -mindepth 1 -type d | head -1)"

mkdir -p "$OUT"
cp_up() { # upstream-relative vendored-relative
  [ -f "$UP/$1" ] || { echo "missing upstream file $1" >&2; exit 1; }
  mkdir -p "$OUT/$(dirname "$2")"
  cp "$UP/$1" "$OUT/$2"
}
cp_up LICENSE.txt LICENSE.txt
cp_up CREDITS.txt CREDITS.txt
# hxt: every file listed in tools/hxt_files.txt, path relative to contrib/hxt
while read -r f; do
  [ -z "$f" ] && continue
  cp_up "contrib/hxt/$f" "hxt/$f"
done < "$HERE/hxt_files.txt"
cp_up src/common/rtree.h third_party/gmsh/rtree.h
cp_up src/numeric/robustPredicates.h third_party/gmsh/robustPredicates.h
cp_up src/numeric/robustPredicates.cpp third_party/gmsh/robustPredicates.cpp
cp_up src/numeric/curvature.h third_party/gmsh/curvature.h
cp_up src/numeric/curvature.cpp third_party/gmsh/curvature.cpp
cp_up src/geo/SPoint3.h third_party/gmsh/SPoint3.h
cp_up src/geo/SVector3.h third_party/gmsh/SVector3.h
cp_up src/geo/SBoundingBox3d.h third_party/gmsh/SBoundingBox3d.h
cp_up src/mesh/automaticMeshSizeField.h src/automaticMeshSizeField.h
cp_up src/mesh/automaticMeshSizeField.cpp src/automaticMeshSizeField.cpp

(cd "$OUT" && patch -p1 --no-backup-if-mismatch --reject-file=- < "$HERE/foammesh.patch")

if [ "$CHECK" = 1 ]; then
  rc=0
  while read -r f; do
    cmp -s "$OUT/$f" "$VENDOR/$f" || { echo "DIFFERS: $f" >&2; rc=1; }
  done < <(cd "$OUT" && find . -type f | sed 's|^\./||' | sort)
  rm -rf "$OUT"
  [ $rc = 0 ] && echo "relift: every lifted file matches the vendored tree byte for byte"
  exit $rc
fi
echo "relift written to $OUT"
