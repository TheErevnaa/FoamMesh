#!/bin/bash
# Regenerates tools/foammesh.patch from the upstream tarball and the two
# modified size-field sources in the vendored tree (maintainer use).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENDOR="$(dirname "$HERE")"
TARBALL="${1:-/opt/foammesh-src/gmsh-4.15.2-source.tgz}"
WORK="$(mktemp -d /tmp/mkpatch.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
tar -xzf "$TARBALL" -C "$WORK"
UP="$(find "$WORK" -maxdepth 1 -mindepth 1 -type d | head -1)"
mkdir -p "$WORK/a/src" "$WORK/b/src"
for f in automaticMeshSizeField.h automaticMeshSizeField.cpp; do
  cp "$UP/src/mesh/$f" "$WORK/a/src/$f"
  cp "$VENDOR/src/$f" "$WORK/b/src/$f"
done
cd "$WORK"
diff -u -r a b > "$HERE/foammesh.patch" || true
