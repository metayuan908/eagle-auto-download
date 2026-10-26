import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SKILL
import adobe_ccv
import external_video
from eagle_plugin import Plugin, checked_result
from media import sha256
from collector import import_assets, prepare

SOURCE = 'https://www-ccv.adobe.io/v1/player/ccv/Abc_123/embed'
RECORD = {'project_url': 'https://www.behance.net/gallery/123/Example',
          'asset_url': SOURCE, 'external_provider': 'adobe-ccv', 'media_type': 'bookmark', 'page_order': 1}


def info():
    return {'_type': 'video', 'width': 1920, 'height': 1080, 'format_id': 'hls-1080',
            'cookies': 'temporary-player-cookie', 'formats': [
                {'url': 'https://cdn-prod-ccv.adobe.com/Abc_123/rend/index.m3u8?hdnts=temporary',
                 'width': 1920, 'height': 1080}]}


class AdobeTests(unittest.TestCase):
    def test_retry_full_decode_checkpoint_and_dry_import_end_to_end(self):
        from imageio_ffmpeg import get_ffmpeg_exe
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.mp4'
            subprocess.run([get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
                            'color=c=blue:s=1920x1080:d=0.1', '-c:v', 'libx264',
                            '-pix_fmt', 'yuv420p', str(source)], check=True, capture_output=True)
            config = json.loads((SKILL / 'references/config.example.json').read_text())
            config['collection'].update(external_video_mode='files', max_concurrent_downloads=1)
            config['eagle']['expected_library_path'] = str(root / 'test.library')
            config['_paths'] = {'state_db': root / 'state.sqlite', 'downloads': root / 'downloads',
                               'reports': root / 'reports'}
            record = {**RECORD, 'studio': 'Example 示例', 'project_title': 'Example 案例',
                      'project_display_name': 'Example 案例', 'category_tags': ['文化'],
                      'industry_tags': ['设计'], 'source_description': 'A source description.',
                      'project_summary': '测试案例介绍。', 'description_status': 'present',
                      'media_scope_reviewed': True}
            manifest = root / 'links.json'; manifest.write_text(json.dumps([record]))
            self.assertEqual(prepare(config, manifest)['prepared'], 1)
            self.assertFalse(import_assets(config)['preflight']['ready'])
            class YDL:
                attempts = 0
                def __init__(self, options): self.options = options
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, *args, **kwargs):
                    YDL.attempts += 1
                    if YDL.attempts == 1: raise RuntimeError('EOF occurred in violation of protocol')
                    return info()
                def process_ie_result(self, data, **kwargs):
                    file = Path(self.options['outtmpl'].replace('%(ext)s', 'mp4'))
                    shutil.copy2(source, file)
                    self.options['post_hooks'][0](str(file)); return data
            with patch('yt_dlp.YoutubeDL', YDL), patch('adobe_ccv.time.sleep'), \
                 patch('media.subprocess.run', wraps=subprocess.run) as decoder, \
                 patch('external_video.ffmpeg_directory', return_value=directory):
                result = external_video.run(config, execute=True)
            self.assertEqual(len(result['downloaded']), 1)
            self.assertEqual(YDL.attempts, 2)
            self.assertEqual(decoder.call_count, 1)  # Download + checkpoint share a verified receipt.
            planned = import_assets(config)
            self.assertTrue(planned['preflight']['ready'])
            self.assertEqual(len(planned['plan']), 1)
            self.assertEqual(planned['summary']['counts']['awaiting_video'], 1)

    def test_player_id_and_formats_are_bound_to_source(self):
        self.assertEqual(adobe_ccv.canonical(SOURCE + '?api_key=behance1'), SOURCE)
        for url in [SOURCE.replace('https:', 'http:'), SOURCE + '?api_key=another',
                    SOURCE.replace('www-ccv.adobe.io', 'evil.example')]:
            with self.assertRaises(ValueError): adobe_ccv.canonical(url)
        bad = info(); bad['formats'][0]['url'] = bad['formats'][0]['url'].replace('Abc_123', 'Another')
        with self.assertRaises(ValueError): adobe_ccv.validate_info(bad, SOURCE)
        bad = info(); bad['has_drm'] = True
        with self.assertRaises(ValueError): adobe_ccv.validate_info(bad, SOURCE)
        bad = info(); bad['formats'] = [{'url': 'https://cdn-prod-ccv.adobe.com/Abc_123/rend/576.mp4'}]
        with self.assertRaisesRegex(ValueError, '画质列表不完整'): adobe_ccv.validate_info(bad, SOURCE)

    def test_download_decodes_before_receipt_and_cache_preserves_new_project_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options_seen = []
            class YDL:
                def __init__(self, options): self.options = options; options_seen.append(options)
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, url, download=False): return info()
                def process_ie_result(self, data, download=True):
                    file = Path(self.options['outtmpl'].replace('%(ext)s', 'mp4'))
                    file.write_bytes(b'fake media; decoding is mocked independently')
                    for hook in self.options['post_hooks']: hook(str(file))
                    return data
            def decode(file, cache_directory=None):
                return [{'sha256': sha256(file), 'width': 1920, 'height': 1080,
                         'duration_seconds': 12, 'has_audio': True}]
            with patch('yt_dlp.YoutubeDL', YDL), patch('external_video.ffmpeg_directory', return_value=directory), \
                 patch('adobe_ccv.prepare_video', side_effect=decode) as decoded:
                result = adobe_ccv.download_one(RECORD, root, proxy='', retries=1)
            self.assertEqual(decoded.call_count, 1)
            self.assertFalse(options_seen[0]['skip_unavailable_fragments'])
            self.assertEqual(options_seen[0]['proxy'], '')
            receipt = adobe_ccv.target_directory(RECORD, root) / 'complete.json'
            self.assertNotIn('temporary', receipt.read_text())
            other = {**RECORD, 'project_url': 'https://www.behance.net/gallery/456/Other', 'project_summary': '另一个案例'}
            cached = adobe_ccv.cached_download(other, root)
            self.assertEqual(cached['project_url'], other['project_url'])
            self.assertEqual(cached['project_summary'], '另一个案例')
            Path(result['local_path']).write_bytes(b'damaged')
            self.assertIsNone(adobe_ccv.cached_download(RECORD, root))

    def test_failed_decode_does_not_create_success_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            class YDL:
                def __init__(self, options): self.options = options
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def extract_info(self, *args, **kwargs): return info()
                def process_ie_result(self, data, **kwargs):
                    file = Path(self.options['outtmpl'].replace('%(ext)s', 'mp4')); file.write_bytes(b'corrupt')
                    self.options['post_hooks'][0](str(file)); return data
            with patch('yt_dlp.YoutubeDL', YDL), patch('external_video.ffmpeg_directory', return_value=directory), \
                 patch('adobe_ccv.prepare_video', side_effect=ValueError('decode failed')):
                with self.assertRaises(ValueError): adobe_ccv.download_one(RECORD, root, retries=1)
            self.assertFalse((adobe_ccv.target_directory(RECORD, root) / 'complete.json').exists())

    def test_adobe_plan_and_dispatch_use_shared_scheduler_without_network(self):
        with patch.object(external_video, 'candidates', return_value=[RECORD]), \
             patch.object(external_video, 'download_one', side_effect=AssertionError('plan only')):
            result = external_video.run({}, execute=False)
        self.assertEqual(result['plan'][0]['player_url'], SOURCE + '?api_key=behance1')
        with patch('adobe_ccv.download_one', return_value={'media_type': 'video'}) as download:
            external_video.download_one(RECORD, Path('/tmp'), proxy='', retries=2)
        self.assertEqual(download.call_args.kwargs, {'proxy': '', 'retries': 2})


class PluginTests(unittest.TestCase):
    def test_business_and_partial_failures_are_not_success(self):
        for payload in [{'success': False, 'error': 'validation failed'},
                        {'success': True, 'data': [{'success': False}]},
                        {'success': True, 'successCount': 1, 'totalCount': 2}]:
            with self.assertRaises(RuntimeError): checked_result(payload)

    def test_disabled_and_mutating_tools_never_call_endpoint(self):
        client = Plugin()
        with patch.object(client, 'request', return_value={'tools': {}}) as request:
            with self.assertRaises(ValueError): client.call('smart_folder_get', {})
            self.assertEqual(request.call_count, 1)  # Only the live tool list.
        with patch.object(client, 'request', side_effect=AssertionError('No write calls')):
            with self.assertRaises(ValueError): client.call('item_add', {'items': []})
        with self.assertRaises(ValueError): Plugin('http://example.com:41596')


if __name__ == '__main__':
    unittest.main()
