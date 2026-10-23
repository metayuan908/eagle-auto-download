"""Wolff Olins project-page adapter for its current server-rendered work site."""
import hashlib
import json
import os
import re
import ssl
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import certifi

from media import atomic_write, sha256

SITE = 'https://www.wolffolins.com'
MAX_PAGE = 20 * 1024 * 1024
MAX_ASSET = 512 * 1024 * 1024


class WorkPage(HTMLParser):
    def __init__(self, project_page=False):
        super().__init__()
        self.project_page = project_page
        self.links = []
        self.media = []
        self.page_title = False
        self.title_parts = []
        self.gallery = False
        self.in_related = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title':
            self.page_title = True
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if not self.project_page or self.in_related:
            return
        if tag in ('h2', 'h3'):
            # Related project cards begin after the visible case-study body.
            self._heading = True
        if tag == 'img':
            classes = attrs.get('class', '')
            if 'flex-auto' in classes and 'self-stretch' in classes:
                src = attrs.get('src', '')
                candidate = parse_qs(urlsplit(src).query).get('url', [src])[0]
                self.media.append(('image', unquote(candidate)))

    def handle_endtag(self, tag):
        if tag == 'title':
            self.page_title = False

    def handle_data(self, data):
        if self.page_title:
            self.title_parts.append(data)


def _context():
    return ssl.create_default_context(cafile=certifi.where())


def _urlopen_retry(request, timeout, attempts=3):
    last_error = None
    for attempt in range(attempts):
        try:
            return urlopen(request, timeout=timeout, context=_context())
        except Exception as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(2 ** (attempt + 1))
    raise last_error


def fetch_html(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'www.wolffolins.com' or
            not parsed.path.startswith('/work')):
        raise ValueError('仅支持 www.wolffolins.com/work 下的页面')
    request = Request(url, headers={'User-Agent': 'EagleAutoDownload/1.0'})
    with _urlopen_retry(request, timeout=30) as response:
        if urlsplit(response.url).hostname != 'www.wolffolins.com':
            raise ValueError('项目页跳转到了未适配域名')
        data = response.read(MAX_PAGE + 1)
    if len(data) > MAX_PAGE:
        raise ValueError('项目页超过 20 MiB 限制')
    return data.decode('utf-8')


def discover(html):
    parser = WorkPage()
    parser.feed(html)
    projects = []
    for link in parser.links:
        url = urljoin(SITE, link)
        path = urlsplit(url).path.rstrip('/')
        if (urlsplit(url).hostname == 'www.wolffolins.com' and
                re.fullmatch(r'/work/[^/]+', path) and path != '/work/archive'):
            # Wolff Olins canonical project URLs omit the trailing slash.
            projects.append(url)
    projects = list(dict.fromkeys(projects))
    if not projects:
        raise ValueError('Wolff Olins 项目列表为空或页面结构已变化')
    return projects


def _clean_asset_url(value):
    value = value.replace('\\/', '/').replace('\\u0026', '&').rstrip('.,;')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.hostname not in (
            'images.ctfassets.net', 'videos.ctfassets.net'):
        return None
    # Use Contentful's original file instead of the site's resized Next.js proxy.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def extract(html, project_url):
    parser = WorkPage(project_page=True)
    # Exclude recommendations and site-wide media after the project body.
    body = re.split(r'Related Projects', html, maxsplit=1, flags=re.IGNORECASE)[0]
    parser.feed(body)
    title = ''.join(parser.title_parts).split(' — ')
    if len(title) >= 2 and title[0].strip().lower() == 'work':
        project_title = title[1].strip()
    else:
        project_title = urlsplit(project_url).path.rstrip('/').split('/')[-1].replace('-', ' ').title()

    assets = []
    seen = set()
    for kind, value in parser.media:
        url = _clean_asset_url(value)
        suffix = Path(urlsplit(url).path).suffix.lower() if url else ''
        if suffix not in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.avif'):
            url = None
        if url and url not in seen:
            seen.add(url)
            assets.append((kind, url))

    # Videos are exposed as direct Contentful URLs in the server-rendered payload,
    # even when the site does not emit a <video>/<source> element.
    body = body.replace('\\/', '/').replace('\\u0026', '&')
    for value in re.findall(r'https://videos\.ctfassets\.net/[^\s"\\<>]+', body):
        url = _clean_asset_url(value)
        if url and Path(urlsplit(url).path).suffix.lower() not in ('.mp4', '.webm'):
            url = None  # Ignore partial URL strings embedded in the page payload.
        if url and url not in seen:
            seen.add(url)
            assets.append(('video', url))

    if not assets:
        raise ValueError('项目正文没有可识别的图片或视频，需检查适配器')
    records = []
    for kind, url in assets:
        filename = Path(urlsplit(url).path).stem
        is_logo = bool(re.search(r'(?:^|[_\-\s])(?:logo|wordmark|logotype)(?:[_\-\s.]|$)',
                                 filename, re.IGNORECASE))
        records.append({'studio': 'Wolff Oline', 'project_title': project_title,
                        'project_url': project_url, 'asset_url': url,
                        'media_type': kind, 'is_project_logo': is_logo,
                        'page_order': len(records) + 1, 'tags': [],
                        'quality_status': 'contentful_original',
                        'declared_width': None, 'declared_height': None})
    return records


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
        url = record['asset_url']
        suffix = Path(urlsplit(url).path).suffix.lower()
        if not suffix:
            failed.append({'asset_url': url, 'error': '素材 URL 缺少文件扩展名'})
            continue
        target = originals / (hashlib.sha256(url.encode()).hexdigest() + suffix)
        if target.is_file() and target.stat().st_size:
            downloaded.append({**record, 'local_path': str(target),
                               'download_sha256': sha256(target)})
            continue
        last_error = None
        for attempt in range(3):
            if delay:
                time.sleep(delay)
            fd, temp = tempfile.mkstemp(dir=originals, prefix='.partial-')
            try:
                request = Request(url, headers={'User-Agent': 'EagleAutoDownload/1.0'})
                with os.fdopen(fd, 'wb') as stream, _urlopen_retry(
                        request, timeout=90, attempts=1) as response:
                    if urlsplit(response.url).hostname not in (
                            'images.ctfassets.net', 'videos.ctfassets.net'):
                        raise ValueError('素材跳转到了未适配域名')
                    length = response.headers.get('Content-Length')
                    size = 0
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_ASSET:
                            raise ValueError('素材超过 512 MiB 限制')
                        stream.write(chunk)
                    if size == 0 or (length is not None and size != int(length)):
                        raise ValueError('素材下载不完整')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, target)
                downloaded.append({**record, 'local_path': str(target),
                                   'download_sha256': sha256(target)})
                last_error = None
                break
            except Exception as exc:
                last_error = exc
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        if last_error:
            failed.append({'asset_url': url, 'error': str(last_error)})

    manifest = Path(directory).resolve() / 'manifest.json'
    atomic_write(manifest, json.dumps(downloaded, ensure_ascii=False, indent=2).encode())
    return {'manifest': str(manifest), 'downloaded': len(downloaded), 'failed': failed}
