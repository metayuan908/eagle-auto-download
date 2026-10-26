"""Exercise real client/ledger code against duplicate-aware API response fixtures."""
import copy
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError
from unittest.mock import patch

from PIL import Image
from _support import CONFIG, WORK
from collector import import_assets, payload, state
from eagle import Eagle, normalized_eagle_name
from ledger import summary
from media import sha256
from preflight import validate_project_scope, validate_queue
import external_video


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = self.root / 'test.library'
        self.library.mkdir()
        self.config = json.loads(CONFIG.read_text())
        self.config['eagle']['expected_library_path'] = str(self.library)
        self.config['_paths'] = {'state_db': self.root/'state.sqlite',
            'downloads': self.root/'downloads', 'reports': self.root/'reports'}
        self.file = self.root/'image.jpg'
        Image.new('RGB', (18, 20), 'red').save(self.file)
        self.record = {'studio': 'Studio', 'project_title': 'Target',
            'project_display_name': 'Target 目标项目', 'project_url': 'https://studio.example/target',
            'asset_url': 'https://studio.example/image.jpg', 'local_path': str(self.file),
            'sha256': sha256(self.file), 'media_type': 'image', 'role': 'original', 'page_order': 1,
            'project_summary': '目标项目简介。', 'source_description': 'Source introduction.',
            'description_status': 'present', 'category_tags': ['文化'], 'industry_tags': ['展览'],
            'media_scope_reviewed': True}
        self.store, self.folders, self.calls = {}, [], []
        self.response_mode = 'ids'
        self.fail_before = self.fail_after = False
        owner = self
        class FixtureEagle(Eagle):
            def request(self, endpoint, payload=None):
                return owner.dispatch(endpoint, payload)
        self.client = FixtureEagle

    def enqueue(self, key='target', record=None, status='pending_import', eagle_id=None):
        with state(self.config) as db:
            db.execute('INSERT INTO assets VALUES (?,?,?,?,NULL)',
                       (key, json.dumps(record or self.record), status, eagle_id))
            db.commit()

    def seed(self, item_id='existing', name='Original 原项目 001 original', url='https://studio.example/original'):
        folder = self.library/'images'/(item_id+'.info')
        folder.mkdir(parents=True)
        shutil.copyfile(self.file, folder/(name+'.jpg'))
        self.store[item_id] = {'id': item_id, 'name': name, 'ext': 'jpg',
            'url': url, 'annotation': '原项目简介。', 'tags': ['旧标签'], 'folders': ['old-folder'],
            'size': self.file.stat().st_size, 'isDeleted': False}
        return self.store[item_id]

    def dispatch(self, endpoint, data):
        self.calls.append((endpoint, copy.deepcopy(data)))
        url = urlsplit(endpoint)
        query = parse_qs(url.query)
        if url.path == '/api/library/info':
            return {'library': {'path': str(self.library)}}
        if url.path == '/api/folder/list':
            return copy.deepcopy(self.folders)
        if url.path == '/api/item/list':
            rows = [copy.deepcopy(x) for x in self.store.values() if not x.get('isDeleted')]
            if query.get('folders'):
                rows = [x for x in rows if query['folders'][0] in x['folders']]
            offset = int(query.get('offset', ['0'])[0])
            return rows[offset:offset+int(query.get('limit', ['200'])[0])]
        if url.path == '/api/item/info':
            return copy.deepcopy(self.store.get(query['id'][0]))
        if url.path == '/api/folder/create':
            folder = {'id': 'folder-'+str(len(self.folders)), 'name': data['folderName'], 'children': []}
            def find(rows, item_id):
                for row in rows:
                    if row['id'] == item_id:
                        return row
                    found = find(row.get('children', []), item_id)
                    if found:
                        return found
            if data.get('parent'):
                find(self.folders, data['parent'])['children'].append(folder)
            else:
                self.folders.append(folder)
            return copy.deepcopy(folder)
        if url.path == '/api/folder/update':
            def update(rows):
                for row in rows:
                    if row['id'] == data['folderId']:
                        row['description'] = data['newDescription']
                    update(row.get('children', []))
            update(self.folders)
            return None
        if url.path == '/api/item/update':
            self.store[data['id']].update({k: copy.deepcopy(v) for k, v in data.items() if k != 'id'})
            return None
        if url.path == '/api/item/addFromPaths':
            if self.fail_before:
                raise TimeoutError('Request outcome unknown')
            incoming = data['items'][0]
            item_id = 'created-'+str(len(self.store))
            name = ''.join(ch for ch in incoming['name'] if ch not in '\\/:*?"<>|')
            directory = self.library/'images'/(item_id+'.info')
            directory.mkdir(parents=True)
            shutil.copyfile(incoming['path'], directory/(name+'.jpg'))
            self.store[item_id] = {**incoming, 'id': item_id, 'name': name, 'url': incoming['website'],
                'ext': 'jpg', 'folders': [data['folderId']], 'size': Path(incoming['path']).stat().st_size}
            if self.fail_after:
                raise TimeoutError('Server applied request; response lost')
            return [{'id': item_id}] if self.response_mode == 'ids' else None
        raise AssertionError(endpoint)

    def run_import(self, **kwargs):
        with patch('collector.Eagle', self.client):
            return import_assets(self.config, True, **kwargs)

    def creates(self):
        return [row for row in self.calls if row[0] == '/api/item/addFromPaths']

    def test_returned_id_is_persisted_and_metadata_and_bytes_are_verified(self):
        self.enqueue()
        result = self.run_import()
        self.assertEqual(result['imported'], 1)
        self.assertTrue(result['summary']['complete'])
        with state(self.config) as db:
            result, ids = db.execute('SELECT result,response_ids FROM attempts').fetchone()
            self.assertEqual(result, 'accepted')
            self.assertEqual(json.loads(ids), ['created-0'])
            self.assertEqual(db.execute('SELECT count(*) FROM files').fetchone()[0], 1)
        self.assertEqual(sum(url.startswith('/api/item/list?') for url, _ in self.calls), 1)

    def test_new_file_combines_final_content_and_metadata_readback(self):
        self.enqueue()
        result = self.run_import()
        self.assertEqual(result['imported'], 1)
        self.assertEqual(result['failed'], [])
        # Poll visibility, then one fresh content + metadata verification.
        self.assertEqual(sum(url.startswith('/api/item/info?') for url, _ in self.calls), 2)
        self.assertLess(len(self.calls), 28)  # v1.6.0 required 28 requests for this case.

    def test_combined_readback_rejects_changed_original_without_accepting_attempt(self):
        self.enqueue()
        original = self.dispatch
        reads = 0
        def dispatch(endpoint, data):
            nonlocal reads
            result = original(endpoint, data)
            if endpoint.startswith('/api/item/info?'):
                reads += 1
                if reads == 1:
                    file = self.library/'images'/(result['id']+'.info')/(result['name']+'.jpg')
                    file.write_bytes(b'changed after submission')
            return result
        self.dispatch = dispatch
        report = self.run_import()
        self.assertEqual(report['imported'], 0)
        self.assertEqual(len(report['failed']), 1)
        self.assertIn('哈希', report['failed'][0]['error'])
        with state(self.config) as db:
            self.assertEqual(db.execute('SELECT status FROM assets').fetchone()[0], 'uncertain')

    def test_combined_readback_checks_library_after_final_item_response(self):
        self.enqueue()
        original = self.dispatch
        reads = 0
        switched = False
        def dispatch(endpoint, data):
            nonlocal reads, switched
            result = original(endpoint, data)
            if endpoint.startswith('/api/item/info?'):
                reads += 1
                if reads == 2:
                    switched = True
            if endpoint == '/api/library/info' and switched:
                return {'library': {'path': str(self.root/'other.library')}}
            return result
        self.dispatch = dispatch
        report = self.run_import()
        self.assertEqual(report['imported'], 0)
        self.assertEqual(len(report['failed']), 1)
        self.assertIn('库不一致', report['failed'][0]['error'])
        self.assertEqual(len(self.creates()), 1)

    def test_combined_readback_refreshes_metadata_after_hashing(self):
        self.enqueue()
        def changed(path):
            value = sha256(path)
            for item in self.store.values():
                item['annotation'] = 'changed during hash verification'
            return value
        with patch('eagle.sha256', side_effect=changed):
            report = self.run_import()
        self.assertEqual(report['imported'], 0)
        self.assertEqual(len(report['failed']), 1)
        self.assertIn('回读验收失败', report['failed'][0]['error'])

    def test_combined_readback_rejects_wrong_returned_identity(self):
        item = self.seed(name='Target 目标项目 001 original', url=self.record['project_url'])
        expected = payload(self.record)
        client = self.client(self.config['eagle'])
        with patch.object(client, 'request', return_value={**item, 'id': 'other'}), \
             patch.object(client, 'check_library'):
            with self.assertRaisesRegex(RuntimeError, '其他条目'):
                client.verify_item('existing', expected, 'old-folder', digest=self.record['sha256'])

    def test_legacy_success_only_response_still_reconciles(self):
        self.response_mode = 'legacy'
        self.enqueue()
        self.assertEqual(self.run_import()['imported'], 1)

    def test_same_bytes_in_other_project_preserve_metadata_and_need_review(self):
        old = copy.deepcopy(self.seed())
        self.enqueue(status='uncertain')
        result = self.run_import()
        self.assertEqual(len(result['metadata_review']), 1)
        self.assertFalse(self.creates())
        for key in ('name', 'url', 'annotation', 'tags'):
            self.assertEqual(self.store['existing'][key], old[key])
        self.assertEqual(len(self.store['existing']['folders']), 2)
        self.assertFalse(result['summary']['complete'])
        with state(self.config) as db:
            self.assertEqual(db.execute('SELECT status,eagle_id FROM assets').fetchone(),
                             ('needs_metadata_review', 'existing'))
        self.config['collection']['shared_metadata_policy'] = 'preserve_existing'
        resumed = self.run_import()
        self.assertEqual(resumed['reused'], 1)
        self.assertTrue(resumed['summary']['complete'])
        self.assertFalse(self.creates())

    def test_saved_id_cannot_bind_different_bytes(self):
        self.seed()
        (self.library/'images/existing.info/Original 原项目 001 original.jpg').write_bytes(b'different')
        self.enqueue(status='uncertain', eagle_id='existing')
        result = self.run_import(retry_uncertain=True)
        self.assertEqual(len(result['failed']), 1)
        self.assertFalse(self.creates())

    def test_duplicate_content_with_two_eagle_ids_is_not_silently_accepted(self):
        self.seed()
        self.seed('another')
        self.enqueue()
        self.assertEqual(len(self.run_import()['failed']), 1)
        self.assertFalse(self.creates())

    def test_response_lost_recovers_without_second_submission(self):
        self.enqueue()
        self.fail_after = True
        self.assertEqual(len(self.run_import()['failed']), 1)
        self.fail_after = False
        self.assertEqual(self.run_import()['reconciled'], 1)
        self.assertEqual(len(self.creates()), 1)

    def test_explicit_retry_is_durably_limited_to_one_resubmission(self):
        self.enqueue(status='uncertain')
        self.fail_before = True
        self.assertEqual(len(self.run_import(retry_uncertain=True)['failed']), 1)
        self.assertEqual(len(self.run_import(retry_uncertain=True)['failed']), 1)
        self.assertEqual(len(self.creates()), 1)
        with state(self.config) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE is_retry=1").fetchone()[0], 1)

    def test_pause_prevents_all_eagle_writes(self):
        self.enqueue()
        self.config['_paths']['state_db'].with_suffix('.import-paused').write_text('paused')
        result = self.run_import()
        self.assertTrue(result['blocked'])
        self.assertFalse(self.calls)

    def test_asset_scope_ignores_unrelated_bad_metadata_and_rejects_unknown_ids(self):
        self.enqueue()
        bad = {**self.record, 'project_url': 'https://studio.example/unreviewed',
               'project_display_name': 'Bad', 'description_status': None}
        self.enqueue('bad', bad)
        self.assertEqual(self.run_import(asset_ids=['target'])['imported'], 1)
        with self.assertRaises(ValueError):
            self.run_import(asset_ids=['missing'])

    def test_project_preflight_keeps_imported_video_as_evidence(self):
        self.config['collection']['external_video_mode'] = 'files'
        video = {**self.record, 'media_type': 'video', 'external_provider': 'vimeo',
                 'asset_url': 'https://vimeo.com/12345678'}
        bookmark = {**video, 'media_type': 'bookmark', 'local_path': None}
        rows = [('video', video, 'imported'), ('link', bookmark, 'uncertain')]
        self.assertTrue(validate_project_scope(self.config, rows, [video['project_url']], verify_files=False)['ready'])
        result = summary([(key, record, status, 'existing' if key=='video' else None)
                          for key, record, status in rows], 'files')
        self.assertTrue(result['complete'])
        self.assertEqual(result['represented_by_video'], {'link': 'video'})
        result = summary([('video', video, 'uncertain', None), ('link', bookmark, 'pending_import', None)], 'files')
        self.assertFalse(result['complete'])
        self.assertEqual(result['counts']['awaiting_video'], 1)

    def test_download_failure_event_replay_does_not_consume_retries(self):
        record = {**self.record, 'media_type': 'bookmark', 'external_provider': 'vimeo'}
        self.enqueue(record=record)
        self.config['collection']['download_failure_fallback'] = 'bookmarks'
        for _ in range(3):
            external_video.record_download_failure(self.config, record, 'download_failed', 'reset', 'same-event')
        with state(self.config) as db:
            saved = json.loads(db.execute('SELECT record FROM assets').fetchone()[0])
            self.assertEqual(saved['download_attempts'], 1)
            self.assertNotIn('download_fallback', saved)

    def test_schema_upgrade_backs_up_original_database_once(self):
        path = self.config['_paths']['state_db']
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE assets(id TEXT PRIMARY KEY,record TEXT,status TEXT,eagle_id TEXT,error TEXT)')
            db.execute('INSERT INTO assets VALUES (?,?,?,NULL,NULL)', ('old', json.dumps(self.record), 'uncertain'))
        with state(self.config):
            pass
        with state(self.config):
            pass
        backups = list((self.root/'state-backups').glob('*.bak'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(db.execute('SELECT id,status FROM assets').fetchone(), ('old', 'uncertain'))
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='files'").fetchall())

    def test_folder_tree_and_full_library_are_loaded_once_per_batch(self):
        self.enqueue()
        image = self.root/'blue.jpg'
        Image.new('RGB', (33, 24), 'blue').save(image)
        self.enqueue('second', {**self.record, 'sha256': sha256(image), 'local_path': str(image), 'page_order': 2})
        self.assertEqual(self.run_import()['imported'], 2)
        self.assertEqual(sum(url == '/api/folder/list' for url, _ in self.calls), 2)
        full_scans = [url for url, _ in self.calls if url.startswith('/api/item/list?') and 'folders=' not in url]
        self.assertEqual(len(full_scans), 1)
        descriptions = [url for url, _ in self.calls if url == '/api/folder/update']
        self.assertEqual(len(descriptions), 1)

    def test_polling_has_real_deadline_and_restores_request_timeout(self):
        eagle = Eagle(self.config['eagle'])
        with patch('eagle.time.monotonic', side_effect=[0, 31]), \
             patch.object(eagle, 'info', side_effect=AssertionError('Deadline already elapsed')):
            self.assertEqual(eagle.await_item({}, item_ids=['item']), [])
        self.assertIsNone(eagle._deadline)

    def test_explicit_recheck_includes_fallbacks_without_erasing_them(self):
        record = {**self.record, 'media_type': 'bookmark', 'external_provider': 'vimeo',
                  'download_fallback': {'reason': 'download_disabled'}}
        self.enqueue(record=record)
        self.assertFalse(external_video.candidates(self.config))
        self.config['collection']['recheck_download_fallbacks'] = True
        self.assertEqual(len(external_video.candidates(self.config)), 1)
        with state(self.config) as db:
            self.assertIn('download_fallback', json.loads(db.execute('SELECT record FROM assets').fetchone()[0]))

    def test_async_bookmark_uses_returned_id_until_visible(self):
        eagle = Eagle(self.config['eagle'])
        with patch.object(eagle, 'info', side_effect=[None, {'id': 'bookmark'}]) as info, \
             patch('eagle.time.sleep'):
            found = eagle.await_bookmark('https://vimeo.com/12345678', 'folder', item_ids=['bookmark'])
        self.assertEqual(found, [{'id': 'bookmark'}])
        self.assertEqual(info.call_count, 2)
        self.assertIsNone(eagle._deadline)

    def test_original_symlink_cannot_be_used_as_content_evidence(self):
        self.seed()
        original = self.library/'images/existing.info/Original 原项目 001 original.jpg'
        original.unlink()
        original.symlink_to(self.file)
        self.enqueue(status='uncertain', eagle_id='existing')
        self.assertEqual(len(self.run_import()['failed']), 1)
        self.assertFalse(self.creates())

    def test_folder_description_must_be_read_back_before_file_submission(self):
        original_dispatch = self.dispatch
        def reject_description(endpoint, data):
            if endpoint == '/api/folder/update':
                return None
            return original_dispatch(endpoint, data)
        self.dispatch = reject_description
        self.enqueue()
        self.assertEqual(len(self.run_import()['failed']), 1)
        self.assertFalse(self.creates())

    def test_bookmark_mode_does_not_wait_for_unselected_video_records(self):
        video = {**self.record, 'media_type': 'video', 'external_provider': 'vimeo'}
        result = summary([('video', video, 'pending_import', None)], 'bookmarks')
        self.assertEqual(result['counts']['excluded_by_mode'], 1)
        self.assertTrue(result['complete'])

    def test_rechecking_imported_fallback_preserves_eagle_identity(self):
        record = {**self.record, 'media_type': 'bookmark', 'external_provider': 'vimeo',
                  'download_fallback': {'reason': 'download_disabled'}}
        self.enqueue(record=record, status='imported', eagle_id='bookmark')
        external_video.record_download_failure(self.config, record, 'download_disabled', 'disabled', 'new-event')
        with state(self.config) as db:
            self.assertEqual(db.execute('SELECT status,eagle_id FROM assets').fetchone(), ('imported', 'bookmark'))
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)

    def test_full_queue_detects_folder_collision_with_imported_project(self):
        old = {**self.record, 'project_url': 'https://studio.example/old'}
        result = validate_queue(self.config, [('old', old, 'imported'),
            ('target', self.record, 'pending_import')], verify_files=False)
        self.assertFalse(result['ready'])
        self.assertIn('folder_collision', {issue['code'] for issue in result['issues']})

    def test_pagination_supports_both_page_number_and_item_offset(self):
        rows = [{'id': str(n)} for n in range(450)]
        for mode in ('pages', 'items'):
            with self.subTest(mode=mode):
                eagle = Eagle(self.config['eagle'])
                def request(endpoint):
                    query = parse_qs(urlsplit(endpoint).query)
                    offset = int(query['offset'][0])
                    start = offset * 200 if mode == 'pages' else offset
                    return rows[start:start+200]
                with patch.object(eagle, 'check_library', return_value={}), patch.object(eagle, 'request', side_effect=request):
                    self.assertEqual([r['id'] for r in eagle.items()], [str(n) for n in range(450)])

    def test_ignored_pagination_is_rejected_instead_of_repeating_or_missing_items(self):
        eagle = Eagle(self.config['eagle'])
        with patch.object(eagle, 'check_library', return_value={}), \
             patch.object(eagle, 'request', return_value=[{'id':str(n)} for n in range(200)]):
            with self.assertRaisesRegex(RuntimeError,'分页'):
                eagle.items()

    def test_fresh_returned_id_can_transiently_return_500_without_resubmitting(self):
        eagle = Eagle(self.config['eagle'])
        with patch.object(eagle,'info',side_effect=[HTTPError('local',500,'indexing',{},None),{'id':'new'}]) as info, \
             patch('eagle.time.sleep'):
            self.assertEqual(eagle.await_item({},item_ids=['new']),[{'id':'new'}])
        self.assertEqual(info.call_count,2)


if __name__ == '__main__':
    unittest.main()
