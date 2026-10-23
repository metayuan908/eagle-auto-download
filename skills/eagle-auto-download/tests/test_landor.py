import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SKILL
from landor import asset_url, canonical, description, discover, extract, download, plain


def html(blocks, name=None):
    data = {'props': {'pageProps': {'initialProps': {'PageData': {'contents': blocks, 'name':name}}}}}
    return '<script id="__NEXT_DATA__" type="application/json">'+json.dumps(data)+'</script>'


def identity():
    return [{'type': 'breadcrumb', 'items': [{'link': {'url': '/en/our-work/example/'}}]},
            {'type': 'caseStudyDetailHeader', 'title': 'Example'}]


class LandorTests(unittest.TestCase):
    def test_only_listing_items_define_project_scope(self):
        page = html([{'type': 'caseStudiesListing', 'items': [
            {'link': {'url': '/en/our-work/example/'}},
            {'link': {'url': '/en/our-work/example/'}}]},
            {'type': 'caseStudyDetailFooter', 'nextItem': {'link': {'url': '/en/our-work/not-listed/'}}}])
        self.assertEqual(discover(page), ['https://landor.com/en/our-work/example'])

    def test_footer_and_video_poster_are_excluded_and_originals_keep_page_order(self):
        page = html(identity()+[
            {'type': 'caseStudyDetailHero', 'image': {'url': 'https://cms.landor.com/media/a/hero.webp'},
             'mobileImage': {'url': 'https://cms.landor.com/media/b/mobile.jpg'}},
            {'type': 'caseStudyDetailVideo', 'video': 'https://cms.landor.com/media/c/movie.mp4',
             'placeholder': {'url': 'https://cms.landor.com/media/d/poster.jpg'}},
            {'type': 'caseStudyDetailFooter', 'nextItem': {'image': {'url': 'https://cms.landor.com/media/e/wrong.jpg'}}}])
        records = extract(page, 'https://landor.com/en/our-work/example/')
        self.assertEqual([r['media_type'] for r in records], ['image','image','video'])
        self.assertEqual([r['page_order'] for r in records], [1,2,3])
        self.assertTrue(all('poster' not in r['asset_url'] and 'wrong' not in r['asset_url'] for r in records))

    def test_two_and_three_image_blocks_preserve_svg_and_deduplicate(self):
        image = {'url': 'https://cms.landor.com/media/a/logo.svg'}
        page = html(identity()+[{'type': 'caseStudyDetail2Images','images': [image,image]},
                               {'type': 'caseStudyDetail3ImagesMirrored','images': [image]}])
        records = extract(page, 'https://landor.com/en/our-work/example')
        self.assertEqual(len(records),1)
        self.assertTrue(records[0]['is_project_logo'])

    def test_project_body_is_description_and_team_and_footer_are_not(self):
        page = html(identity()+[{'type': 'caseStudyDetailText','description': '<p>Project intro.</p>'},
            {'type': 'paseStudyDetailProjectInfo','projectInfoBody': '<p>Real work.</p>', 'teamBody': 'Not the brief'},
            {'type': 'caseStudyDetailFooter','nextItem': {'description': 'Unrelated'}}])
        self.assertEqual(description(page),'Project intro.\nReal work.')

    def test_mismatched_case_identity_rejected(self):
        with self.assertRaisesRegex(ValueError,'身份'):
            extract(html(identity()), 'https://landor.com/en/our-work/other')

    def test_unseen_blocks_do_not_silently_drop_media(self):
        with self.assertRaisesRegex(ValueError,'未适配'):
            extract(html(identity()+[{'type':'newGallery','image':{'url':'https://cms.landor.com/media/a/x.jpg'}}]),
                    'https://landor.com/en/our-work/example')

    def test_navigation_foreign_media_and_signed_links_rejected(self):
        for url in ('https://landor.com/assets/logo.svg','https://example.com/media/a/x.jpg',
                    'https://cms.landor.com/media/a/x.jpg?token=secret'):
            with self.assertRaises(ValueError):asset_url(url)
        with self.assertRaises(ValueError):canonical('https://landor.com/en/contact/')

    def test_cache_receipt_verifies_bytes_and_corruption_forces_download(self):
        with tempfile.TemporaryDirectory() as directory:
            url='https://cms.landor.com/media/a/test.jpg'
            record={'asset_url':url,'project_url':'https://landor.com/en/our-work/example'}
            def response(*args, **kwargs):
                data=io.BytesIO(b'original media bytes')
                data.url=url
                data.headers={'Content-Length':str(len(data.getvalue())),'Content-Type':'image/jpeg'}
                return data
            with patch('landor.urlopen',side_effect=response) as fetch:
                first=download(record,Path(directory))
                download(record,Path(directory))
                self.assertEqual(fetch.call_count,1)
                Path(first['local_path']).write_bytes(b'damaged')
                download(record,Path(directory))
                self.assertEqual(fetch.call_count,2)

    def test_media_html_response_retries_three_times_without_caching(self):
        with tempfile.TemporaryDirectory() as directory:
            url='https://cms.landor.com/media/a/test.mp4'
            def response(*args, **kwargs):
                data=io.BytesIO(b'<html>error</html>');data.url=url
                data.headers={'Content-Type':'text/html'};return data
            with patch('landor.urlopen',side_effect=response) as fetch, patch('landor.time.sleep'):
                with self.assertRaisesRegex(ValueError,'网页'):
                    download({'asset_url':url,'project_url':'https://landor.com/en/our-work/example'},Path(directory))
                self.assertEqual(fetch.call_count,3)
                self.assertFalse(list(Path(directory).glob('*.mp4')))

    def test_svg_heading_uses_explicit_cms_project_name(self):
        blocks=identity()
        blocks[1]['title']='<svg><path d="M0 0"/></svg>'
        blocks.append({'type':'caseStudyDetailImage','image':{'url':'https://cms.landor.com/media/a/image.jpg'}})
        records=extract(html(blocks,name='M&M’S'),'https://landor.com/en/our-work/example')
        self.assertEqual(records[0]['project_title'],'M&M’S')

    def test_literal_ampersand_brand_name_is_not_buffered_as_an_entity(self):
        self.assertEqual(plain("M&M'S"),"M&M'S")


if __name__ == '__main__':
    unittest.main()
