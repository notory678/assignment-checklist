"""Preparation details through HTTP and a disposable real PostgreSQL database."""

import json
import os
import unittest
from html.parser import HTMLParser
from urllib.parse import urlsplit

import test_web as web_helpers
import test_pdf_web as pdf_helpers


class PreparationForms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms = {}
        self.current = None
        self.note = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form' and attrs.get('action', '').endswith('/preparation'):
            self.current = {'route': urlsplit(attrs['action']).path}
        if self.current is not None:
            if tag == 'input' and attrs.get('name') == 'submission_url':
                self.current['submission_url'] = attrs.get('value', '')
            if tag == 'textarea' and attrs.get('name') == 'preparation_note':
                self.note = ''

    def handle_data(self, data):
        if self.note is not None:
            self.note += data

    def handle_endtag(self, tag):
        if tag == 'textarea' and self.note is not None:
            self.current['preparation_note'], self.note = self.note, None
        if tag == 'form' and self.current is not None:
            self.forms[self.current['route']] = self.current
            self.current = None


@unittest.skipUnless(os.getenv('CHECKLIST_TEST_URL'), 'requires isolated CHECKLIST_TEST_URL')
class PreparationWebTests(unittest.TestCase):
    request = web_helpers.WebFlowTests.request
    create_notice = web_helpers.WebFlowTests.create_notice
    generate = web_helpers.WebFlowTests.generate
    detail = web_helpers.WebFlowTests.detail
    upload = pdf_helpers.PdfWebTests.upload
    new_import = pdf_helpers.PdfWebTests.new_import
    save_import = pdf_helpers.PdfWebTests.save_import

    def new_checklist(self):
        notice_id = self.create_notice()
        route, _, values = self.generate(notice_id)
        self.assertEqual(self.request(route + '/save', values)[0], 303)
        forms = PreparationForms(self.detail(notice_id)).forms
        self.assertEqual(len(forms), 4)
        return notice_id, next(iter(forms))

    def save(self, route, url='', note=''):
        return self.request(route, [('submission_url', url), ('preparation_note', note)])

    def stored(self, notice_id, route):
        return PreparationForms(self.detail(notice_id)).forms[route]

    def test_save_reload_update_completion_and_clear_optional_details(self):
        notice_id, route = self.new_checklist()
        url = 'https://example.test/my-app?task=1&mode=demo'
        note = '  보고서 위치: 문서/보고서.pdf\r\n발표 전에 최종 확인  '
        code, html, headers = self.save(route, ' ' + url + ' ', note)
        self.assertEqual(code, 303, html)
        self.assertIn('#checklist-item-', headers['Location'])
        success_url = urlsplit(headers['Location'])
        success = self.request(success_url.path + '?' + success_url.query)[1]
        self.assertIn('준비 내용을 저장했습니다.', success)
        self.assertIn('내가 준비한 내용', success)
        self.assertIn('rel="noopener noreferrer"', success)
        stored = self.stored(notice_id, route)
        self.assertEqual(stored['submission_url'], url)
        self.assertEqual(stored['preparation_note'], '보고서 위치: 문서/보고서.pdf\n발표 전에 최종 확인')
        self.assertIn('0/4 완료', success)
        self.assertEqual(self.request(route.replace('/preparation', '/completion'), [('completed', 'true')])[0], 303)
        self.assertEqual(self.stored(notice_id, route), stored)
        self.assertIn('1/4 완료', self.detail(notice_id))
        for url, note in [('http://127.0.0.1:8001/', ''), ('', '파일 위치만 기록'), ('', '')]:
            self.assertEqual(self.save(route, url, note)[0], 303)
            saved = self.stored(notice_id, route)
            self.assertEqual((saved['submission_url'], saved['preparation_note']), (url, note))
        self.assertIn('1/4 완료', self.detail(notice_id))

    def test_invalid_urls_preserve_input_and_leave_saved_details_unchanged(self):
        notice_id, route = self.new_checklist()
        self.assertEqual(self.save(route, 'https://example.test/saved', '기존 메모')[0], 303)
        before = self.stored(notice_id, route)
        bad_urls = ['javascript:alert(1)', 'data:text/html,bad', 'file:///tmp/report.pdf', 'https://',
                    '//example.test', 'https:example.test', 'https://user:secret@example.test/',
                    'https://example.test/a\nb', 'https://example.test/a\\b', 'https://example.test/' + 'a' * 2048]
        for url in bad_urls:
            with self.subTest(url=url[:60]):
                code, html, _ = self.save(route, url, '수정 중인 메모')
                self.assertEqual(code, 422, html)
                self.assertIn('수정 중인 메모', html)
                self.assertIn('<details class="preparation-editor" open>', html)
                self.assertEqual(self.stored(notice_id, route), before)

    def test_note_limits_are_atomic_and_markup_is_displayed_as_text(self):
        notice_id, route = self.new_checklist()
        note = '</textarea><script>alert("test")</script>\n시연 순서: 정상 → 실패'
        self.assertEqual(self.save(route, 'https://example.test/safe', note)[0], 303)
        html = self.detail(notice_id)
        self.assertNotIn('<script>alert("test")</script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertEqual(self.stored(notice_id, route)['preparation_note'], note)
        before = self.stored(notice_id, route)
        for bad_note in ['가' * 4001, '메모\x00오류']:
            code, _, _ = self.save(route, 'https://example.test/changed', bad_note)
            self.assertEqual(code, 422)
            self.assertEqual(self.stored(notice_id, route), before)
        self.assertEqual(self.save(route, '', '가' * 4000)[0], 303)
        self.assertEqual(len(self.stored(notice_id, route)['preparation_note']), 4000)

    def test_wrong_notice_or_missing_item_cannot_change_saved_details(self):
        notice_id, route = self.new_checklist()
        other_id = self.create_notice()
        self.assertEqual(self.save(route, '', '원래 항목의 메모')[0], 303)
        before = self.stored(notice_id, route)
        wrong_route = route.replace(f'/notices/{notice_id}/', f'/notices/{other_id}/')
        self.assertEqual(self.save(wrong_route, '', '덮어쓰기 시도')[0], 404)
        missing_route = f'/notices/{notice_id}/checklist/999999999/preparation'
        self.assertEqual(self.save(missing_route, '', '없음')[0], 404)
        self.assertEqual(self.stored(notice_id, route), before)

    def test_other_items_and_repeated_extraction_keep_their_details(self):
        notice_id, route = self.new_checklist()
        self.assertEqual(self.save(route, 'https://example.test/ready', '보존할 준비 내용')[0], 303)
        self.assertEqual(self.request(route.replace('/preparation', '/completion'), [('completed', 'true')])[0], 303)
        forms_before = PreparationForms(self.detail(notice_id)).forms
        self.assertTrue(all(not form['preparation_note'] for path, form in forms_before.items() if path != route))
        draft_route, _, values = self.generate(notice_id)
        self.assertEqual(self.request(draft_route + '/save', values)[0], 303)
        self.assertEqual(PreparationForms(self.detail(notice_id)).forms, forms_before)
        self.assertIn('1/4 완료', self.detail(notice_id))

    def test_pdf_evidence_and_page_locations_remain_after_preparation_save(self):
        import_route = self.new_import()
        code, _, headers = self.save_import(import_route)
        self.assertEqual(code, 303)
        notice_id = int(headers['Location'].rsplit('/', 1)[1])
        draft_route, _, values = self.generate(notice_id)
        self.assertEqual(self.request(draft_route + '/save', values)[0], 303)
        before = json.loads(self.request(f'/assignments/{notice_id}')[1])
        route = next(iter(PreparationForms(self.detail(notice_id)).forms))
        self.assertEqual(self.save(route, '', '보고서 파일 위치 기록')[0], 303)
        html = self.detail(notice_id)
        self.assertIn('href="#pdf-page-1"', html)
        self.assertIn('href="#pdf-page-3"', html)
        self.assertIn('보고서는 PDF로 제출하세요.', html)
        self.assertEqual(json.loads(self.request(f'/assignments/{notice_id}')[1]), before)


if __name__ == '__main__':
    unittest.main()
