import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from _support import SKILL
import behance
import tigerpan
from collector import existing_project_urls
from source_media import collect_records

PROFILE = 'https://www.behance.net/example'
PROJECT = 'https://www.behance.net/gallery/123/Example'
IMAGE = 'https://mir-s3-cdn-cf.behance.net/project_modules/'
PLAYER = 'https://www-ccv.adobe.io/v1/player/ccv/Abc_-123/embed'


def project():
    return {'id': 123, 'url': PROJECT, 'name': 'Example project', 'privacy': 'public',
            'owners': [{'username': 'example'}], 'description': '<p>Source description.</p>',
            'modules': [{'id': 10, 'project_id': 123, 'type': 'image',
                         'sizes': {'max_1920': IMAGE + 'fs/small.jpg',
                                   'original': IMAGE + 'source/large.gif'}},
                        {'id': 11, 'project_id': 123, 'type': 'video', 'src': PLAYER + '?api_key=behance1'}],
            'covers': {'original': 'https://mir-s3-cdn-cf.behance.net/projects/cover.jpg'},
            'relatedProjects': [{'modules': [{'type': 'image', 'src': IMAGE + 'source/related.jpg'}]}]}


class BehanceTests(unittest.TestCase):
    def test_video_only_case_queues_pending_source_without_claiming_download(self):
        with tempfile.TemporaryDirectory() as directory:
            row = {'asset_url': PLAYER, 'media_type': 'bookmark', 'page_order': 1}
            with patch('source_media.download', side_effect=AssertionError('Player is not a file')):
                result = collect_records([row], directory, behance.IMAGE_HOSTS, delay=0)
            self.assertEqual(result['downloaded'], 0)
            self.assertEqual(result['queued_external_videos'], 1)
            self.assertEqual(result['records'], 1)
            self.assertEqual(json.loads(Path(result['manifest']).read_text())[0]['asset_url'], PLAYER)

    def test_all_profile_pages_follow_published_cursor(self):
        pages = [b'<a href="/gallery/123/A">A</a><a rel="next" href="/example?after=MTI">Next</a>',
                 b'<a href="/gallery/456/B">B</a>']
        with patch('behance.request', side_effect=pages) as request:
            urls = behance.discover_projects(PROFILE, delay=0)
        self.assertEqual(urls, ['https://www.behance.net/gallery/123/A',
                                'https://www.behance.net/gallery/456/B'])
        self.assertEqual(request.call_args_list[1].args[0], PROFILE + '?after=MTI')

    def test_pagination_cycles_and_owner_redirects_are_rejected(self):
        for next_url in ['/another?after=MTI', '/example?after=MTI']:
            pages = [b'<a href="/gallery/123/A">A</a><a rel="next" href="/example?after=MTI">Next</a>',
                     f'<a href="/gallery/456/B">B</a><a rel="next" href="{next_url}">Next</a>'.encode()]
            with patch('behance.request', side_effect=pages):
                with self.assertRaises(ValueError):
                    behance.discover_projects(PROFILE, delay=0)

    def test_original_gif_body_only_and_stable_video_identity(self):
        rows = behance.extract(project(), PROJECT, owner='example', studio='Example Studio')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['asset_url'], IMAGE + 'source/large.gif')
        self.assertEqual(rows[1]['asset_url'], PLAYER)
        self.assertEqual(rows[1]['external_provider'], 'adobe-ccv')
        self.assertEqual(rows[1]['media_type'], 'bookmark')
        self.assertEqual({r['behance_project_id'] for r in rows}, {'123'})
        self.assertNotIn('media_scope_reviewed', rows[0])

    def test_wrong_owner_module_or_unknown_body_type_stops_extraction(self):
        with self.assertRaisesRegex(ValueError, '作者'):
            behance.extract(project(), PROJECT, owner='someone_else')
        p = project(); p['modules'][0]['project_id'] = 999
        with self.assertRaisesRegex(ValueError, '其他案例'):
            behance.extract(p, PROJECT, owner='example')
        p = project(); p['modules'].append({'type': 'new_carousel'})
        with self.assertRaisesRegex(ValueError, '未适配'):
            behance.extract(p, PROJECT, owner='example')

    def test_collection_and_vimeo_embed_keep_case_association(self):
        p = project(); p['modules'] = [
            {'type': 'media_collection', 'project_id': 123, 'components': [p['modules'][0]]},
            {'type': 'embed', 'embed': '<iframe src="https://player.vimeo.com/video/789?h=abc"></iframe>'}]
        rows = behance.extract(p, PROJECT, owner='example')
        self.assertEqual(rows[0]['source_field'], 'project_modules.media_collection')
        self.assertEqual(rows[1]['asset_url'], 'https://vimeo.com/789')
        self.assertEqual(rows[1]['source_player_url'], 'https://player.vimeo.com/video/789?h=abc')

    def test_same_brand_and_placeholder_slug_do_not_skip_other_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            api = MagicMock()
            api.request.return_value = [{'id': 'studio', 'name': 'Example',
                                         'children': [{'id': 'case', 'name': 'Brand 品牌案例甲'}]}]
            api.items.return_value = []
            urls = ['https://www.behance.net/gallery/123/_', 'https://www.behance.net/gallery/456/_']
            config = {'sites': [{'id': 'behance', 'studio': 'Example'}], 'eagle': {},
                      'project_metadata': {u: {'display_name': 'Brand 品牌案例'} for u in urls},
                      '_paths': {'state_db': Path(directory) / 'state.sqlite'}}
            with patch('collector.Eagle', return_value=api):
                self.assertEqual(existing_project_urls(config, urls), set())


class TigerPanTests(unittest.TestCase):
    def test_pagination_count_and_hidden_cases(self):
        pages = [{'pageNum': 1, 'total': 2, 'totalPage': 2, 'list': [{'id': '1'}]},
                 {'pageNum': 2, 'total': 2, 'totalPage': 2, 'list': [{'id': '2', 'isHide': True}]}]
        with patch('tigerpan.api', side_effect=pages):
            self.assertEqual(tigerpan.discover_projects('https://www.tigerpan.com/works', 0),
                             ['https://www.tigerpan.com/works/1'])
        pages[1]['list'][0]['id'] = '1'
        with patch('tigerpan.api', side_effect=pages):
            with self.assertRaises(ValueError):
                tigerpan.discover_projects('https://www.tigerpan.com/works', 0)

    def test_only_desktop_album_and_native_video_not_mobile_or_banners(self):
        host = 'https://tiger-pan.oss-cn-shenzhen.aliyuncs.com/'
        data = {'id': '1', 'name': 'Test case', 'detailHtml': '<p>Source</p>',
                'albumPics': host + 'body.gif', 'video': host + 'video.mp4',
                'banner': host + 'banner.jpg', 'mobilePics': host + 'crop.jpg'}
        rows = tigerpan.extract(data, 'https://www.tigerpan.com/works/1')
        self.assertEqual([r['asset_url'] for r in rows], [host + 'body.gif', host + 'video.mp4'])
        self.assertEqual(rows[0]['source_description'], 'Source')
        data['isHide'] = True
        with self.assertRaises(ValueError):
            tigerpan.extract(data, 'https://www.tigerpan.com/works/1')


if __name__ == '__main__':
    unittest.main()
