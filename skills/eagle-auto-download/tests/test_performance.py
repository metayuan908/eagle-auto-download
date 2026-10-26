"""Validate cached decode identities and observable request metrics, offline."""
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import CONFIG
import media
from eagle import Eagle


class VideoValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from imageio_ffmpeg import get_ffmpeg_exe
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.sample = Path(cls.temp.name)/'sample.mp4'
        subprocess.run([get_ffmpeg_exe(), '-v', 'error', '-f', 'lavfi', '-i',
                        'color=c=blue:s=64x48:d=0.2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(cls.sample)], check=True, capture_output=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root/'video.mp4'
        shutil.copyfile(self.sample, self.file)
        self.cache = self.root/'validation'

    def test_same_content_reuses_decode_after_rename_and_reports_hits(self):
        metrics = {}
        with patch('media.subprocess.run', wraps=subprocess.run) as decoder:
            first = media.prepare_video(self.file, self.cache, metrics)[0]
            moved = self.file.rename(self.root/'renamed.mp4')
            second = media.prepare_video(moved, self.cache, metrics)[0]
        self.assertEqual(decoder.call_count, 1)
        self.assertEqual(first['sha256'], second['sha256'])
        self.assertEqual(second['local_path'], str(moved.resolve()))
        self.assertEqual(second['source_path'], str(moved.resolve()))
        self.assertEqual(metrics['full_decodes'], 1)
        self.assertEqual(metrics['cache_hits'], 1)
        self.assertGreater(metrics['seconds'], 0)

    def test_changed_bytes_cannot_reuse_receipt_even_with_same_size_and_mtime(self):
        media.prepare_video(self.file, self.cache)
        original = self.file.stat()
        self.file.write_bytes(b'x'*original.st_size)
        os.utime(self.file, ns=(original.st_atime_ns, original.st_mtime_ns))
        metrics = {}
        with self.assertRaisesRegex(ValueError, '解码校验失败'):
            media.prepare_video(self.file, self.cache, metrics)
        self.assertEqual(metrics.get('cache_hits', 0), 0)
        self.assertEqual(metrics['full_decodes'], 1)
        self.assertEqual(len(list(self.cache.glob('*.json'))), 1)

    def test_validator_revision_change_requires_new_decode(self):
        media.prepare_video(self.file, self.cache)
        metrics = {}
        with patch('media.VIDEO_VALIDATOR_REVISION', 2):
            media.prepare_video(self.file, self.cache, metrics)
        self.assertEqual(metrics.get('cache_hits', 0), 0)
        self.assertEqual(metrics['full_decodes'], 1)

    def test_corrupt_or_incomplete_receipt_requires_decode(self):
        media.prepare_video(self.file, self.cache)
        receipt = next(self.cache.glob('*.json'))
        for replacement in ['{broken', '[]', json.dumps({'metadata': {}})]:
            with self.subTest(receipt=replacement):
                receipt.write_text(replacement)
                metrics = {}
                media.prepare_video(self.file, self.cache, metrics)
                self.assertEqual(metrics.get('cache_hits', 0), 0)
                self.assertEqual(metrics['full_decodes'], 1)

    def test_file_modified_during_decode_never_creates_success_receipt(self):
        run = subprocess.run
        def changed(*args, **kwargs):
            result = run(*args, **kwargs)
            self.file.write_bytes(b'changed while decoding')
            return result
        with patch('media.subprocess.run', side_effect=changed):
            with self.assertRaisesRegex(ValueError, '发生变化'):
                media.prepare_video(self.file, self.cache)
        self.assertFalse(list(self.cache.glob('*.json')))


class RequestMetricsTests(unittest.TestCase):
    def test_success_and_error_are_timed_without_storing_query_or_payload(self):
        config = json.loads(CONFIG.read_text())['eagle']
        config['expected_library_path'] = tempfile.gettempdir()
        client = Eagle(config)
        with patch('eagle.urlopen', return_value=io.BytesIO(b'{"status":"success","data":{}}')):
            client.request('/api/item/info?id=private-item')
        with patch('eagle.urlopen', side_effect=TimeoutError('timeout')):
            with self.assertRaises(TimeoutError):
                client.request('/api/item/info?id=another-item')
        metric = client.metrics['endpoints']['/api/item/info']
        self.assertEqual(client.metrics['requests'], 2)
        self.assertEqual(metric['requests'], 2)
        self.assertEqual(metric['errors'], 1)
        self.assertGreaterEqual(metric['seconds'], 0)
        self.assertNotIn('private-item', json.dumps(client.metrics))


if __name__ == '__main__':
    unittest.main()
