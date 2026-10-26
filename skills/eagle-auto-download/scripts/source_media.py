"""Bounded public requests and receipt-backed original downloads for JSON adapters."""
import hashlib
import json
import os
import ssl
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import certifi
from media import atomic_write, sha256


def check_url(url, hosts):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in hosts or
            parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443)):
        raise ValueError('来源链接不在已确认的 HTTPS 域名范围')
    return url


def request(url, hosts, limit=20 * 1024 * 1024):
    check_url(url, hosts)
    req = Request(quote(url, safe=":/?=&%+"), headers={'User-Agent': 'Mozilla/5.0 EagleAutoDownload/1.6'})
    with urlopen(req, timeout=40, context=ssl.create_default_context(cafile=certifi.where())) as response:
        check_url(response.url, hosts)
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('来源响应超过大小限制')
    return raw


def download(record, directory, hosts, max_bytes=512 * 1024 * 1024):
    url = check_url(record['asset_url'], hosts)
    if urlsplit(url).query:
        raise ValueError('原件来源不能保存临时签名参数')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    suffix = Path(urlsplit(url).path).suffix.lower()
    target = directory / (hashlib.sha256(url.encode()).hexdigest() + suffix)
    receipt = target.with_suffix(suffix + '.receipt.json')
    if target.is_file() and not target.is_symlink() and receipt.is_file():
        try:
            old = json.loads(receipt.read_text())
            if old['url'] == url and old['bytes'] == target.stat().st_size and old['sha256'] == sha256(target):
                return {**record, 'local_path': str(target.resolve())}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    for attempt in range(3):
        fd, temporary = tempfile.mkstemp(dir=directory, prefix='.partial-')
        try:
            req = Request(quote(url, safe=":/%"), headers={'Referer': record['project_url'],
                                                           'User-Agent': 'EagleAutoDownload/1.6'})
            with os.fdopen(fd, 'wb') as stream, urlopen(req, timeout=90,
                    context=ssl.create_default_context(cafile=certifi.where())) as response:
                check_url(response.url, hosts)
                if response.headers.get('Content-Type', '').lower().startswith(('text/', 'application/json')):
                    raise ValueError('媒体请求返回网页或 JSON')
                size = 0
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    if size > max_bytes:
                        raise ValueError('媒体超过单文件大小限制')
                    stream.write(block)
                length = response.headers.get('Content-Length')
                if not size or length is not None and size != int(length):
                    raise ValueError('媒体下载不完整')
            os.replace(temporary, target)
            atomic_write(receipt, json.dumps({'url': url, 'bytes': size, 'sha256': sha256(target)}).encode())
            return {**record, 'local_path': str(target.resolve())}
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 ** attempt)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def collect_records(records, directory, hosts, metadata=None, delay=1, workers=2):
    metadata = metadata or {}
    for record in records:
        for key, field in (('project_display_name', 'display_name'), ('project_summary', 'summary'),
                           ('category_tags', 'category_tags'), ('industry_tags', 'industry_tags')):
            if field in metadata:
                record[key] = metadata[field]
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / 'manifest.json'
    done, failed = [], []
    def one(record):
        if record['media_type'] == 'bookmark':
            return record  # A pending external video, never a downloaded file.
        if delay:
            time.sleep(delay)
        return download(record, directory / 'originals', hosts)
    with ThreadPoolExecutor(max_workers=max(1, min(2, workers))) as pool:
        jobs = {pool.submit(one, r): r for r in records}
        for future in as_completed(jobs):
            row = jobs[future]
            try:
                done.append(future.result())
            except Exception as exc:
                failed.append({'asset_url': row['asset_url'], 'error': str(exc)})
            done.sort(key=lambda r: r['page_order'])
            atomic_write(manifest, json.dumps(done, ensure_ascii=False, indent=2).encode())
    external = sum(r['media_type'] == 'bookmark' for r in done)
    return {'manifest': str(manifest), 'downloaded': len(done) - external,
            'queued_external_videos': external, 'records': len(done), 'failed': failed}
