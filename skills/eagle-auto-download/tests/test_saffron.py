import json
import sys
import unittest
from pathlib import Path

from _support import SKILL
import saffron


def page(entry):
    data = {'props': {'pageProps': {'entry': entry}}}
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(data) + '</script>'


class SaffronAdapterTests(unittest.TestCase):
    def test_discovery_finds_unique_case_pages_only(self):
        html = '''<a href="/work/newton">Newton</a><a href="https://saffron-consultants.com/work/galp">Galp</a>
        <a href="/work/newton">again</a><a href="/about">About</a><a href="https://bad.example/work/no">No</a>'''
        self.assertEqual(saffron.discover(html), [
            'https://saffron-consultants.com/work/newton',
            'https://saffron-consultants.com/work/galp'])

    def test_extract_includes_body_media_and_vimeo_but_excludes_related_items(self):
        entry = {'url': 'https://saffron-consultants.com/work/example',
                 'title': 'Example title', 'clients': [{'title': 'Example'}],
                 'pageBlocks': [{'media': [
                     {'imageAsset': [{'src': 'https://saffron.imgix.net/case/hero.webp?auto=format&w=1200',
                                      'oriWidth': 1200, 'oriHeight': 800}]},
                     {'videoAsset': [{'src': 'https://saffron.imgix.net/case/film.mp4?auto=format'}]},
                     {'videoProvider': 'vimeo', 'videoId': 'https://vimeo.com/12345678'}]},
                     {'imageAsset': [{'src': 'https://saffron.imgix.net/case/related.jpg'}]}],
                 'relatedCaseStudies': [{'imageAsset': [{'src': 'https://saffron.imgix.net/related.jpg'}]}]}
        records = saffron.extract(page(entry), entry['url'])
        self.assertEqual([r['media_type'] for r in records], ['image', 'video', 'bookmark', 'image'])
        self.assertEqual(records[0]['asset_url'], 'https://saffron.imgix.net/case/hero.webp')
        self.assertEqual(records[2]['asset_url'], 'https://vimeo.com/12345678')
        self.assertEqual(records[2]['external_provider'], 'vimeo')
        self.assertNotIn('related.jpg', [r['asset_url'] for r in records])

    def test_rejects_vimeo_urls_with_query_or_unapproved_asset_hosts(self):
        entry = {'url': 'https://saffron-consultants.com/work/example',
                 'clients': [{'title': 'Example'}], 'pageBlocks': [{'media': [
                     {'videoId': 'https://vimeo.com/12345678?download=1'},
                     {'imageAsset': [{'src': 'https://evil.example/hero.png'}]}]}]}
        with self.assertRaisesRegex(ValueError, '没有可识别'):
            saffron.extract(page(entry), entry['url'])

    def test_hero_is_added_without_renumbering_body_or_collecting_card(self):
        url = 'https://saffron-consultants.com/work/example'
        entry = {'url': url, 'clients': [{'title': 'Example'}],
                 'pageBlocks': [{'videoId': 'https://vimeo.com/11111111'}],
                 'media': [{'children': [
                     {'cover': [{'videoId': 'https://vimeo.com/22222222'}]},
                     {'card': [{'videoId': 'https://vimeo.com/33333333'}]}]}]}
        records = saffron.extract(page(entry), url)
        self.assertEqual([(r['asset_url'], r['page_order']) for r in records], [
            ('https://vimeo.com/11111111', 1), ('https://vimeo.com/22222222', 2)])


if __name__ == '__main__':
    unittest.main()
