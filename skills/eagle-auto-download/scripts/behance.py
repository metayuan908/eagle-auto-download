"""Public Behance profile pagination and owner-verified project body modules."""
import base64
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

import adobe_ccv
from landor import plain
from source_media import check_url, collect_records, request

SITE = 'https://www.behance.net'
HOSTS = {'www.behance.net'}
IMAGE_HOSTS = {'mir-s3-cdn-cf.behance.net'}
REVISION = 'behance-body-v1'


def profile_name(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname != 'www.behance.net' or p.username or p.password or
            p.query or p.fragment or p.port not in (None, 443) or
            not re.fullmatch(r'/[A-Za-z0-9_-]+/?', p.path)):
        raise ValueError('Behance 列表地址必须是公开用户主页')
    return p.path.strip('/')


def canonical(url):
    p = urlsplit(urljoin(SITE, url))
    if (p.scheme != 'https' or p.hostname != 'www.behance.net' or p.username or p.password or
            p.query or p.fragment or p.port not in (None, 443) or
            not re.fullmatch(r'/gallery/\d+(?:/[^/?#]+)?/?', p.path)):
        raise ValueError('Behance 项目 URL 格式异常')
    return urlunsplit(('https', 'www.behance.net', p.path.rstrip('/'), '', ''))


class ProfileLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.projects, self.next_pages = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag != 'a' or not a.get('href'):
            return
        url = urljoin(SITE, a['href'])
        p = urlsplit(url)
        if p.hostname != 'www.behance.net':
            return
        if re.fullmatch(r'/gallery/\d+(?:/[^/?#]+)?/?', p.path) and not p.query and not p.fragment:
            self.projects.append(canonical(url))
        if 'after' in parse_qs(p.query):
            self.next_pages.append((url, 'next' in a.get('rel', '').split()))


def _offset(url):
    cursor = parse_qs(urlsplit(url).query).get('after', [''])[0]
    try:
        return int(base64.b64decode(cursor + '=' * (-len(cursor) % 4)).decode()) if cursor else 0
    except (ValueError, UnicodeError):
        return None


def discover_projects(profile_url, delay=1):
    owner = profile_name(profile_url)
    current = SITE + '/' + owner
    visited, projects, ids = set(), [], set()
    for _ in range(1000):
        if current in visited:
            raise ValueError('Behance 分页循环')
        visited.add(current)
        parser = ProfileLinks()
        parser.feed(request(current, HOSTS).decode('utf-8', 'replace'))
        fresh = 0
        for url in parser.projects:
            pid = urlsplit(url).path.split('/')[2]
            if pid not in ids:
                ids.add(pid)
                projects.append(url)
                fresh += 1
        candidates = []
        offset = _offset(current)
        for url, is_next in parser.next_pages:
            p = urlsplit(url)
            if (p.path.rstrip('/') != '/' + owner or set(parse_qs(p.query)) != {'after'} or
                    p.username or p.password or p.fragment):
                raise ValueError('Behance 分页跳到其他用户或未知参数')
            if is_next and url in visited:
                raise ValueError('Behance 分页循环')
            forward = _offset(url)
            if url not in visited and (is_next or offset is None or forward is None or forward > offset):
                candidates.append((url, is_next, forward))
        if not candidates:
            if parser.next_pages and fresh == 0:
                raise ValueError('Behance 分页重复，不能当作遍历完成')
            break
        if not fresh:
            raise ValueError('Behance 下一页没有新增项目')
        preferred = [r for r in candidates if r[1]] or candidates
        unique = {r[0]: r for r in preferred}
        if len(unique) > 1 and any(r[2] is None for r in unique.values()):
            raise ValueError('Behance 有多个无法确定顺序的下一页')
        current = min(unique.values(), key=lambda r: r[2] or 0)[0]
        if delay:
            time.sleep(delay)
    else:
        raise ValueError('Behance 分页超过限制')
    if not projects:
        raise ValueError('Behance 公开项目清单为空，需检查登录／访问限制或页面结构')
    return projects


def fetch_html(url):
    pid = urlsplit(canonical(url)).path.split('/')[2]
    raw = request(SITE + '/v2/projects/' + pid + '?client_id=BehanceWebSusi1', HOSTS)
    data = json.loads(raw)
    project = data.get('project')
    if not isinstance(project, dict) or str(project.get('id')) != pid:
        raise ValueError('Behance 项目 API 身份不符')
    return json.dumps(project, ensure_ascii=False)


def best_image(module):
    sizes = module.get('sizes') or {}
    candidates = [(k, v) for k, v in sizes.items()
                  if isinstance(v, str) and not re.search(r'(webp|still|opt_)', k)]
    def rank(pair):
        k, _ = pair
        d = (module.get('dimensions') or {}).get(k, {})
        return (k in ('original', 'source'), d.get('width', 0) * d.get('height', 0),
                max([int(n) for n in re.findall(r'\d+', k)] or [0]))
    value = max(candidates, key=rank)[1] if candidates else module.get('src')
    if not value:
        raise ValueError('Behance 图片模块缺少公开原图地址')
    check_url(value, IMAGE_HOSTS)
    p = urlsplit(value)
    if p.query or not p.path.startswith('/project_modules/'):
        raise ValueError('Behance 原图必须来自项目正文模块，不能使用主页封面')
    return value


class Frames(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'iframe' and dict(attrs).get('src'):
            self.urls.append(dict(attrs)['src'])


def extract(data, project_url, owner=None, studio=None):
    p = json.loads(data) if isinstance(data, str) else data
    project_url = canonical(project_url)
    pid = urlsplit(project_url).path.split('/')[2]
    if str(p.get('id')) != pid or (p.get('url') and canonical(p['url']) != project_url):
        raise ValueError('Behance 案例与请求 URL 不一致')
    owners = {o.get('username', '').casefold() for o in p.get('owners', [])}
    if owner and owner.casefold() not in owners:
        raise ValueError('Behance 案例不属于配置的主页作者')
    if p.get('privacy') not in (None, 'public'):
        raise ValueError('仅处理 Behance 公开案例')
    if not p.get('name') or not isinstance(p.get('modules'), list):
        raise ValueError('Behance 项目正文结构变化')
    modules = p['modules']
    source = '\n'.join(dict.fromkeys(filter(None, [plain(p.get('description', ''))] +
                           [plain(m.get('text', '')) for m in modules if m.get('type') == 'text'])))
    records, seen = [], set()
    def add(m, media_type, url, **extra):
        if str(m.get('project_id', pid)) != pid:
            raise ValueError('Behance 模块归属其他案例')
        if url in seen:
            return
        seen.add(url)
        records.append({'studio': studio or owner or 'Behance', 'project_title': p['name'],
                        'project_url': project_url, 'asset_url': url, 'media_type': media_type,
                        'page_order': len(records) + 1, 'source_description': source,
                        'description_status': 'present' if source else 'absent',
                        'adapter_revision': REVISION, 'behance_project_id': pid,
                        'behance_module_id': str(m.get('id', '')), **extra})
    for m in modules:
        if str(m.get('project_id', pid)) != pid:
            raise ValueError('Behance 模块归属其他案例')
        kind = m.get('type')
        if kind == 'text':
            continue
        if kind == 'image':
            add(m, 'image', best_image(m), source_field='project_modules.image')
        elif kind == 'media_collection':
            for component in m.get('components', []):
                add(component, 'image', best_image(component), source_field='project_modules.media_collection')
            if not m.get('components'):
                raise ValueError('Behance 图片集合为空或结构变化')
        elif kind == 'video':
            url = adobe_ccv.canonical(m.get('src', ''))
            add(m, 'bookmark', url, external_provider=adobe_ccv.PROVIDER,
                source_player_url=adobe_ccv.player_url(url), source_field='project_modules.video')
        elif kind == 'embed':
            from twoxfour import _safe_vimeo_player
            frames = Frames()
            frames.feed(m.get('embed', ''))
            player = _safe_vimeo_player(frames.urls[0]) if len(frames.urls) == 1 else None
            if not player:
                raise ValueError('Behance 正文含未适配的嵌入媒体')
            video_id = urlsplit(player).path.split('/')[-1]
            add(m, 'bookmark', 'https://vimeo.com/' + video_id, external_provider='vimeo',
                source_player_url=player, source_field='project_modules.embed')
        else:
            raise ValueError('Behance 出现未适配正文模块：' + str(kind))
    if not records:
        raise ValueError('Behance 案例正文没有可识别媒体')
    return records


def collect(project_url, directory, delay=1, metadata=None, max_concurrent_downloads=2,
            profile_url=None, studio=None):
    if not profile_url:
        raise ValueError('Behance 采集需要主页地址以核对作者')
    records = extract(fetch_html(project_url), project_url, profile_name(profile_url), studio)
    return collect_records(records, directory, IMAGE_HOSTS, metadata, delay, max_concurrent_downloads)
