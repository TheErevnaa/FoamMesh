#!/usr/bin/env python
"""Generate deterministic FoamMesh icon derivatives from the approved PNG."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, __version__ as PILLOW_VERSION


ROOT = Path(__file__).resolve().parents[1]
BRANDING = ROOT / 'src' / 'resources' / 'branding'
SOURCE = BRANDING / 'foammesh_app_icon.png'
WORDMARK_SOURCE = ROOT / 'white_logo.png'
PNG_SIZES = (128, 256)
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
WATERMARKS = ('light', 'dark')


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _square_source() -> Image.Image:
    image = Image.open(SOURCE).convert('RGBA')
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _wordmark_watermark(size: int = 256) -> Image.Image:
    """Fit the approved Erevnaa artwork into the square VTK logo surface."""
    image = Image.open(WORDMARK_SOURCE).convert('RGBA')
    bounds = image.getchannel('A').getbbox()
    if bounds is not None:
        image = image.crop(bounds)
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(
        image, ((size - image.width) // 2, (size - image.height) // 2))
    return canvas


def main() -> int:
    BRANDING.mkdir(parents=True, exist_ok=True)
    source = _square_source()
    wordmark = _wordmark_watermark()
    outputs: dict[str, dict] = {}
    for size in PNG_SIZES:
        path = BRANDING / f'foammesh_icon_{size}.png'
        source.resize((size, size), Image.Resampling.LANCZOS).save(
            path, format='PNG', optimize=True, compress_level=9)
        outputs[path.name] = {'sha256': _sha256(path), 'size': [size, size]}

    # Keep stable theme-specific resource names for the runtime binding. Both
    # currently use the approved white Erevnaa artwork supplied by the product.
    for theme in WATERMARKS:
        path = BRANDING / f'foammesh_watermark_{theme}.png'
        wordmark.save(
            path, format='PNG', optimize=True, compress_level=9)
        outputs[path.name] = {
            'sha256': _sha256(path), 'size': [256, 256], 'theme': theme,
            'artwork': 'approved white Erevnaa wordmark',
        }

    ico = BRANDING / 'foammesh.ico'
    source.save(ico, format='ICO', sizes=[(size, size) for size in ICO_SIZES])
    outputs[ico.name] = {'sha256': _sha256(ico), 'sizes': list(ICO_SIZES)}

    icns = BRANDING / 'foammesh.icns'
    source.save(icns, format='ICNS')
    with Image.open(icns) as decoded_icns:
        pixel_sizes = sorted({width * scale for width, _height, scale
                              in decoded_icns.info['sizes']})
    outputs[icns.name] = {'sha256': _sha256(icns), 'decoded_pixel_sizes': pixel_sizes}

    manifest = {
        'schema_version': 1,
        'source': {
            'path': SOURCE.name,
            'sha256': _sha256(SOURCE),
            'size': list(Image.open(SOURCE).size),
            'approval': 'approved_by_implementation_request_2026-07-13',
        },
        'wordmark': {
            'status': 'approved',
            'source': WORDMARK_SOURCE.name,
            'sha256': _sha256(WORDMARK_SOURCE),
        },
        'generator': {
            'command': 'python scripts/generate_branding_assets.py',
            'pillow': PILLOW_VERSION,
            'crop': 'centred square from source, then Lanczos resize',
        },
        'outputs': outputs,
    }
    (BRANDING / 'manifest.json').write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
