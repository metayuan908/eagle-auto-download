import sys
import unittest
from pathlib import Path

from _support import SKILL
import wolffolins


class WolffOlinsAdapterTests(unittest.TestCase):
    def test_discovery_ignores_archive_and_non_project_links(self):
        html = '''<a href="/work/blank-street">Blank Street</a>
        <a href="https://www.wolffolins.com/work/benefit">Benefit</a>
        <a href="/work/archive">Archive</a>
        <a href="https://other.example/work/nope">Nope</a>
        <a href="/work/blank-street">duplicate</a>'''
        self.assertEqual(wolffolins.discover(html), [
            'https://www.wolffolins.com/work/blank-street',
            'https://www.wolffolins.com/work/benefit'])

    def test_extract_uses_original_images_and_excludes_related_projects(self):
        image = ('https%3A%2F%2Fimages.ctfassets.net%2Fspace%2Fid%2Fhash%2Fhero.webp')
        html = f'''<html><head><title>Work — Blank Street — Brand identity — Wolff Olins</title></head>
        <body><img class="w-full flex-auto self-stretch" src="/_next/image?url={image}&amp;w=3840&amp;q=75">
        <div>https://videos.ctfassets.net/space/id/hash/film.mp4</div>
        <h2>Related Projects</h2><img class="w-full flex-auto self-stretch" src="https://images.ctfassets.net/space/id/hash/related.jpg">
        https://videos.ctfassets.net/space/id/hash/related.mp4</body></html>'''
        records = wolffolins.extract(html, 'https://www.wolffolins.com/work/blank-street')
        self.assertEqual(records[0]['project_title'], 'Blank Street')
        self.assertEqual([r['media_type'] for r in records], ['image', 'video'])
        self.assertEqual(records[0]['asset_url'],
                         'https://images.ctfassets.net/space/id/hash/hero.webp')
        self.assertTrue(records[1]['asset_url'].endswith('/film.mp4'))

    def test_extract_rejects_pages_without_project_media(self):
        html = '<title>Work — Example — Wolff Olins</title><p>No media</p>'
        with self.assertRaisesRegex(ValueError, '没有可识别'):
            wolffolins.extract(html, 'https://www.wolffolins.com/work/example')


if __name__ == '__main__':
    unittest.main()
