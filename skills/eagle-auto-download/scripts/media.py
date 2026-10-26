"""Validated, atomic media preparation. Originals are never overwritten."""
import hashlib
import io
import json
import math
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageCms, ImageColor, ImageOps


VIDEO_VALIDATOR_REVISION = 1


def prepare_video(source, cache_directory=None, metrics=None):
    """Reuse full-decode results only after hashing the current input bytes."""
    started = time.monotonic()
    metrics = metrics if metrics is not None else {}
    try:
        return _prepare_video(source, cache_directory, metrics)
    finally:
        metrics['seconds'] = metrics.get('seconds', 0.0) + time.monotonic() - started


def _prepare_video(source, cache_directory, metrics):
    from imageio_ffmpeg import get_ffmpeg_exe
    source = Path(source).resolve(strict=True)
    executable = Path(get_ffmpeg_exe()).resolve(strict=True)
    stat = source.stat()
    def unchanged():
        after = source.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) == (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
    digest = sha256(source)
    if not unchanged():
        raise ValueError('视频在校验时发生变化')
    binary = executable.stat()
    validator = {'revision': VIDEO_VALIDATOR_REVISION, 'decoder': str(executable),
                 'decoder_bytes': binary.st_size, 'decoder_mtime_ns': binary.st_mtime_ns}
    receipt = None
    metadata = None
    if cache_directory is not None:
        signature = hashlib.sha256(json.dumps(validator, sort_keys=True).encode()).hexdigest()[:16]
        receipt = Path(cache_directory) / (digest + '-' + signature + '.json')
        try:
            saved = json.loads(receipt.read_text())
            candidate = saved['metadata']
            if (saved['validator'] == validator and saved['sha256'] == digest and
                    saved['bytes'] == stat.st_size and
                    all(type(candidate[k]) is int and candidate[k] > 0 for k in ('width', 'height')) and
                    type(candidate['has_audio']) is bool and
                    type(candidate['duration_seconds']) in (int, float) and
                    math.isfinite(candidate['duration_seconds']) and candidate['duration_seconds'] > 0):
                metadata = {k: candidate[k] for k in ('width', 'height', 'duration_seconds', 'has_audio')}
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            pass  # Old, corrupt or incomplete receipts require a fresh full decode.
    if metadata is not None:
        if not unchanged():
            raise ValueError('视频在校验时发生变化')
        metrics['cache_hits'] = metrics.get('cache_hits', 0) + 1
        return _video_record(source, digest, metadata)
    metrics['full_decodes'] = metrics.get('full_decodes', 0) + 1
    # Decode the original streams without writing or transcoding the input.
    result = subprocess.run([str(executable), '-nostdin', '-hide_banner', '-xerror',
                             '-i', str(source), '-map', '0:v:0', '-map', '0:a?',
                             '-f', 'null', '-'], capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError('视频完整解码校验失败')
    video = re.search(r'Video:.*?\b(\d{2,5})x(\d{2,5})\b', result.stderr)
    duration = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
    if not video or not duration:
        raise ValueError('无法核实视频尺寸或时长')
    seconds = int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3])
    if not unchanged() or sha256(source) != digest or not unchanged():
        raise ValueError('视频在完整解码时发生变化')
    metadata = {'width': int(video[1]), 'height': int(video[2]),
                'duration_seconds': seconds, 'has_audio': 'Audio:' in result.stderr}
    if receipt is not None:
        atomic_write(receipt, json.dumps({'validator': validator, 'sha256': digest,
                     'bytes': stat.st_size, 'metadata': metadata}).encode())
    return _video_record(source, digest, metadata)


def _video_record(source, digest, metadata):
    return [{'source_path': str(source), 'source_sha256': digest,
             'source_format': 'video', 'local_path': str(source), 'sha256': digest,
             'role': 'video', 'conversion': None, 'frames': None,
             **metadata}]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.partial-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def prepare_image(source, output, options):
    source = Path(source).resolve(strict=True)
    digest = sha256(source)
    if source.suffix.lower() == '.svg':
        if source.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('SVG 超过安全大小')
        data = source.read_bytes()
        if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
            raise ValueError('SVG 包含 DTD/entity')
        root = ET.fromstring(data)
        if root.tag.rsplit('}', 1)[-1].lower() != 'svg':
            raise ValueError('SVG 根节点无效')
        lowered = data.lower()
        if b'<script' in lowered or b'<foreignobject' in lowered or b'@import' in lowered:
            raise ValueError('SVG 含脚本、HTML 或外部样式导入')
        for node in root.iter():
            for key, value in node.attrib.items():
                local_key = key.rsplit('}', 1)[-1].lower()
                if local_key.startswith('on'):
                    raise ValueError('SVG 含事件脚本，拒绝导入')
                if local_key == 'href' and value and not value.startswith(('#', 'data:')):
                    raise ValueError('SVG 引用了外部资源，拒绝导入')
                if local_key == 'style':
                    references = re.findall(r'url\(\s*[\'\"]?([^)]*?)[\'\"]?\s*\)', value, re.I)
                    if any(not ref.strip().startswith(('#', 'data:')) for ref in references):
                        raise ValueError('SVG 样式引用了外部资源，拒绝导入')
        view_box = root.get('viewBox') or root.get('viewbox')
        dimensions = []
        if view_box:
            try:
                dimensions = [float(value) for value in re.split(r'[ ,]+', view_box.strip())]
            except ValueError:
                dimensions = []
        def svg_dimension(name, fallback):
            raw = root.get(name, '')
            match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)(?:px)?\s*', raw)
            return int(round(float(match[1]))) if match else fallback
        width = svg_dimension('width', int(round(dimensions[2])) if len(dimensions) == 4 else 300)
        height = svg_dimension('height', int(round(dimensions[3])) if len(dimensions) == 4 else 150)
        if width <= 0 or height <= 0 or width > 100000 or height > 100000:
            raise ValueError('SVG 尺寸异常')
        return [{'source_path': str(source), 'source_sha256': digest,
                 'source_format': 'SVG', 'width': width, 'height': height, 'frames': 1,
                 'local_path': str(source), 'sha256': digest, 'role': 'original',
                 'conversion': None}]
    with Image.open(source) as image:
        fmt = image.format
        frames = getattr(image, 'n_frames', 1)
        # Decode every frame, so truncated animations do not enter the import queue.
        for frame in range(frames):
            image.seek(frame)
            image.load()
        image.seek(0)
        oriented = ImageOps.exif_transpose(image)
        width, height = oriented.size
        base = {'source_path': str(source), 'source_sha256': digest,
                'source_format': fmt, 'width': width, 'height': height,
                'frames': frames}
        if fmt != 'WEBP' or not options['enabled']:
            return [{**base, 'local_path': str(source), 'sha256': digest,
                     'role': 'original', 'conversion': None}]
        rgba = oriented.convert('RGBA')
        rgb = rgba.convert('RGB')
        if image.info.get('icc_profile'):
            profile = ImageCms.ImageCmsProfile(io.BytesIO(image.info['icc_profile']))
            rgb = ImageCms.profileToProfile(rgb, profile,
                                           ImageCms.createProfile('sRGB'), outputMode='RGB')
        rgb.putalpha(rgba.getchannel('A'))
        background = Image.new('RGB', rgb.size, ImageColor.getrgb(options['background']))
        background.paste(rgb, mask=rgb.getchannel('A'))
        encoded = io.BytesIO()
        background.save(encoded, 'JPEG', quality=options['quality'],
                        subsampling=options['subsampling'],
                        icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())
        data = encoded.getvalue()
        output_hash = hashlib.sha256(data).hexdigest()
        target = Path(output).resolve() / (output_hash + '.jpg')
        if target == source:
            raise ValueError('输出路径不能覆盖原文件')
        with Image.open(io.BytesIO(data)) as check:
            check.load()
            if check.format != 'JPEG' or check.size != (width, height):
                raise ValueError('JPG 校验失败')
        atomic_write(target, data)
        converted = {**base, 'local_path': str(target), 'sha256': output_hash,
                     'role': 'jpeg_preview' if frames > 1 else 'jpeg',
                     'conversion': {'format': 'JPEG', 'quality': options['quality'],
                                    'subsampling': options['subsampling'],
                                    'background': options['background']}}
        if frames > 1:
            return [{**base, 'local_path': str(source), 'sha256': digest,
                     'role': 'animation', 'conversion': None}, converted]
        return [converted]
