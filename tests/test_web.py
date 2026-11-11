"""Run against an isolated Docker stack using CHECKLIST_TEST_URL."""

import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class ReviewForm(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = []
        self.textarea = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        name = attrs.get('name')
        if tag == 'input' and name in ('titles', 'evidences', 'selected'):
            if name != 'selected' or 'checked' in attrs:
                self.values.append((name, attrs.get('value', '')))
        if tag == 'textarea' and name == 'evidences':
            self.textarea = ''

    def handle_data(self, data):
        if self.textarea is not None:
            self.textarea += data

    def handle_endtag(self, tag):
        if tag == 'textarea' and self.textarea is not None:
            self.values.append(('evidences', self.textarea))
            self.textarea = None


@unittest.skipUnless(os.getenv('CHECKLIST_TEST_URL'), 'requires isolated CHECKLIST_TEST_URL')
class WebFlowTests(unittest.TestCase):
    def request(self, route, data=None, *, json_data=None):
        headers = {}
        if json_data is not None:
            payload = json.dumps(json_data).encode()
            headers['Content-Type'] = 'application/json'
        else:
            payload = None if data is None else urlencode(data).encode()
        request = Request(os.environ['CHECKLIST_TEST_URL'] + route, data=payload, headers=headers)
        opener = build_opener(NoRedirect)
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read().decode(), response.headers

    def create_notice(self, body='보고서는 PDF로 제출하고, 배포 주소를 포함하세요.\r\n정상·실패 사례를 시연하세요.\r\nAI 사용 내역도 제출하세요.'):
        code, raw, _ = self.request('/assignments', json_data={'title': '자동 추출 검증', 'body': body})
        self.assertEqual(code, 201, raw)
        return json.loads(raw)['id']

    def generate(self, notice_id):
        code, _, headers = self.request(f'/notices/{notice_id}/extract', [('engine', 'basic')])
        self.assertEqual(code, 303)
        route = urlsplit(headers['Location']).path
        code, html, _ = self.request(route)
        self.assertEqual(code, 200, html)
        return route, html, ReviewForm(html).values

    def detail(self, notice_id):
        return self.request(f'/notices/{notice_id}')[1]

    def test_edit_exclude_suggestion_and_persist_completion(self):
        notice_id = self.create_notice()
        route, preview, values = self.generate(notice_id)
        self.assertIn('미기재', preview)
        self.assertIn('기본 추출 결과 · AI 미사용', preview)
        self.assertNotIn('class="checklist-item"', self.detail(notice_id))
        self.assertEqual([v for k, v in values if k == 'selected'], ['0', '1', '2', '3'])
        self.assertEqual(len([v for k, v in values if k == 'evidences']), 5)
        changed = []
        title_index = 0
        for key, value in values:
            if key == 'selected' and value == '2':
                continue
            if key == 'titles':
                if title_index == 0:
                    value = '최종 보고서 PDF 준비'
                title_index += 1
            changed.append((key, value))
        code, raw, _ = self.request(route + '/save', changed)
        self.assertEqual(code, 303, raw)
        detail = self.detail(notice_id)
        self.assertEqual(detail.count('class="checklist-item"'), 3)
        self.assertIn('최종 보고서 PDF 준비', detail)
        self.assertNotIn('준비 제안</span>', detail)
        self.assertIn('미기재', detail)
        import re
        completion = re.search(r'action="([^" ]+/completion)"', detail).group(1)
        code, _, _ = self.request(urlsplit(completion).path, [('completed', 'true')])
        self.assertEqual(code, 303)
        self.assertIn('1/3 완료', self.detail(notice_id))

    def test_bad_evidence_aborts_entire_save_and_preserves_edits(self):
        notice_id = self.create_notice()
        route, _, values = self.generate(notice_id)
        changed = []
        first = True
        for key, value in values:
            if key == 'evidences' and first:
                value, first = '<script>근거 없음</script>', False
            changed.append((key, value))
        code, html, _ = self.request(route + '/save', changed)
        self.assertEqual(code, 422)
        self.assertIn('원문에서 그대로 복사', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>근거 없음</script>', html)
        self.assertEqual(self.detail(notice_id).count('class="checklist-item"'), 0)
        self.assertEqual(self.request(route + '/save', values)[0], 303)

    def test_no_selection_and_invalid_ids_do_not_save(self):
        notice_id = self.create_notice()
        route, _, values = self.generate(notice_id)
        none_selected = [(k, v) for k, v in values if k != 'selected']
        self.assertEqual(self.request(route + '/save', none_selected)[0], 422)
        self.assertEqual(self.request(route + '/save', none_selected + [('selected', '-1')])[0], 422)
        self.assertEqual(self.detail(notice_id).count('class="checklist-item"'), 0)

    def test_repeat_and_concurrent_saves_are_deduplicated(self):
        notice_id = self.create_notice()
        route, _, values = self.generate(notice_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            codes = list(pool.map(lambda _: self.request(route + '/save', values)[0], range(2)))
        self.assertEqual(codes, [303, 303])
        self.assertEqual(self.detail(notice_id).count('class="checklist-item"'), 4)
        route2, _, values2 = self.generate(notice_id)
        self.assertEqual(self.request(route2 + '/save', values2)[0], 303)
        self.assertEqual(self.detail(notice_id).count('class="checklist-item"'), 4)
        self.assertEqual(self.request(route)[0], 303)

    def test_cannot_access_other_notice_draft(self):
        notice_id, other_id = self.create_notice(), self.create_notice()
        route, _, values = self.generate(notice_id)
        other_route = route.replace(f'/notices/{notice_id}/', f'/notices/{other_id}/')
        self.assertEqual(self.request(other_route)[0], 404)
        self.assertEqual(self.request(other_route + '/save', values)[0], 404)

    def test_deadline_and_selected_suggestion(self):
        notice_id = self.create_notice('제출 기한: 2026-11-18 23:59\n보고서는 PDF로 제출하세요.')
        route, html, values = self.generate(notice_id)
        self.assertIn('2026-11-18 23:59', html)
        values.append(('selected', '2'))
        self.assertEqual(self.request(route + '/save', values)[0], 303)
        detail = self.detail(notice_id)
        self.assertIn('준비 제안</span>', detail)
        self.assertIn('공지의 필수 조건이 아닌', detail)
        raw = json.loads(self.request(f'/assignments/{notice_id}')[1])
        self.assertEqual(raw['deadline_evidence'], '제출 기한: 2026-11-18 23:59')

    def test_empty_and_missing_ai_have_clear_messages(self):
        notice_id = self.create_notice('이번 주 수업은 강의실에서 진행합니다.')
        _, html, values = self.generate(notice_id)
        self.assertIn('추출할 제출 조건을 찾지 못했습니다', html)
        self.assertEqual(values, [])
        code, html, _ = self.request(f'/notices/{notice_id}/extract', [('engine', 'ai')])
        self.assertEqual(code, 422)
        self.assertIn('AI 연결 설정이 없습니다', html)
        self.assertEqual(self.request(f'/notices/{notice_id}/extract', [('engine', 'unknown')])[0], 422)


if __name__ == '__main__':
    unittest.main()
