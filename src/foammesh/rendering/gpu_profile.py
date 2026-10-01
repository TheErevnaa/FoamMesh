"""Which GPU draws the viewport, and how much to ask of it (Plan 37, 2026-10-01).

The user's rule: no fixed cell limit, and the display should lean on the GPU
instead of refusing. So the display budgets -- how many triangles the preview
keeps, above how many visible faces depth peeling stops, above how many the
view drops detail while the camera moves -- scale with the GPU that actually
renders, whatever its vendor.

**The adapters.** :func:`display_adapters` lists every adapter Windows has
(DXGI ``EnumAdapters1``: name, dedicated video memory, shared memory, the
software flag) and the order Windows itself ranks them in for high
performance (``IDXGIFactory6::EnumAdapterByGpuPreference``). Each gets a tier:

* **software** -- DXGI's software flag, or a software rasteriser's name
  ("GDI Generic", Microsoft Basic Render, llvmpipe, SwiftShader).
* **discrete** / **integrated** -- from the adapter's own properties first:
  at least :data:`DISCRETE_MIN_DEDICATED` of dedicated video memory reads as
  a card, under :data:`INTEGRATED_MAX_DEDICATED` as an iGPU's carve-out. In
  between (an APU with a large BIOS frame buffer, an old card) the name
  decides, from a table covering NVIDIA, AMD and Intel.

:func:`ranked` orders them discrete before integrated before software, then by
dedicated memory, then by Windows' own preference.

**What renders.** OpenGL on Windows does not let a process pick an adapter:
the driver hands out a context on the GPU it chooses (the main display's on a
desktop, the high-performance one on a switchable laptop when asked). So the
"use the best adapter that gives a working context, and fall through" order
is carried out as far as a process can:

1. :func:`prefer_discrete_gpu` asks for the high-performance GPU before any
   OpenGL loads (the NVIDIA Optimus shim's ``SHIM_MCCOMPAT``; AMD switchable
   graphics reads only an ``AmdPowerXpressRequestHighPerformance`` export
   in the executable, which the packaged bootloader does not carry). Each
   driver falls back to the
   integrated GPU on its own when the discrete one is off or absent.
2. The view draws with whatever context the driver gave. :func:`profile_from`
   matches the renderer string to the adapter list and budgets for *that*
   adapter -- its tier and its dedicated memory.
3. A renderer that cannot draw (software, OpenGL < 3.2, a lost context) is
   ``gl_health``'s graphics safe mode, as before: the app never refuses to
   start or blanks the view because a GPU is missing.
4. When the GPU drawing is ranked below one Windows lists,
   :func:`switch_hint` names both and says how to change it in Windows. No
   Windows setting or registry value is written here.

One OpenGL window renders on one GPU; nothing here splits a frame across
cards.

No Qt, no VTK at module scope.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

DISCRETE = 'discrete'
INTEGRATED = 'integrated'
SOFTWARE = 'software'
UNKNOWN = 'unknown'
#: The order adapters are preferred in; the app runs on any of them.
FALLBACK_ORDER = (DISCRETE, INTEGRATED, SOFTWARE)

MIB = 1024 * 1024
GIB = 1024 * MIB

#: Dedicated video memory at or above which an adapter reads as a card.
DISCRETE_MIN_DEDICATED = 2 * GIB
#: Below this it reads as an integrated GPU's carve-out.
INTEGRATED_MAX_DEDICATED = 1 * GIB

#: ``FOAMMESH_GPU=integrated`` keeps the process off the discrete GPU (a
#: switchable laptop on battery, a driver that misbehaves); ``discrete`` or
#: unset prefers it.
ENV_GPU = 'FOAMMESH_GPU'
#: NVIDIA Optimus: 0x800000001 asks the shim for the NVIDIA GPU; the shim
#: falls back to the integrated GPU when the NVIDIA one is not available.
NVIDIA_SHIM = 'SHIM_MCCOMPAT'
NVIDIA_SHIM_DISCRETE = '0x800000001'

_SOFTWARE = re.compile(r'gdi generic|llvmpipe|softpipe|swiftshader|'
                       r'microsoft basic render|software rasterizer|swrast|'
                       r'mesa offscreen', re.I)
#: Intel Arc A/B-series cards; the "Arc Graphics" of a Core Ultra is an iGPU.
_ARC = re.compile(r'intel.*\barc\b.*\b[ab]\d{3}', re.I)
_INTEGRATED = re.compile(
    r'intel.*(uhd|hd graphics|iris|graphics \d|\bgraphics$)|'
    r'^amd radeon\(tm\) graphics|^amd radeon graphics|radeon\(tm\) vega|'
    r'radeon vega \d+ graphics|radeon \d{3}m\b|'
    r'apple m\d|mali|adreno|powervr', re.I)
_DISCRETE = re.compile(
    r'nvidia|geforce|quadro|\brtx\b|\bgtx\b|tesla|'
    r'radeon (rx|pro|vii|r9|hd)|radeon\(tm\) (rx|pro)|firepro|instinct', re.I)


def classify(renderer: str) -> str:
    """The tier an adapter or OpenGL renderer *name* suggests."""
    text = str(renderer or '').strip()
    if not text:
        return UNKNOWN
    if _SOFTWARE.search(text):
        return SOFTWARE
    if _ARC.search(text):
        return DISCRETE
    if _INTEGRATED.search(text):
        return INTEGRATED
    if _DISCRETE.search(text):
        return DISCRETE
    return UNKNOWN


@dataclass(frozen=True)
class Adapter:
    name: str
    dedicated_bytes: int = 0
    shared_bytes: int = 0
    software: bool = False
    #: Windows' high-performance order (0 first); None when not reported.
    preference: int | None = None
    tier: str = field(default=UNKNOWN)

    def to_dict(self) -> dict:
        return asdict(self)


def adapter_tier(name: str, dedicated_bytes: int = 0,
                 software: bool = False) -> str:
    """An adapter's tier from its own properties, the name breaking ties."""
    if software or classify(name) == SOFTWARE:
        return SOFTWARE
    dedicated = int(dedicated_bytes or 0)
    by_name = classify(name)
    if dedicated >= DISCRETE_MIN_DEDICATED and by_name != INTEGRATED:
        return DISCRETE
    if 0 < dedicated < INTEGRATED_MAX_DEDICATED and by_name != DISCRETE:
        return INTEGRATED
    if by_name != UNKNOWN:
        return by_name
    if dedicated >= DISCRETE_MIN_DEDICATED:
        return DISCRETE
    return INTEGRATED if dedicated else UNKNOWN


def make_adapter(name, dedicated_bytes=0, shared_bytes=0, software=False,
                 preference=None) -> Adapter:
    return Adapter(str(name), int(dedicated_bytes or 0),
                   int(shared_bytes or 0), bool(software), preference,
                   adapter_tier(name, dedicated_bytes, software))


_TIER_RANK = {DISCRETE: 0, INTEGRATED: 1, UNKNOWN: 2, SOFTWARE: 3}


def ranked(adapters) -> list[Adapter]:
    """Best first: discrete, integrated, software; then dedicated memory;
    then the order Windows prefers them in for high performance."""
    def key(adapter: Adapter):
        preference = (adapter.preference if adapter.preference is not None
                      else 1 << 30)
        return (_TIER_RANK.get(adapter.tier, 2), -adapter.dedicated_bytes,
                preference)
    return sorted(adapters, key=key)


def _normal(name: str) -> str:
    text = re.sub(r'\((tm|r|c)\)', ' ', str(name or ''), flags=re.I)
    text = text.split('/')[0]                     # "/PCIe/SSE2"
    text = re.sub(r'\b(series|graphics adapter)\b', ' ', text, flags=re.I)
    return ' '.join(text.lower().split())


def match_adapter(renderer: str, adapters) -> Adapter | None:
    """The adapter an OpenGL renderer string names, or None."""
    wanted = _normal(renderer)
    if not wanted:
        return None
    best = None
    for adapter in adapters:
        name = _normal(adapter.name)
        if not name:
            continue
        if name == wanted:
            return adapter
        if name in wanted or wanted in name:
            if best is None or len(_normal(best.name)) < len(name):
                best = adapter
    return best


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DisplayBudget:
    """What one GPU is asked to draw. All display limits: above them the
    picture is decimated or simplified and says so; nothing is refused."""
    tier: str
    #: Triangles the boundary preview keeps before the worker decimates it.
    preview_max_triangles: int
    #: Bytes the preview's files may take before the window opens them.
    preview_max_bytes: int
    #: Visible faces above which translucency is not depth-peeled.
    peeling_faces: int
    #: Visible faces above which the view drops detail while the camera moves.
    interactive_lod_faces: int
    #: Triangles the whole scene's interaction copy is reduced to.
    lod_target_triangles: int

    def to_dict(self) -> dict:
        return asdict(self)


#: Bytes per preview triangle in the worker's .vtp (float32 points,
#: connectivity and offsets): MEASURED 36.0 by
#: ``scripts/measure_display_budget.py`` on 5 M and 20 M-quad surfaces
#: (2026-10-01); rounded up for cell data and triangulated surfaces.
PREVIEW_BYTES_PER_TRIANGLE = 48
#: GPU bytes a drawn triangle costs with the surface-with-edges style (vertex
#: and normal buffers, index buffer, the edge pass) on VTK's OpenGL2 mapper.
GPU_BYTES_PER_TRIANGLE = 80
#: Share of the dedicated memory the scene may take; the rest is the frame
#: buffers, peeling layers and everything else on the card.
GPU_MEMORY_SHARE = 0.4
#: The most a preview keeps whatever the card: past this the read-back of the
#: .vtp on the VTK thread, not the GPU, is what the user waits for.
PREVIEW_CEILING_TRIANGLES = 200_000_000

_FIXED = {
    # The figures in force before 2026-10-01 (resource_budget D12 and
    # gl_health.FACE_BUDGET), for a view that has not drawn yet.
    UNKNOWN: DisplayBudget(UNKNOWN, 2_000_000, 256 * MIB, 1_000_000,
                           1_000_000, 500_000),
    SOFTWARE: DisplayBudget(SOFTWARE, 1_000_000, 128 * MIB, 200_000,
                            250_000, 100_000),
    # MEASURED on an AMD Radeon(TM) Graphics iGPU, off screen at 1280x800
    # (scripts/measure_display_budget.py, 2026-10-01): a still frame of 5 M
    # quads 36 ms, of 20 M quads 120 ms; the 200 k-cell reduced copy 8 ms. So
    # drags reduce above 4 M faces and the preview keeps 8 M triangles.
    INTEGRATED: DisplayBudget(INTEGRATED, 8_000_000,
                              8_000_000 * PREVIEW_BYTES_PER_TRIANGLE,
                              1_000_000, 4_000_000, 400_000),
    # A card whose memory nobody reported is budgeted as an 8 GB one.
    DISCRETE: DisplayBudget(DISCRETE, 40_000_000,
                            40_000_000 * PREVIEW_BYTES_PER_TRIANGLE,
                            8_000_000, 12_000_000, 1_500_000),
}


def budget_for(tier: str, gpu_memory_bytes: int | None = None) -> DisplayBudget:
    """The display budget of a tier; a discrete card's scales with its memory."""
    base = _FIXED.get(tier, _FIXED[UNKNOWN])
    if tier != DISCRETE or not gpu_memory_bytes or gpu_memory_bytes <= 0:
        return base
    triangles = int(gpu_memory_bytes * GPU_MEMORY_SHARE / GPU_BYTES_PER_TRIANGLE)
    triangles = max(_FIXED[INTEGRATED].preview_max_triangles,
                    min(PREVIEW_CEILING_TRIANGLES, triangles))
    scale = triangles / base.preview_max_triangles
    return DisplayBudget(
        DISCRETE, triangles, int(triangles * PREVIEW_BYTES_PER_TRIANGLE),
        max(_FIXED[INTEGRATED].peeling_faces, int(base.peeling_faces * scale)),
        max(_FIXED[INTEGRATED].interactive_lod_faces,
            int(base.interactive_lod_faces * min(scale, 3.0))),
        base.lod_target_triangles)


# --------------------------------------------------------------------------
# The GPU in use
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GpuProfile:
    renderer: str
    vendor: str
    tier: str
    gpu_memory_bytes: int
    #: Every adapter Windows lists, best first.
    adapters: tuple
    #: The listed adapter the renderer string names, or None.
    adapter: Adapter | None
    budget: DisplayBudget

    def best_adapter(self) -> Adapter | None:
        candidates = [a for a in self.adapters if a.tier != SOFTWARE]
        return candidates[0] if candidates else None

    def to_dict(self) -> dict:
        data = asdict(self)
        data['adapters'] = [a.to_dict() for a in self.adapters]
        return data


_current: GpuProfile | None = None


def profile_from(info: dict | None, adapters=None) -> GpuProfile:
    """The profile for the GL strings ``info`` (as ``gl_health`` records them).

    The tier and memory come from the matched adapter's own properties when
    the renderer names a listed adapter; else from the renderer string and
    whatever memory the driver reported through OpenGL.
    """
    info = dict(info or {})
    renderer = str(info.get('renderer') or '')
    if adapters is None:
        adapters = display_adapters()
    adapters = tuple(ranked(adapters))
    adapter = match_adapter(renderer, adapters)
    try:
        gl_memory = int(info.get('gpu_memory_bytes') or 0)
    except (TypeError, ValueError):
        gl_memory = 0
    if adapter is not None and classify(renderer) != SOFTWARE:
        tier = adapter.tier
        memory = adapter.dedicated_bytes or gl_memory
    else:
        tier = adapter_tier(renderer, gl_memory) if renderer else UNKNOWN
        if tier == UNKNOWN and renderer:
            tier = INTEGRATED          # a named renderer nobody knows: modest
        memory = gl_memory
    return GpuProfile(renderer, str(info.get('vendor') or ''), tier, memory,
                      adapters, adapter, budget_for(tier, memory))


def set_current(info: dict | None, adapters=None) -> GpuProfile:
    """Record the GPU a view is drawing with; the budgets follow it."""
    global _current
    profile = profile_from(info, adapters)
    if _current is None or (_current.renderer, _current.gpu_memory_bytes) != (
            profile.renderer, profile.gpu_memory_bytes):
        logger.info('Viewport GPU: %s (%s, %s); adapters: %s; display '
                    'budget %s', profile.renderer or 'unknown', profile.tier,
                    _format_memory(profile.gpu_memory_bytes),
                    '; '.join(f'{a.name} ({a.tier}, '
                              f'{_format_memory(a.dedicated_bytes)})'
                              for a in profile.adapters) or 'none listed',
                    profile.budget.to_dict())
    _current = profile
    return profile


def current(log_directory=None) -> GpuProfile:
    """The GPU this process draws with; else the one the last session used.

    Before any view has drawn, the strings ``gl_health`` recorded in the logs
    folder stand in (same machine, same driver choice as a rule); with
    nothing recorded the budget is the ``unknown`` tier's.
    """
    if _current is not None:
        return _current
    info = {}
    try:
        from foammesh.rendering import gl_health
        if log_directory is None:
            try:
                from foammesh.support import lifecycle
                log_directory = lifecycle.log_directory()
            except Exception:                              # noqa: BLE001
                log_directory = None
        info = gl_health.recorded(log_directory)
    except Exception:                                      # noqa: BLE001
        info = {}
    return profile_from(info, adapters=())


def display_budget(log_directory=None) -> DisplayBudget:
    return current(log_directory).budget


#: Share of the memory free now that the preview the window holds may take.
PREVIEW_MEMORY_SHARE = 0.25


def preview_budget(budget: DisplayBudget | None = None,
                   available: int | None = None) -> tuple[int, int]:
    """``(max_triangles, max_bytes)`` for the boundary preview.

    The user's rule (2026-10-01): no fixed display cap. The triangles the
    preview keeps follow the GPU that draws it (2 M before a view has drawn,
    8 M on an integrated GPU, scaled with a discrete card's memory -- about
    100 M on a 20 GB card). The bytes are the window's own memory, so they
    are also held to :data:`PREVIEW_MEMORY_SHARE` of what is free now, and
    never below resource_budget's D12 figures (2 M triangles, 256 MB).
    """
    from foammesh.support import resource_budget

    shown = budget if budget is not None else display_budget()
    floor_triangles = resource_budget.PREVIEW_MAX_TRIANGLES
    floor_bytes = resource_budget.PREVIEW_MAX_BYTES
    triangles = max(floor_triangles, shown.preview_max_triangles)
    limit = max(floor_bytes, shown.preview_max_bytes)
    if available is None:
        try:
            available = resource_budget.physical_memory()[1]
        except Exception:                                  # noqa: BLE001
            available = 0
    if available:
        limit = max(floor_bytes, min(limit, int(available * PREVIEW_MEMORY_SHARE)))
        per_triangle = max(1, shown.preview_max_bytes
                           // max(1, shown.preview_max_triangles))
        triangles = max(floor_triangles, min(triangles, limit // per_triangle))
    return int(triangles), int(limit)


def reset() -> None:
    """Tests only."""
    global _current, _adapters
    _current = None
    _adapters = None


def _format_memory(value: int) -> str:
    return f'{value / GIB:.1f} GB' if value else 'memory not reported'


# --------------------------------------------------------------------------
# What Windows lists
# --------------------------------------------------------------------------

_adapters: tuple | None = None


def display_adapters() -> tuple:
    """Every adapter Windows lists, best first; read-only, cached."""
    global _adapters
    if _adapters is not None:
        return _adapters
    found: list[Adapter] = []
    if sys.platform == 'win32':
        try:
            found = _dxgi_adapters()
        except Exception:                                  # noqa: BLE001
            logger.debug('DXGI adapter listing failed', exc_info=True)
            found = []
        if not found:
            try:
                found = [make_adapter(name) for name in _gdi_adapter_names()]
            except Exception:                              # noqa: BLE001
                found = []
    _adapters = tuple(ranked(found))
    return _adapters


def _gdi_adapter_names() -> list[str]:
    import ctypes
    from ctypes import wintypes

    class DisplayDevice(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD),
                    ('DeviceName', wintypes.WCHAR * 32),
                    ('DeviceString', wintypes.WCHAR * 128),
                    ('StateFlags', wintypes.DWORD),
                    ('DeviceID', wintypes.WCHAR * 128),
                    ('DeviceKey', wintypes.WCHAR * 128)]

    names: list[str] = []
    user32 = ctypes.windll.user32
    for index in range(64):
        device = DisplayDevice()
        device.cb = ctypes.sizeof(DisplayDevice)
        if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
            break
        name = device.DeviceString.strip()
        if name and name not in names:
            names.append(name)
    return names


def _guid(text: str):
    import ctypes
    import uuid

    class GUID(ctypes.Structure):
        _fields_ = [('data', ctypes.c_ubyte * 16)]

    value = GUID()
    ctypes.memmove(value.data, uuid.UUID(text).bytes_le, 16)
    return value


_IID_FACTORY1 = '770aae78-f26f-4dba-a829-253c83d1b387'
_IID_FACTORY6 = 'c1b6694f-ff09-44a9-b03c-77900a0a1d17'
_IID_ADAPTER1 = '29038f61-3839-4626-91fd-086879011a05'
_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 2
_DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE = 2


def _dxgi_adapters() -> list[Adapter]:
    """DXGI's adapters with their memory, in Windows' high-performance order."""
    import ctypes
    from ctypes import wintypes

    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]

    class Desc1(ctypes.Structure):
        _fields_ = [('Description', wintypes.WCHAR * 128),
                    ('VendorId', wintypes.UINT), ('DeviceId', wintypes.UINT),
                    ('SubSysId', wintypes.UINT), ('Revision', wintypes.UINT),
                    ('DedicatedVideoMemory', ctypes.c_size_t),
                    ('DedicatedSystemMemory', ctypes.c_size_t),
                    ('SharedSystemMemory', ctypes.c_size_t),
                    ('AdapterLuid', LUID), ('Flags', wintypes.UINT)]

    HRESULT = ctypes.c_long

    def method(obj, index, *argtypes):
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(
            ctypes.c_void_p)))[0]
        prototype = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, *argtypes)
        return lambda *args: prototype(vtable[index])(obj, *args)

    def release(obj):
        if obj:
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(
                ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(
                    ctypes.c_void_p)))[0][2])(obj)

    def describe(adapter):
        desc = Desc1()
        method(adapter, 10, ctypes.POINTER(Desc1))(ctypes.byref(desc))
        return desc

    dxgi = ctypes.WinDLL('dxgi')
    factory = ctypes.c_void_p()
    iid = _guid(_IID_FACTORY1)
    if dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
        return []
    try:
        descs: list = []
        for index in range(32):
            adapter = ctypes.c_void_p()
            hr = method(factory.value, 12, wintypes.UINT,
                        ctypes.POINTER(ctypes.c_void_p))(
                index, ctypes.byref(adapter))
            if hr != 0:
                break
            try:
                descs.append(describe(adapter.value))
            finally:
                release(adapter.value)
        preference: dict = {}
        factory6 = ctypes.c_void_p()
        iid6 = _guid(_IID_FACTORY6)
        if method(factory.value, 0, ctypes.c_void_p,
                  ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(iid6), ctypes.byref(factory6)) == 0:
            try:
                iid_adapter = _guid(_IID_ADAPTER1)
                for index in range(32):
                    adapter = ctypes.c_void_p()
                    hr = method(factory6.value, 29, wintypes.UINT, ctypes.c_int,
                                ctypes.c_void_p,
                                ctypes.POINTER(ctypes.c_void_p))(
                        index, _DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE,
                        ctypes.byref(iid_adapter), ctypes.byref(adapter))
                    if hr != 0:
                        break
                    try:
                        luid = describe(adapter.value).AdapterLuid
                        preference.setdefault(
                            (luid.LowPart, luid.HighPart), index)
                    finally:
                        release(adapter.value)
            finally:
                release(factory6.value)
    finally:
        release(factory.value)
    adapters = []
    seen = set()
    for desc in descs:
        luid = (desc.AdapterLuid.LowPart, desc.AdapterLuid.HighPart)
        if luid in seen:
            continue
        seen.add(luid)
        adapters.append(make_adapter(
            desc.Description.strip(), desc.DedicatedVideoMemory,
            desc.SharedSystemMemory,
            bool(desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE),
            preference.get(luid)))
    return adapters


# --------------------------------------------------------------------------
# What to tell the user
# --------------------------------------------------------------------------

def switch_hint(profile: GpuProfile) -> str:
    """What to tell the user when a better GPU sits idle, or ''.

    Only when the view is drawn by an adapter ranked below a discrete one
    Windows lists -- an integrated or software renderer next to a card, or
    the smaller of two cards -- the cases where a Windows setting would make
    the viewport faster.
    """
    best = profile.best_adapter()
    if best is None or best.tier != DISCRETE or not profile.renderer:
        return ''
    using = profile.adapter
    if using is not None and (using is best or using.name == best.name):
        return ''
    if using is None and profile.tier == DISCRETE:
        return ''                     # a card we could not match: leave it
    if using is not None and using.tier == DISCRETE and \
            using.dedicated_bytes >= best.dedicated_bytes:
        return ''
    return (f'The viewport is drawn by {profile.renderer}, not the {best.name} '
            f'this PC also has, so large meshes are shown with less detail. '
            f'OpenGL draws on the GPU Windows hands it (on a desktop, the GPU '
            f'of the main display). To use the {best.name}: in Settings > '
            f'System > Display > Graphics set FoamMesh to "High performance"; '
            f'if that does not change it, make a display connected to the '
            f'{best.name} the main display, or pick it as the OpenGL GPU for '
            f'FoamMesh in the card maker\'s control panel. Then restart '
            f'FoamMesh.')


# --------------------------------------------------------------------------
# Preferring the discrete GPU, in process
# --------------------------------------------------------------------------

def prefer_discrete_gpu(environ=None, adapters=None) -> str | None:
    """Ask the driver for the high-performance GPU before OpenGL is loaded.

    Returns the variable it set, or None. The NVIDIA Optimus shim is the one
    driver that reads an in-process request (``SHIM_MCCOMPAT``), and it falls
    back to the integrated GPU on its own when the NVIDIA one is off or
    missing, so setting it never leaves the process without a renderer. AMD
    switchable graphics reads only the ``AmdPowerXpressRequestHighPerformance``
    export of the executable, and Intel Arc + Intel iGPU systems only the
    Windows preference. Nothing is set when the user has a value of their
    own, when ``FOAMMESH_GPU=integrated`` asks to stay off the discrete GPU,
    or when no NVIDIA adapter is listed.
    """
    environ = os.environ if environ is None else environ
    if str(environ.get(ENV_GPU, '')).strip().lower() == INTEGRATED:
        return None
    if NVIDIA_SHIM in environ:
        return None
    if adapters is None:
        if sys.platform != 'win32':
            return None
        adapters = display_adapters()
    if not any('nvidia' in str(getattr(a, 'name', a)).lower()
               for a in adapters):
        return None
    environ[NVIDIA_SHIM] = NVIDIA_SHIM_DISCRETE
    return NVIDIA_SHIM


# --------------------------------------------------------------------------
# The card's memory, from a current context
# --------------------------------------------------------------------------

GL_GPU_MEMORY_INFO_DEDICATED_VIDMEM_NVX = 0x9047
GL_VBO_FREE_MEMORY_ATI = 0x87FB


def gl_memory_bytes() -> int:
    """Dedicated memory of the GPU whose context is current, or 0.

    A fallback for an adapter DXGI did not list: ``GL_NVX_gpu_memory_info``
    (total dedicated, KiB) or ``GL_ATI_meminfo`` (free buffer memory, KiB).
    An enum the driver does not know only sets GL_INVALID_ENUM, which is read
    back and cleared.
    """
    if sys.platform != 'win32':
        return 0
    try:
        import ctypes
        gl = ctypes.windll.opengl32
        gl.glGetError()
        for enum in (GL_GPU_MEMORY_INFO_DEDICATED_VIDMEM_NVX,
                     GL_VBO_FREE_MEMORY_ATI):
            values = (ctypes.c_int * 4)()
            gl.glGetIntegerv(enum, values)
            if gl.glGetError() == 0 and values[0] > 0:
                return int(values[0]) * 1024
    except Exception:                                      # noqa: BLE001
        return 0
    return 0


__all__ = ['Adapter', 'DisplayBudget', 'GpuProfile', 'FALLBACK_ORDER',
           'adapter_tier', 'budget_for', 'classify', 'current',
           'display_adapters', 'display_budget', 'make_adapter',
           'match_adapter', 'prefer_discrete_gpu', 'preview_budget',
           'profile_from', 'ranked',
           'set_current', 'switch_hint']
