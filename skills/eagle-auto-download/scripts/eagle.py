"""Eagle public API client; all writes require the configured library."""
import json
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from media import sha256


def normalized_eagle_name(name):
    """Match names after Eagle removes filesystem-reserved ASCII punctuation."""
    cleaned = re.sub(r'[\\/:*?"<>|]', '', str(name))
    return re.sub(r'\s+', ' ', cleaned).strip().casefold()


class Eagle:
    def __init__(self, config):
        self.base_url = config['base_url'].rstrip('/')
        url = urlsplit(self.base_url)
        if url.scheme != 'http' or url.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Eagle 接口必须是本机 HTTP 地址')
        expected = config.get('expected_library_path')
        if not expected:
            raise ValueError('缺少 eagle.expected_library_path')
        self.expected = Path(expected).expanduser().resolve()
        self._items = None
        self._folders = None
        self._digests = {}
        self._deadline = None
        self._offset_mode = None
        self.metrics = {'requests': 0, 'full_scans': 0, 'folder_tree_reads': 0}

    def request(self, endpoint, payload=None):
        self.metrics['requests'] += 1
        data = None if payload is None else json.dumps(payload).encode('utf-8')
        request = Request(self.base_url + endpoint, data=data,
                          headers={'Content-Type': 'application/json'})
        remaining = self._deadline - time.monotonic() if self._deadline is not None else 20
        if remaining <= 0:
            raise TimeoutError('Eagle 回读已达到截止时间')
        with urlopen(request, timeout=min(20, remaining)) as response:
            result = json.load(response)
        if result.get('status') != 'success':
            raise RuntimeError('Eagle API 未返回成功状态')
        return result.get('data', {k: result[k] for k in ('id', 'ids') if k in result} or None)

    def check_library(self):
        info = self.request('/api/library/info')
        actual = info.get('library', {}).get('path')
        if not actual or Path(actual).resolve() != self.expected:
            raise RuntimeError('当前 Eagle 库与配置的测试库不一致，请先在 Eagle 中打开测试库')
        return info

    def post(self, endpoint, payload):
        if getattr(self, 'write_guard', None) and not self.write_guard():
            raise RuntimeError('导入已暂停，停止后续 Eagle 写入')
        self.check_library()
        return self.request(endpoint, payload)

    @staticmethod
    def response_ids(data):
        """Accept documented ID-bearing responses, retaining success-only compatibility."""
        if isinstance(data, str):
            return [data] if re.fullmatch(r'[A-Za-z0-9_-]+', data) else []
        if isinstance(data, list):
            return list(dict.fromkeys(i for row in data for i in Eagle.response_ids(row)))
        if isinstance(data, dict):
            return Eagle.response_ids(data.get('id', data.get('ids', [])))
        return []

    def iter_items(self, filters=None):
        """Detect page-index versus item-offset APIs; never silently skip page two."""
        self.check_library()
        filters = dict(filters or {})
        def page(offset):
            rows = self.request('/api/item/list?' + urlencode(
                {**filters, 'limit': 200, 'offset': offset, 'orderBy': 'CREATEDATE'}))
            if not isinstance(rows, list):
                raise RuntimeError('Eagle 对账结果格式异常')
            return rows
        rows = page(0)
        seen = set()
        offset = 0
        while rows:
            ids = [item['id'] for item in rows]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise RuntimeError('Eagle 分页出现重叠；停止以免漏查或重复导入')
            seen.update(ids)
            yield from rows
            if len(rows) < 200:
                break
            if offset == 0 and self._offset_mode is None:
                probe = page(1)
                if not probe:
                    break
                probe_ids = [item['id'] for item in probe]
                if ids[1:] == probe_ids[:199]:
                    self._offset_mode = 'items'
                    offset, rows = 200, page(200)
                elif not set(ids).intersection(probe_ids):
                    self._offset_mode = 'pages'
                    offset, rows = 1, probe
                else:
                    raise RuntimeError('无法确认 Eagle 分页语义，停止全库对账')
            else:
                offset += 1 if self._offset_mode == 'pages' else 200
                rows = page(offset)
        self.check_library()

    def items(self, refresh=False):
        self.check_library()
        if self._items is None or refresh:
            self.metrics['full_scans'] += 1
            self._items = {row['id']: row for row in self.iter_items() if not row.get('isDeleted')}
        return list(self._items.values())

    def info(self, item_id):
        self.check_library()
        try:
            item = self.request('/api/item/info?' + urlencode({'id': item_id}))
        except HTTPError as exc:
            if exc.code != 404:
                raise  # A server error is not proof that the item is absent.
            return None
        if not item or item.get('isDeleted'):
            return None
        if item.get('id') != item_id:
            raise RuntimeError('Eagle 返回了其他条目的身份')
        if self._items is not None:
            self._items[item_id] = item
        self.check_library()
        return item

    def file_digest(self, item):
        """Read an Eagle original without writing library internals; API identity first."""
        self.check_library()
        item_id = item['id']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', item_id):
            raise ValueError('Eagle 条目 ID 格式异常')
        directory = self.expected / 'images' / (item_id + '.info')
        ext = item.get('ext', '')
        name = item.get('name', '')
        if not ext or not re.fullmatch(r'[A-Za-z0-9]+', ext) or Path(name).name != name:
            raise RuntimeError('无法安全确定 Eagle 原件路径')
        path = directory / (name + '.' + ext)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.expected) or directory.is_symlink() or path.is_symlink():
            raise RuntimeError('Eagle 原件路径越界或为符号链接')
        stat = resolved.stat()
        key = (str(resolved), stat.st_size, stat.st_mtime_ns)
        if key not in self._digests:
            digest = sha256(resolved)
            after = resolved.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise RuntimeError('Eagle 原件在验收时发生变化')
            self._digests[key] = digest
        self.check_library()
        return self._digests[key]

    def verify_file(self, item_id, digest):
        item = self.info(item_id)
        if not item or self.file_digest(item) != digest:
            raise RuntimeError('Eagle 文件身份验收失败：条目缺失或原件哈希不一致')
        return item

    def find_content_matches(self, digest, path, known_ids=()):
        size = Path(path).stat().st_size
        candidates = {row['id']: row for row in self.items()
                      if row.get('ext') != 'url' and row.get('size') == size}
        for item_id in known_ids:
            item = self.info(item_id)
            if item:
                candidates[item_id] = item
        found = []
        for item_id in candidates:
            item = self.info(item_id)
            if item and self.file_digest(item) == digest:
                found.append(item)
        return found

    def find_items(self, marker):
        return [item for item in self.items() if marker in item.get('annotation', '').splitlines()]

    def find_matching_items(self, expected, legacy_marker=None, refresh=False):
        matches = []
        for item in self.items(refresh=refresh):
            legacy = legacy_marker and legacy_marker in item.get('annotation', '').splitlines()
            identity = (normalized_eagle_name(item.get('name', '')) ==
                        normalized_eagle_name(expected['name']) and
                        item.get('url', '').rstrip('/') == expected['website'].rstrip('/'))
            if legacy or identity:
                matches.append(item)
        return matches

    def find_matching_items_in_folder(self, expected, folder_id, legacy_marker=None):
        matches = []
        for item in self.iter_items({'folders': folder_id}):
            if item.get('isDeleted') or folder_id not in item.get('folders', []):
                continue
            legacy = legacy_marker and legacy_marker in item.get('annotation', '').splitlines()
            identity = (normalized_eagle_name(item.get('name', '')) ==
                        normalized_eagle_name(expected['name']) and
                        item.get('url') == expected.get('website'))
            if legacy or identity:
                matches.append(item)
        return matches

    def find_bookmarks(self, url, folder_id=None):
        return [item for item in self.iter_items({'folders': folder_id} if folder_id else {})
                if not item.get('isDeleted') and item.get('ext') == 'url' and
                item.get('url', '').rstrip('/') == url.rstrip('/') and
                (not folder_id or folder_id in item.get('folders', []))]

    def folder(self, names):
        self.check_library()
        if self._folders is None:
            self.metrics['folder_tree_reads'] += 1
            self._folders = self.request('/api/folder/list')
        children = self._folders
        parent = None
        for name in names:
            matches = [f for f in children if f.get('name') == name]
            if len(matches) > 1:
                raise RuntimeError('存在同名文件夹，请先整理后再导入')
            if matches:
                folder = matches[0]
            else:
                payload = {'folderName': name}
                if parent:
                    payload['parent'] = parent
                folder = self.post('/api/folder/create', payload)
                if not isinstance(folder, dict) or not folder.get('id'):
                    raise RuntimeError('Eagle 创建文件夹返回值缺少 ID')
                children.append(folder)
            parent = folder['id']
            children = folder.setdefault('children', [])
        return parent

    def ensure_folder_description(self, folder_id, description):
        if not description:
            return
        def walk(rows):
            for row in rows:
                if row['id'] == folder_id:
                    return row
                found = walk(row.get('children', []))
                if found:
                    return found
        folder = walk(self._folders or [])
        if folder is None:
            raise RuntimeError('项目文件夹缓存缺失')
        if folder.get('description', '') != description:
            self.post('/api/folder/update', {'folderId': folder_id, 'newDescription': description})
            self.metrics['folder_tree_reads'] += 1
            self._folders = self.request('/api/folder/list')
            self.check_library()
            confirmed = walk(self._folders)
            if not confirmed or confirmed.get('description', '') != description:
                raise RuntimeError('项目文件夹中文简介回读不一致')

    def await_item(self, expected, legacy_marker=None, folder_id=None, attempts=60,
                   item_ids=(), timeout=30):
        previous = self._deadline
        self._deadline = time.monotonic() + timeout
        try:
            for attempt in range(attempts):
                if time.monotonic() >= self._deadline:
                    break
                try:
                    if item_ids:
                        matches = [item for key in item_ids if (item := self.info(key))]
                    elif folder_id:
                        matches = self.find_matching_items_in_folder(expected, folder_id, legacy_marker)
                    else:
                        matches = self.find_matching_items(expected, legacy_marker, refresh=True)
                except HTTPError as exc:
                    if exc.code not in (500, 503):
                        raise
                    matches = []  # Fresh submission can precede asynchronous item indexing.
                if matches:
                    if self._items is not None:
                        self._items.update((item['id'], item) for item in matches)
                    return matches
                if attempt + 1 < attempts:
                    time.sleep(min(0.5, max(0, self._deadline - time.monotonic())))
        except TimeoutError:
            pass
        finally:
            self._deadline = previous
        return []

    def await_bookmark(self, url, folder_id, item_ids=(), timeout=30):
        previous = self._deadline
        self._deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < self._deadline:
                try:
                    matches = ([item for key in item_ids if (item := self.info(key))]
                               if item_ids else self.find_bookmarks(url, folder_id))
                except HTTPError as exc:
                    if exc.code not in (500, 503):
                        raise
                    matches = []
                if matches:
                    return matches
                time.sleep(min(0.5, max(0, self._deadline - time.monotonic())))
        except TimeoutError:
            pass
        finally:
            self._deadline = previous
        return []

    def verify_item(self, item_id, expected, folder_id, bookmark=False):
        """Accept observable metadata, not merely a successful submission response."""
        self.check_library()
        current = self.request('/api/item/info?' + urlencode({'id': item_id}))
        wanted_url = expected.get('website', expected.get('url'))
        if (not current or current.get('isDeleted') or
                normalized_eagle_name(current.get('name', '')) !=
                normalized_eagle_name(expected['name']) or
                current.get('url', '').rstrip('/') != wanted_url.rstrip('/') or
                current.get('annotation', '') != expected.get('annotation', '') or
                not set(expected.get('tags', [])).issubset(current.get('tags', [])) or
                folder_id not in current.get('folders', [])):
            raise RuntimeError('Eagle 回读验收失败：名称、来源、中文简介、标签或项目文件夹不一致；停止本批')
        if bookmark:
            if current.get('ext') != 'url':
                raise RuntimeError('Eagle 外链格式验收失败')
            self.verify_bookmark_thumbnail(item_id)
        return current

    def verify_bookmark_thumbnail(self, item_id, attempts=40):
        """Wait for Eagle's asynchronous bookmark poster, refreshing once if needed."""
        from PIL import Image

        refreshed = False
        last_error = None
        for attempt in range(attempts):
            try:
                raw_path = self.request('/api/item/thumbnail?' + urlencode({'id': item_id}))
                # Eagle may percent-encode spaces and non-ASCII library paths.
                thumbnail = Path(unquote(str(raw_path)))
                if thumbnail.is_file():
                    with Image.open(thumbnail) as image:
                        image.verify()
                    return thumbnail
                last_error = FileNotFoundError(str(thumbnail))
            except Exception as exc:
                last_error = exc
            if not refreshed and attempt >= 2:
                # This official local API call regenerates the thumbnail; it does
                # not create another bookmark or modify library files directly.
                self.post('/api/item/refreshThumbnail', {'id': item_id})
                refreshed = True
            if attempt + 1 < attempts:
                time.sleep(0.5)
        raise RuntimeError('Eagle 书签预览尚未核实；保留 uncertain，停止本批，不能算视频文件') from last_error

    def set_cover(self, folder_id, item_id, project_url):
        """Eagle 4.0 local script interface; no library files or UI are edited."""
        def find_folder():
            self.check_library()
            def walk(rows):
                for row in rows:
                    if row['id'] == folder_id:
                        return row
                    found = walk(row.get('children', []))
                    if found:
                        return found
            return walk(self.request('/api/folder/list'))
        folder = find_folder()
        if not folder:
            raise RuntimeError('项目文件夹不存在')
        if folder.get('coverId') == item_id:
            return
        # Unlike folder/update, this version-specific route can set the cover via
        # Eagle's own in-memory model and saveFolder method.
        values = json.dumps({'library': str(self.expected), 'folder': folder_id,
                             'item': item_id, 'projectUrl': project_url}, ensure_ascii=False)
        script = """(() => {
 const input = INPUT;
 const scope = angular.element('body').scope();
 if (decodeURI(scope.rootDir) !== input.library) throw new Error('Wrong library');
 const folder = scope.folderMappings[input.folder];
 const item = scope.itemMappings[input.item];
 if (!folder || !item || !item.folders.includes(input.folder) ||
     item.url !== input.projectUrl) throw new Error('Cover ownership mismatch');
 folder.coverId = item.id;
 scope.saveFolder(); scope.updateSidebarList(); scope.$applyAsync();
})();""".replace('INPUT', values)
        self.post('/api/script/inject', {'script': script})
        for attempt in range(6):
            folder = find_folder()
            if folder and folder.get('coverId') == item_id:
                return
            time.sleep(0.5 * (attempt + 1))
        raise RuntimeError('文件夹封面尚未核实；Eagle 版本可能不支持此接口')
