"""Landor English case studies: explicit CMS blocks, never Up Next or posters."""
import hashlib
import json
import os
import re
import ssl
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import certifi
from media import atomic_write, sha256
from saffron import NextData

SITE = 'https://landor.com'
ASSET_HOST = 'cms.landor.com'
REVISION = 'landor-content-v1'
MAX_PAGE = 20 * 1024 * 1024
MAX_ASSET = 512 * 1024 * 1024
IMAGES = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.avif'}
VIDEOS = {'.mp4', '.webm', '.mov', '.mkv'}
NON_MEDIA = {'breadcrumb', 'caseStudyDetailHeader', 'caseStudyDetailText',
             'paseStudyDetailProjectInfo', 'caseStudyDetailProjectInfo', 'caseStudyDetailFooter'}
MEDIA = {'caseStudyDetailHero', 'caseStudyDetailImage', 'caseStudyDetailVideo',
         'caseStudyDetail2Images', 'caseStudyDetail3Images', 'caseStudyDetail3ImagesMirrored'}


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        if tag in ('br', 'p', 'li'):
            self.parts.append(' ')


def plain(value):
    parser = Text()
    parser.feed(value or '')
    parser.close()
    return re.sub(r'\s+', ' ', ''.join(parser.parts)).strip()


def page_data(html):
    parser = NextData()
    parser.feed(html)
    if not parser.parts:
        raise ValueError('Landor 页面缺少 __NEXT_DATA__')
    data = json.loads(''.join(parser.parts))
    props = data.get('props', {}).get('pageProps', {}).get('initialProps', {})
    page = props.get('PageData')
    if props.get('isErrorPage') or not isinstance(page, dict) or not isinstance(page.get('contents'), list):
        raise ValueError('Landor 页面内容结构变化或返回错误页')
    return page


def canonical(value):
    parsed = urlsplit(urljoin(SITE, value))
    if (parsed.scheme != 'https' or parsed.hostname != 'landor.com' or parsed.username or
            parsed.password or parsed.query or parsed.fragment or
            not re.fullmatch(r'/en/our-work(?:/[^/]+)?/?', parsed.path)):
        raise ValueError('Landor URL 不在已适配的英文案例范围')
    return urlunsplit(('https', 'landor.com', parsed.path.rstrip('/'), '', ''))


def discover(html):
    pages = []
    for block in page_data(html)['contents']:
        if block.get('type') != 'caseStudiesListing':
            continue
        for item in block.get('items', []):
            url = canonical(item.get('link', {}).get('url', ''))
            if re.fullmatch(r'/en/our-work/[^/]+', urlsplit(url).path):
                pages.append(url)
    pages = list(dict.fromkeys(pages))
    if not pages:
        raise ValueError('Landor 项目清单为空，需检查分页和页面结构')
    return pages


def description(html):
    parts = []
    for block in page_data(html)['contents']:
        if block.get('type') == 'caseStudyDetailText':
            parts.append(plain(block.get('description')))
        elif block.get('type') in ('paseStudyDetailProjectInfo', 'caseStudyDetailProjectInfo'):
            parts.append(plain(block.get('projectInfoBody')))
    return '\n'.join(value for value in parts if value)


def asset_url(value):
    if not isinstance(value, str):
        raise ValueError('Landor 素材链接格式异常')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname != ASSET_HOST or parsed.username or
            parsed.password or parsed.query or parsed.fragment or not parsed.path.startswith('/media/') or
            Path(parsed.path).suffix.lower() not in IMAGES | VIDEOS):
        raise ValueError('Landor 素材不在已确认的 CMS 原件范围：' + value)
    return value


def extract(html, project_url):
    page = page_data(html)
    project_url = canonical(project_url)
    headers = [block for block in page['contents'] if block.get('type') == 'caseStudyDetailHeader']
    crumbs = [block for block in page['contents'] if block.get('type') == 'breadcrumb']
    if (len(headers) != 1 or not headers[0].get('title') or len(crumbs) != 1 or
            not crumbs[0].get('items') or
            canonical(crumbs[0]['items'][-1].get('link', {}).get('url', '')) != project_url):
        raise ValueError('Landor 案例身份与请求链接不一致')
    unknown = {block.get('type') for block in page['contents']} - MEDIA - NON_MEDIA
    if unknown:
        raise ValueError('Landor 有未适配内容块，停止以免漏采或混入推荐：' + str(unknown))
    title = plain(headers[0]['title']) or plain(page.get('name')) or plain(page.get('seo', {}).get('pageTitle'))
    if not title:
        raise ValueError('Landor 案例标题没有可核实的文本或 CMS 名称')
    source = description(html)
    records, seen = [], set()
    for block in page['contents']:
        kind = block['type']
        if kind in NON_MEDIA:
            continue
        if kind == 'caseStudyDetailVideo':
            video = block.get('video')
            if isinstance(video, dict):
                video = video.get('src')
            assets = [{'url': video}] if video else []
        elif kind == 'caseStudyDetailHero':
            assets = [block.get('image'), block.get('mobileImage')]
        elif kind == 'caseStudyDetailImage':
            assets = [block.get('image')]
        else:
            assets = block.get('images', [])
        for asset in assets:
            if not asset or not asset.get('url'):
                continue
            url = asset_url(asset['url'])
            if url in seen:
                continue
            suffix = Path(urlsplit(url).path).suffix.lower()
            if kind == 'caseStudyDetailVideo' and suffix not in VIDEOS:
                raise ValueError('Landor 视频块未交付可下载视频文件')
            seen.add(url)
            records.append({'studio': 'LANDOR 朗涛', 'project_title': title,
                'project_url': project_url, 'asset_url': url,
                'media_type': 'video' if suffix in VIDEOS else 'image',
                'page_order': len(records)+1, 'tags': [], 'quality_status': 'cms_original',
                'adapter_revision': REVISION, 'source_description': source,
                'description_status': 'present' if source else 'absent',
                'declared_width': asset.get('width'), 'declared_height': asset.get('height'),
                'is_project_logo': suffix in IMAGES and bool(re.search(
                    r'(?:^|[_\-\s])(?:logo|wordmark|logotype)(?:[_\-\s.]|$)',
                    Path(urlsplit(url).path).stem, re.I))})
    if not records:
        raise ValueError('Landor 当前案例正文没有可识别素材')
    return records


def fetch_html(url):
    url = canonical(url)
    request = Request(url + '/', headers={'User-Agent': 'EagleAutoDownload/1.0'})
    with urlopen(request, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as response:
        if canonical(response.url) != url:
            raise ValueError('Landor 页面跳转到其他案例或站点')
        raw = response.read(MAX_PAGE+1)
    if len(raw) > MAX_PAGE:
        raise ValueError('Landor 项目页超过 20 MiB')
    return raw.decode('utf-8')


def download(record, originals):
    url = asset_url(record['asset_url'])
    target = originals / (hashlib.sha256(url.encode()).hexdigest() + Path(urlsplit(url).path).suffix.lower())
    receipt = target.with_suffix(target.suffix + '.json')
    if target.is_file() and receipt.is_file():
        try:
            cached = json.loads(receipt.read_text())
            if cached.get('url') == url and target.stat().st_size == cached.get('size') and sha256(target) == cached.get('sha256'):
                return {**record, 'local_path': str(target), 'download_sha256': cached['sha256']}
        except (OSError, ValueError, TypeError):
            pass  # A damaged receipt is not evidence that the cached original is complete.
    for attempt in range(3):
        fd, temporary = tempfile.mkstemp(dir=originals, prefix='.partial-')
        try:
            request = Request(url, headers={'User-Agent': 'EagleAutoDownload/1.0', 'Referer': record['project_url']+'/'})
            with os.fdopen(fd, 'wb') as stream, urlopen(request, timeout=90,
                    context=ssl.create_default_context(cafile=certifi.where())) as response:
                if urlsplit(response.url).hostname != ASSET_HOST:
                    raise ValueError('Landor 素材跳转到未适配域名')
                if response.headers.get('Content-Type', '').lower().startswith(('text/', 'application/json')):
                    raise ValueError('Landor 素材请求返回网页或 JSON，不是媒体文件')
                length, size = response.headers.get('Content-Length'), 0
                while True:
                    chunk = response.read(1024*1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_ASSET:
                        raise ValueError('Landor 单素材超过 512 MiB')
                    stream.write(chunk)
                if not size or length is not None and size != int(length):
                    raise ValueError('Landor 素材下载不完整')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            digest = sha256(target)
            atomic_write(receipt, json.dumps({'url': url, 'sha256': digest, 'size': size}).encode())
            return {**record, 'local_path': str(target), 'download_sha256': digest}
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**(attempt+1))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def collect(project_url, directory, delay=1, metadata=None, max_concurrent_downloads=2):
    records = extract(fetch_html(project_url), project_url)
    metadata = metadata or {}
    for record in records:
        for key, field in (('project_display_name', 'display_name'), ('category_tags', 'category_tags'),
                           ('industry_tags', 'industry_tags'), ('project_summary', 'summary')):
            if field in metadata:
                record[key] = metadata[field]
    originals = Path(directory).resolve() / 'originals'
    originals.mkdir(parents=True, exist_ok=True)
    downloaded, failed = [], []
    manifest = originals.parent / 'manifest.json'
    workers = max(1, min(2, max_concurrent_downloads))
    def one(record):
        if delay:
            time.sleep(delay)
        return download(record, originals)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, record): record for record in records}
        for future in as_completed(futures):
            record = futures[future]
            try:
                downloaded.append(future.result())
            except Exception as exc:
                failed.append({'asset_url': record['asset_url'], 'error': str(exc)})
            downloaded.sort(key=lambda value: value['page_order'])
            atomic_write(manifest, json.dumps(downloaded, ensure_ascii=False, indent=2).encode())
    return {'manifest': str(manifest), 'downloaded': len(downloaded), 'failed': failed}
