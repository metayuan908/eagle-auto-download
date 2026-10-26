"""Adobe's project-linked public CCV player, decoded files, no persistent signed URLs."""
import fcntl
import json
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from media import atomic_write, prepare_video, sha256

PROVIDER = 'adobe-ccv'
HOST = 'www-ccv.adobe.io'
CDN = 'cdn-prod-ccv.adobe.com'


def canonical(url):
    p = urlsplit(url)
    match = re.fullmatch(r'/v1/player/ccv/([A-Za-z0-9_-]{1,80})/embed/?', p.path)
    query = parse_qs(p.query, keep_blank_values=True)
    if (p.scheme != 'https' or p.hostname != HOST or p.port not in (None, 443) or
            p.username or p.password or p.fragment or not match or
            set(query) - {'api_key', 'bgcolor'} or
            query.get('api_key', ['behance1']) != ['behance1']):
        raise ValueError('Adobe 视频必须来自项目正文明确给出的 CCV 播放器')
    return urlunsplit(('https', HOST, '/v1/player/ccv/' + match[1] + '/embed', '', ''))


def player_url(url):
    return canonical(url) + '?api_key=behance1'


def target_directory(record, directory):
    video_id = urlsplit(canonical(record['asset_url'])).path.split('/')[-2]
    return Path(directory) / PROVIDER / video_id


def cached_download(record, directory):
    target = target_directory(record, directory)
    try:
        old = json.loads((target / 'complete.json').read_text())
        file = Path(old['local_path'])
        if (old['source_url'] != canonical(record['asset_url']) or file.is_symlink() or
                file.resolve(strict=True).parent != target.resolve() or not file.stat().st_size or
                sha256(file) != old['sha256']):
            return None
        keys = ('local_path', 'sha256', 'download_quality', 'quality_status')
        return {**record, **{k: old[k] for k in keys}, 'media_type': 'video', 'external_provider': PROVIDER}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def validate_info(info, source):
    if not info or info.get('_type', 'video') != 'video' or info.get('has_drm'):
        raise ValueError('播放器没有提供独立的公开非 DRM 视频')
    video_id = urlsplit(canonical(source)).path.split('/')[-2]
    formats = info.get('formats') or []
    if not any(f.get('width') and f.get('height') for f in formats):
        raise ValueError('播放器画质列表不完整，不能把低清 MP4 回退当作最高画质')
    for f in formats:
        p = urlsplit(f.get('url', ''))
        if (p.scheme != 'https' or p.hostname != CDN or p.username or p.password or
                not p.path.startswith('/' + video_id + '/rend/')):
            raise ValueError('播放器媒体与当前 Adobe 视频不匹配')


def download_one(record, directory, *, proxy=None, retries=3):
    from external_video import ffmpeg_directory, reject_drm
    from yt_dlp import YoutubeDL
    target = target_directory(record, directory)
    target.mkdir(parents=True, exist_ok=True)
    with (target / '.file.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cached = cached_download(record, directory)
        if cached:
            return cached
        options = {'format': 'bv+ba/b', 'merge_output_format': 'mp4/mkv',
                   'ffmpeg_location': ffmpeg_directory(Path(directory)),
                   'outtmpl': str(target / 'video.%(ext)s'), 'noplaylist': True,
                   'quiet': True, 'no_warnings': True, 'socket_timeout': 25,
                   'retries': 3, 'fragment_retries': 5, 'continuedl': True,
                   'skip_unavailable_fragments': False, 'concurrent_fragment_downloads': 1,
                   'overwrites': False, 'allow_unplayable_formats': False,
                   'match_filter': reject_drm, 'http_headers': {'Referer': record['project_url']}}
        if proxy is not None:
            options['proxy'] = proxy  # Empty string is an explicit, diagnosed direct connection.
        for attempt in range(retries):
            try:
                finished = []
                options['post_hooks'] = [lambda path: finished.append(path)]
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(player_url(record['asset_url']), download=False)
                    validate_info(info, record['asset_url'])
                    info = ydl.process_ie_result(info, download=True)
                if not finished:
                    raise RuntimeError('下载器未交付完整视频')
                file = Path(finished[-1])
                if file.is_symlink() or file.resolve(strict=True).parent != target.resolve():
                    raise ValueError('下载视频路径越界')
                try:
                    validation = prepare_video(file, Path(directory).parent / 'video-validation')[0]
                except Exception:
                    # Keep the rejected generated file for inspection; a later
                    # attempt must not accept yt-dlp's "already downloaded" path.
                    file.rename(file.with_name('invalid-' + uuid.uuid4().hex + file.suffix))
                    raise
                for dimension in ('width', 'height'):
                    if info.get(dimension) and validation[dimension] < info[dimension]:
                        raise ValueError('下载的视频分辨率低于选中的播放版本')
                metadata = {'source_url': canonical(record['asset_url']), 'local_path': str(file.resolve()),
                            'sha256': validation['sha256'], 'quality_status': 'highest_available_adobe_playback',
                            'download_quality': {'format_id': info.get('format_id'),
                                'width': validation['width'], 'height': validation['height'],
                                'duration': validation['duration_seconds'], 'has_audio': validation['has_audio'],
                                'file_bytes': file.stat().st_size}}
                atomic_write(target / 'complete.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode())
                return {**record, **metadata, 'media_type': 'video', 'external_provider': PROVIDER}
            except Exception as exc:
                from external_video import failure_kind
                if failure_kind(exc) in ('authentication_required', 'access_denied', 'rate_limited',
                                        'access_verification_required', 'protected_media') or attempt + 1 == retries:
                    raise
                time.sleep(2 ** attempt)
