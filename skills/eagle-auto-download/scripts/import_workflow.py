"""Resumable import: file identity and project metadata are verified separately."""
import json
import time
from collections import defaultdict

import ledger
from eagle import Eagle as EagleAPI, normalized_eagle_name
from media import sha256
from preflight import external_mode, pause_path, selected, validate_project_scope, validate_queue


def import_assets(config, execute=False, project_urls=None, retry_uncertain=False, asset_ids=None):
    from collector import Eagle, state, payload, bookmark_payload, display_name, verify_accepted
    report = {'imported': 0, 'reconciled': 0, 'reused': 0, 'pending': 0,
              'metadata_review': [], 'failed': [], 'plan': [], 'covers': 0}
    started = time.monotonic()
    with state(config) as db:
        rows = [(key, json.loads(record), status, eagle_id) for key, record, status, eagle_id in
                db.execute('SELECT id,record,status,eagle_id FROM assets')]
        urls = {url.rstrip('/') for url in project_urls or []}
        ids = set(asset_ids or [])
        if ids - {key for key, _, _, _ in rows}:
            raise ValueError('asset-id 不存在于状态库')
        if ids and urls and any(key in ids and record['project_url'].rstrip('/') not in urls
                               for key, record, _, _ in rows):
            raise ValueError('asset-id 与指定项目范围不一致')
        if ids and not urls:
            urls = {record['project_url'].rstrip('/') for key, record, _, _ in rows if key in ids}
        targets = ids or None
        evidence = [(key, record, status) for key, record, status, _ in rows]
        report['preflight'] = (validate_project_scope(config, evidence, urls, target_ids=targets)
                               if urls else validate_queue(config, evidence))
        report['summary'] = ledger.summary(rows, external_mode(config))
        videos = {(record['project_url'].rstrip('/'), source.rstrip('/'))
                  for _, record, _, _ in rows if record.get('media_type') == 'video'
                  and record.get('external_provider') for source in
                  [record['asset_url']] + record.get('sources', [])}
        if execute and not report['preflight']['ready']:
            report['blocked'] = True
            return report
        queue = [(key, record, status, eagle_id) for key, record, status, eagle_id in rows
                 if status not in ledger.ACCEPTED and selected(record, external_mode(config)) and
                 (not urls or record['project_url'].rstrip('/') in urls) and (not ids or key in ids)
                 and not (external_mode(config) == 'files' and record.get('media_type') == 'bookmark'
                     and (record['project_url'].rstrip('/'), record['asset_url'].rstrip('/')) in videos)]
        known_files = defaultdict(set)
        for _, record, status, eagle_id in rows:
            if record.get('media_type') != 'bookmark' and eagle_id:
                known_files[record['sha256']].add(eagle_id)
        if not execute:
            for key, record, status, _ in queue:
                value = bookmark_payload(record) if record.get('media_type') == 'bookmark' else payload(record)
                report['plan'].append({**value, 'asset_id': key,
                    'action': 'reconcile_only' if status in ledger.PROTECTED else 'reconcile_or_create',
                    'known_content_ids': sorted(known_files.get(record.get('sha256'), []))})
            return report
        eagle = Eagle(config['eagle'])
        eagle.check_library()
        eagle.write_guard = lambda: not pause_path(config).exists()
        library = ledger.library_key(config)
        bound = db.execute("SELECT value FROM settings WHERE key='eagle_library'").fetchone()
        if bound and bound[0] != library:
            raise RuntimeError('此队列绑定了其他 Eagle 资源库；请使用独立队列，不能复用旧 Eagle ID')
        db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('eagle_library',?)", (library,))
        db.commit()
        policy = config['collection'].get('shared_metadata_policy', 'review')
        for digest, item_id in db.execute('SELECT sha256,eagle_id FROM files WHERE library=?', (library,)):
            known_files[digest].add(item_id)
        for key, record, status, old_id in queue:
            if pause_path(config).exists():
                report['paused'] = True
                break
            attempt_id = None
            try:
                bookmark = record.get('media_type') == 'bookmark'
                expected = bookmark_payload(record) if bookmark else payload(record)
                expected['annotation'] = record.get('project_summary') or ''
                own_ids = ledger.saved_ids(db, key, library)
                if old_id:
                    own_ids.append(old_id)
                own_ids = list(dict.fromkeys(own_ids))
                if retry_uncertain and status == 'uncertain' and not bookmark:
                    eagle.items(refresh=True)  # A retry requires a fresh full-library miss.
                matches = []
                for item_id in own_ids:
                    item = eagle.info(item_id)
                    if not item:
                        continue
                    if bookmark:
                        if item.get('ext') != 'url' or item.get('url', '').rstrip('/') != record['asset_url'].rstrip('/'):
                            raise RuntimeError('已保存 Eagle ID 与书签来源不一致，停止恢复')
                    else:
                        item = eagle.verify_file(item_id, record['sha256'])
                    matches.append(item)
                if not matches:
                    if bookmark:
                        matches = eagle.find_bookmarks(record['asset_url'])
                    else:
                        if sha256(record['local_path']) != record['sha256']:
                            raise RuntimeError('待导入文件已变化，请重新准备')
                        matches = eagle.find_content_matches(record['sha256'], record['local_path'],
                                                             known_files[record['sha256']])
                        if not matches:
                            matches = eagle.find_matching_items(expected, 'collector-id:' + key)
                            matches = [eagle.verify_file(item['id'], record['sha256'])
                                       for item in matches]
                if len(matches) > 1:
                    raise RuntimeError('对账发现多个匹配条目，需明确保留的 Eagle ID')
                if not matches and status in ledger.PROTECTED and not (
                        status == 'uncertain' and retry_uncertain):
                    report['pending'] += 1
                    continue
                names = [record['studio'], display_name(record)]
                if config['eagle'].get('root_folder'):
                    names.insert(0, config['eagle']['root_folder'])
                folder_id = eagle.folder(names)
                eagle.ensure_folder_description(folder_id, record.get('project_summary') or '')
                created = False
                if not matches:
                    thumbnail = None
                    if bookmark:
                        from saffron import vimeo_thumbnail
                        thumbnail = vimeo_thumbnail(record['asset_url'], config['_paths']['downloads'] / 'vimeo-thumbnails')
                    attempt_id = ledger.begin_attempt(db, key, library, retry=status == 'uncertain')
                    endpoint = '/api/item/addBookmark' if bookmark else '/api/item/addFromPaths'
                    request = ({**bookmark_payload(record, thumbnail), 'folderId': folder_id} if bookmark else
                               {'items': [payload(record)], 'folderId': folder_id})
                    response = eagle.post(endpoint, request)
                    returned_ids = EagleAPI.response_ids(response)
                    ledger.finish_attempt(db, attempt_id, 'response_received', returned_ids)
                    if bookmark:
                        matches = eagle.await_bookmark(record['asset_url'], folder_id, item_ids=returned_ids)
                    else:
                        matches = eagle.await_item(expected, 'collector-id:' + key,
                                                   folder_id=folder_id, item_ids=returned_ids)
                        if not matches:
                            # Duplicate handling can reuse an older name/URL even on success-only APIs.
                            eagle.items(refresh=True)
                            matches = eagle.find_content_matches(record['sha256'], record['local_path'])
                    if len(matches) != 1:
                        raise RuntimeError('提交已记录，尚未核实条目；保留 uncertain，下次先对账')
                    created = True
                item = matches[0]
                if bookmark:
                    item = eagle.info(item['id'])
                    if not item or item.get('ext') != 'url' or item.get('url', '').rstrip('/') != record['asset_url'].rstrip('/'):
                        raise RuntimeError('书签格式或来源不一致')
                same_identity = (normalized_eagle_name(item.get('name', '')) ==
                                 normalized_eagle_name(expected['name']) and
                                 item.get('url', '').rstrip('/') == expected.get('website', expected.get('url', '')).rstrip('/'))
                shared = not same_identity and not bookmark
                # A bookmark returned by a duplicate-aware API may also be shared.
                if bookmark:
                    shared = not same_identity or (
                        bool(set(item.get('folders', [])) - {folder_id}) and
                        item.get('annotation', '') != expected['annotation'])
                # Ordinary new files get one combined hash/metadata readback below.
                # Verify content before attaching a shared or misplaced item.
                if not bookmark and (shared or folder_id not in item.get('folders', [])):
                    item = eagle.verify_file(item['id'], record['sha256'])
                if folder_id not in item.get('folders', []):
                    eagle.post('/api/item/update', {'id': item['id'],
                               'folders': list(dict.fromkeys(item.get('folders', []) + [folder_id]))})
                if shared:
                    current = eagle.info(item['id'])
                    if not current or folder_id not in current.get('folders', []):
                        raise RuntimeError('共享素材的项目关联尚未核实')
                    if bookmark:
                        eagle.verify_bookmark_thumbnail(item['id'])
                    elif record.get('is_project_logo') and policy == 'preserve_existing':
                        eagle.set_cover(folder_id, item['id'], current['url'])
                        report['covers'] += 1
                    result = 'reused' if policy == 'preserve_existing' else 'needs_metadata_review'
                    ledger.link(db, key, record, library, item['id'], folder_id, result, 'preserve_existing')
                    if result == 'needs_metadata_review':
                        report['metadata_review'].append({'asset_id': key, 'eagle_id': item['id'],
                            'project_url': record['project_url'], 'existing_name': current.get('name'),
                            'existing_url': current.get('url'), 'policy': policy})
                    else:
                        report['reused'] += 1
                else:
                    if bookmark:
                        eagle.post('/api/item/update', {'id': item['id'], 'name': expected['name'],
                                   'annotation': expected['annotation'], 'tags': expected['tags']})
                    verify_accepted(eagle, record, item['id'], folder_id, file_item=item)
                    if record.get('is_project_logo'):
                        eagle.set_cover(folder_id, item['id'], record['project_url'])
                        report['covers'] += 1
                    ledger.link(db, key, record, library, item['id'], folder_id, 'imported', 'project_metadata')
                    report['imported' if created else 'reconciled'] += 1
                if not bookmark:
                    known_files[record['sha256']].add(item['id'])
                ledger.finish_attempt(db, attempt_id, 'needs_metadata_review' if shared and
                                      policy == 'review' else 'accepted', [item['id']])
            except Exception as exc:
                db.execute('UPDATE assets SET error=? WHERE id=?', (str(exc), key))
                db.commit()
                ledger.finish_attempt(db, attempt_id, 'uncertain', error=str(exc))
                report['failed'].append({'asset_id': key, 'attempt_id': attempt_id, 'error': str(exc)})
                break
        current = [(key, json.loads(record), status, eagle_id) for key, record, status, eagle_id in
                   db.execute('SELECT id,record,status,eagle_id FROM assets')]
        report['summary'] = ledger.summary(current, external_mode(config))
        report['scope_summary'] = ledger.summary([row for row in current if
            not urls or row[1]['project_url'].rstrip('/') in urls],
            external_mode(config))
        report['status'] = ('failed' if report['failed'] else 'needs_metadata_review' if
            report['metadata_review'] else 'complete' if report['scope_summary']['complete'] else 'pending')
        current_records = {key: record for key, record, _, _ in current}
        for key, replacement in report['summary']['represented_by_video'].items():
            record = current_records[key]
            db.execute('INSERT INTO project_links(asset_id,project_url,status,replaced_by_asset_id) '
                       "VALUES (?,?,'represented_by_video',?) ON CONFLICT(asset_id) DO UPDATE SET "
                       "status='represented_by_video',replaced_by_asset_id=excluded.replaced_by_asset_id",
                       (key, record['project_url'], replacement))
        db.commit()
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['api_metrics'] = getattr(eagle, 'metrics', {})
    return report
