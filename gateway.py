#!/usr/bin/env python3
"""FDOS Draw Things Gateway v1.7 — OpenAI-compatible image API -> local Draw Things CLI only.

Adds:
- alias model IDs (for Cherry Studio UI profiles such as gemini-3.1-flash-image-preview)
- aspect ratio + image size mapping to width/height
- optional JSON config file so the real Draw Things model can be changed without code edits
- debug logging of incoming image requests
"""

import base64
import json
import os
import time
import uuid
import shutil
import subprocess
import tempfile
import io
import re
import threading
import traceback
from email.parser import BytesParser
from email.policy import default as email_policy
from datetime import datetime, timezone
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen

HOST = '127.0.0.1'
PORT = int(os.environ.get('FDOS_GATEWAY_PORT', '7777'))
MAX_REQUEST_BYTES = 256 * 1024
MAX_REPLY_BYTES = 100 * 1024 * 1024
CONFIG_PATH = os.environ.get('FDOS_GATEWAY_CONFIG', os.path.join(os.path.dirname(__file__), 'gateway_config.json'))

DEFAULT_CONFIG = {
    "model_aliases": {
        # Cherry Studio UI profile model ID -> actual Draw Things model ID
        "gemini-3.1-flash-image-preview": "z_image_turbo_1.0_i8x.ckpt"
    },
    # safe: keep within lighter local limits; true_4k: allow full 4096 long edge when user picks 4K
    "resolution_profile": "safe",
    "default_aspect_ratio": "16:9",
    "default_image_size": "1K",
    "debug_log_requests": True,
    # Optional hard cap. In safe mode we keep 4K at 2048 long edge. In true_4k mode we allow 4096.
    "max_side": 4096,
    "save_directory": "~/Pictures/FDOS-DrawThings",
}

RATIO_PRESETS = {
    '1:1': (1, 1),
    '3:4': (3, 4),
    '4:3': (4, 3),
    '9:16': (9, 16),
    '16:9': (16, 9),
    '2:3': (2, 3),
    '3:2': (3, 2),
    '21:9': (21, 9),
}

# Long edge targets. Safe profile avoids dangerous local sizes for 24 GB unified memory.
LONG_EDGE_BY_TIER = {
    'safe': {
        '1K': 1024,
        '2K': 2048,
        '4K': 2048,
    },
    'true_4k': {
        '1K': 1024,
        '2K': 2048,
        '4K': 4096,
    },
}

MODEL_CACHE_TTL = 10
_MODEL_CACHE = {'at': 0.0, 'items': []}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                user_cfg = json.load(f)
            if isinstance(user_cfg, dict):
                for k, v in user_cfg.items():
                    cfg[k] = v
        except Exception as exc:
            print(f'[config] failed to load {CONFIG_PATH}: {exc}', flush=True)
    return cfg


CONFIG = load_config()


def cli_path():
    """Discover installed CLI, optionally overridden by FDOS_DRAW_THINGS_CLI."""
    setting = os.environ.get('FDOS_DRAW_THINGS_CLI')
    cli = setting or shutil.which('draw-things-cli')
    if not cli or not Path(cli).is_file():
        raise RuntimeError('draw-things-cli not found. Install CLI or set FDOS_DRAW_THINGS_CLI.')
    return cli


def parse_models_list(stdout):
    """Parse `draw-things-cli models list --downloaded-only` fixed-spacing text table.

    Model ID is the first token; remaining columns are separated by two or more
    spaces. Fail visibly if the CLI changes its table format.
    """
    items = []
    seen = set()
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith(('MODEL ', 'Models directory:', '-')):
            continue
        cells = re.split(r'\s{2,}', line)
        if len(cells) < 4 or not re.fullmatch(r'[A-Za-z0-9_.-]+\.ckpt', cells[0]):
            continue
        model_id, name = cells[:2]
        if cells[3].lower() not in ('yes', 'true'):
            continue
        # Exclude video families from Painting; v1.7 is still-images only.
        video_stems = ('ltx_', 'longcat_', 'wan_', 'hunyuan_video', 'cosmos_video')
        if model_id.lower().startswith(video_stems):
            continue
        if model_id in seen:
            continue
        items.append({'id': model_id, 'name': name,
                      'downloaded': True, 'type': 'base_model', 'domain': 'image',
                      'source': cells[2]})
        seen.add(model_id)
    return items


def available_models(force=False):
    now = time.monotonic()
    if not force and _MODEL_CACHE['items'] and now - _MODEL_CACHE['at'] < MODEL_CACHE_TTL:
        return _MODEL_CACHE['items']
    cli = cli_path()
    proc = subprocess.run([cli, 'models', 'list', '--downloaded-only'],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError('CLI model listing failed: '+(proc.stderr or proc.stdout)[-1500:])
    parsed = parse_models_list(proc.stdout)
    if not parsed:
        raise RuntimeError('CLI returned no downloaded image models; check model list format')
    _MODEL_CACHE['at'] = now
    _MODEL_CACHE['items'] = parsed
    return parsed


# Three profiles per installed image model. Ratio is selected in Cherry's GPT Image size menu.
# The gpt-image-1@ suffix is used to attempt reuse of GPT Image controls.
# Actual Cherry Studio behavior depends on installed version; verify with raw_request.
PROFILE_PREFIX = 'gpt-image-1@fdos--'
PROFILE_TIERS = ('1k', '2k', '4k')
CHAT_PROFILE_PREFIX = 'fdos-chat--'
MAX_CHAT_REQUEST_BYTES = 100 * 1024 * 1024
CHERRY_SIZE_TO_RATIO = {
    '1024x1024': '1:1',
    '1536x1024': '16:9',
    '1024x1536': '9:16',
}


def profile_id(base_model, tier):
    return f'{PROFILE_PREFIX}{base_model}--{tier}'


def parse_profile_id(requested_model):
    if not isinstance(requested_model, str) or not requested_model.startswith(PROFILE_PREFIX):
        return None
    payload = requested_model[len(PROFILE_PREFIX):]
    parts = payload.rsplit('--', 1)
    if len(parts) != 2:
        return None
    model_id, tier = parts
    if tier == 'edit':
        model_id, tier = model_id.rsplit('--', 1) if '--' in model_id else ('', '')
    if not model_id or len(model_id) > 250 or tier not in PROFILE_TIERS:
        return None
    if not all(c.isalnum() or c in '._-' for c in model_id):
        return None
    return model_id, tier.upper()


def chat_profile_id(base_model, tier):
    return f'{CHAT_PROFILE_PREFIX}{base_model}--{tier}'


def parse_chat_profile_id(requested_model):
    if not isinstance(requested_model, str) or not requested_model.startswith(CHAT_PROFILE_PREFIX):
        return None
    payload = requested_model[len(CHAT_PROFILE_PREFIX):]
    parts = payload.rsplit('--', 1)
    if len(parts) != 2 or parts[1] not in PROFILE_TIERS:
        return None
    model_id = parts[0]
    if not model_id or len(model_id) > 250 or not all(c.isalnum() or c in '._-' for c in model_id):
        return None
    return model_id, parts[1].upper()


def dimensions_for_profile(body, tier):
    # GPT Image's size menu sends three dimensions which we reinterpret as ratios.
    # Model ID controls only resolution tier. Do not read 'quality' as a tier.
    cherry_size = find_first_string(body, 'size')
    if cherry_size is None or cherry_size.strip().lower() == 'auto':
        ratio = CONFIG.get('default_aspect_ratio', '1:1')
        source = 'model_tier+default_ratio'
    else:
        ratio = CHERRY_SIZE_TO_RATIO.get(cherry_size.strip().lower())
        if ratio is None:
            raise ValueError('Unsupported Cherry size for profile: %r. Expected 1024x1024, 1536x1024, 1024x1536.' % cherry_size)
        source = 'model_tier+cherry_size'
    rw, rh = RATIO_PRESETS[ratio]
    long_edge = {'1K': 1024, '2K': 2048, '4K': 4096}[tier]
    if rw >= rh:
        width = long_edge
        height = clamp_multiple_64(long_edge * rh / rw)
    else:
        height = long_edge
        width = clamp_multiple_64(long_edge * rw / rh)
    return width, height, ratio, tier, source


def advertised_models():
    data = []
    seen = set()
    downloaded = available_models()
    for m in downloaded:
        base = m['id']
        name = str(m.get('name') or base)
        supports_edit = True  # Publishing edits; actual support varies by model
        for tier in PROFILE_TIERS:
            model_id = profile_id(base, tier)
            data.append({'id': model_id, 'object': 'model', 'created': 0,
                         'owned_by': 'fdos-draw-things',
                         'name': f'{name} · {tier.upper()}'})
            seen.add(model_id)
            chat_id = chat_profile_id(base, tier)
            data.append({'id': chat_id, 'object': 'model', 'created': 0,
                         'owned_by': 'fdos-draw-things',
                         'name': f'{name} · {tier.upper()} · Chat image'})
            seen.add(chat_id)
            if supports_edit:
                edit_id = model_id + '--edit'
                data.append({'id': edit_id, 'object': 'model', 'created': 0,
                             'owned_by': 'fdos-draw-things',
                             'name': f'{name} · {tier.upper()} · Edit / References'})
                seen.add(edit_id)
    # Legacy direct model IDs and aliases remain available for existing Cherry entries.
    for m in downloaded:
        if m['id'] not in seen:
            data.append({'id': m['id'], 'object': 'model', 'created': 0, 'owned_by': 'draw-things'})
            seen.add(m['id'])
    aliases = CONFIG.get('model_aliases') or {}
    for alias_id, real_id in aliases.items():
        if isinstance(alias_id, str) and isinstance(real_id, str) and alias_id not in seen:
            data.append({'id': alias_id, 'object': 'model', 'created': 0, 'owned_by': 'fdos-draw-things'})
    return data


def resolve_model(requested_model):
    profile = parse_profile_id(requested_model) or parse_chat_profile_id(requested_model)
    if profile:
        return profile[0], True
    aliases = CONFIG.get('model_aliases') or {}
    if requested_model in aliases:
        return aliases[requested_model], True
    return requested_model, False


CANDIDATE_PATHS = [
    (),
    ('metadata',),
    ('extra_body',),
    ('extra',),
    ('options',),
    ('image',),
    ('image_generation',),
    ('imageGeneration',),
]


KEY_GROUPS = {
    'ratio': ('aspect_ratio', 'aspectRatio', 'ratio', 'image_aspect_ratio', 'imageAspectRatio'),
    'tier': ('image_size', 'imageSize', 'quality', 'resolution_tier', 'resolutionTier', 'sizeTier'),
    'size': ('size',),
}


def find_first_string(body, group):
    keys = KEY_GROUPS[group]
    for path in CANDIDATE_PATHS:
        obj = body
        ok = True
        for p in path:
            if not isinstance(obj, dict) or p not in obj:
                ok = False
                break
            obj = obj[p]
        if not ok or not isinstance(obj, dict):
            continue
        for key in keys:
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def normalize_ratio(val):
    if not isinstance(val, str):
        return None
    val = val.strip().lower().replace(' ', '')
    mapping = {
        '1:1': '1:1', 'square': '1:1',
        '3:4': '3:4',
        '4:3': '4:3',
        '9:16': '9:16', 'portrait': '9:16',
        '16:9': '16:9', 'landscape': '16:9',
        '2:3': '2:3',
        '3:2': '3:2',
        '21:9': '21:9',
    }
    return mapping.get(val, val if val in RATIO_PRESETS else None)


def normalize_tier(val):
    if not isinstance(val, str):
        return None
    raw = val.strip().upper().replace(' ', '')
    mapping = {
        '1K': '1K', '1024': '1K',
        '2K': '2K', '2048': '2K',
        '4K': '4K', '4096': '4K',
        'AUTO': '1K',
    }
    return mapping.get(raw)


def clamp_multiple_64(num):
    return max(256, int(round(num / 64.0)) * 64)


def dimensions_from_ratio_and_tier(ratio_label, tier_label):
    ratio_label = normalize_ratio(ratio_label) or '1:1'
    tier_label = normalize_tier(tier_label) or '1K'
    rw, rh = RATIO_PRESETS[ratio_label]
    profile_name = CONFIG.get('resolution_profile', 'safe')
    table = LONG_EDGE_BY_TIER.get(profile_name, LONG_EDGE_BY_TIER['safe'])
    long_edge = table[tier_label]
    max_side = int(CONFIG.get('max_side', 4096))
    long_edge = min(long_edge, max_side)
    if rw >= rh:
        width = long_edge
        height = clamp_multiple_64(long_edge * rh / rw)
    else:
        height = long_edge
        width = clamp_multiple_64(long_edge * rw / rh)
    width = min(width, max_side)
    height = min(height, max_side)
    return width, height, ratio_label, tier_label


def parse_explicit_size(size_text):
    if not isinstance(size_text, str):
        return None
    text = size_text.lower().strip()
    if text == 'auto':
        return None
    if 'x' not in text:
        return None
    try:
        w, h = (int(x) for x in text.split('x', 1))
    except ValueError:
        return None
    if not (256 <= w <= int(CONFIG.get('max_side', 4096)) and 256 <= h <= int(CONFIG.get('max_side', 4096))):
        return None
    if w % 64 != 0 or h % 64 != 0:
        # align to multiples of 64
        w = clamp_multiple_64(w)
        h = clamp_multiple_64(h)
    return w, h


def choose_dimensions(body):
    explicit = find_first_string(body, 'size')
    size_parsed = parse_explicit_size(explicit)
    if size_parsed:
        w, h = size_parsed
        return w, h, None, None, 'explicit_size'
    ratio = find_first_string(body, 'ratio')
    tier = find_first_string(body, 'tier')
    from_request = bool(ratio or tier)
    ratio = normalize_ratio(ratio) or CONFIG.get('default_aspect_ratio', '16:9')
    tier = normalize_tier(tier) or CONFIG.get('default_image_size', '1K')
    w, h, ratio, tier = dimensions_from_ratio_and_tier(ratio, tier)
    return w, h, ratio, tier, 'ratio_tier' if from_request else 'config_default'


def save_generation_images(images, requested_model, actual_model, body, width, height, ratio, tier, method, params):
    """Save the exact images sent to Cherry with a JSON sidecar; no silent overwrite."""
    folder = Path(os.path.expanduser(CONFIG.get('save_directory', '~/Pictures/FDOS-DrawThings')))
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, encoded in enumerate(images, start=1):
        if not isinstance(encoded, str):
            raise ValueError('CLI image is not base64 text')
        if encoded.startswith('data:'):
            encoded = encoded.split(',', 1)[1]
        raw = base64.b64decode(encoded, validate=True)
        if raw.startswith(b'\x89PNG\r\n\x1a\n'):
            ext = 'png'
            # actual image size in PNG IHDR, independent of requested dimensions
            actual_width = int.from_bytes(raw[16:20], 'big')
            actual_height = int.from_bytes(raw[20:24], 'big')
        elif raw[:3] == b'\xff\xd8\xff':
            ext = 'jpg'
            actual_width = None
            actual_height = None
        elif raw[:4] == b'RIFF' and raw[8:12] == b'WEBP':
            ext = 'webp'
            actual_width = None
            actual_height = None
        else:
            raise ValueError('Unsupported image content from CLI: expected PNG, JPEG or WebP')
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        stem = f'FDOS_{stamp}_{uuid.uuid4().hex[:8]}_{index:02d}'
        image_path = folder / (stem + '.' + ext)
        meta_path = folder / (stem + '.json')
        # Exclusive creation prevents overwriting earlier generations.
        with image_path.open('xb') as f:
            f.write(raw)
        metadata = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(),
            'requested_model': requested_model,
            'draw_things_model': actual_model,
            'prompt': body.get('prompt'),
            'reference_count': body.get('reference_count', 0),
            'operation': body.get('operation', 'generation'),
            'negative_prompt': body.get('negative_prompt'),
            'requested_dimensions': {'width': width, 'height': height},
            'actual_dimensions': {'width': actual_width, 'height': actual_height},
            'aspect_ratio': ratio,
            'size_tier': tier,
            'dimension_source': method,
            'parameters_sent_to_cli': params,
            'source': 'FDOS Draw Things Gateway v1.7',
        }
        with meta_path.open('x', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
        saved.append(str(image_path))
        print(f'[saved] {image_path} ({len(raw)} bytes)', flush=True)
    return saved



def model_operations(model_id):
    """Gateway uses CLI for every model: no external API capability negotiation."""
    return {'txt2img', 'edit'}


def run_draw_things_cli(model_id, prompt, width, height, *, negative_prompt=None, steps=None,
                        seed=None, count=1, references=None, crop_canvas=False):
    """Run Draw Things CLI for txt2img or multi-image editing.

    All reference bytes are persisted only in a temporary directory. The CLI's
    first --image is a canvas when its aspect ratio matches, and additional
    --image inputs are moodboard references.
    """
    cli = cli_path()
    results = []
    with tempfile.TemporaryDirectory(prefix='fdos-dt-cli-') as tmpdir:
        source_paths = []
        for index, encoded in enumerate(references or [], 1):
            raw = base64.b64decode(encoded, validate=True)
            ext = '.png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else (
                '.jpg' if raw.startswith(b'\xff\xd8\xff') else '.webp')
            source_path = os.path.join(tmpdir, f'reference_{index}{ext}')
            Path(source_path).write_bytes(raw)
            source_paths.append(source_path)
        for index in range(count):
            out = os.path.join(tmpdir, f'out_{index+1}.png')
            cmd = [cli, 'generate', '--local', '--offline', '--no-download-missing',
                   '--model', model_id, '--prompt', prompt,
                   '--width', str(width), '--height', str(height),
                   '--disable-preview', '--output', out]
            for source_path in source_paths:
                cmd.extend(['--image', source_path])
            if source_paths and crop_canvas:
                cmd.append('--crop')
            if negative_prompt:
                cmd.extend(['--negative-prompt', negative_prompt])
            if isinstance(steps, int):
                cmd.extend(['--steps', str(steps)])
            if isinstance(seed, int):
                cmd.extend(['--seed', str(seed + index)])
            print(f'[cli] model={model_id} refs={len(source_paths)} size={width}x{height} crop={crop_canvas} job={index+1}/{count}', flush=True)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if proc.returncode != 0:
                details = (proc.stderr or proc.stdout or '').strip()
                raise RuntimeError('draw-things-cli failed: ' + (details[-2500:] or f'exit {proc.returncode}'))
            if not os.path.exists(out):
                raise RuntimeError('draw-things-cli did not create output image')
            raw = Path(out).read_bytes()
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise RuntimeError('draw-things-cli produced a non-PNG result')
            results.append(base64.b64encode(raw).decode('ascii'))
    return results


def first_image_dimensions(encoded):
    """Obtain PNG/JPEG dimensions to decide if first reference can be the canvas."""
    raw = base64.b64decode(encoded, validate=True)
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):
        return int.from_bytes(raw[16:20], 'big'), int.from_bytes(raw[20:24], 'big')
    if raw[:3] == b'\xff\xd8\xff':
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                return image.size
        except ImportError:
            return None
    return None


def canvas_crop_required(refs, width, height):
    if not refs:
        return False
    dims = first_image_dimensions(refs[0])
    if not dims:
        return False
    return abs(dims[0] / dims[1] - width / height) > .015


MAX_EDIT_BODY = 100 * 1024 * 1024
MAX_REF_BYTES = 15 * 1024 * 1024
MAX_EDIT_REFS = 10
# Prevent overlapping local GPU edits. Cherry sometimes retries an edit while the first is running.
_EDIT_BUSY = threading.Lock()


def image_b64(value):
    """Validate local image bytes/base64; never fetch arbitrary URLs."""
    if isinstance(value, dict):
        value = value.get('image_url') or value.get('image') or value.get('b64_json')
    if not isinstance(value, (str, bytes)):
        raise ValueError('Reference image must be uploaded as PNG/JPEG/WebP or base64 data URL')
    if isinstance(value, str):
        if value.startswith('data:'):
            value = value.split(',', 1)[1]
        if value.startswith(('http://', 'https://')):
            raise ValueError('Remote image URLs are not supported; upload images from your Mac')
        try:
            data = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ValueError('Invalid reference image base64') from exc
    else:
        data = value
    if not data or len(data) > MAX_REF_BYTES:
        raise ValueError('Reference image must be between 1 byte and 15 MB')
    valid = (data.startswith(b'\x89PNG\r\n\x1a\n') or data.startswith(b'\xff\xd8\xff') or
             (data[:4] == b'RIFF' and data[8:12] == b'WEBP'))
    if not valid:
        raise ValueError('Only PNG, JPEG and WebP reference images are accepted')
    return base64.b64encode(data).decode('ascii')


def parse_edit_request(content_type, data):
    """Support both standard OpenAI multipart and JSON editing request bodies."""
    if 'multipart/form-data' in content_type.lower():
        envelope = (b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode('utf-8') +
                    b'\r\n\r\n' + data)
        msg = BytesParser(policy=email_policy).parsebytes(envelope)
        if not msg.is_multipart():
            raise ValueError('Invalid multipart form-data')
        body, images = {}, []
        for part in msg.iter_parts():
            field = part.get_param('name', header='content-disposition')
            if not field:
                continue
            raw = part.get_payload(decode=True)
            if raw is None:
                raw = b''
            if field == 'image' or field == 'image[]' or re.fullmatch(r'images?\[\d+\]', field):
                images.append(image_b64(raw))
            elif field in ('prompt','model','size','quality','n','response_format','steps','seed','negative_prompt'):
                body[field] = raw.decode('utf-8', errors='strict')
            elif field == 'mask':
                # Only instruction-edit without mask is supported in this initial release.
                if raw:
                    raise ValueError('Masks are not supported in this gateway edit route')
        body['images'] = images
        return body
    if 'application/json' in content_type.lower():
        body = json.loads(data)
        if not isinstance(body, dict):
            raise ValueError('Expected JSON object')
        images = body.get('images', body.get('image', []))
        if isinstance(images, (str, dict)):
            images = [images]
        if not isinstance(images, list):
            raise ValueError('images must be an array')
        return {**body, 'images': [image_b64(x) for x in images]}
    raise ValueError('Use multipart/form-data or application/json for image edits')


def choose_edit_dimensions(body, requested_model):
    profile = parse_profile_id(requested_model)
    if profile:
        return dimensions_for_profile(body, profile[1])
    return choose_dimensions(body)


def edit_payload(model, prompt, refs, width, height, count=1, seed=None, steps=None):
    params = {'width': width, 'height': height, 'batch_size': count}
    if seed is not None:
        params['seed'] = int(seed)
    if steps is not None:
        params['steps'] = int(steps)
    payload = {'model': model, 'from': {'image': refs[0]},
               'instruction': prompt, 'params': params}
    if len(refs) > 1:
        payload['references'] = [{'image': {'image': ref}, 'role': 'reference'} for ref in refs[1:]]
    return payload, params



def chat_latest_request(messages):
    """Extract the last user request, supporting OpenAI multimodal content arrays.

    External URLs are deliberately not fetched (SSRF and untrusted URLs).
    """
    if not isinstance(messages, list):
        raise ValueError('messages must be an array')
    user = next((m for m in reversed(messages) if isinstance(m, dict) and m.get('role') == 'user'), None)
    if user is None:
        raise ValueError('No user message found')
    content = user.get('content', '')
    refs, texts = [], []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get('type')
            if kind in ('text', 'input_text'):
                value = part.get('text')
                if isinstance(value, str):
                    texts.append(value)
            elif kind in ('image_url', 'input_image'):
                val = part.get('image_url', part.get('image'))
                if isinstance(val, dict):
                    val = val.get('url') or val.get('image')
                if not isinstance(val, str):
                    raise ValueError('Image attachment must be a base64 data URL')
                refs.append(image_b64(val))
    else:
        raise ValueError('Unsupported user message content')
    prompt = '\n'.join(x.strip() for x in texts if x.strip()).strip()
    if not prompt or len(prompt) > 20000:
        raise ValueError('Provide a text prompt of up to 20000 characters')
    if len(refs) > MAX_EDIT_REFS:
        raise ValueError('At most 10 uploaded reference images are supported')
    return prompt, refs


def chat_dimensions(model, body):
    profile = parse_chat_profile_id(model)
    if not profile:
        raise ValueError('Unknown FDOS Chat image model ID')
    tier = profile[1]
    ratio = body.get('aspect_ratio', '1:1')
    # For ordinary chat, explicit text options are simpler than trying to derive
    # Image Generation UI controls that chat views do not have.
    messages = body.get('messages', [])
    last = next((m for m in reversed(messages) if isinstance(m, dict) and m.get('role') == 'user'), {})
    raw = last.get('content')
    combined = raw if isinstance(raw, str) else ' '.join(
        p.get('text','') for p in raw if isinstance(p,dict) and isinstance(p.get('text'),str)
    ) if isinstance(raw,list) else ''
    found = re.search(r'(?<![\d])(?:1\s*[:xх×]\s*1|16\s*[:xх×]\s*9|9\s*[:xх×]\s*16)(?![\d])', combined)
    if found:
        ratio = found.group(0).replace(' ','').replace('х',':').replace('×',':').replace('x',':')
    if ratio not in ('1:1','16:9','9:16'):
        raise ValueError('Chat aspect_ratio must be 1:1, 16:9 or 9:16')
    rw,rh = RATIO_PRESETS[ratio]
    edge = {'1K':1024,'2K':2048,'4K':4096}[tier]
    width = edge if rw >= rh else clamp_multiple_64(edge*rw/rh)
    height = edge if rh >= rw else clamp_multiple_64(edge*rh/rw)
    return width,height,ratio,tier


def image_reply_links(saved):
    lines = ['Готово. Изображение сохранено локально:']
    for i, path in enumerate(saved, 1):
        name = Path(path).name
        url = f'http://127.0.0.1:{PORT}/generated/{quote(name)}'
        lines.append(f'![Результат {i}]({url})')
        lines.append(f'[Открыть изображение {i}]({url})')
        lines.append(f'Файл: `{path}`')
    return '\n\n'.join(lines)



class Handler(BaseHTTPRequestHandler):
    server_version = 'FDOS-DrawThings-Gateway/1.7'

    def reply(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            print('[response] client disconnected before reply', flush=True)

    def fail(self, status, message, code='gateway_error'):
        self.reply(status, {'error': {'message': str(message), 'type': 'api_error', 'code': code}})

    def handle_upstream_error(self, exc):
        self.fail(502, 'Local Draw Things CLI error: %s' % exc, 'cli_error')

    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')
        try:
            if path == '/health':
                try:
                    models = available_models()
                    self.reply(200, {'status':'ok', 'backend':'draw-things-cli',
                                     'models_count':len(models), 'cli':cli_path(),
                                     'config_path':CONFIG_PATH})
                except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                    self.reply(503, {'status':'degraded', 'backend':'draw-things-cli', 'error':str(exc)})
            elif path == '/v1/models':
                self.reply(200, {'object': 'list', 'data': advertised_models()})
            else:
                self.fail(404, 'Unknown endpoint', 'not_found')
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            self.handle_upstream_error(exc)

    def handle_chat_image_decoded(self, body):
        """A dedicated chat image model; avoid treating image verification as generation."""
        try:
            if not isinstance(body, dict):
                raise ValueError('JSON object required')
            selected = body.get('model','')
            profile = parse_chat_profile_id(selected)
            if not profile:
                self.fail(400,'Select a FDOS Chat image profile','model_not_found')
                return
            real_model = profile[0]
            if real_model not in {m['id'] for m in available_models()}:
                self.fail(404,'Model is not downloaded: '+real_model,'model_not_found')
                return
            prompt, refs = chat_latest_request(body.get('messages'))
            width,height,ratio,tier = chat_dimensions(selected,body)
            count = body.get('n',1)
            if isinstance(count,bool) or not isinstance(count,int) or not 1 <= count <= 4:
                raise ValueError('n must be integer 1..4')
            params = {'width':width,'height':height,'batch_size':count}
            if refs:
                crop_canvas = canvas_crop_required(refs, width, height)
                print(f'[chat] cli edit model={real_model} refs={len(refs)} size={width}x{height} crop={crop_canvas}',flush=True)
                images = run_draw_things_cli(real_model,prompt,width,height,
                                             count=count,references=refs,crop_canvas=crop_canvas)
            else:
                print(f'[chat] cli model={real_model} size={width}x{height}',flush=True)
                images = run_draw_things_cli(real_model,prompt,width,height,count=count)
            safe = {'prompt':prompt,'reference_count':len(refs),'operation':'chat_edit' if refs else 'chat_generate'}
            saved = save_generation_images(images,selected,real_model,safe,width,height,ratio,tier,'chat',params)
            content = image_reply_links(saved)
            response_id = 'chatcmpl-fdos-'+uuid.uuid4().hex[:12]
            created = int(time.time())
            if body.get('stream'):
                # Cherry SDK typically expects SSE for chat models.
                def send_chunk(content_piece, finish_reason=None):
                    obj = {'id':response_id,'object':'chat.completion.chunk','created':created,
                           'model':selected,'choices':[{'index':0,
                           'delta':{'content':content_piece} if content_piece is not None else {},
                           'finish_reason':finish_reason}]}
                    self.wfile.write(('data: '+json.dumps(obj,ensure_ascii=False)+'\n\n').encode('utf-8'))
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream; charset=utf-8')
                self.send_header('Cache-Control','no-cache')
                self.send_header('Connection','close')
                self.end_headers()
                send_chunk(content)
                send_chunk(None,'stop')
                self.wfile.write(b'data: [DONE]\n\n')
                self.wfile.flush()
            else:
                self.reply(200,{'id':response_id,'object':'chat.completion','created':created,
                    'model':selected,'choices':[{'index':0,'message':{'role':'assistant','content':content},
                                                'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}})
        except (ValueError,TypeError,json.JSONDecodeError) as exc:
            self.fail(400,str(exc),'invalid_request_error')
        except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
            self.fail(502,str(exc),'execution_failed')
        except (BrokenPipeError,ConnectionResetError):
            pass

    def handle_image_edit(self):
        """Paint editor: single-/multi-reference edits with guaranteed terminal logs."""
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:8]
        acquired = _EDIT_BUSY.acquire(blocking=False)
        if not acquired:
            print(f'[edit:{request_id}] rejected overlapping edit: another edit is running', flush=True)
            self.fail(429, 'FLUX editing is already running. Wait for the previous edit to complete before sending another.', 'edit_busy')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_EDIT_BODY:
                self.fail(413, 'Image editing request is missing or exceeds 100 MB', 'invalid_request_error')
                return
            body = parse_edit_request(self.headers.get('Content-Type', ''), self.rfile.read(length))
            requested_model = body.get('model')
            prompt = body.get('prompt')
            refs = body.get('images', [])
            if not isinstance(requested_model, str) or not isinstance(prompt, str) or not prompt.strip():
                self.fail(400, 'model and prompt required', 'invalid_request_error')
                return
            if not 1 <= len(refs) <= MAX_EDIT_REFS:
                self.fail(400, 'Supply between 1 and 10 reference images', 'invalid_request_error')
                return
            actual_model, _ = resolve_model(requested_model)
            if actual_model not in {m['id'] for m in available_models()}:
                self.fail(400, 'Unknown local Draw Things model: ' + actual_model, 'model_not_found')
                return
            if not cli_path():
                self.fail(503, 'draw-things-cli is required for local image editing', 'cli_missing')
                return
            width, height, ratio, tier, source = choose_edit_dimensions(body, requested_model)
            n = int(body.get('n', 1))
            if not 1 <= n <= 4:
                raise ValueError('n must be 1..4')
            params = {'width': width, 'height': height, 'batch_size': n}
            for key in ('steps', 'seed'):
                value = body.get(key)
                if value is not None:
                    params[key] = int(value)
            crop_canvas = canvas_crop_required(refs, width, height)
            print(f'[edit:{request_id}] CLI_START model={actual_model} refs={len(refs)} size={width}x{height} tier={tier} ratio={ratio} crop={crop_canvas} prompt_chars={len(prompt)}', flush=True)
            images = run_draw_things_cli(
                actual_model, prompt, width, height,
                negative_prompt=body.get('negative_prompt'),
                steps=params.get('steps'), seed=params.get('seed'),
                count=n, references=refs, crop_canvas=crop_canvas)
            safe_body = {'prompt': prompt, 'reference_count': len(refs), 'operation': 'edit'}
            saved = save_generation_images(images, requested_model, actual_model, safe_body,
                                           width, height, ratio, tier, source, params)
            self.reply(200, {'created': int(time.time()), 'data': [
                {'b64_json': image_data, 'revised_prompt': prompt} for image_data in images]})
            print(f'[edit:{request_id}] success seconds={time.monotonic()-started:.1f} files={len(saved)}', flush=True)
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            print(f'[edit:{request_id}] INPUT_ERROR {exc}',flush=True)
            self.fail(400, str(exc), 'invalid_request_error')
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            print(f'[edit:{request_id}] UPSTREAM_ERROR {exc}',flush=True)
            self.handle_upstream_error(exc)
        except (BrokenPipeError, ConnectionResetError) as exc:
            print(f'[edit:{request_id}] CLIENT_DISCONNECTED after={time.monotonic()-started:.1f}s: {exc}', flush=True)
        except Exception as exc:
            print(f'[edit:{request_id}] UNEXPECTED_ERROR {exc}\n{traceback.format_exc()}',flush=True)
            self.fail(500, 'Unexpected edit failure: '+str(exc), 'internal_error')
        finally:
            _EDIT_BUSY.release()
            print(f'[edit:{request_id}] finished elapsed={time.monotonic()-started:.1f}s',flush=True)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip('/')
        if path == '/v1/images/edits':
            return self.handle_image_edit()
        if path == '/v1/chat/completions':
            # Peek model without consuming request payload; dedicated chat models
            # use image generation, legacy image-model verification stays unchanged.
            length = int(self.headers.get('Content-Length','0'))
            if 0 < length <= MAX_CHAT_REQUEST_BYTES:
                raw = self.rfile.read(length)
                try:
                    probe = json.loads(raw)
                    if isinstance(probe,dict) and parse_chat_profile_id(probe.get('model')):
                        self._chat_body = probe
                        return self.handle_chat_image_decoded(probe)
                except (ValueError,TypeError,json.JSONDecodeError):
                    pass
                self._legacy_body = raw
            else:
                self.fail(413,'Invalid chat request size','invalid_request_error')
                return
            try:
                size = len(self._legacy_body)
                if size <= 0 or size > MAX_REQUEST_BYTES:
                    self.fail(413, 'Missing or oversized JSON body', 'invalid_request_error')
                    return
                body = json.loads(self._legacy_body)
                model = body.get('model')
                actual_model, _ = resolve_model(model)
                dt_models = {m['id'] for m in available_models()}
                if actual_model not in dt_models:
                    self.fail(404, 'Unknown Draw Things model', 'model_not_found')
                    return
                if body.get('stream'):
                    self.fail(400, 'Streaming chat not supported; this is an image-only gateway', 'unsupported_feature')
                    return
                message = 'Model is available for image generation. This gateway does not implement general chat.'
                self.reply(200, {
                    'id': 'chatcmpl-fdos-image-verification',
                    'object': 'chat.completion',
                    'created': int(time.time()),
                    'model': model,
                    'choices': [{
                        'index': 0,
                        'message': {'role': 'assistant', 'content': message},
                        'finish_reason': 'stop'
                    }],
                    'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
                })
            except (ValueError, json.JSONDecodeError):
                self.fail(400, 'Invalid request body', 'invalid_request_error')
            except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
                self.handle_upstream_error(exc)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path != '/v1/images/generations':
            self.fail(404, 'This version supports /v1/images/generations only', 'not_found')
            return

        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size <= 0 or size > MAX_REQUEST_BYTES:
                self.fail(413, 'Missing or oversized JSON body', 'invalid_request_error')
                return
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                self.fail(400, 'Request must be a JSON object', 'invalid_request_error')
                return

            requested_model = body.get('model')
            prompt = body.get('prompt')
            if not isinstance(requested_model, str) or not isinstance(prompt, str) or not prompt.strip():
                self.fail(400, 'model and nonempty prompt are required', 'invalid_request_error')
                return

            actual_model, used_alias = resolve_model(requested_model)
            dt_models = {m['id'] for m in available_models()}
            if actual_model not in dt_models:
                self.fail(400, f'Model not found in CLI downloaded image models: {actual_model}', 'model_not_found')
                return

            profile = parse_profile_id(requested_model)
            if profile:
                width, height, ratio, tier, method = dimensions_for_profile(body, profile[1])
            else:
                width, height, ratio, tier, method = choose_dimensions(body)
            if not (256 <= width <= int(CONFIG.get('max_side', 4096)) and 256 <= height <= int(CONFIG.get('max_side', 4096))):
                self.fail(400, 'Resolved dimensions are outside allowed range', 'invalid_size')
                return
            if width % 64 != 0 or height % 64 != 0:
                self.fail(400, 'Resolved dimensions must be multiples of 64', 'invalid_size')
                return

            count = body.get('n', 1)
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 4:
                self.fail(400, 'n must be between 1 and 4', 'invalid_request_error')
                return

            params = {
                'width': width,
                'height': height,
                'batch_size': count,
            }
            for key in ('steps', 'seed'):
                value = body.get(key)
                if value is not None:
                    if not isinstance(value, int) or isinstance(value, bool):
                        self.fail(400, key + ' must be an integer', 'invalid_request_error')
                        return
                    params[key] = value

            payload = {
                'model': actual_model,
                'prompt': prompt,
                'params': params,
            }
            if body.get('negative_prompt'):
                payload['negative_prompt'] = body['negative_prompt']

            if CONFIG.get('debug_log_requests'):
                print('[image] request_model=%s alias=%s actual_model=%s method=%s ratio=%s tier=%s size=%sx%s'
                      % (requested_model, used_alias, actual_model, method, ratio, tier, width, height), flush=True)
                # Useful during first tests with Cherry Studio UI to discover its exact request shape.
                print('[image] raw_request=' + json.dumps(body, ensure_ascii=False), flush=True)

            engine = 'draw_things_cli'
            images = run_draw_things_cli(actual_model, prompt, width, height,
                                         negative_prompt=body.get('negative_prompt'),
                                         steps=params.get('steps'), seed=params.get('seed'), count=count)
            if CONFIG.get('debug_log_requests'):
                print('[image] engine=' + engine, flush=True)

            if body.get('response_format', 'b64_json') == 'url':
                self.fail(400, 'URL response format not supported; use b64_json', 'unsupported_response_format')
                return

            # Persist before replying 200, so a successful response means files were saved.
            save_generation_images(images, requested_model, actual_model, body,
                                   width, height, ratio, tier, method, params)
            self.reply(200, {
                'created': int(time.time()),
                'data': [{'b64_json': im, 'revised_prompt': prompt} for im in images],
            })
        except json.JSONDecodeError:
            self.fail(400, 'Invalid JSON', 'invalid_request_error')
        except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            self.handle_upstream_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass



# --- FDOS Control Panel v0.6: independent browser UI with exact parameters ---
from urllib.parse import parse_qs, quote
from pathlib import Path as _Path

PANEL_HTML = _Path(__file__).with_name('panel.html')
MAX_UI_BODY = 60 * 1024 * 1024


def model_catalog():
    """Downloaded still-image models only; CLI is the sole backend."""
    return [dict(m) for m in available_models()]


def operation_names(capabilities):
    return {'txt2img', 'edit'}


def clean_base64(value):
    if not isinstance(value, str):
        raise ValueError('Reference image must be base64')
    if value.startswith('data:'):
        value = value.split(',', 1)[1]
    raw = base64.b64decode(value, validate=True)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('Each reference image must be 8 MB or less')
    if not (raw.startswith(b'\x89PNG\r\n\x1a\n') or raw[:3] == b'\xff\xd8\xff' or
            (raw[:4] == b'RIFF' and raw[8:12] == b'WEBP')):
        raise ValueError('Reference must be PNG, JPEG or WebP')
    return value


class PanelHandler(Handler):
    server_version = 'FDOS-DrawThings-Gateway/1.7'

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith('/generated/'):
            # Only serve known file names from the output directory, never paths.
            from urllib.parse import unquote
            name = unquote(parsed.path[len('/generated/'):])
            if not name or name != Path(name).name or not re.fullmatch(r'FDOS_[A-Za-z0-9_-]+\.(png|jpg|webp)',name):
                self.fail(404,'File not found','not_found')
                return
            file_path = Path(os.path.expanduser(CONFIG.get('save_directory','~/Pictures/FDOS-DrawThings'))) / name
            if not file_path.is_file():
                self.fail(404,'File not found','not_found')
                return
            content = file_path.read_bytes()
            mimetype = 'image/png' if name.endswith('.png') else 'image/jpeg' if name.endswith('.jpg') else 'image/webp'
            self.send_response(200)
            self.send_header('Content-Type',mimetype)
            self.send_header('Content-Length',str(len(content)))
            self.send_header('Cache-Control','private, max-age=3600')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(content)
            return
        if parsed.path in ('/', '/panel'):
            raw = PANEL_HTML.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(raw)
            return
        if parsed.path == '/ui/models':
            try:
                self.reply(200, {'items': model_catalog()})
            except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
                self.handle_upstream_error(exc)
            return
        if parsed.path == '/ui/capabilities':
            model = parse_qs(parsed.query).get('model', [''])[0]
            if model not in {m['id'] for m in model_catalog() if m['downloaded']}:
                self.fail(404, 'Model not downloaded', 'model_not_found')
                return
            self.reply(200, {'model': model, 'operations': ['txt2img', 'edit'],
                             'notes': ['Handled by local Draw Things CLI; model-specific edit support varies.']})
            return
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != '/ui/generate':
            return super().do_POST()
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 1 or length > MAX_UI_BODY:
                self.fail(413, 'Image request too large', 'invalid_request_error')
                return
            if 'application/json' not in self.headers.get('Content-Type', ''):
                self.fail(415, 'Expected application/json', 'invalid_request_error')
                return
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                self.fail(400, 'JSON object required', 'invalid_request_error')
                return
            model = body.get('model')
            prompt = body.get('prompt')
            if not isinstance(model, str) or not isinstance(prompt, str) or not prompt.strip():
                self.fail(400, 'Select model and provide prompt', 'invalid_request_error')
                return
            if len(prompt) > 20000:
                self.fail(400, 'Prompt too long', 'invalid_request_error')
                return
            installed = {m['id'] for m in model_catalog() if m['downloaded']}
            if model not in installed:
                self.fail(400, 'Model not installed: '+model, 'model_not_found')
                return
            ratio = body.get('aspect_ratio')
            tier = body.get('image_size')
            if ratio not in RATIO_PRESETS or tier not in ('1K', '2K', '4K'):
                self.fail(400, 'Choose a valid aspect ratio and image size', 'invalid_request_error')
                return
            width, height, ratio, tier = dimensions_from_ratio_and_tier(ratio, tier)
            params = {'width': width, 'height': height, 'batch_size': 1}
            for key in ('steps', 'seed'):
                val = body.get(key)
                if val is not None:
                    if isinstance(val, bool) or not isinstance(val, int) or val < 0 or (key == 'steps' and not 1 <= val <= 150):
                        self.fail(400, 'Invalid ' + key, 'invalid_request_error')
                        return
                    params[key] = val
            refs = body.get('references', [])
            if not isinstance(refs, list) or len(refs) > 5:
                self.fail(400, 'Maximum 5 reference images', 'invalid_request_error')
                return
            refs = [clean_base64(r.get('image')) for r in refs if isinstance(r, dict)]
            crop_canvas = canvas_crop_required(refs, width, height)
            print(f'[panel] cli model={model} size={width}x{height} ratio={ratio} tier={tier} references={len(refs)} crop={crop_canvas}', flush=True)
            images = run_draw_things_cli(model, prompt, width, height,
                                         steps=params.get('steps'), seed=params.get('seed'),
                                         references=refs, crop_canvas=crop_canvas)
            # Keep metadata compact: do not write reference image base64 into JSON sidecars.
            save_body = {'prompt': prompt, 'reference_count': len(refs), 'operation': 'cli_edit' if refs else 'cli_generate'}
            saved = save_generation_images(images, model, model, save_body,
                                           width, height, ratio, tier, 'panel_direct', params)
            self.reply(200, {'model': model, 'width': width, 'height': height,
                             'images': images, 'saved': saved})
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.fail(400, str(exc), 'invalid_request_error')
        except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
            self.handle_upstream_error(exc)
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == '__main__':
    print(f'FDOS Draw Things Gateway v1.7 at http://{HOST}:{PORT}; backend=draw-things-cli only', flush=True)
    print(f'[config] path={CONFIG_PATH}', flush=True)
    print(f'[config] aliases={CONFIG.get("model_aliases")}', flush=True)
    print(f'[config] resolution_profile={CONFIG.get("resolution_profile")}', flush=True)
    print(f'[config] save_directory={os.path.expanduser(CONFIG.get("save_directory", "~/Pictures/FDOS-DrawThings"))}', flush=True)
    print('[panel] open http://127.0.0.1:%s/panel' % PORT, flush=True)
    ThreadingHTTPServer((HOST, PORT), PanelHandler).serve_forever()
