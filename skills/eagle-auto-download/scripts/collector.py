#!/usr/bin/env python3
"""Interbrand project collection, validated media preparation and Eagle import."""
import argparse
import fcntl
import hashlib
import json
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from eagle import Eagle
from media import atomic_write, prepare_image, prepare_video, sha256
import ledger
from preflight import (validate_queue, validate_project_scope,
                       pause_path as import_pause_path, selected, external_mode)


def load_config(path):
    path = Path(path).resolve(strict=True)
    config = json.loads(path.read_text())
    if config.get('schema_version') != 1:
        raise ValueError('不支持的配置版本')
    options = config['collection']['webp_to_jpeg']
    if options.get('enabled') is not True or options.get('preserve_dimensions') is not True:
        raise ValueError('当前版本要求启用 WebP 转 JPG 并保留尺寸')
    if options.get('keep_downloaded_original') is not True:
        raise ValueError('当前版本要求保留本地原文件')
    if type(options['quality']) is not int or not 1 <= options['quality'] <= 100:
        raise ValueError('JPEG quality 必须为 1–100 的整数')
    if options['subsampling'] not in (0, 1, 2):
        raise ValueError('无效的 JPEG subsampling')
    if options['animated_policy'] != 'preserve_original_and_generate_jpeg_preview':
        raise ValueError('不支持此动画处理策略')
    # Relative storage paths are anchored to the workspace config's parent directory.
    base = path.parent.parent
    config['_paths'] = {key: (base / value).resolve() for key, value in config['paths'].items()}
    for metadata in config.get('project_metadata', {}).values():
        if not isinstance(metadata.get('category_tags', []), list) or any(
                not isinstance(tag, str) for tag in metadata.get('category_tags', [])):
            raise ValueError('category_tags 必须是字符串列表')
        if not isinstance(metadata.get('industry_tags', []), list) or any(
                not isinstance(tag, str) for tag in metadata.get('industry_tags', [])):
            raise ValueError('industry_tags 必须是字符串列表')
        for key in ('display_name', 'summary', 'description_source'):
            if metadata.get(key) is not None and not isinstance(metadata[key], str):
                raise ValueError(key + ' 必须是字符串或 null')
        if metadata.get('description_status') not in (None, 'present', 'absent'):
            raise ValueError('description_status 必须是 present 或 absent')
        if 'media_scope_reviewed' in metadata and type(metadata['media_scope_reviewed']) is not bool:
            raise ValueError('media_scope_reviewed 必须是布尔值')
    if config['collection'].get('external_video_mode') not in (None, 'files', 'bookmarks', 'both'):
        raise ValueError('external_video_mode 必须是 files、bookmarks 或 both')
    if config['collection'].get('download_failure_fallback') not in (None, 'bookmarks'):
        raise ValueError('download_failure_fallback 必须为 null 或 bookmarks')
    if config['collection'].get('shared_metadata_policy', 'review') not in ('review', 'preserve_existing'):
        raise ValueError('shared_metadata_policy 必须是 review 或 preserve_existing')
    fallback_after = config['collection'].get('download_failure_fallback_after', 3)
    if type(fallback_after) is not int or fallback_after < 1:
        raise ValueError('download_failure_fallback_after 必须是正整数')
    return config


@contextmanager
def state(config):
    path = config['_paths']['state_db']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix('.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('已有采集器实例运行') from exc
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS assets (id TEXT PRIMARY KEY, '
                       'record TEXT NOT NULL, status TEXT NOT NULL, eagle_id TEXT, error TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS projects (url TEXT PRIMARY KEY, '
                       'status TEXT NOT NULL, error TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.commit()
            ledger.initialize(db, path)
            yield db


def validate_source(record):
    required = ('studio', 'project_title', 'project_url', 'asset_url')
    if record.get('media_type') != 'bookmark':
        required += ('local_path',)
    for key in required:
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError('素材记录缺少 ' + key)
    for key in ('project_url', 'asset_url'):
        url = urlsplit(record[key])
        if url.scheme not in ('http', 'https') or not url.hostname:
            raise ValueError(key + ' 必须是 HTTP(S) 地址')
        if url.username or url.password or url.query or url.fragment:
            raise ValueError(key + ' 需使用不含凭证、签名或查询参数的来源链接')
    if not isinstance(record.get('tags', []), list) or any(
            not isinstance(tag, str) for tag in record.get('tags', [])):
        raise ValueError('tags 必须是字符串列表')


def prepare(config, manifest):
    manifest = Path(manifest).resolve(strict=True)
    sources = json.loads(manifest.read_text())
    if not isinstance(sources, list) or not sources:
        raise ValueError('素材清单必须是非空 JSON 数组')
    report = {'prepared': 0, 'existing': 0, 'failed': [], 'assets': []}
    with state(config) as db:
        for index, source in enumerate(sources, 1):
            try:
                meta = config.get('project_metadata', {}).get(source.get('project_url'), {})
                source = {**source, 'project_display_name': meta.get('display_name', source.get('project_display_name')),
                          'category_tags': meta.get('category_tags', source.get('category_tags', [])),
                          'industry_tags': meta.get('industry_tags', source.get('industry_tags', [])),
                          'project_summary': meta.get('summary', source.get('project_summary'))}
                for key in ('description_status', 'media_scope_reviewed', 'category_status'):
                    if key in meta:
                        source[key] = meta[key]
                # Preserve extracted source text; a review cannot erase evidence that exists.
                if not source.get('source_description') and meta.get('description_source'):
                    source['source_description'] = meta['description_source']
                validate_source(source)
                if source.get('media_type') == 'bookmark':
                    outputs = [{'local_path': None, 'sha256': hashlib.sha256(
                        source['asset_url'].encode()).hexdigest(), 'role': 'external-link',
                        'media_type': 'bookmark'}]
                else:
                    local = (manifest.parent / source['local_path']).resolve(strict=True)
                    outputs = (prepare_video(local) if source.get('media_type') == 'video' else
                               prepare_image(local, config['_paths']['downloads'] / 'prepared',
                                             config['collection']['webp_to_jpeg']))
                for output in outputs:
                    # Project-scoped dedup retains sources across projects.
                    key = json.dumps([source['studio'], source['project_url'], output['sha256']],
                                     ensure_ascii=False).encode()
                    asset_id = hashlib.sha256(key).hexdigest()
                    record = {**source, **output, 'asset_id': asset_id,
                              'prepared_at': datetime.now(timezone.utc).isoformat(),
                              'page_order': source.get('page_order', index),
                              'sources': [source['asset_url']]}
                    row = db.execute('SELECT record,status FROM assets WHERE id=?', (asset_id,)).fetchone()
                    if row:
                        old = json.loads(row[0])
                        if row[1] in ledger.PROTECTED:
                            changed = [key for key in ('project_display_name', 'project_summary', 'category_tags',
                                                      'industry_tags', 'adapter_revision', 'source_description')
                                       if old.get(key) != record.get(key)]
                            if changed:
                                raise RuntimeError('已提交素材的元数据发生变化，需先按旧记录对账并明确迁移，不能直接覆盖：' + ','.join(changed))
                            record = dict(old)
                        # Page order is part of the Eagle name; retain it when content repeats.
                        record['page_order'] = old['page_order']
                        record['sources'] = sorted(set(old['sources'] + [source['asset_url']]))
                        db.execute('UPDATE assets SET record=? WHERE id=?',
                                   (json.dumps(record, ensure_ascii=False), asset_id))
                        report['existing'] += 1
                    else:
                        db.execute('INSERT INTO assets VALUES (?, ?, ?, NULL, NULL)',
                                   (asset_id, json.dumps(record, ensure_ascii=False), 'pending_import'))
                        report['prepared'] += 1
                    db.commit()
                    report['assets'].append(asset_id)
            except Exception as exc:
                report['failed'].append({'index': index, 'error': str(exc)})
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    atomic_write(config['_paths']['reports'] / (stamp + '-prepare.json'),
                 json.dumps(report, ensure_ascii=False, indent=2).encode())
    return report


def site_url(config):
    sites = config.get('sites')
    if not sites or len(sites) != 1:
        raise ValueError('每份配置需指定一个适配站点')
    site = sites[0]
    supported = {
        'interbrand': 'https://interbrand.com/work/',
        'wolffolins': 'https://www.wolffolins.com/work',
        'saffron': 'https://saffron-consultants.com/work',
        'twoxfour': 'https://2x4.org/filter/all/',
        'landor': 'https://landor.com/en/our-work/',
    }
    if site.get('id') not in supported or site.get('list_url', '').rstrip('/') != supported[site['id']].rstrip('/'):
        raise ValueError('站点地址与当前适配器不匹配')
    return site['list_url']


def site_adapter(config):
    site_url(config)  # Validate both the identifier and the configured URL.
    return __import__(config['sites'][0]['id'])


def existing_project_urls(config, project_urls):
    """Match discovered cases against the configured studio folder in Eagle."""
    eagle = Eagle(config['eagle'])
    eagle.check_library()
    site = config['sites'][0]
    folders = eagle.request('/api/folder/list')
    studio = next((folder for folder in folders if folder.get('name') == site['studio']), None)

    def flatten(rows):
        for row in rows:
            yield row
            yield from flatten(row.get('children', []))

    tree = list(flatten([studio])) if studio else []
    ids = {folder['id'] for folder in tree}
    names = [''.join(ch for ch in folder.get('name', '').casefold() if ch.isalnum())
             for folder in studio.get('children', [])] if studio else []
    item_urls = {item.get('url', '').rstrip('/') for item in eagle.items()
                 if not item.get('isDeleted') and (not ids or ids.intersection(item.get('folders', [])))}

    with state(config) as db:
        statuses = dict(db.execute('SELECT url,status FROM projects'))
    existing = set()
    metadata = config.get('project_metadata', {})
    for url in project_urls:
        slug = url.rstrip('/').split('/')[-1]
        norm_slug = ''.join(ch for ch in slug.casefold() if ch.isalnum())
        display = metadata.get(url, {}).get('display_name', '')
        display = display.encode('ascii', 'ignore').decode().strip()
        norm_display = ''.join(ch for ch in display.casefold() if ch.isalnum())
        matched = (url.rstrip('/') in item_urls or
                   any(name.startswith(norm_slug) or
                       (norm_display and name.startswith(norm_display)) for name in names))
        if matched and statuses.get(url) not in ('failed', 'pending'):
            existing.add(url)
    return existing


def scan(config, baseline=False):
    adapter = site_adapter(config)
    projects = adapter.discover(adapter.fetch_html(site_url(config)))
    with state(config) as db:
        initialized = db.execute("SELECT value FROM settings WHERE key='baseline_initialized'").fetchone() is not None
        if baseline:
            for url in projects:
                db.execute('INSERT OR IGNORE INTO projects VALUES (?, ?, NULL)', (url, 'baseline'))
            db.execute("INSERT OR REPLACE INTO settings VALUES ('baseline_initialized', 'true')")
            db.commit()
            return {'baseline_projects': len(projects), 'new_projects': [], 'baseline_initialized': True}
        known = {url for url, status in db.execute('SELECT url,status FROM projects')
                 if status in ('baseline', 'collected')}
    known.update(existing_project_urls(config, projects))
    return {'discovered_projects': len(projects), 'new_projects': [p for p in projects if p not in known],
            'baseline_initialized': initialized}


def collect_project(config, url):
    adapter = site_adapter(config)
    if config['sites'][0]['id'] == 'interbrand':
        url = url.rstrip('/') + '/'
    else:
        url = url.rstrip('/')
    project_id = hashlib.sha256(url.encode()).hexdigest()[:16]
    collect_args = (url, config['_paths']['downloads'] / project_id,
                    config['collection']['request_delay_seconds'],
                    config.get('project_metadata', {}).get(url, {}))
    if config['sites'][0]['id'] in ('twoxfour', 'landor'):
        result = adapter.collect(*collect_args,
                                 max_concurrent_downloads=config['collection'].get('max_concurrent_downloads', 2))
    else:
        result = adapter.collect(*collect_args)
    if result['downloaded']:
        result['preparation'] = prepare(config, result['manifest'])
        result['failed'] += result['preparation']['failed']
    with state(config) as db:
        db.execute('INSERT OR REPLACE INTO projects VALUES (?, ?, ?)',
                   (url, 'failed' if result['failed'] else 'collected',
                    json.dumps(result['failed'], ensure_ascii=False) if result['failed'] else None))
        db.commit()
    return result


def run(config, execute=False):
    result = scan(config)
    if not result['baseline_initialized']:
        mode = config['first_run']['mode']
        if mode not in ('baseline_only', 'download_history'):
            raise ValueError('缺少首次运行策略 first_run.mode')
        if mode == 'baseline_only':
            result = scan(config, baseline=True)
        else:
            with state(config) as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES ('baseline_initialized', 'true')")
                db.commit()
    result['collections'], result['failed'] = [], []
    for url in result['new_projects']:
        try:
            collected = collect_project(config, url)
            result['collections'].append({'project_url': url, **collected})
            result['failed'] += collected['failed']
        except Exception as exc:
            result['failed'].append({'project_url': url, 'error': str(exc)})
    if execute:
        try:
            result['import'] = import_assets(config, True)
            result['failed'] += result['import']['failed']
            result['blocked'] = result['import'].get('blocked', False)
            result['metadata_review'] = result['import'].get('metadata_review', [])
        except Exception as exc:
            result['failed'].append({'stage': 'import', 'error': str(exc)})
    return result


def display_name(record):
    return record.get('project_display_name') or record['project_title']


def payload(record):
    return {'path': record['local_path'],
            'name': f"{display_name(record)} {record['page_order']:03d} {record['role']}",
            'website': record['project_url'], 'annotation': record.get('project_summary') or '',
            'tags': list(dict.fromkeys(record.get('category_tags', []) + record.get('industry_tags', [])))}


def bookmark_payload(record, thumbnail=None):
    payload = {'url': record['asset_url'],
               'name': f"{display_name(record)} Vimeo {record['page_order']:03d}",
               'tags': list(dict.fromkeys(record.get('category_tags', []) + record.get('industry_tags', [])))}
    if thumbnail:
        payload['base64'] = thumbnail
    return payload


def verify_accepted(eagle, record, item_id, folder_id):
    bookmark = record.get('media_type') == 'bookmark'
    expected = bookmark_payload(record) if bookmark else payload(record)
    expected['annotation'] = record.get('project_summary') or ''
    eagle.verify_item(item_id, expected, folder_id, bookmark=bookmark)


def import_assets(config, execute=False, project_urls=None, retry_uncertain=False, asset_ids=None):
    from import_workflow import import_assets as perform_import
    return perform_import(config, execute, project_urls, retry_uncertain, asset_ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('check-config')
    sub.add_parser('inspect-eagle')
    sub.add_parser('discover')
    sub.add_parser('scan')
    sub.add_parser('baseline')
    run_parser = sub.add_parser('run')
    run_parser.add_argument('--execute', action='store_true')
    collect_parser = sub.add_parser('collect')
    collect_parser.add_argument('--project-url', required=True)
    inspect_parser = sub.add_parser('inspect-project')
    inspect_parser.add_argument('--project-url', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('--manifest', required=True)
    imp = sub.add_parser('import')
    imp.add_argument('--execute', action='store_true', help='实际导入；默认只显示计划')
    imp.add_argument('--project-url', action='append',
                     help='仅计划或导入指定项目；可重复使用，仍执行项目范围预检')
    imp.add_argument('--retry-uncertain', action='store_true',
                     help='用户明确要求重试时使用；仅在全库对账确认无匹配条目后每项重提一次')
    imp.add_argument('--asset-id', action='append',
                     help='仅处理指定状态记录；可重复使用，适合逐条恢复 uncertain 素材')
    status_parser = sub.add_parser('status')
    status_parser.add_argument('--details', action='store_true', help='显示逐条原始状态，默认输出完成摘要')
    flight = sub.add_parser('preflight', help='离线检查命名、简介、素材归属、外链模式和文件完整性')
    flight.add_argument('--include-imported', action='store_true', help='同时审计历史已导入记录，不修改 Eagle')
    flight.add_argument('--project-url', action='append',
                        help='仅检查指定项目；可重复使用，目录重名仍按全库检查')
    sub.add_parser('pause-import', help='持久暂停导入；正在执行的请求返回后停止后续写入')
    sub.add_parser('resume-import', help='仅解除导入暂停；不立即导入')
    args = parser.parse_args()
    operation_lock = None
    try:
        config = load_config(args.config)
        if args.command == 'pause-import':
            atomic_write(import_pause_path(config), b'paused\n')
            print(json.dumps({'import_paused': True, 'note': '需确认旧进程退出或操作锁释放'}, ensure_ascii=False))
            return 0
        if args.command in ('prepare', 'collect', 'import', 'baseline', 'run', 'resume-import'):
            lock_path = config['_paths']['state_db'].with_suffix('.operation.lock')
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            operation_lock = lock_path.open('a')
            try:
                fcntl.flock(operation_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('已有采集或导入任务运行') from exc
        if args.command == 'resume-import':
            import_pause_path(config).unlink(missing_ok=True)
            result = {'import_paused': False, 'note': '尚未开始导入'}
        elif args.command == 'preflight':
            with state(config) as db:
                rows = [(key, json.loads(record), status) for key, record, status in
                        db.execute('SELECT id,record,status FROM assets')]
            if args.project_url:
                result = validate_project_scope(config, rows, args.project_url, include_imported=args.include_imported)
                report_name = 'preflight-scope.json'
            else:
                result = validate_queue(config, rows, include_imported=args.include_imported)
                report_name = 'preflight-all.json' if args.include_imported else 'preflight.json'
            atomic_write(config['_paths']['reports'] / report_name,
                         json.dumps(result, ensure_ascii=False, indent=2).encode())
        elif args.command == 'check-config':
            Eagle(config['eagle'])
            result = {'local_pipeline': 'ready', 'website_collection': config['sites'][0]['id'] + '_adapter',
                      'schedule': 'not_installed'}
        elif args.command == 'inspect-eagle':
            info = Eagle(config['eagle']).check_library()
            result = {'library': info['library'], 'version': info.get('applicationVersion')}
        elif args.command == 'prepare':
            result = prepare(config, args.manifest)
        elif args.command == 'discover':
            adapter = site_adapter(config)
            result = {'projects': adapter.discover(adapter.fetch_html(site_url(config)))}
        elif args.command in ('scan', 'baseline'):
            result = scan(config, baseline=args.command == 'baseline')
        elif args.command == 'run':
            result = run(config, args.execute)
        elif args.command == 'collect':
            url = args.project_url.rstrip('/')
            if config['sites'][0]['id'] == 'interbrand':
                url += '/'
            result = collect_project(config, url)
        elif args.command == 'inspect-project':
            url = args.project_url.rstrip('/')
            if config['sites'][0]['id'] == 'interbrand':
                url += '/'
            adapter = site_adapter(config)
            html = adapter.fetch_html(url)
            if config['sites'][0]['id'] == 'interbrand':
                from interbrand import page_data, walk
                from html import unescape
                import re
                project = page_data(html)['caseStudiesByAcfField']
                content = project['caseStudyContent']
                texts = []
                for group in ('challengeGroup', 'whatWeDidGroup', 'outcomeStatementsGroup'):
                    for node in walk(content.get(group)):
                        for value in node.values():
                            if isinstance(value, str) and '<p' in value:
                                text = unescape(re.sub('<[^>]+>', ' ', value)).strip()
                                if text not in texts:
                                    texts.append(text)
                result = {'project_title': project['title'], 'project_url': url,
                          'description_source': '\n'.join(texts)[:6000],
                          'media': adapter.extract(html, url)}
            elif config['sites'][0]['id'] == 'saffron':
                from saffron import description
                media = adapter.extract(html, url)
                result = {'project_title': media[0]['project_title'],
                          'project_url': url, 'description_source': description(html),
                          'media': media}
            else:
                media = adapter.extract(html, url)
                result = {'project_title': media[0]['project_title'],
                          'project_url': url, 'description_source': media[0].get('source_description'),
                          'media': media}
        elif args.command == 'import':
            result = import_assets(config, args.execute, args.project_url, args.retry_uncertain,
                                   args.asset_id)
        else:
            with state(config) as db:
                if args.details:
                    result = [dict(zip(('asset_id', 'status', 'eagle_id', 'error'), row)) for row in
                              db.execute('SELECT id,status,eagle_id,error FROM assets')]
                else:
                    result = ledger.summary([(key, json.loads(record), status, eagle_id) for
                        key, record, status, eagle_id in db.execute('SELECT id,record,status,eagle_id FROM assets')],
                        external_mode(config))
        if args.command in ('collect', 'discover', 'import', 'scan', 'baseline', 'run'):
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            atomic_write(config['_paths']['reports'] / (stamp + '-' + args.command + '.json'),
                         json.dumps(result, ensure_ascii=False, indent=2).encode())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and (result.get('failed') or result.get('blocked') or result.get('metadata_review') or
                                                 (args.command == 'preflight' and not result['ready'])) else 0
    except Exception as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if operation_lock:
            operation_lock.close()


if __name__ == '__main__':
    sys.exit(main())
