# foammesh-sizefield (Linux x86-64)

The automatic size-field tool (Plan 41 GN25), shipped with FoamMesh. The app
copies this one file into the user's own OpenFOAM 13 WSL distribution when the
user asks ("Set up automatic sizing"): `$HOME/.local/share/foammesh/sizefield/`.
No root, no distribution image, no download.

| | |
|---|---|
| file | `foammesh-sizefield` |
| version | `foammesh-sizefield 1.0.0 (gmsh-4.15.2 AutomaticMeshSizeField, p4est 2.2)` |
| size | 952,912 bytes |
| sha256 | `18b8259a3a26b294e1aeed8d02cf2a2b4f543f95d58788ca0a7dbd691ae342c2` |

`tests/unit/test_gn25e_sizefield_tool_setup.py` fails if the sha256 here and the
file differ.

## How it was built

Inside the WSL distribution `OpenFOAM13Runtime` (Ubuntu 22.04, `libp4est-dev` and
`libsc-dev` installed there), from `vendor/gmsh_sizefield`:

    BUILD_DIR=/tmp/sf-static-build PREFIX=/tmp/sf-static-prefix \
        bash scripts/build_sizefield.sh

`build_sizefield.sh` passes `-DFOAMMESH_STATIC_P4EST=ON` when `libp4est.a` and
`libsc.a` are installed, so **p4est and libsc are linked into the binary**. What
it still needs at run time is what an OpenFOAM 13 distribution already has:
glibc, libstdc++, libgcc_s, libgomp, zlib, OpenMPI (`libmpi`, `libopen-rte`,
`libopen-pal`) and their `libhwloc`, `libevent_core`, `libevent_pthreads`,
`libudev`. `ldd` of the bundle lists nothing else.

Rebuild it after any change under `vendor/gmsh_sizefield`, replace the file here
and update the size and sha256 above.
