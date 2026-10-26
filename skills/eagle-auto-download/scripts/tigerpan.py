"""Tiger Pan's public CMS: visible cases, desktop body albums and native video."""
import json
import re
import time
from urllib.parse import urlencode, urlsplit

from landor import plain
from source_media import check_url, collect_records, request

SITE = 'https://www.tigerpan.com'
API = 'https://api.tigerpan.cn'
HOSTS = {'api.tigerpan.cn'}
MEDIA_HOSTS = {'tiger-pan.oss-cn-shenzhen.aliyuncs.com'}
REVISION = 'tigerpan-detail-v1'


def canonical(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname != 'www.tigerpan.com' or p.username or p.password or
            p.query or p.fragment or p.port not in (None, 443) or
            not re.fullmatch(r'/works(?:/\d+)?/?', p.path)):
        raise ValueError('Tiger Pan 地址不在作品列表或案例范围')
    return SITE + p.path.rstrip('/')


def api(path, params=None):
    data = json.loads(request(API + path + ('?' + urlencode(params) if params else ''), HOSTS))
    if data.get('code') != 200 or 'data' not in data:
        raise ValueError('Tiger Pan 公共 API 返回异常')
    return data['data']


def discover_projects(list_url, delay=1):
    if canonical(list_url) != SITE + '/works':
        raise ValueError('Tiger Pan 列表地址不匹配')
    rows, ids, expected = [], set(), None
    page, total_pages = 1, 1
    while page <= total_pages:
        data = api('/works/list', {'pageNum': page, 'pageSize': 100, 'orderByClause': 'sequence asc'})
        if data.get('pageNum') != page or not isinstance(data.get('list'), list):
            raise ValueError('Tiger Pan 分页结构变化')
        if expected is None:
            expected, total_pages = data['total'], data['totalPage']
            if not 1 <= total_pages <= 1000:
                raise ValueError('Tiger Pan 分页数量异常')
        if data.get('total') != expected or data.get('totalPage') != total_pages:
            raise ValueError('Tiger Pan 列表在分页期间发生变化')
        for row in data['list']:
            pid = str(row['id'])
            if not pid.isdigit() or pid in ids:
                raise ValueError('Tiger Pan 项目身份重复或异常')
            ids.add(pid)
            if not row.get('isHide') and not row.get('isDelete'):
                rows.append(SITE + '/works/' + pid)
        page += 1
        if delay and page <= total_pages:
            time.sleep(delay)
    if len(ids) != expected or not rows:
        raise ValueError('Tiger Pan 项目总数与分页不一致')
    return rows


def fetch_html(url):
    url = canonical(url)
    pid = urlsplit(url).path.split('/')[-1]
    if not pid.isdigit():
        raise ValueError('需要 Tiger Pan 案例详情地址')
    return json.dumps(api('/works/' + pid), ensure_ascii=False)


def extract(data, project_url):
    p = json.loads(data) if isinstance(data, str) else data
    url = canonical(project_url)
    if (str(p.get('id')) != urlsplit(url).path.split('/')[-1] or
            p.get('isHide') or p.get('isDelete')):
        raise ValueError('Tiger Pan 案例身份不符、隐藏或已删除')
    title = p.get('name') or p.get('title')
    if not title:
        raise ValueError('Tiger Pan 案例没有标题')
    source = plain(p.get('detailHtml', ''))
    values = [(u.strip(), 'image', 'albumPics') for u in (p.get('albumPics') or '').split(',') if u.strip()]
    if p.get('video'):
        values.append((p['video'], 'video', 'video'))
    records, seen = [], set()
    for asset, kind, field in values:
        check_url(asset, MEDIA_HOSTS)
        if urlsplit(asset).query:
            raise ValueError('Tiger Pan 原件地址含未知参数')
        if asset in seen:
            continue
        seen.add(asset)
        records.append({'studio': 'Tiger Pan 潘虎设计实验室', 'project_title': title,
            'project_url': url, 'asset_url': asset, 'media_type': kind,
            'page_order': len(records) + 1, 'source_description': source,
            'description_status': 'present' if source else 'absent', 'adapter_revision': REVISION,
            'source_field': field, 'is_project_logo': False})
    if not records:
        raise ValueError('Tiger Pan 案例正文没有媒体')
    return records


def collect(project_url, directory, delay=1, metadata=None, max_concurrent_downloads=2):
    records = extract(fetch_html(project_url), project_url)
    return collect_records(records, directory, MEDIA_HOSTS, metadata, delay, max_concurrent_downloads)
