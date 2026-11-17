# foammesh-envhooks

A local PyPI path dependency of `pixi.toml`. It installs two files into the
env's site-packages:

- `foammesh_envhooks.pth` — one line, `import foammesh_envhooks`, which Python's
  `site` module runs at every interpreter start;
- `foammesh_envhooks.py` — on Windows, loads `System32\icuuc.dll` so PyPI Qt
  (PySide6 >= 6.11) imports inside the conda env; a no-op elsewhere.

So every Python started from the env (a GUI child, a test subprocess, a campaign
runner) can import PySide6 first, without `import foammesh`. `foammesh/support/win_icu.py`
stays for Anaconda/pip installs. Not part of `requirements*.txt` (pip users do
not need it; `gen_requirements.py` skips it). `_build.py` is a dependency-free
PEP 517 backend, so the package builds offline. Pixi reinstalls it only when
its version changes: bump `version` in `pyproject.toml` after editing the hook.
