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
        self.assertIn('Groq가 2개 페이지를 추천했습니다. 근거 문장을 확인한 1개 페이지만', preview)
        self.assertIn('보고서 PDF 제출 조건', preview)
        self.assertIn('AI 추천 · 근거 미확인', preview)
        self.assertIn('3페이지는 AI가 제시한 근거 문장을 원문에서 찾지 못해', preview)
        self.assertEqual(preview.count('checked'), 1)  # unverified page 3 is shown but not preselected
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


    def test_pdf_pages_can_be_appended_to_an_existing_notice_once(self):
        """TC21: chosen PDF pages become a follow-up notice; items, completion and the first PDF stay intact."""
        import store
        import web
        assignment_id = store.save_pdf_import(self.new_import(), '기존 과제', ['2'])
        store.add_checklist_item(store.get_assignment(assignment_id), '보고서', '보고서는 PDF로 제출하세요.')
        item = store.list_checklist(assignment_id)[0]
        store.set_checklist_completed(assignment_id, item.id, True)
        count_before = len(store.list_assignments())

        from pdf_input import PdfDocument, PdfPage
        follow_up = store.create_pdf_import(PdfDocument(filename='추가안내.pdf', pages=[
            PdfPage(number=1, text='표지'), PdfPage(number=2, text='발표 자료는 PDF로 제출하세요.'),
        ]))
        preview = web.preview_pdf(self.request, follow_up).body.decode()
        self.assertIn(f'<option value="{assignment_id}" >기존 공지에 추가: 기존 과제</option>', preview)

        response = web.save_pdf_notice(self.request, follow_up, title='발표 안내', selected=['2'],
                                       append_to=str(assignment_id))
        self.assertEqual(response.status_code, 303)
        # Appending goes straight to the review of the updated notice.
        location = response.headers['location']
        self.assertIn(f'/notices/{assignment_id}/extractions/', location)
        self.assertTrue(location.endswith('?appended=1'))
        draft_id = UUID(location.split('/extractions/')[1].split('?')[0])
        review = web.review_extraction(self.request, assignment_id, draft_id, appended=1).body.decode()
        self.assertIn('추가 공지를 붙였습니다', review)
        self.assertIn('이미 체크리스트에 있음', review)  # '보고서는 PDF로 제출하세요.' is already saved
        draft = store.get_extraction_draft(assignment_id, draft_id)
        new_index = next(i for i, item in enumerate(draft.result.items) if '발표 자료' in item.evidence)
        old_index = next(i for i, item in enumerate(draft.result.items) if item.evidence == '보고서는 PDF로 제출하세요.')
        self.assertIn(f'id="select-{new_index}" name="selected" value="{new_index}" checked', review)
        self.assertNotIn(f'id="select-{old_index}" name="selected" value="{old_index}" checked', review)
        assignment = store.get_assignment(assignment_id)
        self.assertTrue(assignment.body.endswith('[추가 공지] 발표 안내 (추가안내.pdf 2페이지)\n발표 자료는 PDF로 제출하세요.'))
        self.assertEqual(len(store.list_assignments()), count_before)  # no new notice
        self.assertTrue(store.list_checklist(assignment_id)[0].completed)
        detail = web.notice_detail(self.request, assignment_id).body.decode()
        self.assertIn('<h3 class="appendix-heading">[추가 공지] 발표 안내 (추가안내.pdf 2페이지)</h3>', detail)
        self.assertIn('href="#pdf-page-2"', detail)  # original PDF page links still work
        # The appended sentence is a valid quote now, and its quote links to the appended PDF page.
        store.add_checklist_item(assignment, '발표 자료', '발표 자료는 PDF로 제출하세요.')
        detail = web.notice_detail(self.request, assignment_id).body.decode()
        self.assertIn('id="pdf-a1-page-2"', detail)
        self.assertIn('<a href="#pdf-a1-page-2">추가안내.pdf 2페이지</a>', detail)
        self.assertIn('<a href="#pdf-page-2">PDF 2페이지</a>', detail)  # the first PDF keeps its own links

        # Re-submitting the same preview does not append twice, and the preview now opens the notice.
        again = web.save_pdf_notice(self.request, follow_up, title='발표 안내', selected=['2'], append_to=str(assignment_id))
        self.assertEqual(again.status_code, 303)
        self.assertEqual(store.get_assignment(assignment_id).body.count('[추가 공지]'), 1)
        self.assertEqual(web.preview_pdf(self.request, follow_up).status_code, 303)

    def test_pdf_append_errors_keep_the_notice_unchanged(self):
        """TC21"""
        import store
        import web
        from fastapi import HTTPException
        target = store.create_assignment(store.AssignmentCreate(title='대상', body='가' * 29_990))
        import_id = self.new_import()
        response = web.save_pdf_notice(self.request, import_id, title='', selected=['2'], append_to=str(target.id))
        self.assertEqual(response.status_code, 422)
        self.assertIn('30,000자', response.body.decode())
        self.assertIn(f'value="{target.id}" selected', response.body.decode())
        response = web.save_pdf_notice(self.request, import_id, title='', selected=[], append_to=str(target.id))
        self.assertEqual(response.status_code, 422)
        with self.assertRaises(HTTPException) as caught:
            web.save_pdf_notice(self.request, import_id, title='', selected=['2'], append_to='999999999')
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(store.get_assignment(target.id).body, '가' * 29_990)
        # The preview is still unsaved and can be stored as a new notice.
        self.assertEqual(web.preview_pdf(self.request, import_id).status_code, 200)


    def test_pdf_appended_to_a_text_notice_gets_page_links(self):
        """TC22: quotes from an appended PDF show its file name and page, also on the review screen."""
        import store
        import web
        from pdf_input import PdfDocument, PdfPage
        assignment = store.create_assignment(store.AssignmentCreate(title='텍스트 공지', body='소스 코드는 ZIP으로 첨부하세요.'))
        first = store.create_pdf_import(PdfDocument(filename='1차.pdf', pages=[
            PdfPage(number=1, text='표지'), PdfPage(number=3, text='보고서는 PDF로 제출하세요.')]))
        second = store.create_pdf_import(PdfDocument(filename='2차.pdf', pages=[
            PdfPage(number=5, text='배포 주소를 포함하세요.')]))
        store.append_pdf_import(first, assignment.id, '', ['3'])
        store.append_pdf_import(second, assignment.id, '배포', ['5'])
        assignment = store.get_assignment(assignment.id)
        self.assertEqual([pdf.filename for pdf in assignment.appended_pdfs], ['1차.pdf', '2차.pdf'])
        segments = web.source_segments(assignment)
        self.assertEqual([segment['kind'] for segment in segments], ['text', 'pdf', 'pdf'])
        self.assertTrue(segments[0]['original'])

        response = web.generate_checklist(self.request, assignment.id, engine='basic')
        draft_id = UUID(response.headers['location'].rsplit('/', 1)[-1])
        review = web.review_extraction(self.request, assignment.id, draft_id).body.decode()
        self.assertIn('<a href="#pdf-a1-page-3">1차.pdf 3페이지</a>', review)
        self.assertIn('<a href="#pdf-a2-page-5">2차.pdf 5페이지</a>', review)
        self.assertNotIn('<h3>추가 공지</h3>', review)  # the original text is not labelled as a follow-up


if __name__ == '__main__':
    unittest.main()
