"""Saffron case-study adapter for the current Next.js/Craft CMS site."""
import hashlib
import json
import os
import re
import ssl
import tempfile
import time
import base64
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import certifi

from media import atomic_write, sha256

SITE = 'https://saffron-consultants.com'
ASSET_HOST = 'saffron.imgix.net'
MAX_PAGE = 20 * 1024 * 1024
MAX_ASSET = 512 * 1024 * 1024
ALLOWED_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.mp4', '.webm'}


class NextData(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'script' and dict(attrs).get('id') == '__NEXT_DATA__':
            self.active = True

    def handle_data(self, data):
        if self.active:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'script':
            self.active = False


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href'):
            self.values.append(attrs['href'])


def _page_data(html):
    parser = NextData()
    parser.feed(html)
    if not parser.parts:
        raise ValueError('Saffron 页面缺少 __NEXT_DATA__，页面结构可能已变化')
    data = json.loads(''.join(parser.parts))
    return data.get('props', {}).get('pageProps', {})


def discover(html):
    parser = Links()
    parser.feed(html)
    projects = []
    for link in parser.values:
        url = urljoin(SITE, link)
        parsed = urlsplit(url)
        if parsed.scheme == 'https' and parsed.hostname == 'saffron-consultants.com' and re.fullmatch(r'/work/[^/]+', parsed.path):
            projects.append(urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/'), '', '')))
    projects = list(dict.fromkeys(projects))
    if not projects:
        raise ValueError('Saffron 项目列表为空或页面结构已变化')
    return projects


def _entry(html, project_url):
    entry = _page_data(html).get('entry')
    canonical = (entry or {}).get('url', '').rstrip('/')
    if not entry or canonical != project_url.rstrip('/'):
        raise ValueError('Saffron 项目数据与请求 URL 不匹配')
    if not isinstance(entry.get('pageBlocks'), list):
        raise ValueError('Saffron 项目页缺少 pageBlocks')
    return entry


def description(html):
    props = _page_data(html)
    entry = props.get('entry') or {}
    seo = entry.get('seo') or []
    return (seo[0].get('description') if seo and isinstance(seo[0], dict) else None)


def vimeo_thumbnail(video_url, cache_dir):
    """Fetch the public Vimeo poster through oEmbed for Eagle bookmark previews."""
    parsed = urlsplit(video_url)
    if (parsed.scheme != 'https' or parsed.hostname != 'vimeo.com' or
            not re.fullmatch(r'/\d+', parsed.path)):
        raise ValueError('无效的 Vimeo 视频链接')
    cache = Path(cache_dir).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (parsed.path.lstrip('/') + '.jpg')
    if not target.is_file() or not target.stat().st_size:
        endpoint = 'https://vimeo.com/api/oembed.json?' + urlencode({'url': video_url})
        request = Request(endpoint, headers={'User-Agent': 'EagleAutoDownload/1.0'})
        with urlopen(request, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as response:
            data = json.loads(response.read(2 * 1024 * 1024 + 1))
        thumb = urlsplit(data.get('thumbnail_url', ''))
        if thumb.scheme != 'https' or thumb.hostname != 'i.vimeocdn.com':
            raise ValueError('Vimeo oEmbed 未提供允许的预览图地址')
        request = Request(data['thumbnail_url'], headers={'User-Agent': 'EagleAutoDownload/1.0'})
        with urlopen(request, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as response:
            if urlsplit(response.url).hostname != 'i.vimeocdn.com':
                raise ValueError('Vimeo 预览图跳转到未适配域名')
            raw = response.read(10 * 1024 * 1024 + 1)
        if not raw or len(raw) > 10 * 1024 * 1024:
            raise ValueError('Vimeo 预览图为空或超过 10 MiB')
        atomic_write(target, raw)
    return base64.b64encode(target.read_bytes()).decode('ascii')


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _asset_url(asset):
    src = asset.get('src')
    if not isinstance(src, str):
        return None
    parsed = urlsplit(src)
    suffix = Path(parsed.path).suffix.lower()
    if parsed.scheme != 'https' or parsed.hostname != ASSET_HOST or parsed.username or parsed.password:
        return None
    if suffix not in ALLOWED_EXTENSIONS:
        return None
    # The CMS supplies an imgix URL; query parameters are rendition controls.
    # Keep the exact CMS-provided resource path while removing transformation options.
    return urlunsplit(('https', ASSET_HOST, parsed.path, '', ''))


def extract(html, project_url):
    entry = _entry(html, project_url)
    clients = entry.get('clients') or []
    title = next((c.get('title') for c in clients if c.get('title')), None)
    title = title or urlsplit(project_url).path.rsplit('/', 1)[-1].replace('-', ' ').replace('_', ' ').title()
    records, seen = [], set()
    # Only the current entry's cover is its hero. Card/gallery renditions and
    # relatedCaseStudies are recommendations/thumbnails, not the displayed hero.
    # Append newly supported hero assets after the body so existing import names
    # keep their stable page_order during a resumed collection.
    heroes = [child['cover'] for node in entry.get('media', [])
              for child in node.get('children', []) if child.get('cover')]
    for node in _walk([entry['pageBlocks'], heroes]):
        for field, kind in (('imageAsset', 'image'), ('videoAsset', 'video')):
            for asset in node.get(field, []) if isinstance(node.get(field, []), list) else []:
                url = _asset_url(asset)
                if not url or url in seen:
                    continue
                seen.add(url)
                records.append({'studio': 'Saffron', 'project_title': title,
                                'project_url': project_url, 'asset_url': url,
                                'media_type': kind,
                                'is_project_logo': bool(re.search(
                                    r'(?:^|[_\-\s])(?:logo|wordmark|logotype)(?:[_\-\s.]|$)',
                                    Path(urlsplit(url).path).stem, re.IGNORECASE)),
                                'page_order': len(records) + 1, 'tags': [],
                                'quality_status': 'cms_asset',
                                'declared_width': asset.get('oriWidth'),
                                'declared_height': asset.get('oriHeight')})
        video_id = node.get('videoId')
        if isinstance(video_id, str):
            parsed = urlsplit(video_id)
            if (parsed.scheme == 'https' and parsed.hostname in ('vimeo.com', 'www.vimeo.com') and
                    re.fullmatch(r'/\d+', parsed.path) and not parsed.query and not parsed.fragment):
                url = urlunsplit(('https', 'vimeo.com', parsed.path, '', ''))
                if url not in seen:
                    seen.add(url)
                    records.append({'studio': 'Saffron', 'project_title': title,
                                    'project_url': project_url, 'asset_url': url,
                                    'media_type': 'bookmark', 'external_provider': 'vimeo',
                                    'is_project_logo': False, 'page_order': len(records) + 1,
                                    'tags': [], 'quality_status': 'vimeo_public_embed',
                                    'declared_width': None, 'declared_height': None})
    if not records:
        raise ValueError('Saffron 项目正文没有可识别的直接图片/视频文件')
    return records


def fetch_html(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'saffron-consultants.com' or
            not re.fullmatch(r'/work(?:/[^/]+)?/?', parsed.path)):
        raise ValueError('仅支持 saffron-consultants.com/work 下的页面')
    request = Request(url, headers={'User-Agent': 'EagleAutoDownload/1.0'})
    with urlopen(request, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as response:
        if urlsplit(response.url).hostname != 'saffron-consultants.com':
            raise ValueError('Saffron 页面跳转到了未适配域名')
        data = response.read(MAX_PAGE + 1)
    if len(data) > MAX_PAGE:
        raise ValueError('项目页超过 20 MiB 限制')
    return data.decode('utf-8')


def collect(project_url, directory, delay=1, metadata=None):
    html = fetch_html(project_url)
    records = extract(html, project_url)
    metadata = metadata or {}
    for record in records:
        record.update(project_display_name=metadata.get('display_name'),
                      category_tags=metadata.get('category_tags', []),
                      industry_tags=metadata.get('industry_tags', []),
                      project_summary=metadata.get('summary'))
    originals = Path(directory).resolve() / 'originals'
    originals.mkdir(parents=True, exist_ok=True)
    downloaded, failed = [], []
    for record in records:
        if delay:
            time.sleep(delay)
        url = record['asset_url']
        suffix = Path(urlsplit(url).path).suffix.lower()
        if record['media_type'] == 'bookmark':
            downloaded.append({**record, 'local_path': None, 'download_sha256': None})
            continue
        target = originals / (hashlib.sha256(url.encode()).hexdigest() + suffix)
        if target.is_file() and target.stat().st_size:
            downloaded.append({**record, 'local_path': str(target), 'download_sha256': sha256(target)})
            continue
        last_error = None
        for attempt in range(3):
            fd, temp = tempfile.mkstemp(dir=originals, prefix='.partial-')
            try:
                request = Request(url, headers={'User-Agent': 'EagleAutoDownload/1.0'})
                with os.fdopen(fd, 'wb') as stream, urlopen(
                        request, timeout=90, context=ssl.create_default_context(cafile=certifi.where())) as response:
                    if urlsplit(response.url).hostname != ASSET_HOST:
                        raise ValueError('素材跳转到了未适配域名')
                    expected_length = response.headers.get('Content-Length')
                    size = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_ASSET:
                            raise ValueError('素材超过 512 MiB 限制')
                        stream.write(chunk)
                    if not size or (expected_length is not None and size != int(expected_length)):
                        raise ValueError('素材下载不完整')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, target)
                downloaded.append({**record, 'local_path': str(target), 'download_sha256': sha256(target)})
                last_error = None
                break
            except Exception as exc:
                last_error = exc
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
            if attempt < 2:
                time.sleep(2 ** (attempt + 1))
        if last_error:
            failed.append({'asset_url': url, 'error': str(last_error)})
    manifest = Path(directory).resolve() / 'manifest.json'
    atomic_write(manifest, json.dumps(downloaded, ensure_ascii=False, indent=2).encode())
    return {'manifest': str(manifest), 'downloaded': len(downloaded), 'failed': failed}
