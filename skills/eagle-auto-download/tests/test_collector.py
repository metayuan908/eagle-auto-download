import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from _support import WORK, CONFIG
from collector import import_assets, load_config, payload, prepare, scan, state
from eagle import Eagle
from interbrand import discover, extract
from media import prepare_image, sha256


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK)
        self.directory = Path(self.temp.name)
        self.config = json.loads(CONFIG.read_text())
        self.config['eagle']['expected_library_path'] = str(self.directory / 'test.library')
        self.config['_paths'] = {'state_db': self.directory / 'state.sqlite',
                                 'downloads': self.directory / 'downloads',
                                 'reports': self.directory / 'reports'}
        self.options = self.config['collection']['webp_to_jpeg']
        self.config['project_metadata'] = {'https://studio.example/project/': {
            'display_name': 'Project 项目', 'category_tags': ['企业'],
            'description_status': 'absent', 'media_scope_reviewed': True}}

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self):
        source = self.directory / 'source.webp'
        Image.new('RGBA', (40, 30), (255, 0, 0, 0)).save(source, 'WEBP', lossless=True)
        record = {'studio': 'Studio', 'project_title': 'Project',
                  'project_url': 'https://studio.example/project/',
                  'asset_url': 'https://studio.example/image.webp', 'local_path': str(source)}
        manifest = self.directory / 'manifest.json'
        manifest.write_text(json.dumps([record]))
        return source, manifest

    def test_transparency_dimensions_true_jpeg_and_original_preserved(self):
        source, _ = self.manifest()
        before = sha256(source)
        records = prepare_image(source, self.directory / 'out', self.options)
        self.assertEqual(sha256(source), before)
        with Image.open(records[0]['local_path']) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.size, (40, 30))
            self.assertEqual(image.getpixel((20, 20)), (255, 255, 255))

    def test_animated_webp_preserves_every_frame(self):
        source = self.directory / 'animated.webp'
        frames = [Image.new('RGB', (24, 24), color) for color in ('red', 'blue')]
        frames[0].save(source, 'WEBP', save_all=True, append_images=frames[1:], duration=100, loop=0)
        records = prepare_image(source, self.directory / 'out', self.options)
        self.assertEqual([r['role'] for r in records], ['animation', 'jpeg_preview'])
        with Image.open(records[0]['local_path']) as image:
            self.assertEqual(image.n_frames, 2)

    def test_corrupt_input_never_queued(self):
        source, manifest = self.manifest()
        source.write_bytes(b'not an image')
        result = prepare(self.config, manifest)
        self.assertEqual(result['prepared'], 0)
        self.assertEqual(len(result['failed']), 1)

    def test_project_dedup_and_multiple_sources(self):
        _, manifest = self.manifest()
        self.assertEqual(prepare(self.config, manifest)['prepared'], 1)
        records = json.loads(manifest.read_text())
        records[0]['asset_url'] = 'https://studio.example/alternate.webp'
        manifest.write_text(json.dumps(records))
        self.assertEqual(prepare(self.config, manifest)['existing'], 1)
        with state(self.config) as db:
            record = json.loads(db.execute('SELECT record FROM assets').fetchone()[0])
            self.assertEqual(len(record['sources']), 2)
        records[0]['project_url'] = 'https://studio.example/other-project/'
        manifest.write_text(json.dumps(records))
        self.assertEqual(prepare(self.config, manifest)['prepared'], 1)

    def test_import_plan_does_not_connect_to_eagle(self):
        _, manifest = self.manifest()
        prepare(self.config, manifest)
        with patch('collector.Eagle', side_effect=AssertionError('No API call allowed')):
            self.assertEqual(len(import_assets(self.config)['plan']), 1)

    def test_vimeo_external_links_are_queued_and_imported_as_bookmarks(self):
        self.config['collection']['external_video_mode'] = 'bookmarks'
        manifest = self.directory / 'bookmarks.json'
        manifest.write_text(json.dumps([{'studio': 'Saffron', 'project_title': 'Blue Car Rental',
            'project_display_name': 'Blue Car Rental 冰岛租车',
            'project_url': 'https://saffron-consultants.com/work/blue_car_rental',
            'asset_url': 'https://vimeo.com/12345678', 'media_type': 'bookmark',
            'local_path': None, 'page_order': 1, 'category_tags': ['企业'],
            'industry_tags': ['租车'], 'project_summary': '中文简介。'}]))
        self.config['project_metadata']['https://saffron-consultants.com/work/blue_car_rental'] = {
            'description_status': 'present', 'description_source': 'The project introduction.',
            'media_scope_reviewed': True}
        self.assertEqual(prepare(self.config, manifest)['prepared'], 1)
        with state(self.config) as db:
            record = json.loads(db.execute('SELECT record FROM assets').fetchone()[0])
            self.assertIsNone(record['local_path'])
        with patch('collector.Eagle', side_effect=AssertionError('Dry run must not contact Eagle')):
            plan = import_assets(self.config, False)
            self.assertEqual(plan['plan'][0]['url'], 'https://vimeo.com/12345678')
            self.assertFalse(plan['failed'])
        class FakeEagle:
            items = []
            posts = []
            def __init__(self, config): pass
            def check_library(self): return {}
            def folder(self, names): return 'project-folder'
            def ensure_folder_description(self, *args): pass
            def info(self, item_id): return next(x for x in self.items if x['id'] == item_id)
            def find_bookmarks(self, url, folder_id=None):
                return [x for x in self.items if x['url'] == url and (not folder_id or folder_id in x['folders'])]
            def await_bookmark(self, url, folder_id, **kwargs): return self.find_bookmarks(url, folder_id)
            def post(self, endpoint, payload):
                self.posts.append((endpoint, payload))
                if endpoint == '/api/item/addBookmark':
                    self.items.append({**payload, 'id': 'bookmark', 'ext': 'url', 'folders': [payload['folderId']]})
            def request(self, endpoint): return {'annotation': ''}
            def verify_item(self, *args, **kwargs): return {}
        with patch('collector.Eagle', FakeEagle), patch('saffron.vimeo_thumbnail', return_value='dGh1bWI='):
            result = import_assets(self.config, True)
        self.assertEqual(result['imported'], 1)
        endpoint, payload = FakeEagle.posts[0]
        self.assertEqual(endpoint, '/api/item/addBookmark')
        self.assertEqual(payload['url'], 'https://vimeo.com/12345678')
        self.assertEqual(payload['name'], 'Blue Car Rental 冰岛租车 Vimeo 001')
        self.assertEqual(payload['base64'], 'dGh1bWI=')

    def test_industry_tags_bilingual_name_and_optional_summary(self):
        _, manifest = self.manifest()
        self.config['project_metadata'] = {'https://studio.example/project/': {
            'display_name': 'LEGO 乐高', 'category_tags': ['儿童'],
            'industry_tags': ['玩具', '积木'], 'summary': '项目简介。'}}
        prepare(self.config, manifest)
        with state(self.config) as db:
            record = json.loads(db.execute('SELECT record FROM assets').fetchone()[0])
        item = payload(record)
        self.assertTrue(item['name'].startswith('LEGO 乐高 '))
        self.assertEqual(item['tags'], ['儿童', '玩具', '积木'])
        self.assertEqual(item['annotation'], '项目简介。')
        self.assertNotIn('collector-id:', item['annotation'])
        record['project_summary'] = None
        self.assertEqual(payload(record)['annotation'], '')

    def test_project_scoped_preflight_is_strict_for_selected_project_only(self):
        _, manifest = self.manifest()
        self.assertEqual(prepare(self.config, manifest)['prepared'], 1)
        records = json.loads(manifest.read_text())
        records[0]['project_title'] = 'Unreviewed Project'
        records[0]['project_url'] = 'https://studio.example/unreviewed/'
        records[0]['asset_url'] = 'https://studio.example/unreviewed.webp'
        manifest.write_text(json.dumps(records))
        self.assertEqual(prepare(self.config, manifest)['prepared'], 1)

        scoped = import_assets(self.config, False,
                               ['https://studio.example/project/'])
        self.assertTrue(scoped['preflight']['ready'])
        self.assertEqual(len(scoped['plan']), 1)
        self.assertEqual(scoped['preflight']['scope_project_urls'],
                         ['https://studio.example/project'])

        invalid_scope = import_assets(self.config, True,
                                      ['https://studio.example/unreviewed/'])
        self.assertFalse(invalid_scope['preflight']['ready'])
        self.assertTrue(invalid_scope['blocked'])

    def test_project_scoped_preflight_checks_name_collisions_globally(self):
        _, manifest = self.manifest()
        self.config['project_metadata']['https://studio.example/project/']['display_name'] = 'Shared 项目'
        self.assertEqual(prepare(self.config, manifest)['prepared'], 1)
        records = json.loads(manifest.read_text())
        records[0]['project_url'] = 'https://studio.example/other/'
        records[0]['asset_url'] = 'https://studio.example/other.webp'
        records[0]['project_title'] = 'Other Project'
        manifest.write_text(json.dumps(records))
        self.config['project_metadata']['https://studio.example/other/'] = {
            'display_name': 'Shared 项目', 'category_tags': ['企业'],
            'description_status': 'absent', 'media_scope_reviewed': True}
        self.assertEqual(prepare(self.config, manifest)['prepared'], 1)
        with state(self.config) as db:
            rows = db.execute('SELECT id,record FROM assets').fetchall()
            other_id = next(asset_id for asset_id, encoded in rows
                            if json.loads(encoded)['project_url'] == 'https://studio.example/other/')
            db.execute("UPDATE assets SET status='imported',eagle_id='existing-item' WHERE id=?",
                       (other_id,))
            db.commit()

        scoped = import_assets(self.config, False, ['https://studio.example/project/'])
        self.assertFalse(scoped['preflight']['ready'])
        self.assertIn('folder_collision',
                      {issue['code'] for issue in scoped['preflight']['issues']})

    def test_baseline_tracks_history_and_only_reports_new_projects(self):
        self.config['sites'] = [{'id': 'interbrand', 'list_url': 'https://interbrand.com/work/'}]
        old = 'https://interbrand.com/work/old/'
        new = 'https://interbrand.com/work/new/'
        with patch('interbrand.fetch_html', return_value='html'), patch('interbrand.discover', return_value=[old]), \
             patch('collector.existing_project_urls', return_value=set()):
            self.assertFalse(scan(self.config)['baseline_initialized'])
            self.assertEqual(scan(self.config, True)['baseline_projects'], 1)
        with patch('interbrand.fetch_html', return_value='html'), patch('interbrand.discover', return_value=[old, new]), \
             patch('collector.existing_project_urls', return_value=set()):
            self.assertEqual(scan(self.config)['new_projects'], [new])

    def test_folder_creation_uses_live_folder_api(self):
        eagle = Eagle(self.config['eagle'])
        folders = [{'id': 'studio', 'name': 'Interbrand',
                    'children': [{'id': 'project', 'name': 'LEGO 乐高', 'children': []}]}]
        with patch.object(eagle, 'check_library', return_value={'folders': []}), \
             patch.object(eagle, 'request', return_value=folders) as request:
            self.assertEqual(eagle.folder(['Interbrand', 'LEGO 乐高']), 'project')
            request.assert_called_once_with('/api/folder/list')

    def test_wrong_library_rejected(self):
        eagle = Eagle(self.config['eagle'])
        with patch.object(eagle, 'request', return_value={'library': {'path': '/some/other.library'}}):
            with self.assertRaises(RuntimeError):
                eagle.post('/api/item/addFromPaths', {'items': []})

    def test_cover_is_saved_and_verified_through_backend(self):
        eagle = Eagle(self.config['eagle'])
        saved = {'cover': None}
        def request(endpoint, payload=None):
            if endpoint == '/api/folder/list':
                return [{'id': 'project', 'name': 'LEGO 乐高', 'coverId': saved['cover']}]
            raise AssertionError(endpoint)
        def post(endpoint, payload):
            self.assertEqual(endpoint, '/api/script/inject')
            saved['cover'] = 'logo'
        with patch.object(eagle, 'check_library', return_value={}), \
             patch.object(eagle, 'request', side_effect=request), \
             patch.object(eagle, 'post', side_effect=post):
            eagle.set_cover('project', 'logo', 'asset')
            self.assertEqual(saved['cover'], 'logo')

    def test_timeout_is_reconciled_without_second_submission(self):
        _, manifest = self.manifest()
        prepare(self.config, manifest)
        with state(self.config) as db:
            expected = payload(json.loads(db.execute('SELECT record FROM assets').fetchone()[0]))
        class FakeEagle:
            posts = 0
            def __init__(self, config): pass
            def check_library(self): return {}
            def folder(self, names): return 'folder'
            def ensure_folder_description(self, *args): pass
            def find_content_matches(self, *args): return []
            def verify_file(self, *args): return {**expected, 'id': 'item', 'url': expected['website'], 'folders': ['folder']}
            def find_matching_items(self, expected, marker):
                return [] if self.posts == 0 else [{'id': 'item'}]
            def request(self, endpoint): return {'annotation': ''}
            def verify_item(self, *args, **kwargs): return {}
            def post(self, endpoint, payload):
                FakeEagle.posts += 1
                raise TimeoutError('Response lost after server imported item')
        with patch('collector.Eagle', FakeEagle):
            self.assertEqual(len(import_assets(self.config, True)['failed']), 1)
            self.assertEqual(import_assets(self.config, True)['reconciled'], 1)
            self.assertEqual(import_assets(self.config, True)['imported'], 0)
            self.assertEqual(FakeEagle.posts, 1)

    def test_uncertain_missing_item_is_not_automatically_resent(self):
        _, manifest = self.manifest()
        prepare(self.config, manifest)
        with state(self.config) as db:
            db.execute("UPDATE assets SET status='uncertain'")
            db.commit()
        class FakeEagle:
            def __init__(self, config): pass
            def check_library(self): return {}
            def find_content_matches(self, *args): return []
            def find_matching_items(self, expected, marker): return []
            def post(self, *args): raise AssertionError('Must not resend')
        with patch('collector.Eagle', FakeEagle):
            self.assertEqual(import_assets(self.config, True)['pending'], 1)

    def test_explicit_retry_resubmits_uncertain_only_after_fresh_miss(self):
        _, manifest = self.manifest()
        prepare(self.config, manifest)
        with state(self.config) as db:
            db.execute("UPDATE assets SET status='uncertain'")
            asset_id = db.execute('SELECT id FROM assets').fetchone()[0]
            expected = payload(json.loads(db.execute('SELECT record FROM assets').fetchone()[0]))
            db.commit()

        class FakeEagle:
            posts = 0
            def __init__(self, config): pass
            def check_library(self): return {}
            def folder(self, names): return 'folder'
            def items(self, **kwargs): return []
            def ensure_folder_description(self, *args): pass
            def find_content_matches(self, *args): return []
            def verify_file(self, *args): return {**expected, 'id': 'created', 'url': expected['website'], 'folders': ['folder']}
            def find_matching_items(self, expected, marker): return []
            def request(self, endpoint): return {'annotation': ''}
            def post(self, endpoint, payload):
                if endpoint == '/api/item/addFromPaths': FakeEagle.posts += 1
            def await_item(self, *args, **kwargs):
                return [{**expected, 'id': 'created', 'url': expected['website'], 'folders': ['folder']}]
            def verify_item(self, *args, **kwargs): return {}

        with patch('collector.Eagle', FakeEagle):
            report = import_assets(self.config, True, retry_uncertain=True,
                                   asset_ids=[asset_id])
        self.assertEqual(report['imported'], 1)
        self.assertEqual(report['failed'], [])
        self.assertEqual(FakeEagle.posts, 1)

    def test_reprepare_cannot_overwrite_identity_of_submitted_item(self):
        _, manifest = self.manifest()
        prepare(self.config, manifest)
        with state(self.config) as db:
            db.execute("UPDATE assets SET status='uncertain'")
            before = db.execute('SELECT record FROM assets').fetchone()[0]
            db.commit()
        self.config['project_metadata']['https://studio.example/project/']['display_name'] = 'Changed 新名称'
        result = prepare(self.config, manifest)
        self.assertEqual(len(result['failed']), 1)
        with state(self.config) as db:
            self.assertEqual(db.execute('SELECT record FROM assets').fetchone()[0], before)


class AdapterTests(unittest.TestCase):
    def html(self, data):
        return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(
            {'props': {'pageProps': {'data': data}}}) + '</script>'

    def test_pagination_is_not_silently_ignored(self):
        data = {'caseStudies': {'__typename': 'RootQueryToCaseStudyConnection',
                               'pageInfo': {'hasNextPage': True}, 'edges': []}}
        with self.assertRaises(ValueError):
            discover(self.html(data))

    def test_related_projects_and_hidden_sections_are_excluded(self):
        media = lambda name: {'__typename': 'MediaItem', 'url':
                    'https://d338ucc43w64ir.cloudfront.net/' + name + '.webp'}
        data = {'caseStudiesByAcfField': {'title': 'LEGO', 'uri': '/work/lego/',
                'caseStudyContent': {'heroGroup': {'brandLogo': {'node': media('lego')}},
                'relatedCarouselGroup': media('unrelated'),
                'showBGBSection': False, 'bgbSectionGroup': media('disabled')}}}
        rows = extract(self.html(data), 'https://interbrand.com/work/lego/')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['asset_url'].endswith('lego.webp'))
        self.assertTrue(rows[0]['is_project_logo'])


if __name__ == '__main__':
    unittest.main()
