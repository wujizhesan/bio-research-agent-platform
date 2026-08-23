"""Deterministic image metadata and quality-control adapter for multimodal workflows."""
import hashlib
import json
import re
import struct
from pathlib import Path
from xml.etree import ElementTree


PLUGIN_NAME = 'Microscopy and image QC'
PLUGIN_VERSION = '0.1.0'
PLUGIN_API_VERSION = 1
PLUGIN_CAPABILITIES = (
    'imaging.image_qc',
)


def _parameters(properties, required=()):
    return {
        'type': 'object',
        'properties': properties,
        'required': list(required),
        'additionalProperties': False,
    }


def _envelope(operation, payload):
    return {
        'status': payload.get('status', 'ok'),
        'plugin': 'imaging',
        'operation': operation,
        'result': payload,
        'provenance': {
            'backend': PLUGIN_NAME,
            'version': PLUGIN_VERSION,
        },
    }


def _numeric(value):
    if value is None:
        return None
    match = re.match(r'^\s*([0-9]+(?:\.[0-9]+)?)', str(value))
    if not match:
        return None
    number = float(match.group(1))
    return int(number) if number.is_integer() else number


def _inspect_svg(path):
    root = ElementTree.parse(path).getroot()
    width = _numeric(root.attrib.get('width'))
    height = _numeric(root.attrib.get('height'))
    view_box = root.attrib.get('viewBox', '').replace(',', ' ').split()
    if (width is None or height is None) and len(view_box) == 4:
        width = _numeric(view_box[2])
        height = _numeric(view_box[3])
    if width is None or height is None:
        raise ValueError('SVG image requires width/height or a four-value viewBox')
    return {
        'format': 'svg',
        'width': width,
        'height': height,
        'channels': 'vector',
        'mode': 'vector',
        'frames': 1,
        'elements': max(0, sum(1 for _ in root.iter()) - 1),
    }


def _inspect_raster(path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ValueError('raster image inspection requires Pillow') from exc
    with Image.open(path) as image:
        return {
            'format': str(image.format or path.suffix.lstrip('.')).lower(),
            'width': image.width,
            'height': image.height,
            'channels': len(image.getbands()),
            'mode': image.mode,
            'frames': int(getattr(image, 'n_frames', 1)),
        }


def inspect_image(image_path, output_dir='output/imaging', modality='microscopy'):
    source = Path(image_path)
    if not source.is_file():
        raise ValueError(f'image does not exist: {source}')
    if source.suffix.lower() not in {'.svg', '.png', '.jpg', '.jpeg', '.tif', '.tiff'}:
        raise ValueError('unsupported image format; expected SVG, PNG, JPEG or TIFF')
    metrics = _inspect_svg(source) if source.suffix.lower() == '.svg' else _inspect_raster(source)
    raw = source.read_bytes()
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / 'image_qc.json'
    metrics.update({
        'file_bytes': len(raw),
        'sha256': hashlib.sha256(raw).hexdigest(),
    })
    payload = {
        'status': 'completed',
        'tool': 'deterministic-image-metadata',
        'input': str(source),
        'modality': str(modality),
        'metrics': metrics,
        'manifest_path': str(manifest_path),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return payload


TOOLS = {
    'inspect_image': {
        'description': 'Inspect microscopy or scientific image metadata with reproducible dimensions, channels and SHA-256 provenance.',
        'parameters': _parameters({
            'image_path': {'type': 'string'},
            'output_dir': {'type': 'string'},
            'modality': {'type': 'string'},
        }, ('image_path',)),
        'function': inspect_image,
    },
}
