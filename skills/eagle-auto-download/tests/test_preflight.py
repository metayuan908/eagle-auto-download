import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import WORK
from collector import import_assets, state
from eagle import Eagle
from media import sha256
from preflight import pause_path, validate_queue


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        path = self.directory / 'source.png'
        path.write_bytes(b'previously validated image')
        self.record = {'studio': '2x4', 'project_url': 'https://2x4.org/work/mathaf/',
                       'asset_url': 'https://2x4.org/wp-content/uploads/main.png',
                       'project_title': 'Mathaf', 'project_display_name': 'Mathaf 阿拉伯现代艺术博物馆',
                       'page_order': 1, 'role': 'original', 'media_type': 'image',
                       'local_path': str(path), 'sha256': sha256(path),
                       'category_tags': ['文化'], 'industry_tags': ['博物馆'],
                       'description_status': 'present', 'source_description': 'Museum visual identity.',
                       'project_summary': '博物馆视觉形象设计。', 'media_scope_reviewed': True,
                       'adapter_revision': '2x4-main-v2'}
        self.config = {'eagle': {'base_url': 'http://localhost:41595',
                                 'expected_library_path': str(self.directory / 'test.library')},
                       'collection': {}, '_paths': {'state_db': self.directory / 'state.sqlite'}}

    def check(self, record=None, **kwargs):
        return validate_queue(self.config, [('a', record or self.record, 'pending_import')], **kwargs)

    def codes(self, record):
        return {x['code'] for x in self.check(record)['issues']}

    def test_valid_metadata_and_unchanged_files_pass(self):
        self.assertTrue(self.check()['ready'])

    def test_english_name_and_missing_translation_block_whole_batch_before_api(self):
        bad = {**self.record, 'project_display_name': 'Mathaf · Arab Museum', 'project_summary': None}
        with state(self.config) as db:
            for key, record in [('valid', self.record), ('invalid', bad)]:
                db.execute('INSERT INTO assets VALUES (?, ?, ?, NULL, NULL)',
                           (key, json.dumps(record), 'pending_import'))
            db.commit()
        with patch('collector.Eagle', side_effect=AssertionError('Must reject before any Eagle call')):
            report = import_assets(self.config, True)
        self.assertTrue(report['blocked'])
        self.assertEqual(report['imported'], 0)
        self.assertIn('bilingual_name', {x['code'] for x in report['preflight']['issues']})
        self.assertIn('chinese_summary', {x['code'] for x in report['preflight']['issues']})

    def test_missing_summary_is_allowed_only_for_reviewed_source_without_description(self):
        no_summary = {**self.record, 'project_summary': None}
        self.assertIn('chinese_summary', self.codes(no_summary))
        no_summary.update(description_status='absent', source_description='')
        self.assertTrue(self.check(no_summary)['ready'])
        no_summary['source_description'] = 'Original text still exists.'
        self.assertIn('description_conflict', self.codes(no_summary))

    def test_unreviewed_metadata_and_old_adapter_are_blocked(self):
        old = {**self.record, 'media_scope_reviewed': False,
               'adapter_revision': None, 'description_status': None, 'category_tags': []}
        self.assertEqual(self.codes(old), {'media_scope_unreviewed', 'stale_adapter',
                                         'description_unreviewed', 'category_tags'})

    def test_explicit_unknown_category_does_not_force_invented_tag(self):
        record = {**self.record, 'category_tags': [], 'category_status': 'unknown'}
        self.assertTrue(self.check(record)['ready'])

    def test_changed_file_fails_before_submission(self):
        Path(self.record['local_path']).write_bytes(b'changed')
        self.assertIn('file_integrity', self.codes(self.record))

    def test_technical_provenance_cannot_be_annotation(self):
        record = {**self.record, 'project_summary': '中文简介\ncollector-id:technical'}
        self.assertIn('annotation_provenance', self.codes(record))

    def test_pause_survives_new_import_invocation_and_prevents_api(self):
        pause_path(self.config).write_text('paused\n')
        self.assertTrue(self.check()['paused'])
        with patch('collector.Eagle', side_effect=AssertionError('Must stay paused')):
            report = import_assets(self.config, True)
        self.assertTrue(report['blocked'])
        self.assertTrue(report['preflight']['paused'])
        self.assertTrue(pause_path(self.config).exists())

    def test_mid_operation_pause_blocks_next_eagle_write(self):
        eagle = Eagle(self.config['eagle'])
        eagle.write_guard = lambda: not pause_path(self.config).exists()
        pause_path(self.config).write_text('paused\n')
        with patch.object(eagle, 'check_library', side_effect=AssertionError('No network')):
            with self.assertRaisesRegex(RuntimeError, '导入已暂停'):
                eagle.post('/api/item/update', {'id': 'a', 'annotation': '中文'})

    def bookmark(self):
        return {**self.record, 'media_type': 'bookmark', 'external_provider': 'vimeo',
                'asset_url': 'https://vimeo.com/123', 'local_path': None}

    def test_external_mode_must_be_explicit_and_bookmark_cannot_substitute_for_file(self):
        bookmark = self.bookmark()
        self.assertIn('external_mode', self.codes(bookmark))
        self.config['collection']['external_video_mode'] = 'files'
        result = self.check(bookmark)
        self.assertIn('external_file_missing', {x['code'] for x in result['issues']})
        self.assertEqual(result['counts']['excluded_by_mode'], 1)
        self.config['collection']['external_video_mode'] = 'bookmarks'
        self.assertTrue(self.check(bookmark)['ready'])

    def test_file_mode_requires_downloaded_video_and_excludes_bookmark_from_import(self):
        bookmark = self.bookmark()
        video = {**self.record, 'media_type': 'video', 'external_provider': 'vimeo',
                 'asset_url': bookmark['asset_url']}
        self.config['collection']['external_video_mode'] = 'files'
        result = validate_queue(self.config, [('b', bookmark, 'pending_import'),
                                              ('v', video, 'pending_import')])
        self.assertTrue(result['ready'])
        self.assertEqual(result['counts']['video'], 1)
        self.assertEqual(result['counts']['excluded_by_mode'], 1)

    def test_files_mode_includes_only_explicit_download_failure_fallbacks(self):
        bookmark = self.bookmark()
        self.config['collection'] = {'external_video_mode': 'files',
                                     'download_failure_fallback': 'bookmarks'}
        fallback = {**bookmark, 'download_fallback': {
            'reason': 'download_disabled', 'detail': 'Vimeo 作者未开放下载', 'attempts': 1}}
        result = validate_queue(self.config, [('b', fallback, 'pending_import')])
        self.assertTrue(result['ready'])
        self.assertEqual(result['counts']['bookmark'], 1)
        self.config['collection']['download_failure_fallback'] = None
        result = validate_queue(self.config, [('b', fallback, 'pending_import')])
        self.assertFalse(result['ready'])
        self.assertIn('external_fallback_disabled', {x['code'] for x in result['issues']})

    def test_different_projects_with_same_folder_name_are_blocked(self):
        other = {**self.record, 'project_url': 'https://2x4.org/work/other/'}
        result = validate_queue(self.config, [('a', self.record, 'pending_import'),
                                              ('b', other, 'pending_import')])
        self.assertFalse(result['ready'])
        self.assertIn('folder_collision', {x['code'] for x in result['issues']})

    def test_bad_sample_stops_before_second_submission(self):
        with state(self.config) as db:
            for key in ('first', 'second'):
                db.execute('INSERT INTO assets VALUES (?, ?, ?, NULL, NULL)',
                           (key, json.dumps(self.record), 'pending_import'))
            db.commit()

        class FakeEagle:
            submissions = 0
            def __init__(self, config): pass
            def check_library(self): return {}
            def folder(self, names): return 'folder'
            def ensure_folder_description(self, *args): pass
            def find_content_matches(self, *args): return []
            def find_matching_items(self, *args): return []
            def post(self, endpoint, payload):
                if endpoint == '/api/item/addFromPaths':
                    self.submissions += 1
            def await_item(self, *args, **kwargs): return [{'id': 'created'}]
            def verify_file(self, *args): return {'id': 'created', 'folders': ['folder']}
            def verify_item(self, *args, **kwargs): raise RuntimeError('回读验收失败')

        with patch('collector.Eagle', FakeEagle):
            report = import_assets(self.config, True)
        self.assertEqual(report['imported'], 0)
        self.assertEqual(len(report['failed']), 1)
        with state(self.config) as db:
            self.assertEqual(db.execute('SELECT status FROM assets ORDER BY id').fetchall(),
                             [('uncertain',), ('pending_import',)])

    def test_metadata_readback_rejects_missing_description_or_wrong_folder(self):
        eagle = Eagle(self.config['eagle'])
        expected = {'name': 'Mathaf 博物馆 001', 'website': self.record['project_url'],
                    'annotation': '中文简介。', 'tags': ['文化', '博物馆']}
        current = {'name': expected['name'], 'url': expected['website'],
                   'annotation': expected['annotation'], 'tags': expected['tags'], 'folders': ['folder']}
        with patch.object(eagle, 'check_library'), patch.object(eagle, 'request', return_value=current):
            self.assertEqual(eagle.verify_item('id', expected, 'folder'), current)
        for changed in ({'annotation': ''}, {'folders': ['different']}, {'tags': ['文化']}):
            with patch.object(eagle, 'check_library'), patch.object(eagle, 'request', return_value={**current, **changed}):
                with self.assertRaisesRegex(RuntimeError, '回读验收失败'):
                    eagle.verify_item('id', expected, 'folder')

    def test_metadata_readback_accepts_eagle_stripped_reserved_punctuation(self):
        eagle = Eagle(self.config['eagle'])
        expected = {'name': 'The T*******s T***s: 项目 013/video',
                    'website': self.record['project_url'], 'annotation': '中文简介。',
                    'tags': ['文化']}
        current = {'name': 'The Ts Ts 项目 013video', 'url': expected['website'],
                   'annotation': expected['annotation'], 'tags': expected['tags'],
                   'folders': ['folder']}
        with patch.object(eagle, 'check_library'), patch.object(eagle, 'request', return_value=current):
            self.assertEqual(eagle.verify_item('id', expected, 'folder'), current)

    def test_await_item_polls_only_target_folder_until_created(self):
        eagle = Eagle(self.config['eagle'])
        expected = {'name': 'Example 001', 'website': self.record['project_url']}
        seen = {'count': 0, 'folder': None}

        def find(row, folder, marker):
            seen['count'] += 1
            seen['folder'] = folder
            return [{'id': 'created', 'name': row['name'], 'url': row['website']}] if seen['count'] == 3 else []

        with patch.object(eagle, 'find_matching_items_in_folder', side_effect=find), \
             patch('eagle.time.sleep'):
            result = eagle.await_item(expected, 'marker', folder_id='project-folder')
        self.assertEqual(result[0]['id'], 'created')
        self.assertEqual(seen, {'count': 3, 'folder': 'project-folder'})

    def test_bookmark_without_decodable_thumbnail_is_not_accepted(self):
        eagle = Eagle(self.config['eagle'])
        expected = {'name': 'Mathaf 博物馆 Vimeo 001', 'url': 'https://vimeo.com/123',
                    'annotation': '中文简介。', 'tags': ['文化']}
        current = {**expected, 'ext': 'url', 'folders': ['folder']}
        def request(endpoint):
            return current if '/info?' in endpoint else str(self.directory / 'missing-preview.jpg')
        with patch.object(eagle, 'check_library'), patch.object(eagle, 'request', side_effect=request), \
             patch.object(eagle, 'post'), patch('eagle.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, '预览尚未核实'):
                eagle.verify_item('id', expected, 'folder', bookmark=True)

    def test_bookmark_preview_waits_refreshes_and_decodes_api_path(self):
        eagle = Eagle(self.config['eagle'])
        expected = {'name': 'Mathaf 博物馆 Vimeo 001', 'url': 'https://vimeo.com/123',
                    'annotation': '中文简介。', 'tags': ['文化']}
        current = {**expected, 'ext': 'url', 'folders': ['folder']}
        thumbnail = self.directory / '预览 image.jpg'
        calls = {'thumb': 0, 'refresh': 0}

        def request(endpoint):
            if '/info?' in endpoint:
                return current
            calls['thumb'] += 1
            if calls['thumb'] <= 3:
                return str(self.directory / 'not-ready.jpg')
            from urllib.parse import quote
            return quote(str(thumbnail), safe='/')

        def post(endpoint, payload):
            self.assertEqual(endpoint, '/api/item/refreshThumbnail')
            self.assertEqual(payload, {'id': 'id'})
            calls['refresh'] += 1

        from PIL import Image
        Image.new('RGB', (2, 2), 'white').save(thumbnail)
        with patch.object(eagle, 'check_library'), patch.object(eagle, 'request', side_effect=request), \
             patch.object(eagle, 'post', side_effect=post), patch('eagle.time.sleep'):
            self.assertEqual(eagle.verify_item('id', expected, 'folder', bookmark=True), current)
        self.assertEqual(calls['refresh'], 1)


if __name__ == '__main__':
    unittest.main()
