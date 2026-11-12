"""PDF page recommendation, notes and follow-up notices through web handlers and an isolated DB.

Only enable CHECKLIST_DB_TESTS in a disposable test stack. Groq responses are mocked.
"""

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import UUID


def groq_reply(content):
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}]}).encode())


@unittest.skipUnless(os.getenv('CHECKLIST_DB_TESTS') == '1', 'requires isolated CHECKLIST_DB_TESTS=1')
class FeatureFlowTests(unittest.TestCase):
    def setUp(self):
        import store
        from main import app
        from starlette.requests import Request

        store.initialize_database()
        self.request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/',
                                'headers': [], 'server': ('localhost', 8000), 'router': app.router})

    def new_import(self):
        import store
        from pdf_input import PdfDocument, PdfPage
        return store.create_pdf_import(PdfDocument(filename='강의자료.pdf', pages=[
            PdfPage(number=1, text='1주차 수업 소개'),
            PdfPage(number=2, text='과제 안내\n보고서는 PDF로 제출하세요.'),
            PdfPage(number=3, text='참고 문헌'),
        ]))

    @patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'fake-key', 'GROQ_MODEL': 'openai/gpt-oss-20b'})
    def test_recommended_pages_are_preselected_and_saved_after_confirmation(self):
        """TC17 (R08)"""
        import store
        import web
        import_id = self.new_import()
        before = web.preview_pdf(self.request, import_id).body.decode()
        self.assertIn('AI 페이지 추천 (Groq)', before)
        self.assertEqual(before.count('checked'), 3)  # without a recommendation every readable page is selected

        reply = groq_reply({'pages': [
            {'number': 2, 'summary': '보고서 PDF 제출 조건', 'evidence': '보고서는 PDF로 제출하세요.'},
            {'number': 3, 'summary': '지어낸 추천', 'evidence': '영상을 제출하세요.'},
        ]})
        with patch('extraction.urlopen', return_value=reply) as call:
            response = web.recommend_pdf(self.request, import_id)
        self.assertEqual(response.status_code, 303)
        call.assert_called_once()
        preview = web.preview_pdf(self.request, import_id).body.decode()
        self.assertIn('Groq가 1개 페이지를 추천했습니다', preview)
        self.assertIn('보고서 PDF 제출 조건', preview)
        self.assertIn('근거 문장을 찾을 수 없는 추천을 제외했습니다', preview)
        self.assertEqual(preview.count('checked'), 1)
        self.assertIn('value="2" data-text-length', preview.split('checked')[0][-200:])

        assignment_id = store.save_pdf_import(import_id, '강의 과제', ['2'])
        self.assertEqual(store.get_assignment(assignment_id).body, '과제 안내\n보고서는 PDF로 제출하세요.')
        # Once saved, the recommendation route goes to the notice and never calls Groq again.
        with patch('extraction.urlopen') as call:
            self.assertEqual(web.recommend_pdf(self.request, import_id).status_code, 303)
        call.assert_not_called()

    @patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'fake-key', 'GROQ_MODEL': 'openai/gpt-oss-20b'})
    def test_recommendation_failure_keeps_manual_selection(self):
        """TC17 (R06, R08)"""
        import web
        import_id = self.new_import()
        error = HTTPError('https://api.groq.com/openai/v1/chat/completions', 429, 'error', {}, None)
        with patch('extraction.urlopen', side_effect=error):
            response = web.recommend_pdf(self.request, import_id)
        self.assertEqual(response.status_code, 422)
        self.assertIn('무료 사용 한도', response.body.decode())
        self.assertEqual(web.preview_pdf(self.request, import_id).body.decode().count('checked'), 3)

    @patch.dict('os.environ', {'GROQ_API_KEY': ''})
    def test_recommendation_button_hidden_without_key(self):
        import web
        preview = web.preview_pdf(self.request, self.new_import()).body.decode()
        self.assertNotIn('AI 페이지 추천 (Groq)', preview)
        self.assertIn('Groq API 키를 설정하면', preview)

    def test_notes_are_unselected_by_default_and_can_be_saved(self):
        """TC18 (R02)"""
        import store
        import web
        assignment = store.create_assignment(store.AssignmentCreate(
            title='비고 검증', body='보고서는 PDF로 제출하세요.\n시연 영상은 선택 제출입니다.'))
        response = web.generate_checklist(self.request, assignment.id, engine='basic')
        draft_id = UUID(response.headers['location'].rsplit('/', 1)[-1])
        review = web.review_extraction(self.request, assignment.id, draft_id).body.decode()
        self.assertIn('비고 · 선택·예시·참고', review)
        draft = store.get_extraction_draft(assignment.id, draft_id)
        kinds = [item.kind for item in draft.result.items]
        self.assertEqual(kinds, ['requirement', 'note', 'suggestion'])
        self.assertIn('id="select-0" name="selected" value="0" checked', review)
        self.assertNotIn('value="1" checked', review)

        titles = [item.title for item in draft.result.items]
        evidences = [item.evidence for item in draft.result.items]
        response = web.save_review(self.request, assignment.id, draft_id, selected=['0', '1'],
                                   titles=titles, evidences=evidences)
        self.assertEqual(response.status_code, 303)
        items = store.list_checklist(assignment.id)
        self.assertEqual([(item.kind, item.evidence) for item in items],
                         [('requirement', '보고서는 PDF로 제출하세요.'), ('note', '시연 영상은 선택 제출입니다.')])
        self.assertIn('비고 · 선택·예시·참고', web.notice_detail(self.request, assignment.id).body.decode())

    def test_follow_up_text_on_a_pdf_notice_is_shown_and_pages_still_link(self):
        """TC16 with a PDF notice"""
        import store
        import web
        assignment_id = store.save_pdf_import(self.new_import(), 'PDF 과제', ['2'])
        store.add_checklist_item(store.get_assignment(assignment_id), '보고서', '보고서는 PDF로 제출하세요.')
        store.append_notice(assignment_id, '마감 안내', '11월 20일까지 제출하세요.')
        detail = web.notice_detail(self.request, assignment_id).body.decode()
        self.assertIn('<h3>추가 공지</h3>', detail)
        self.assertIn('[추가 공지] 마감 안내\n11월 20일까지 제출하세요.', detail)
        self.assertIn('href="#pdf-page-2"', detail)


if __name__ == '__main__':
    unittest.main()
