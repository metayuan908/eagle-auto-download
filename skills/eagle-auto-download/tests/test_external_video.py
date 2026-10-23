import sys
import json
import io
import tempfile
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from threading import Event
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

from _support import SKILL
import external_video


class ExternalVideoTests(unittest.TestCase):
    def test_embed_url_uses_only_source_video_id(self):
        self.assertEqual(external_video.player_url('https://vimeo.com/12345678'),
                         'https://player.vimeo.com/video/12345678')
        for url in ['https://evil.example/12345678', 'https://vimeo.com/123?a=b',
                    'https://user:password@vimeo.com/123', 'http://vimeo.com/123']:
            with self.assertRaises(ValueError):
                external_video.player_url(url)

    def test_connection_block_stops_bulk_requests_and_does_not_prepare(self):
        rows = [{'project_url': 'https://saffron-consultants.com/work/example',
                 'asset_url': f'https://vimeo.com/{n}', 'page_order': n}
                for n in (11111111, 22222222)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'_paths': {'downloads': root / 'downloads', 'reports': root / 'reports'}}
            with patch.object(external_video, 'candidates', return_value=rows), \
                 patch.object(external_video, 'download_one', side_effect=RuntimeError('HTTP Error 401: Unauthorized')) as fetch, \
                 patch.object(external_video, 'prepare', side_effect=AssertionError('No file to prepare')):
                report = external_video.run(config, execute=True)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(report['blocked'][0]['kind'], 'authentication_required')
            self.assertEqual(report['not_attempted'], 1)
            self.assertEqual(report['downloaded'], [])
            self.assertTrue(Path(report['report_path']).is_file())

    def test_plan_does_not_download_and_drm_is_rejected(self):
        rows = [{'project_url': 'https://saffron-consultants.com/work/example',
                 'asset_url': 'https://vimeo.com/11111111', 'page_order': 1}]
        with patch.object(external_video, 'candidates', return_value=rows), \
             patch.object(external_video, 'download_one', side_effect=AssertionError('Plan only')):
            report = external_video.run({}, execute=False)
        self.assertEqual(report['plan'][0]['referer'], rows[0]['project_url'])
        self.assertIsNotNone(external_video.reject_drm({'has_drm': True}))
        self.assertIsNotNone(external_video.reject_drm({'requested_formats': [{'has_drm': True}]}))
        self.assertIsNone(external_video.reject_drm({'has_drm': False}))

    def test_official_download_quality_and_video_identity(self):
        source = 'https://vimeo.com/11111111'
        prefix = 'https://player.vimeo.com/progressive_redirect/download/11111111/container/'
        formats = [{'url': prefix + '720.mp4?signature=public', 'height': 720},
                   {'url': prefix + '1080.mp4?signature=public', 'height': 1080}]
        self.assertEqual(external_video.select_official_download(formats, source)['height'], 1080)
        formats.append({'url': prefix + 'source.mp4?signature=public', 'quality': 'original'})
        self.assertEqual(external_video.select_official_download(formats, source)['quality'], 'original')
        with self.assertRaises(ValueError):
            external_video.select_official_download([{'url': prefix.replace('11111111', '22222222') + 'file.mp4'}], source)

    def test_official_links_plan_only_targets_provided_videos(self):
        rows = [{'project_url': 'https://saffron-consultants.com/work/example',
                 'asset_url': f'https://vimeo.com/{n}', 'page_order': n}
                for n in (11111111, 22222222)]
        with patch.object(external_video, 'candidates', return_value=rows):
            report = external_video.run({}, official_links=[{'source_url': rows[1]['asset_url'], 'downloads': []}])
        self.assertEqual(report['selected'], 1)
        self.assertEqual(report['plan'][0]['source_url'], rows[1]['asset_url'])

    def test_public_download_uses_page_issued_guest_permissions(self):
        source = 'https://vimeo.com/11111111'
        props = {'clipId': '11111111', 'clip': {'privacy': {'download': True}},
                 'pageMetadata': {'clipSignature': 'public-page-signature'},
                 'viewerBootstrap': {'user': None, 'jwt': 'guest-viewer-token'}}
        page = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({
            'props': {'pageProps': props}}) + '</script>'
        files = {'download': [{'link': 'https://player.vimeo.com/progressive_redirect/download/11111111/file.mp4?signature=public',
                               'width': 1920, 'height': 1080, 'size': 1234, 'quality': 'hd'}]}
        with patch.object(external_video, 'public_response', side_effect=[page.encode(), json.dumps(files).encode()]) as fetch:
            formats = external_video.discover_official_downloads(source)
        self.assertEqual(formats[0]['height'], 1080)
        api_url, headers = fetch.call_args_list[1].args
        self.assertIn('anon_signature=public-page-signature', api_url)
        self.assertEqual(headers['Authorization'], 'jwt guest-viewer-token')
        self.assertNotIn('guest-viewer-token', json.dumps(formats))
        self.assertNotIn('public-page-signature', json.dumps(formats))

    def test_disabled_download_or_wrong_video_never_queries_files(self):
        props = {'clipId': '11111111', 'clip': {'privacy': {'download': False}}}
        page = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': props}}) + '</script>'
        with patch.object(external_video, 'public_response', return_value=page.encode()) as fetch:
            with self.assertRaisesRegex(RuntimeError, 'download_disabled'):
                external_video.discover_official_downloads('https://vimeo.com/11111111')
        self.assertEqual(fetch.call_count, 1)
        with self.assertRaises(ValueError):
            external_video.public_page_data(page, '22222222')

    def test_configured_download_failures_become_bookmark_fallbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'collection': {'download_failure_fallback': 'bookmarks',
                                     'download_failure_fallback_after': 3},
                      '_paths': {'state_db': root/'state.sqlite'}}
            record = {'studio': '2x4', 'project_title': 'Example',
                      'project_url': 'https://2x4.org/work/example',
                      'asset_url': 'https://vimeo.com/12345678',
                      'media_type': 'bookmark', 'external_provider': 'vimeo',
                      'page_order': 1}
            with external_video.state(config) as db:
                db.execute('INSERT INTO assets VALUES (?, ?, ?, NULL, NULL)',
                           ('asset', json.dumps(record), 'pending_import'))
                db.commit()
            self.assertIsNone(external_video.record_download_failure(
                config, record, 'download_failed', 'connection reset'))
            self.assertIsNone(external_video.record_download_failure(
                config, record, 'download_failed', 'connection reset'))
            fallback = external_video.record_download_failure(
                config, record, 'download_failed', 'connection reset')
            self.assertEqual(fallback['reason'], 'retry_limit_exhausted')
            with external_video.state(config) as db:
                saved = json.loads(db.execute('SELECT record FROM assets').fetchone()[0])
            self.assertEqual(saved['download_attempts'], 3)
            self.assertEqual(saved['download_fallback']['reason'], 'retry_limit_exhausted')

    def test_author_disabled_download_falls_back_without_waiting_for_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'collection': {'download_failure_fallback': 'bookmarks',
                                     'download_failure_fallback_after': 3},
                      '_paths': {'state_db': root/'state.sqlite'}}
            record = {'project_url': 'https://2x4.org/work/example',
                      'asset_url': 'https://vimeo.com/12345678',
                      'media_type': 'bookmark', 'external_provider': 'vimeo'}
            with external_video.state(config) as db:
                db.execute('INSERT INTO assets VALUES (?, ?, ?, NULL, NULL)',
                           ('asset', json.dumps(record), 'pending_import'))
                db.commit()
            fallback = external_video.record_download_failure(
                config, record, 'download_disabled', 'download disabled')
            self.assertEqual(fallback['reason'], 'download_disabled')
            self.assertEqual(fallback['attempts'], 1)

    def test_global_access_block_does_not_turn_every_video_into_a_bookmark(self):
        row = {'project_url': 'https://2x4.org/work/example',
               'asset_url': 'https://vimeo.com/12345678', 'page_order': 1}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'collection': {'download_failure_fallback': 'bookmarks'},
                      '_paths': {'downloads': root/'downloads', 'reports': root/'reports',
                                 'state_db': root/'state.sqlite'}}
            with patch.object(external_video, 'candidates', return_value=[row]), \
                 patch.object(external_video, 'download_one',
                              side_effect=RuntimeError('HTTP Error 403: Forbidden')), \
                 patch.object(external_video, 'record_download_failure') as mark:
                report = external_video.run(config, execute=True)
            mark.assert_not_called()
            self.assertEqual(report['blocked'][0]['kind'], 'access_denied')
            self.assertEqual(report['fallback_bookmarks'], [])

    def test_pause_checkpoints_first_file_and_never_starts_the_next(self):
        rows = [{'project_url': 'https://example.com/work', 'asset_url': f'https://vimeo.com/{n}',
                 'page_order': n} for n in (11111111, 22222222)]
        stop = Event()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'_paths': {'downloads': root/'downloads', 'reports': root/'reports'}}
            downloaded = {**rows[0], 'local_path': str(root/'video.mp4'),
                          'download_quality': {'width': 1920, 'height': 1080}}
            with patch.object(external_video, 'candidates', return_value=rows), \
                 patch.object(external_video, 'download_one', return_value=downloaded) as fetch, \
                 patch.object(external_video, 'checkpoint', return_value={'prepared': 1}) as commit:
                report = external_video.run(config, execute=True, stop_event=stop,
                                            progress=lambda *args: stop.set())
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(commit.call_count, 1)
            self.assertEqual(report['status'], 'paused')
            self.assertEqual(report['not_attempted'], 1)
            saved = json.loads(Path(report['report_path']).read_text())
            self.assertEqual(saved['preparation']['prepared'], 1)
            self.assertEqual(saved['status'], 'paused')

    def test_reconcile_prepares_cache_without_any_network_download(self):
        rows = [{'project_url': 'https://example.com/work', 'asset_url': f'https://vimeo.com/{n}',
                 'page_order': n} for n in (11111111, 22222222)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {'_paths': {'downloads': root/'downloads', 'reports': root/'reports'}}
            with patch.object(external_video, 'candidates', return_value=rows), \
                 patch.object(external_video, 'cached_download', side_effect=[rows[0], None]), \
                 patch.object(external_video, 'checkpoint', return_value={'prepared': 1}) as commit, \
                 patch.object(external_video, 'public_response', side_effect=AssertionError('Offline only')), \
                 patch.object(external_video, 'download_one', side_effect=AssertionError('Offline only')):
                report = external_video.reconcile_cache(config)
            self.assertEqual(report['recovered'], [rows[0]['asset_url']])
            self.assertEqual(report['missing_or_invalid'], [rows[1]['asset_url']])
            self.assertEqual(commit.call_count, 1)

    def test_cache_rejects_outside_path_and_changed_bytes(self):
        record = {'project_url': 'https://example.com/work', 'asset_url': 'https://vimeo.com/11111111'}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); target = root/'vimeo/11111111'; target.mkdir(parents=True)
            outside = root/'outside.mp4'; outside.write_bytes(b'valid original')
            meta = {'local_path': str(outside), 'sha256': external_video.sha256(outside)}
            complete = target/'complete.json'; complete.write_text(json.dumps(meta))
            self.assertIsNone(external_video.cached_download(record, root))
            file = target/'video.mp4'; file.write_bytes(b'valid original')
            meta.update(local_path=str(file), download_quality={'height': 1080})
            complete.write_text(json.dumps(meta))
            self.assertIsNotNone(external_video.cached_download(record, root))
            file.write_bytes(b'corrupt replacement')
            self.assertIsNone(external_video.cached_download(record, root))

    def test_guest_api_401_refreshes_only_the_normal_public_grant_once(self):
        props = {'clipId': '11111111', 'clip': {'privacy': {'download': True}},
                 'pageMetadata': {'clipSignature': 'page-signature'},
                 'viewerBootstrap': {'user': None, 'jwt': 'guest-token'}}
        page = ('<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': props}}) + '</script>').encode()
        rejected = HTTPError('https://api.vimeo.com/videos/11111111', 401, 'Unauthorized', {}, None)
        files = json.dumps({'download': [{'link': 'https://player.vimeo.com/progressive_redirect/download/11111111/file.mp4',
                                          'height': 1080, 'quality': 'hd'}]}).encode()
        with patch.object(external_video, 'public_response', side_effect=[page, rejected, page, files]) as fetch:
            result = external_video.discover_official_downloads('https://vimeo.com/11111111')
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(result[0]['height'], 1080)

    def test_status_codes_are_not_inferred_from_digits_in_a_video_url(self):
        self.assertEqual(external_video.failure_kind('Failed for https://vimeo.com/1140123403'), 'download_failed')
        self.assertEqual(external_video.failure_kind('HTTP Error 429: Too Many Requests'), 'rate_limited')
        self.assertEqual(external_video.failure_kind('HTTP Error 403: Forbidden'), 'access_denied')
        self.assertEqual(external_video.failure_kind('connection_verification: Verify to continue'),
                         'access_verification_required')

    def test_preexisting_pause_marker_never_starts_a_download(self):
        rows = [{'project_url': 'https://example.com/work', 'asset_url': 'https://vimeo.com/11111111', 'page_order': 1}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = {'_paths': {'downloads': root/'downloads', 'reports': root/'reports'}}
            marker = external_video.pause_path(config); marker.parent.mkdir(parents=True); marker.touch()
            with patch.object(external_video, 'candidates', return_value=rows), \
                 patch.object(external_video, 'download_one', side_effect=AssertionError('Paused')):
                report = external_video.run(config, execute=True)
            self.assertEqual(report['status'], 'paused')
            self.assertEqual(report['not_attempted'], 1)

    def test_merged_identical_video_sources_are_all_excluded_from_downloads(self):
        project = 'https://example.com/work'
        first, second = 'https://vimeo.com/11111111', 'https://vimeo.com/22222222'
        records = [{'project_url': project, 'asset_url': url, 'media_type': 'bookmark',
                    'external_provider': 'vimeo', 'page_order': i} for i, url in enumerate((first, second))]
        records.append({'project_url': project, 'asset_url': second, 'media_type': 'video',
                        'external_provider': 'vimeo', 'sources': [first, second]})
        db = SimpleNamespace(execute=lambda sql: [(json.dumps(row),) for row in records])
        with patch.object(external_video, 'state', return_value=nullcontext(db)):
            self.assertEqual(external_video.candidates({}), [])

    def test_failed_player_video_validation_never_marks_cache_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); file = root/'vimeo/11111111/video.mp4'
            class FakeDownloader:
                def __init__(self, options): self.options = options
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def extract_info(self, url, download):
                    file.write_bytes(b'not a complete video')
                    for hook in self.options['post_hooks']: hook(str(file))
                    return {'width': 1920, 'height': 1080}
            record = {'project_url': 'https://example.com/work', 'asset_url': 'https://vimeo.com/11111111'}
            with patch.object(external_video, 'discover_official_downloads',
                              side_effect=external_video.PublicDownloadsUnavailable('No public menu')), \
                 patch.object(external_video, 'discover_player_url', return_value='https://player.vimeo.com/video/11111111'), \
                 patch.object(external_video, 'ffmpeg_directory', return_value=str(root)), \
                 patch.object(external_video, 'prepare_video', side_effect=ValueError('Incomplete video')), \
                 patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=FakeDownloader)}):
                with self.assertRaisesRegex(ValueError, 'Incomplete video'):
                    external_video.download_one(record, root)
            self.assertFalse((file.parent/'complete.json').exists())

    def test_public_watch_404_falls_back_to_verified_source_embed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); file = root/'vimeo/11111111/video.mp4'
            requested = {}
            class FakeDownloader:
                def __init__(self, options): self.options = options
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def extract_info(self, url, download):
                    requested['url'] = url
                    file.write_bytes(b'validated video')
                    for hook in self.options['post_hooks']: hook(str(file))
                    return {'width': 1920, 'height': 1080, 'duration': 1,
                            'requested_formats': []}
            record = {'project_url': 'https://2x4.org/work/example',
                      'asset_url': 'https://vimeo.com/11111111',
                      'source_player_url': 'https://player.vimeo.com/video/11111111'}
            error = HTTPError('https://vimeo.com/11111111?action=load_download_config',
                              404, 'Not Found', {}, None)
            validation = [{'width': 1920, 'height': 1080,
                           'duration_seconds': 1, 'has_audio': True}]
            with patch.object(external_video, 'discover_official_downloads', side_effect=error), \
                 patch.object(external_video, 'ffmpeg_directory', return_value=str(root)), \
                 patch.object(external_video, 'prepare_video', return_value=validation), \
                 patch.dict(sys.modules, {'yt_dlp': SimpleNamespace(YoutubeDL=FakeDownloader)}):
                result = external_video.download_one(record, root)
            self.assertEqual(result['quality_status'], 'highest_available_yt_dlp')
            self.assertEqual(result['media_type'], 'video')
            self.assertEqual(requested['url'], record['source_player_url'])
            self.assertTrue((file.parent/'complete.json').is_file())

    def test_source_player_url_must_match_video_and_known_vimeo_host(self):
        record = {'asset_url': 'https://vimeo.com/11111111',
                  'source_player_url': 'https://player.vimeo.com/video/22222222'}
        with self.assertRaisesRegex(ValueError, '不匹配'):
            external_video.source_player_url(record)

    def test_transfer_uses_http_length_not_approximate_menu_size(self):
        class Response(io.BytesIO):
            url = 'https://player.vimeo.com/progressive_redirect/download/11111111/file.mp4'
            def __init__(self, body, declared_length):
                super().__init__(body)
                self.headers = {'Content-Type': 'video/mp4', 'Content-Length': str(declared_length)}
        body = b'video bytes for transfer validation'
        record = {'asset_url': 'https://vimeo.com/11111111', 'official_downloads': [{
            'url': Response.url, 'height': 1080, 'filesize': len(body) + 100}]}
        validation = [{'width': 1920, 'height': 1080, 'duration_seconds': 1, 'has_audio': True}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(external_video, 'urlopen', return_value=Response(body, len(body))), \
                 patch.object(external_video, 'prepare_video', return_value=validation):
                result = external_video.download_official(record, root)
            self.assertEqual(result['download_quality']['file_bytes'], len(body))
            self.assertEqual(result['download_quality']['advertised_file_bytes'], len(body)+100)
            with patch.object(external_video, 'urlopen', return_value=Response(body, len(body)+1)):
                with self.assertRaisesRegex(ValueError, '不完整'):
                    external_video.download_official(record, root)


if __name__ == '__main__':
    unittest.main()
