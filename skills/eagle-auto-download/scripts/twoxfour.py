"""2x4 project-page adapter for the studio's WordPress work archive."""
import hashlib
import os
import re
import ssl
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import certifi

from media import sha256

SITE = 'https://2x4.org'
ARCHIVE = 'https://2x4.org/filter/all/'
PROJECT_NAMES = {}
MAX_PAGE = 20 * 1024 * 1024
MAX_ASSET = 512 * 1024 * 1024
ALLOWED_IMAGE = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg'}
ALLOWED_VIDEO = {'.mp4', '.webm', '.mov'}
SECTORS = {
    'corporate': ('企业', '企业品牌'), 'culture': ('文化', '文化艺术'),
    'design': ('设计', '设计行业'), 'education': ('教育', '教育机构'),
    'fashion': ('时尚', '时尚与奢侈品'), 'hospitality': ('餐饮', '餐饮与酒店'),
    'media': ('媒体', '媒体娱乐'), 'retail': ('零售', '零售消费'),
}


def _context():
    return ssl.create_default_context(cafile=certifi.where())


def _request(url, limit, timeout=30):
    request = Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; EagleAutoDownload/1.0)',
        'Accept-Language': 'en-US,en;q=0.9',
    })
    for attempt in range(5):
        try:
            with urlopen(request, timeout=timeout, context=_context()) as response:
                if urlsplit(response.url).hostname != '2x4.org':
                    raise ValueError('2x4 页面或素材跳转到了未适配域名')
                data = response.read(limit + 1)
            break
        except HTTPError as exc:
            if exc.code != 429 or attempt == 4:
                raise
            retry_after = exc.headers.get('Retry-After')
            try:
                delay = max(3, min(60, int(retry_after))) if retry_after else 3 * (2 ** attempt)
            except ValueError:
                delay = 3 * (2 ** attempt)
            time.sleep(delay)
    if len(data) > limit:
        raise ValueError('响应超过大小限制')
    return data


def fetch_html(url):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != '2x4.org' or
            not (parsed.path == '/filter/all/' or re.fullmatch(r'/work/[^/]+/?', parsed.path))):
        raise ValueError('仅支持 2x4.org/filter/all/ 与 /work/{slug}/')
    return _request(url, MAX_PAGE).decode('utf-8', 'replace')


class ArchiveLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.articles = []
        self.current = None
        self.active_part = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        href = attrs.get('href')
        classes = set((attrs.get('class') or '').split())
        if tag == 'article' and 'teaser' in classes:
            self.current = {'url': None, 'prefix': [], 'suffix': []}
        elif self.current and tag == 'a' and href:
            self.current['url'] = href
        elif self.current and tag == 'span' and ('prefix' in classes or 'suffix' in classes):
            self.active_part = 'prefix' if 'prefix' in classes else 'suffix'
        elif tag == 'a' and href:
            self.links.append(href)

    def handle_endtag(self, tag):
        if tag == 'span':
            self.active_part = None
        if tag == 'article' and self.current:
            self.articles.append(self.current)
            self.current = None

    def handle_data(self, data):
        if self.current and self.active_part:
            self.current[self.active_part].append(data.strip())


def discover(html):
    parser = ArchiveLinks()
    parser.feed(html)
    projects = []
    PROJECT_NAMES.clear()
    for article in parser.articles:
        url = urljoin(SITE, article.get('url') or '')
        parsed = urlsplit(url)
        if (parsed.scheme == 'https' and parsed.hostname == '2x4.org' and
                re.fullmatch(r'/work/[^/]+/?', parsed.path)):
            url = urlunsplit(('https', '2x4.org', parsed.path.rstrip('/') + '/', '', ''))
            prefix = ' '.join(' '.join(article['prefix']).split()).title()
            suffix = ' '.join(' '.join(article['suffix']).split())
            label = ' · '.join(value for value in (prefix, suffix) if value)
            if label:
                PROJECT_NAMES[url.rstrip('/')] = label
            projects.append(url)
    for link in parser.links:
        url = urljoin(SITE, link)
        parsed = urlsplit(url)
        if (parsed.scheme == 'https' and parsed.hostname == '2x4.org' and
                re.fullmatch(r'/work/[^/]+/?', parsed.path)):
            projects.append(urlunsplit(('https', '2x4.org', parsed.path.rstrip('/') + '/', '', '')))
    projects = list(dict.fromkeys(projects))
    if len(projects) < 100:
        raise ValueError(f'2x4 All 列表只发现 {len(projects)} 个项目，需检查页面/反爬结构')
    return projects


class Project(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_main = False
        self.main_article = False
        self.article_depth = 0
        self.main_finished = False
        self.in_related = False
        self.related_depth = 0
        self.in_h1 = False
        self.in_client = False
        self.client_depth = 0
        self.in_body = False
        self.body_depth = 0
        self.in_text = False
        self.title_parts, self.client_parts, self.summary_parts = [], [], []
        self.images, self.videos, self.vimeo_players, self.links = [], [], [], []
        self.categories = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get('class') or '').split())
        if self.in_related:
            if tag == 'aside':
                self.related_depth += 1
            return
        if self.main_article and tag == 'aside':
            self.in_related, self.related_depth = True, 1
            return
        if tag == 'article' and not self.main_article and not self.main_finished and 'full' in classes and 'work' in classes:
            self.main_article, self.article_depth = True, 1
            for cls in classes:
                if cls.startswith('category-'):
                    self.categories.append(cls.removeprefix('category-'))
        elif self.main_article and tag == 'article':
            self.article_depth += 1
        if not self.main_article or self.article_depth != 1:
            return
        if tag in ('h1', 'h2') and 'entry-title' in classes:
            self.in_h1 = True
        if tag == 'div' and 'client' in classes:
            self.in_client = True
            self.client_depth = 1
        if tag == 'div' and self.in_body:
            self.body_depth += 1
        if tag == 'div' and 'body' in classes:
            self.in_body = True
            self.body_depth = 1
        if tag in ('p', 'li', 'blockquote') and self.in_body:
            self.in_text = True
        if tag == 'img':
            self.images.append({k: attrs.get(k) for k in ('src', 'srcset', 'data-src', 'data-original', 'alt')})
        if tag in ('video', 'source'):
            if attrs.get('src'):
                self.videos.append(attrs['src'])
            if attrs.get('data-src'):
                self.videos.append(attrs['data-src'])
        if attrs.get('data-video'):
            self.videos.append(attrs['data-video'])
        if tag == 'iframe' and (attrs.get('src') or attrs.get('data-src')):
            video = attrs.get('src') or attrs.get('data-src')
            self.videos.append(video)
            if urlsplit(urljoin(SITE, video)).hostname == 'player.vimeo.com':
                self.vimeo_players.append(video)
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])

    def handle_endtag(self, tag):
        if self.in_related:
            if tag == 'aside':
                self.related_depth -= 1
                if self.related_depth <= 0:
                    self.in_related = False
            return
        if not self.main_article:
            return
        if tag == 'article':
            self.article_depth -= 1
            if self.article_depth <= 0:
                self.main_article, self.main_finished = False, True
            return
        if self.article_depth != 1:
            return
        if tag == 'div' and self.in_client:
            self.client_depth -= 1
            if self.client_depth <= 0:
                self.in_client = False
        if tag in ('h1', 'h2'):
            self.in_h1 = False
        if tag in ('p', 'li', 'blockquote'):
            self.in_text = False
        if tag == 'div' and self.in_body:
            self.body_depth -= 1
            if self.body_depth <= 0:
                self.in_body = False

    def handle_data(self, data):
        if not self.main_article or self.in_related or self.article_depth != 1:
            return
        value = data.strip()
        if not value:
            return
        if self.in_h1:
            self.title_parts.append(value)
        if self.in_client:
            self.client_parts.append(value)
        if self.in_body and self.in_text:
            self.summary_parts.append(value)


def _best_image(attrs):
    candidates = []
    for key in ('data-original', 'data-src', 'src'):
        if attrs.get(key):
            candidates.append((0, attrs[key]))
    srcset = attrs.get('srcset') or ''
    for entry in srcset.split(','):
        bits = entry.strip().split()
        if not bits:
            continue
        width = int(bits[1][:-1]) if len(bits) > 1 and bits[1].endswith('w') and bits[1][:-1].isdigit() else 0
        candidates.append((width, bits[0]))
    for _, value in sorted(candidates, reverse=True):
        url = urljoin(SITE, unescape(value).replace('\\/', '/'))
        parsed = urlsplit(url)
        suffix = Path(parsed.path).suffix.lower()
        if (parsed.scheme == 'https' and parsed.hostname == '2x4.org' and
                parsed.path.startswith('/wp-content/uploads/') and suffix in ALLOWED_IMAGE):
            return urlunsplit(('https', '2x4.org', quote(parsed.path, safe='/@:+-_.~%'), '', ''))
    return None


def _project_parts(html, project_url):
    parser = Project()
    parser.feed(html)
    if not parser.title_parts:
        raise ValueError('2x4 案例页缺少主标题或项目正文结构已变化')
    canonical = project_url.rstrip('/')
    title = ' '.join(parser.title_parts)
    client = ' '.join(parser.client_parts).strip() or title
    summary = ' '.join(parser.summary_parts)
    summary = re.sub(r'\s+', ' ', unescape(summary)).strip()
    summary = summary or None
    return title, client, summary, parser


def _safe_video(value):
    url = urljoin(SITE, value.strip().replace('\\/', '/'))
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password or
            parsed.hostname not in ('2x4.org', 'vimeo.com', 'www.vimeo.com', 'player.vimeo.com')):
        return None
    suffix = Path(parsed.path).suffix.lower()
    if parsed.hostname == '2x4.org' and parsed.path.startswith('/wp-content/uploads/') and suffix in ALLOWED_VIDEO:
        return urlunsplit(('https', parsed.hostname, quote(parsed.path, safe='/@:+-_.~%'), '', ''))
    vimeo = re.fullmatch(r'/(?:video/)?(\d+)/?', parsed.path)
    if parsed.hostname in ('vimeo.com', 'www.vimeo.com', 'player.vimeo.com') and vimeo:
        return urlunsplit(('https', 'vimeo.com', '/' + vimeo.group(1), '', ''))
    return None


def _safe_vimeo_player(value):
    url = urljoin(SITE, value.strip().replace('\\/', '/'))
    parsed = urlsplit(url)
    video = re.fullmatch(r'/video/(\d+)/?', parsed.path)
    if (parsed.scheme != 'https' or parsed.hostname != 'player.vimeo.com' or
            parsed.username or parsed.password or parsed.fragment or not video):
        return None
    # Preserve Vimeo's unlisted hash; discard ordinary player controls.
    from urllib.parse import parse_qs
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {'h'} or len(query.get('h', [])) > 1:
        return None
    if query.get('h') and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', query['h'][0]):
        return None
    return urlunsplit(('https', 'player.vimeo.com', '/video/' + video.group(1),
                       ('h=' + query['h'][0]) if query.get('h') else '', ''))


def extract(html, project_url):
    title, client, summary, parser = _project_parts(html, project_url)
    media, seen = [], set()
    source_players = {}
    for value in parser.vimeo_players:
        player = _safe_vimeo_player(value)
        source = _safe_video(value)
        if player and source:
            source_players.setdefault(source, player)
    for attrs in parser.images:
        url = _best_image(attrs)
        if url and url not in seen:
            seen.add(url)
            filename = Path(urlsplit(url).path).stem
            media.append({'asset_url': url, 'media_type': 'image',
                          'is_project_logo': bool(re.search(r'(?:^|[-_\s])(?:logo|wordmark|logotype)(?:[-_\s.]|$)', filename, re.I)),
                          'quality_status': 'largest_page_srcset'})
    for value in parser.videos + parser.links:
        url = _safe_video(value)
        if not url or url in seen:
            continue
        seen.add(url)
        if urlsplit(url).hostname == 'vimeo.com':
            item = {'asset_url': url, 'media_type': 'bookmark', 'external_provider': 'vimeo',
                    'is_project_logo': False, 'quality_status': 'public_vimeo_embed'}
            if url in source_players:
                item['source_player_url'] = source_players[url]
            media.append(item)
        else:
            media.append({'asset_url': url, 'media_type': 'video', 'is_project_logo': False,
                          'quality_status': 'wordpress_original_video'})
    if not media:
        raise ValueError('项目正文没有可识别的图片或视频')
    records = []
    categories = list(dict.fromkeys(parser.categories))
    sector_tags = [SECTORS[item][1] for item in categories if item in SECTORS]
    category_tags = list(dict.fromkeys(SECTORS[item][0] for item in categories if item in SECTORS))
    for index, item in enumerate(media, 1):
        slug_name = Path(urlsplit(project_url).path.rstrip('/')).name.replace('-', ' ').title()
        display_name = PROJECT_NAMES.get(project_url.rstrip('/')) or f'{slug_name} · {client}'
        records.append({'studio': '2x4', 'project_title': title,
                        'project_display_name': display_name,
                        'project_url': project_url, **item, 'page_order': index,
                        'tags': category_tags + sector_tags,
                        'category_tags': category_tags, 'industry_tags': sector_tags,
                        'project_summary': summary, 'source_description': summary or '',
                        'adapter_revision': '2x4-main-v2',
                        'declared_width': None, 'declared_height': None})
    return records


def collect(project_url, directory, delay=1, metadata=None, max_concurrent_downloads=2):
    html = fetch_html(project_url)
    records = extract(html, project_url)
    metadata = metadata or {}
    for record in records:
        record['project_display_name'] = metadata.get('display_name', record['project_display_name'])
        record['category_tags'] = metadata.get('category_tags', record.get('category_tags', []))
        record['industry_tags'] = metadata.get('industry_tags', record.get('industry_tags', []))
        # Annotations are reserved for Chinese summaries, so keep English page
        # copy in the local record only until a Chinese summary is supplied.
        record['project_summary'] = metadata.get('summary')
    originals = Path(directory).resolve() / 'originals'
    originals.mkdir(parents=True, exist_ok=True)
    def download(record):
        url = record['asset_url']
        if record['media_type'] == 'bookmark':
            return {**record, 'local_path': None, 'download_sha256': None}, None
        suffix = Path(urlsplit(url).path).suffix.lower()
        target = originals / (hashlib.sha256(url.encode()).hexdigest() + suffix)
        if target.is_file() and target.stat().st_size:
            return {**record, 'local_path': str(target), 'download_sha256': sha256(target)}, None
        last_error = None
        for attempt in range(3):
            if delay:
                time.sleep(delay)
            fd, temp = tempfile.mkstemp(dir=originals, prefix='.partial-')
            try:
                with os.fdopen(fd, 'wb') as stream:
                    request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; EagleAutoDownload/1.0)'})
                    with urlopen(request, timeout=90, context=_context()) as response:
                        if urlsplit(response.url).hostname != '2x4.org':
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
                        if not size or (length and size != int(length)):
                            raise ValueError('素材下载不完整')
                        stream.flush()
                        os.fsync(stream.fileno())
                os.replace(temp, target)
                result = {**record, 'local_path': str(target), 'download_sha256': sha256(target)}
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass
                if attempt < 2:
                    retry_after = (exc.headers.get('Retry-After')
                                   if isinstance(exc, HTTPError) and exc.code == 429 else None)
                    try:
                        backoff = max(3, min(60, int(retry_after))) if retry_after else (3 * (2 ** attempt))
                    except ValueError:
                        backoff = 3 * (2 ** attempt)
                    time.sleep(backoff)
        if last_error:
            return None, {'asset_url': url, 'error': str(last_error)}
        return result, None

    workers = max(1, min(int(max_concurrent_downloads), 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        outcomes = list(pool.map(download, records))
    downloaded = [item for item, error in outcomes if item]
    failed = [error for item, error in outcomes if error]
    manifest = Path(directory).resolve() / 'manifest.json'
    import json
    manifest.write_text(json.dumps(downloaded, ensure_ascii=False, indent=2))
    return {'manifest': str(manifest), 'downloaded': downloaded, 'failed': failed}
