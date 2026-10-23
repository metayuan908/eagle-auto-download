import sys
import unittest
import tempfile
from unittest.mock import patch
from pathlib import Path

from _support import SKILL
import twoxfour


class TwoxFourTests(unittest.TestCase):
    def test_discovery_requires_full_archive_and_uses_card_titles(self):
        articles = ''.join(
            f'<article class="teaser"><a href="https://2x4.org/work/case-{i}/">'
            f'<h3 class="entry-title"><span class="prefix">CASE {i}</span>'
            f'<span class="suffix">CLIENT {i}</span></h3></a></article>'
            for i in range(222))
        projects = twoxfour.discover(articles)
        self.assertEqual(len(projects), 222)
        self.assertEqual(twoxfour.PROJECT_NAMES['https://2x4.org/work/case-0'],
                         'Case 0 · CLIENT 0')
        with self.assertRaises(ValueError):
            twoxfour.discover('<article class="teaser"><a href="/work/one/">x</a></article>')

    def test_extract_keeps_body_media_and_ignores_related_project(self):
        page = '''<main><article id="post-1" class="full work category-culture category-branding">
          <div class="client"><a>Sample Museum</a></div><h1 class="entry-title"><span>Sample Museum</span></h1>
          <section class="slide image"><img src="https://2x4.org/wp-content/uploads/2026/01/main-800x600.jpg"
          srcset="https://2x4.org/wp-content/uploads/2026/01/main-800x600.jpg 800w,
          https://2x4.org/wp-content/uploads/2026/01/main.jpg 1600w" /></section>
          <div class="video-wrapper" data-video="https://2x4.org/wp-content/uploads/2026/01/clip.mp4"></div>
          <iframe src="https://player.vimeo.com/video/123456?h=hash"></iframe>
          </article><section id="work"><article class="teaser"><img src="https://2x4.org/wp-content/uploads/2026/01/related.jpg"></article></section></main>'''
        items = twoxfour.extract(page, 'https://2x4.org/work/sample-museum/')
        self.assertEqual([x['media_type'] for x in items], ['image', 'video', 'bookmark'])
        self.assertEqual(items[0]['asset_url'], 'https://2x4.org/wp-content/uploads/2026/01/main.jpg')
        self.assertEqual(items[2]['asset_url'], 'https://vimeo.com/123456')
        self.assertEqual(items[2]['source_player_url'], 'https://player.vimeo.com/video/123456?h=hash')
        self.assertEqual(items[0]['category_tags'], ['文化'])
        self.assertTrue(all(x['project_summary'] is None for x in items))

    def test_non_ascii_asset_paths_are_percent_encoded(self):
        page = '''<article id="post-2" class="full work category-design">
          <h1 class="entry-title">Korean title</h1>
          <img src="https://2x4.org/wp-content/uploads/2026/01/신세계-디자인.png" />
          </article>'''
        items = twoxfour.extract(page, 'https://2x4.org/work/korean-title/')
        self.assertIn('%EC%8B%A0%EC%84%B8%EA%B3%84', items[0]['asset_url'])

    def test_nested_recommendation_media_and_text_are_excluded(self):
        # The real site nests Related Projects inside the main article, unlike
        # the old fixture which placed recommendations after </article>.
        page = '''<article class="full work category-culture">
          <h1 class="entry-title">Mathaf</h1>
          <img src="https://2x4.org/wp-content/uploads/2026/01/mathaf.png">
          <div class="body"><p>Original project introduction.</p></div>
          <iframe src="https://player.vimeo.com/video/1231414670"></iframe>
          <aside id="bottom"><h2>Related Projects</h2>
            <article class="teaser work"><div class="video-wrapper"
              data-video="https://2x4.org/wp-content/uploads/2021/05/dadu-8_3.mp4"></div>
              <iframe src="https://player.vimeo.com/video/999"></iframe>
              <div class="body"><p>Unrelated introduction.</p></div>
            </article></aside></article>
          <article class="full work"><h1 class="entry-title">Another project</h1>
            <img src="https://2x4.org/wp-content/uploads/2026/01/other.png"></article>'''
        items = twoxfour.extract(page, 'https://2x4.org/work/mathaf/')
        self.assertEqual(len(items), 2)
        self.assertFalse(any('dadu' in item['asset_url'] for item in items))
        self.assertEqual(items[1]['asset_url'], 'https://vimeo.com/1231414670')
        self.assertEqual(items[1]['source_player_url'], 'https://player.vimeo.com/video/1231414670')
        self.assertEqual(items[0]['source_description'], 'Original project introduction.')
        self.assertEqual(items[0]['adapter_revision'], '2x4-main-v2')

    def test_nested_article_outside_aside_is_also_excluded(self):
        page = '''<article class="full work"><h1 class="entry-title">Main</h1>
          <article class="teaser"><img src="https://2x4.org/wp-content/uploads/2026/01/related.png"></article>
          <img src="https://2x4.org/wp-content/uploads/2026/01/main.png"></article>'''
        items = twoxfour.extract(page, 'https://2x4.org/work/main/')
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]['asset_url'].endswith('/main.png'))

    def test_collect_preserves_extracted_major_category_and_source_text(self):
        page = '''<article class="full work category-culture"><h1 class="entry-title">Mathaf</h1>
          <div class="body"><p>Museum identity.</p></div>
          <iframe src="https://player.vimeo.com/video/1231414670"></iframe></article>'''
        with tempfile.TemporaryDirectory() as directory, patch('twoxfour.fetch_html', return_value=page):
            result = twoxfour.collect('https://2x4.org/work/mathaf/', directory, delay=0)
        records = result['downloaded']
        self.assertFalse(result['failed'])
        self.assertEqual(records[0]['category_tags'], ['文化'])
        self.assertEqual(records[0]['source_description'], 'Museum identity.')
        self.assertIsNone(records[0]['project_summary'])


if __name__ == '__main__':
    unittest.main()
