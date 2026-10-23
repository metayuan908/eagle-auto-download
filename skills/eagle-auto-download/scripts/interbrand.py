"""Interbrand Next.js data adapter. Reject incomplete pagination and unknown layouts."""
import json
import os
import ssl
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

import certifi

from media import atomic_write, sha256


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


def page_data(html):
    parser = NextData()
    parser.feed(html)
    if not parser.parts:
        raise ValueError('Interbrand 页面结构已变化，缺少 __NEXT_DATA__')
    return json.loads(''.join(parser.parts))['props']['pageProps']['data']


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def discover(html):
    connections = [v for v in walk(page_data(html))
                   if v.get('__typename') == 'RootQueryToCaseStudyConnection']
    if len(connections) != 1:
        raise ValueError('无法确认 Interbrand 项目列表范围')
    connection = connections[0]
    if connection.get('pageInfo', {}).get('hasNextPage') is not False:
        raise ValueError('项目列表存在未加载分页，不能报告完整发现')
    urls = list(dict.fromkeys(urljoin('https://interbrand.com', edge['node']['uri'])
                             for edge in connection['edges']))
    if not urls:
        raise ValueError('项目列表为空，需要检查页面结构')
    return urls


def extract(html, project_url):
    project = page_data(html).get('caseStudiesByAcfField')
    if not project or urljoin('https://interbrand.com', project.get('uri', '')) != project_url:
        raise ValueError('项目数据与请求 URL 不匹配')
    content = project['caseStudyContent']
    logo = ((content.get('heroGroup') or {}).get('brandLogo') or {}).get('node') or {}
    optional = {'imageSection2Group': 'showImageSection2',
                'imageSection3Group': 'showImageSection3',
                'imageSection4Group': 'showImageSection4',
                'testimonialGroup': 'showTestimonialSection',
                'mediaResponseGroup': 'showMediaResponseSection',
                'bgbSectionGroup': 'showBGBSection',
                'outcomeStatementsGroup': 'showOutcomeSection'}
    # A whitelist excludes recommended projects, global navigation and list thumbnails.
    groups = ['heroGroup', 'challengeGroup', 'imageSection1Group', 'whatWeDidGroup',
              'imageSection2Group', 'outcomeStatementsGroup', 'testimonialGroup',
              'imageSection3Group', 'mediaResponseGroup', 'imageSection4Group', 'bgbSectionGroup']
    records, seen = [], set()
    for group in groups:
        if group in optional and content.get(optional[group]) is not True:
            continue
        for node in walk(content.get(group)):
            if node.get('__typename') != 'MediaItem' or not node.get('url'):
                continue
            url = node['url']
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.hostname != 'd338ucc43w64ir.cloudfront.net':
                raise ValueError('发现未经适配的素材域名')
            if url in seen:
                continue
            seen.add(url)
            details = node.get('mediaDetails') or {}
            kind = 'video' if (node.get('mimeType') or '').startswith('video/') or parsed.path.lower().endswith('.mp4') else 'image'
            records.append({'studio': 'Interbrand', 'project_title': project['title'],
                            'project_url': project_url, 'asset_url': url, 'media_type': kind,
                            'is_project_logo': url == logo.get('url'),
                            'page_order': len(records) + 1, 'tags': [],
                            'quality_status': 'cms_original',
                            'declared_width': details.get('width'),
                            'declared_height': details.get('height')})
    if not records:
        raise ValueError('项目正文没有可识别素材，需检查适配器')
    return records


def fetch_html(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'interbrand.com' or not parsed.path.startswith('/work/'):
        raise ValueError('仅支持 interbrand.com/work/ 下的链接')
    request = Request(url, headers={'User-Agent': 'BrandReferenceCollector/0.1'})
    with urlopen(request, timeout=30, context=ssl.create_default_context(cafile=certifi.where())) as response:
        if urlsplit(response.url).hostname != 'interbrand.com':
            raise ValueError('页面跳转到了未适配域名')
        data = response.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise ValueError('页面超过大小限制')
    return data.decode('utf-8')


def collect(project_url, directory, delay=1, metadata=None):
    import hashlib
    html = fetch_html(project_url)
    records = extract(html, project_url)
    metadata = metadata or {}
    for record in records:
        record.update(project_display_name=metadata.get('display_name'),
                      category_tags=metadata.get('category_tags', []),
                      industry_tags=metadata.get('industry_tags', []),
                      project_summary=metadata.get('summary'))
    directory = Path(directory).resolve()
    originals = directory / 'originals'
    originals.mkdir(parents=True, exist_ok=True)
    successful, failed = [], []
    for record in records:
        time.sleep(delay)
        url = record['asset_url']
        suffix = Path(urlsplit(url).path).suffix
        target = originals / (hashlib.sha256(url.encode()).hexdigest() + suffix)
        fd, temp = tempfile.mkstemp(dir=originals, prefix='.partial-')
        try:
            request = Request(url, headers={'User-Agent': 'BrandReferenceCollector/0.1'})
            with os.fdopen(fd, 'wb') as stream, urlopen(request, timeout=60,
                    context=ssl.create_default_context(cafile=certifi.where())) as response:
                if urlsplit(response.url).hostname != 'd338ucc43w64ir.cloudfront.net':
                    raise ValueError('素材跳转到了未适配域名')
                length = response.headers.get('Content-Length')
                size = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > 512 * 1024 * 1024:
                        raise ValueError('素材超过 512 MiB 限制')
                    stream.write(chunk)
                if size == 0 or (length is not None and size != int(length)):
                    raise ValueError('素材下载不完整')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
            successful.append({**record, 'local_path': str(target), 'download_sha256': sha256(target)})
        except Exception as exc:
            failed.append({'asset_url': url, 'error': str(exc)})
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    manifest = directory / 'manifest.json'
    atomic_write(manifest, json.dumps(successful, ensure_ascii=False, indent=2).encode())
    return {'manifest': str(manifest), 'downloaded': len(successful), 'failed': failed}
