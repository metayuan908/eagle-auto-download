"""Download permitted Vimeo files through its public Download control.

Checkpoint validated files, pause without scheduling more work, and reconcile
local caches without network access. yt-dlp remains an optional player fallback.
"""
import argparse
import fcntl
import hashlib
import json
import re
import shutil
import os
import sys
import ssl
import signal
import tempfile
import uuid
from threading import Event
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import certifi

from collector import load_config, prepare, state
from media import atomic_write, prepare_video, sha256

GITHUB_SOURCES = [
    'https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/vimeo.py',
    'https://github.com/yt-dlp/yt-dlp/issues/5492',
    'https://github.com/yt-dlp/yt-dlp#format-selection',
]


class PublicDownloadsUnavailable(RuntimeError):
    pass


def public_page_data(page, video_id):
    match = re.search(r'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
                      page, re.DOTALL)
    if not match:
        if any(marker in page.lower() for marker in
               ('verify to continue', 'security of your connection', 'cf-chl-')):
            raise RuntimeError('connection_verification: Vimeo 返回了连接验证页')
        raise PublicDownloadsUnavailable('Vimeo 公开页面未提供新版下载数据')
    props = json.loads(match.group(1)).get('props', {}).get('pageProps', {})
    if str(props.get('clipId')) != video_id:
        raise ValueError('Vimeo 公开页面与来源视频不匹配')
    return props


def public_response(url, headers):
    with urlopen(Request(url, headers=headers), timeout=30,
                 context=ssl.create_default_context(cafile=certifi.where())) as response:
        actual = urlsplit(response.url)
        if actual.scheme != 'https' or actual.hostname != urlsplit(url).hostname:
            raise ValueError('Vimeo 公共接口跳转到了未识别域名')
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('Vimeo 公共接口超过 2 MiB 限制')
    return raw


def discover_official_downloads(source_url):
    # A page-issued guest grant can expire. Refresh that normal grant once;
    # an API rejection must never trigger browser credential extraction.
    for attempt in range(2):
        try:
            return _discover_official_downloads(source_url)
        except HTTPError as exc:
            if exc.code == 401 and urlsplit(exc.filename or '').hostname == 'api.vimeo.com' and attempt == 0:
                continue
            raise


def _discover_official_downloads(source_url):
    """Follow Vimeo's public Download button flow, with fresh guest authority.

    The public watch page supplies clipSignature and a guest viewer JWT. The
    current web Download control uses them with API v3.4.12/anon_signature.
    They are normal page-issued permissions, never browser login/clearance
    cookies, and remain in memory. Respect the video's download toggle.
    """
    player_url(source_url)  # Validate the canonical source before any request.
    video_id = urlsplit(source_url).path.strip('/')
    headers = {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest',
               'Referer': source_url, 'User-Agent': 'EagleAutoDownload/1.0'}
    # This route is published in yt-dlp. On the current Next.js site it returns
    # the public watch page rather than the old source_file JSON response.
    raw = public_response(source_url + '?action=load_download_config', headers)
    props = public_page_data(raw.decode('utf-8', 'replace'), video_id)
    clip = props.get('clip', {})
    if clip.get('privacy', {}).get('download') is False:
        raise RuntimeError('download_disabled: Vimeo 作者未开放下载')
    if clip.get('privacy', {}).get('download') is not True:
        raise PublicDownloadsUnavailable('Vimeo 页面未明确开放下载')
    viewer = props.get('viewerBootstrap', {})
    signature = props.get('pageMetadata', {}).get('clipSignature')
    token = viewer.get('jwt')
    if viewer.get('user') is not None or not signature or not token:
        raise PublicDownloadsUnavailable('Vimeo 页面未提供匿名下载访问数据')
    endpoint = 'https://api.vimeo.com/videos/' + video_id + '?' + urlencode({
        'fields': 'download', 'anon_signature': signature,
    })
    data = json.loads(public_response(endpoint, {
        'Authorization': 'jwt ' + token,
        'Accept': 'application/vnd.vimeo.*+json;version=3.4.12',
        'Referer': source_url, 'User-Agent': 'EagleAutoDownload/1.0',
    }))
    formats = [{'url': item['link'], 'width': item.get('width'),
                'height': item.get('height'), 'filesize': item.get('size'),
                'quality': item.get('quality'), 'public_name': item.get('public_name')}
               for item in data.get('download', []) if item.get('link') and
               item.get('quality') in ('source', 'hd', 'sd', 'uhd')]
    if not formats:
        raise PublicDownloadsUnavailable('Vimeo 官方匿名下载菜单未提供文件')
    select_official_download(formats, source_url)  # Validate every supplied URL.
    return formats


def player_url(source_url):
    parsed = urlsplit(source_url)
    if (parsed.scheme != 'https' or parsed.hostname != 'vimeo.com' or
            parsed.query or parsed.fragment or parsed.username or parsed.password or
            not re.fullmatch(r'/\d+', parsed.path)):
        raise ValueError('当前外链下载器只处理页面明确关联的 Vimeo 数字 ID')
    return 'https://player.vimeo.com/video/' + parsed.path.strip('/')


def source_player_url(record):
    """Validate a player URL extracted from the source project's embed."""
    candidate = record.get('source_player_url')
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    video_id = urlsplit(record['asset_url']).path.strip('/')
    if (parsed.scheme != 'https' or parsed.hostname != 'player.vimeo.com' or
            parsed.path != '/video/' + video_id or parsed.username or parsed.password or
            parsed.fragment):
        raise ValueError('项目页播放器地址与来源视频不匹配')
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {'h'} or len(query.get('h', [])) > 1:
        raise ValueError('项目页播放器地址包含未识别参数')
    if query.get('h') and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', query['h'][0]):
        raise ValueError('项目页播放器隐私参数格式异常')
    return candidate


class VimeoPageMetadata(HTMLParser):
    """Read only Vimeo's own canonical player URL from the public watch page."""
    def __init__(self):
        super().__init__()
        self.player = None

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        values = dict(attrs)
        if values.get('name', '').lower() == 'twitter:player':
            self.player = values.get('content')


def discover_player_url(source_url, referer):
    """Use the player link Vimeo itself publishes on the video's public page."""
    fallback = player_url(source_url)
    request = Request(source_url, headers={
        'User-Agent': 'EagleAutoDownload/1.0', 'Referer': referer,
    })
    with urlopen(request, timeout=25,
                 context=ssl.create_default_context(cafile=certifi.where())) as response:
        final = urlsplit(response.url)
        if final.scheme != 'https' or final.hostname not in ('vimeo.com', 'www.vimeo.com'):
            raise ValueError('Vimeo 页面跳转到未识别域名')
        page = response.read(2 * 1024 * 1024 + 1)
    if len(page) > 2 * 1024 * 1024:
        raise ValueError('Vimeo 页面超过 2 MiB 限制')
    parser = VimeoPageMetadata()
    parser.feed(page.decode('utf-8', 'replace'))
    if not parser.player:
        return fallback
    candidate = urlsplit(parser.player)
    expected_id = urlsplit(source_url).path.strip('/')
    if (candidate.scheme != 'https' or candidate.hostname != 'player.vimeo.com' or
            candidate.path != '/video/' + expected_id or candidate.username or candidate.password or
            candidate.fragment):
        raise ValueError('Vimeo 页面公开的播放器地址与来源视频不匹配')
    query = parse_qs(candidate.query, keep_blank_values=True)
    if set(query) - {'h'} or len(query.get('h', [])) > 1:
        raise ValueError('Vimeo 页面播放器地址包含未识别参数')
    if query.get('h') and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', query['h'][0]):
        raise ValueError('Vimeo 播放器隐私参数格式异常')
    return parser.player


def failure_kind(message):
    text = str(message).lower()
    status = getattr(message, 'code', None)
    if status is None:
        match = re.search(r'\b(?:http(?: error)?|status(?: code)?)\s*[:=]?\s*(\d{3})\b', text)
        status = int(match[1]) if match else None
    if 'no module named' in text:
        return 'dependency_missing'
    if 'download_disabled:' in text:
        return 'download_disabled'
    if any(word in text for word in ('connection_verification:', 'captcha', 'turnstile',
                                     'security of your connection', 'cloudflare', 'not a bot')):
        return 'access_verification_required'
    if status == 429:
        return 'rate_limited'
    if status == 401:
        return 'authentication_required'
    if status == 403:
        return 'access_denied'
    if status is not None and status >= 500:
        return 'server_error'
    if any(word in text for word in ('drm', 'encrypted with')):
        return 'protected_media'
    if any(word in text for word in ('logged-in', 'login required', 'sign in', 'password')):
        return 'authentication_required'
    return 'download_failed'


def request_stage(error):
    url = urlsplit((error.filename if isinstance(error, HTTPError) else getattr(error, 'url', '')) or '')
    if url.hostname == 'api.vimeo.com':
        return 'official_download_api'
    if url.hostname == 'player.vimeo.com':
        return 'official_media' if '/progressive_redirect/' in url.path else 'player'
    if url.hostname and url.hostname.endswith('.vimeocdn.com'):
        return 'official_media'
    return 'public_page' if url.hostname == 'vimeo.com' else 'download_or_validation'


def safe_error(message):
    # Transient CDN signatures are not needed in persistent diagnostics.
    return re.sub(r'(https?://[^\s?]+)\?[^\s]+', r'\1?[redacted]', str(message))[:1200]


def candidates(config):
    with state(config) as db:
        records = [json.loads(row[0]) for row in db.execute('SELECT record FROM assets')]
    done = {(r['project_url'], source) for r in records
            if r.get('media_type') == 'video' and r.get('external_provider') == 'vimeo'
            for source in set([r['asset_url']] + r.get('sources', []))}
    links = {}
    for record in records:
        if record.get('media_type') != 'bookmark' or record.get('external_provider') != 'vimeo':
            continue
        if record.get('download_fallback') and not config.get('collection', {}).get('recheck_download_fallbacks'):
            continue
        key = (record['project_url'], record['asset_url'])
        if key not in done:
            links.setdefault(key, record)
    return sorted(links.values(), key=lambda r: (r['project_url'], r['page_order']))


def record_download_failure(config, record, kind, detail, attempt_id=None):
    """Track retries and optionally turn terminal failures into explicit bookmarks."""
    fallback = None
    if not config.get('_paths', {}).get('state_db'):
        return None
    attempt_id = attempt_id or uuid.uuid4().hex
    with state(config) as db:
        row = db.execute('SELECT id,record FROM assets').fetchall()
        for asset_id, encoded in row:
            current = json.loads(encoded)
            if (current.get('media_type') != 'bookmark' or
                    current.get('project_url') != record.get('project_url') or
                    current.get('asset_url') != record.get('asset_url')):
                continue
            previous = db.execute('SELECT result FROM attempts WHERE attempt_id=?', (attempt_id,)).fetchone()
            if previous:
                return current.get('download_fallback')
            db.execute('INSERT INTO attempts VALUES (?,?,?,?,?,0,?,NULL,?)',
                       (attempt_id, asset_id, 'download', '', datetime.now(timezone.utc).isoformat(),
                        kind, safe_error(detail)))
            attempts = int(current.get('download_attempts', 0)) + 1
            current['download_attempts'] = attempts
            enabled = config.get('collection', {}).get('download_failure_fallback') == 'bookmarks'
            fallback_after = max(1, int(config.get('collection', {}).get(
                'download_failure_fallback_after', config.get('collection', {}).get('retry_limit', 3))))
            reason = None
            if enabled and kind == 'download_disabled':
                reason = 'download_disabled'
            elif enabled and attempts >= fallback_after:
                reason = 'retry_limit_exhausted'
            if reason:
                fallback = {'reason': reason, 'detail': safe_error(detail), 'attempts': attempts}
                current['download_fallback'] = fallback
            db.execute('UPDATE assets SET record=?, error=? WHERE id=?',
                       (json.dumps(current, ensure_ascii=False), safe_error(detail), asset_id))
            db.commit()
            break
    return fallback


def cached_download(record, directory):
    """Validate a local cache without HTTP or Eagle writes. Ignore stale caches."""
    player_url(record['asset_url'])
    target_dir = directory / 'vimeo' / urlsplit(record['asset_url']).path.strip('/')
    complete = target_dir / 'complete.json'
    if not complete.is_file():
        return None
    try:
        previous = json.loads(complete.read_text())
        file = Path(previous['local_path']).resolve(strict=True)
        if file.parent != target_dir.resolve() or not file.is_file():
            return None
        if record.get('official_downloads'):
            selected = select_official_download(record['official_downloads'], record['asset_url'])
            choice = {key: selected.get(key) for key in ('quality', 'width', 'height', 'filesize')}
            if previous.get('official_choice') != choice:
                return None
        if not file.stat().st_size or sha256(file) != previous['sha256']:
            return None
        allowed = ('local_path', 'sha256', 'external_provider', 'media_type', 'official_choice',
                   'download_quality', 'quality_status')
        metadata = {key: previous[key] for key in allowed if key in previous}
        metadata['local_path'] = str(file)
        source = {key: value for key, value in record.items() if key != 'official_downloads'}
        return {**source, **metadata, 'media_type': 'video'}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def checkpoint(config, record):
    """Commit one file's local preparation before scheduling further downloads."""
    key = hashlib.sha256((record['project_url'] + '\n' + record['asset_url']).encode()).hexdigest()
    manifest = config['_paths']['downloads'] / 'external-videos' / 'manifests' / (key + '.json')
    record = {key: value for key, value in record.items() if key != 'download_fallback'}
    atomic_write(manifest, json.dumps([record], ensure_ascii=False, indent=2).encode())
    return prepare(config, manifest)


def ffmpeg_directory(directory):
    binary = shutil.which('ffmpeg')
    if binary:
        return str(Path(binary).parent)
    from imageio_ffmpeg import get_ffmpeg_exe
    tools_dir = directory / 'tools'
    tools_dir.mkdir(parents=True, exist_ok=True)
    executable = tools_dir / 'ffmpeg'
    if not executable.exists():
        executable.symlink_to(Path(get_ffmpeg_exe()).resolve())
    return str(tools_dir)


def reject_drm(info, *, incomplete=False):
    if info.get('has_drm') or any(f.get('has_drm') for f in info.get('requested_formats') or []):
        return 'DRM protected media is not collected'
    return None


def select_official_download(formats, source_url):
    video_id = urlsplit(source_url).path.strip('/')
    allowed = []
    for item in formats:
        parsed = urlsplit(item.get('url', ''))
        if (parsed.scheme != 'https' or parsed.hostname != 'player.vimeo.com' or
                not parsed.path.startswith('/progressive_redirect/download/' + video_id + '/') or
                parsed.username or parsed.password):
            raise ValueError('下载地址必须来自当前 Vimeo 视频的官方下载菜单')
        allowed.append(item)
    if not allowed:
        raise ValueError('没有官方可下载文件')
    return max(allowed, key=lambda f: (f.get('quality', '').lower() in ('source', 'original'),
                                       f.get('height') or 0, f.get('width') or 0,
                                       f.get('filesize') or 0))


def download_official(record, target_dir):
    selected = select_official_download(record['official_downloads'], record['asset_url'])
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / 'video.mp4'
    request = Request(selected['url'], headers={'Referer': record['asset_url']})
    fd, temporary = tempfile.mkstemp(dir=target_dir, prefix='.partial-')
    try:
        with os.fdopen(fd, 'wb') as stream, urlopen(
                request, timeout=45, context=ssl.create_default_context(cafile=certifi.where())) as response:
            final = urlsplit(response.url)
            if final.scheme != 'https' or not (final.hostname == 'player.vimeo.com' or
                                               (final.hostname or '').endswith('.vimeocdn.com')):
                raise ValueError('官方文件跳转到了未识别域名')
            content_type = response.headers.get('Content-Type', '').split(';')[0]
            if content_type not in ('video/mp4', 'application/octet-stream'):
                raise ValueError('官方地址未返回视频文件，可能过期或需要连接验证')
            expected = response.headers.get('Content-Length')
            size = 0
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > 512 * 1024 * 1024:
                    raise ValueError('视频超过当前 512 MiB 单文件限制')
                stream.write(block)
            if not size or (expected and int(expected) != size):
                raise ValueError('官方视频文件下载不完整')
            stream.flush()
            os.fsync(stream.fileno())
        validation = prepare_video(temporary)[0]
        if selected.get('height') and validation['height'] < selected['height']:
            raise ValueError('实际视频分辨率低于官方标注')
        os.replace(temporary, target)
        return {'local_path': str(target.resolve()), 'sha256': sha256(target),
                'external_provider': 'vimeo', 'media_type': 'video',
                'official_choice': {key: selected.get(key) for key in ('quality', 'width', 'height', 'filesize')},
                'download_quality': {'method': 'official_download', 'width': validation['width'],
                                     'height': validation['height'], 'duration': validation['duration_seconds'],
                                     'has_audio': validation['has_audio'], 'file_bytes': size,
                                     'advertised_file_bytes': selected.get('filesize')},
                'quality_status': 'highest_available_official_download'}
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def download_one(record, directory):
    player_url(record['asset_url'])
    video_id = urlsplit(record['asset_url']).path.strip('/')
    target_dir = directory / 'vimeo' / video_id
    target_dir.mkdir(parents=True, exist_ok=True)
    # One video can be referenced by several projects. Share its validated file
    # while keeping those project records separate, without concurrent writes.
    with (target_dir / '.file.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _download_one(record, directory)


def _download_one(record, directory):
    source_record = {key: value for key, value in record.items() if key != 'official_downloads'}
    video_id = urlsplit(record['asset_url']).path.strip('/')
    target_dir = directory / 'vimeo' / video_id
    target_dir.mkdir(parents=True, exist_ok=True)
    complete = target_dir / 'complete.json'
    cached = cached_download(record, directory)
    if cached:
        return cached
    if not record.get('official_downloads'):
        try:
            record = {**record, 'official_downloads': discover_official_downloads(record['asset_url'])}
        except PublicDownloadsUnavailable:
            pass  # Fall back to the source-linked player, never fake a menu URL.
        except HTTPError as exc:
            parsed = urlsplit(exc.filename or '')
            if exc.code not in (404, 410) or parsed.hostname not in ('vimeo.com', 'www.vimeo.com'):
                raise
            # Some publishers expose a working embedded player while the Vimeo
            # watch page is unavailable. Use only the player URL extracted from
            # the source project's page; never infer a download URL.
    if record.get('official_downloads'):
        metadata = download_official(record, target_dir)
        atomic_write(complete, json.dumps(metadata, ensure_ascii=False, indent=2).encode())
        return {**source_record, **metadata}
    video_url = source_player_url(record)
    if not video_url:
        video_url = discover_player_url(record['asset_url'], record['project_url'])
    from yt_dlp import YoutubeDL
    finished = []
    options = {
        'format': 'bv+ba/b',
        'merge_output_format': 'mp4/mkv',
        'ffmpeg_location': ffmpeg_directory(directory),
        'outtmpl': str(target_dir / 'video.%(ext)s'),
        'http_headers': {'Referer': record['project_url']},
        'noplaylist': True, 'quiet': True, 'no_warnings': True,
        'socket_timeout': 25, 'retries': 1, 'fragment_retries': 1,
        'concurrent_fragment_downloads': 1, 'continuedl': True,
        'overwrites': False, 'allow_unplayable_formats': False,
        'match_filter': reject_drm,
        'post_hooks': [lambda filename: finished.append(filename)],
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(video_url, download=True)
    if not info or not finished:
        raise RuntimeError('下载器没有交付完整文件')
    file = Path(finished[-1]).resolve(strict=True)
    if file.parent != target_dir.resolve() or not file.stat().st_size:
        raise RuntimeError('下载文件路径或大小校验失败')
    validation = prepare_video(file)[0]
    metadata = {
        'local_path': str(file), 'sha256': sha256(file),
        'external_provider': 'vimeo', 'media_type': 'video',
        'download_quality': {**{key: info.get(key) for key in
                              ('format_id', 'vcodec', 'acodec')},
                             'width': validation['width'], 'height': validation['height'],
                             'duration': validation['duration_seconds'],
                             'has_audio': validation['has_audio']},
        'quality_status': 'highest_available_yt_dlp',
    }
    atomic_write(complete, json.dumps(metadata, ensure_ascii=False, indent=2).encode())
    return {**source_record, **metadata}


def pause_path(config):
    return config['_paths']['downloads'] / 'external-videos' / '.pause-request'


def select_queue(config, limit=None, project_url=None, official_links=None):
    queue = candidates(config)
    if official_links is not None:
        sources = {row['source_url']: row['downloads'] for row in official_links}
        queue = [{**row, 'official_downloads': sources[row['asset_url']]}
                 for row in queue if row['asset_url'] in sources]
    if project_url:
        queue = [row for row in queue if row['project_url'] == project_url.rstrip('/')]
    return queue, queue[:limit] if limit is not None else queue


def merge_preparation(report, result):
    target = report.setdefault('preparation', {'prepared': 0, 'existing': 0, 'failed': [], 'assets': []})
    for key in ('prepared', 'existing'):
        target[key] += result.get(key, 0)
    for key in ('failed', 'assets'):
        target[key].extend(result.get(key, []))


def report_writer(config, report, suffix):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    output = config['_paths']['reports'] / (stamp + '-' + suffix + '.json')
    report['report_path'] = str(output)
    def save():
        atomic_write(output, json.dumps(report, ensure_ascii=False, indent=2).encode())
    return save


def reconcile_cache(config, limit=None, project_url=None, progress=None):
    """Prepare existing validated files only. This operation never requests HTTP."""
    queue, selected = select_queue(config, limit, project_url)
    report = {'available': len(queue), 'selected': len(selected), 'recovered': [],
              'missing_or_invalid': [], 'failed': [], 'status': 'running'}
    save = report_writer(config, report, 'external-video-reconcile')
    directory = config['_paths']['downloads'] / 'external-videos'
    for record in selected:
        cached = cached_download(record, directory)
        if not cached:
            report['missing_or_invalid'].append(record['asset_url'])
            continue
        try:
            result = checkpoint(config, cached)
            merge_preparation(report, result)
            report['recovered'].append(record['asset_url'])
            if progress:
                progress(len(report['recovered']), len(selected), {'source_url': record['asset_url'],
                                                                 'quality': cached['download_quality']})
        except Exception as exc:
            report['failed'].append({'source_url': record['asset_url'], 'error': safe_error(exc)})
        save()
    report['status'] = 'completed_with_errors' if report['failed'] or report.get('preparation', {}).get('failed') else 'completed'
    save()
    return report


def run(config, limit=None, project_url=None, execute=False, official_links=None, progress=None,
        stop_event=None):
    queue, selected = select_queue(config, limit, project_url, official_links)
    report = {'run_id': uuid.uuid4().hex, 'github_sources': GITHUB_SOURCES, 'available': len(queue),
              'selected': len(selected), 'downloaded': [], 'blocked': [], 'failed': [],
              'fallback_bookmarks': []}
    directory = config.get('_paths', {}).get('downloads')
    directory = directory / 'external-videos' if directory else None
    if not execute:
        report['plan'] = []
        for row in selected:
            cached = cached_download(row, directory) if directory else None
            report['plan'].append({'source_url': row['asset_url'], 'player_url': player_url(row['asset_url']),
                                   'referer': row['project_url'],
                                   'phase': 'cached_ready_to_prepare' if cached else 'needs_download'})
        report['cached_ready_to_prepare'] = sum(row['phase'] == 'cached_ready_to_prepare' for row in report['plan'])
        report['needs_download'] = len(selected) - report['cached_ready_to_prepare']
        report['paused'] = pause_path(config).is_file() if directory else False
        return report
    workers = min(2, max(1, config.get('collection', {}).get('max_concurrent_downloads', 1)))
    stop_event = stop_event or Event()
    report.update(status='running', paused=False, not_attempted=len(selected))
    save = report_writer(config, report, 'external-video-download')
    save()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending, cursor, halted = {}, 0, False
        while pending or (not halted and cursor < len(selected)):
            if stop_event.is_set() or pause_path(config).is_file():
                halted = True
                report['paused'] = True
            while not halted and cursor < len(selected) and len(pending) < workers:
                record = selected[cursor]
                pending[pool.submit(download_one, record, directory)] = record
                cursor += 1
            if not pending:
                break
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                record = pending.pop(future)
                try:
                    downloaded = future.result()
                except Exception as exc:
                    kind = failure_kind(exc)
                    problem = {'source_url': record['asset_url'], 'project_url': record['project_url'],
                               'kind': kind, 'stage': request_stage(exc), 'error': safe_error(exc)}
                    problem['attempt_id'] = hashlib.sha256((report['run_id'] + '\n' +
                        record['project_url'] + '\n' + record['asset_url']).encode()).hexdigest()
                    globally_blocked = kind in (
                        'access_verification_required', 'access_denied',
                        'authentication_required', 'rate_limited', 'dependency_missing')
                    # A site-wide access or dependency block says nothing about
                    # whether this particular video is downloadable. Keep it
                    # for a later retry instead of converting it to a bookmark.
                    fallback = (None if globally_blocked else
                                record_download_failure(config, record, kind, exc, problem['attempt_id']))
                    if fallback:
                        problem['bookmark_fallback'] = fallback
                        report['fallback_bookmarks'].append({
                            'source_url': record['asset_url'],
                            'project_url': record['project_url'], **fallback})
                    if globally_blocked:
                        report['blocked'].append(problem)
                        halted = True
                    else:
                        report['failed'].append(problem)
                else:
                    item = {'source_url': record['asset_url'], 'local_path': downloaded['local_path'],
                            'quality': downloaded['download_quality']}
                    report['downloaded'].append(item)
                    try:
                        result = checkpoint(config, downloaded)
                        merge_preparation(report, result)
                        if result.get('failed'):
                            halted = True
                    except Exception as exc:
                        merge_preparation(report, {'failed': [{'source_url': record['asset_url'],
                                                               'error': safe_error(exc)}]})
                        halted = True
                    if progress:
                        progress(len(report['downloaded']), len(selected), item)
                report['not_attempted'] = len(selected) - sum(len(report[key]) for key in ('downloaded', 'blocked', 'failed'))
                save()
    if report['paused']:
        report['status'] = 'paused'
    elif report['blocked']:
        report['status'] = 'blocked'
    elif report['failed'] or report.get('preparation', {}).get('failed'):
        report['status'] = 'completed_with_errors'
    else:
        report['status'] = 'completed'
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--project-url')
    parser.add_argument('--official-links', help='JSON file containing actual official menu source_url/downloads entries')
    parser.add_argument('--recheck-fallbacks', action='store_true', help='本次重新检查已回退链接的下载权限；不删除已有书签')
    parser.add_argument('command', choices=('plan', 'download', 'pause', 'reconcile'))
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    config = load_config(args.config)
    if args.recheck_fallbacks:
        config['collection']['recheck_download_fallbacks'] = True
    marker = pause_path(config)
    if args.command == 'pause':
        atomic_write(marker, b'paused\n')
        print(json.dumps({'status': 'pause_requested', 'message':
                          '不再派发新下载；当前最多两个文件完成并保存后退出'}, ensure_ascii=False))
        return 0
    lock = None
    if args.command in ('download', 'reconcile'):
        lock_path = config['_paths']['downloads'] / 'external-videos' / '.download.lock'
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_path.open('a')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('已有外链视频下载实例运行')
    stop_event, previous_handlers = Event(), {}
    if args.command == 'download':
        marker.unlink(missing_ok=True)  # Only an explicit download resumes work.
        def request_pause(signum, frame):
            stop_event.set()
            atomic_write(marker, b'paused\n')
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, request_pause)
    def progress(count, total, item):
        print(f"[{count}/{total}] {item['source_url']} "
              f"{item['quality'].get('width')}x{item['quality'].get('height')}",
              file=sys.stderr, flush=True)
    try:
        if args.command == 'reconcile':
            report = reconcile_cache(config, args.limit, args.project_url, progress)
        else:
            official_links = json.loads(Path(args.official_links).read_text()) if args.official_links else None
            report = run(config, args.limit, args.project_url, args.command == 'download',
                         official_links, progress, stop_event)
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if lock:
            lock.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(bool(report.get('blocked') or report.get('failed') or
                    report.get('preparation', {}).get('failed')))


if __name__ == '__main__':
    raise SystemExit(main())
