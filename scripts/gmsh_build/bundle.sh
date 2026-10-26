#!/bin/bash
# Stage the built Gmsh as one installable folder and a tarball (Plan 48 GM16a).
#
# The folder holds libgmsh, gmsh.py, the gmsh executable and every shared
# library they need that the official wheel's libgmsh does not already need
# from the distribution. It is relocatable (RPATH is $ORIGIN), so the app
# copies and unpacks it under the distribution user's home, the way
# foammesh-sizefield is copied: no distribution image, no root, no download.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=pins.sh
source "$HERE/pins.sh"

INSTALL="$WORK/install/$BUILD_ID"
WHEEL_LIB="${WHEEL_LIB:-/usr/local/lib/libgmsh.so.4.15}"
rm -rf "$STAGE" && mkdir -p "$STAGE/lib" "$STAGE/bin"

LIBGMSH="$(ls "$INSTALL"/lib/libgmsh.so.4.15 2>/dev/null || ls "$INSTALL"/lib64/libgmsh.so.4.15)"
GMSHPY="$(ls "$INSTALL"/lib/gmsh.py 2>/dev/null || ls "$INSTALL"/lib64/gmsh.py)"
cp -L "$LIBGMSH" "$STAGE/lib/libgmsh.so.4.15"
cp -L "$GMSHPY" "$STAGE/lib/gmsh.py"
ln -s libgmsh.so.4.15 "$STAGE/lib/libgmsh.so"
[ -x "$INSTALL/bin/gmsh" ] && cp -L "$INSTALL/bin/gmsh" "$STAGE/bin/gmsh"
chmod u+w "$STAGE"/lib/* "$STAGE"/bin/* 2>/dev/null || true

# Names the wheel's libgmsh takes from the distribution: those stay there.
ldd "$WHEEL_LIB" | awk '$2=="=>"{print $1}' | sort -u > "$WORK/system-libs.txt"
# The C/C++ runtime and loader are never bundled.
cat >> "$WORK/system-libs.txt" <<'EOF'
libc.so.6
libm.so.6
libdl.so.2
libpthread.so.0
librt.so.1
libgcc_s.so.1
libstdc++.so.6
libgomp.so.1
libz.so.1
ld-linux-x86-64.so.2
linux-vdso.so.1
EOF
sort -u "$WORK/system-libs.txt" -o "$WORK/system-libs.txt"

bundled=()
collect() { # file ; copy each dependency that is not a system library
  local f="$1" name path
  while read -r name path; do
    grep -qx "$name" "$WORK/system-libs.txt" && continue
    [ -e "$STAGE/lib/$name" ] && continue
    cp -L "$path" "$STAGE/lib/$name"; chmod u+w "$STAGE/lib/$name"
    bundled+=("$name")
    collect "$STAGE/lib/$name"
  done < <(ldd "$f" | awk '$2=="=>" && $3 ~ /^\//{print $1, $3}')
}
collect "$STAGE/lib/libgmsh.so.4.15"
[ -x "$STAGE/bin/gmsh" ] && collect "$STAGE/bin/gmsh"

for f in "$STAGE"/lib/*.so*; do [ -L "$f" ] || patchelf --set-rpath '$ORIGIN' "$f"; done
[ -x "$STAGE/bin/gmsh" ] && patchelf --set-rpath '$ORIGIN/../lib' "$STAGE/bin/gmsh"

printf '%s\n' "${bundled[@]:-}" | sort > "$STAGE/BUNDLED_LIBRARIES.txt"
( cd "$STAGE" && find . -type f -o -type l | sort | xargs -d '\n' sha256sum 2>/dev/null ) > "$WORK/stage/$BUILD_ID.files.sha256" || true
mv "$WORK/stage/$BUILD_ID.files.sha256" "$STAGE/FILES.sha256"
tar -C "$WORK/stage" --owner=0 --group=0 --sort=name --mtime='2026-01-01 00:00:00' \
    -czf "$WORK/stage/$BUILD_ID.tar.gz" "$BUILD_ID"
sha256sum "$STAGE/lib/libgmsh.so.4.15" "$WORK/stage/$BUILD_ID.tar.gz"
ls -la "$WORK/stage"
