"""Exercise local Ollama extraction through web handlers and an isolated real DB.

Only enable CHECKLIST_DB_TESTS in a disposable test stack. Model responses are mocked.
"""

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import UUID


@unittest.skipUnless(os.getenv('CHECKLIST_DB_TESTS') == '1', 'requires isolated CHECKLIST_DB_TESTS=1')
class LocalAIFlowTests(unittest.TestCase):
    def test_pdf_ai_evidence_pages_are_derived_again_after_review_edits(self):
        import store
        import web
        from main import app
        from pdf_input import PdfDocument, PdfPage
        from starlette.requests import Request

        store.initialize_database()
        import_id = store.create_pdf_import(PdfDocument(filename='과제.pdf', pages=[
            PdfPage(number=2, text='보고서를 PDF로 제출하세요.'),
            PdfPage(number=4, text='배포 주소를 포함하세요.'),
        ]))
        assignment_id = store.save_pdf_import(import_id, 'PDF AI 검증', ['2', '4'])
        request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/',
                           'headers': [], 'server': ('localhost', 8000), 'router': app.router})
        output = {'items': [{'title': '보고서 준비', 'evidence': '보고서를 PDF로 제출하세요.', 'kind': 'requirement'}], 'deadline_evidence': ''}
        raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}).encode()
        with patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'fake-key'}), patch('extraction.urlopen', return_value=io.BytesIO(raw)):
            response = web.generate_checklist(request, assignment_id, engine='ai')
        draft_id = UUID(response.headers['location'].rsplit('/', 1)[-1])
        review = web.review_extraction(request, assignment_id, draft_id).body.decode()
        self.assertIn('href="#pdf-page-2"', review)
        response = web.save_review(request, assignment_id, draft_id, selected=['0'],
                                   titles=['주소 준비'], evidences=['배포 주소를 포함하세요.'])
        self.assertEqual(response.status_code, 303)
        detail = web.notice_detail(request, assignment_id).body.decode()
        self.assertIn('href="#pdf-page-4"', detail)
        self.assertNotIn('href="#pdf-page-2"', detail)

    def test_groq_result_review_save_and_local_backup(self):
        import store
        import web
        from main import app
        from starlette.requests import Request

        store.initialize_database()
        assignment = store.create_assignment(store.AssignmentCreate(title='Groq 연동 검증', body='PDF로 제출하세요.'))
        request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/',
                           'headers': [], 'server': ('localhost', 8000), 'router': app.router})
        output = {'items': [{'title': 'PDF 준비', 'evidence': 'PDF로 제출하세요.', 'kind': 'requirement'}], 'deadline_evidence': ''}
        raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}).encode()
        with patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'fake-key', 'GROQ_MODEL': 'openai/gpt-oss-20b', 'OLLAMA_MODEL': 'qwen3:4b'}):
            with patch('extraction.urlopen', return_value=io.BytesIO(raw)):
                response = web.generate_checklist(request, assignment.id, engine='ai')
            draft_id = UUID(response.headers['location'].rsplit('/', 1)[-1])
            draft = store.get_extraction_draft(assignment.id, draft_id)
            self.assertEqual(draft.result.provider, 'groq')
            self.assertIn('Groq', web.review_extraction(request, assignment.id, draft_id).body.decode())
            response = web.save_review(request, assignment.id, draft_id, selected=['0'], titles=['PDF 준비'], evidences=['PDF로 제출하세요.'])
            self.assertEqual(response.status_code, 303)
            self.assertEqual(len(store.list_checklist(assignment.id)), 1)
            with patch('extraction.urlopen', side_effect=HTTPError('https://api.groq.com', 429, 'limited', {}, None)):
                response = web.generate_checklist(request, assignment.id, engine='ai')
            html = response.body.decode()
            self.assertEqual(response.status_code, 422)
            self.assertIn('무료 사용 한도', html)
            self.assertIn('로컬 AI로 생성', html)
            self.assertNotIn('fake-key', html)
            self.assertEqual(len(store.list_checklist(assignment.id)), 1)

    def test_local_result_review_save_completion_and_failure(self):
        import store
        import web
        from main import app
        from starlette.requests import Request

        store.initialize_database()
        assignment = store.create_assignment(store.AssignmentCreate(
            title='local Ollama 연동 검증', body='보고서를 PDF로 제출하세요.\n제출 기한: 11월 18일',
        ))
        request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/',
                           'headers': [], 'server': ('localhost', 8000), 'router': app.router})
        output = {'items': [
            {'title': '보고서 PDF 준비', 'evidence': '보고서를 PDF로 제출하세요.', 'kind': 'requirement'},
            {'title': '영상 제출', 'evidence': '영상을 제출하세요.', 'kind': 'requirement'},
            {'title': '파일 확인', 'evidence': '', 'kind': 'suggestion'},
        ], 'deadline_evidence': '제출 기한: 11월 18일'}
        raw = json.dumps({'done': True, 'done_reason': 'stop', 'message': {'content': json.dumps(output)}}).encode()
        with patch.dict('os.environ', {'AI_PROVIDER': 'ollama', 'OLLAMA_BASE_URL': 'http://127.0.0.1:11434', 'OLLAMA_MODEL': 'qwen3:4b'}):
            detail = web.render_detail(request, assignment).body.decode()
            self.assertIn('무료 로컬 AI · Ollama', detail)
            self.assertIn('이 컴퓨터에서 공지를 분석', detail)
            self.assertNotIn('hf-test-token', detail)

            with patch('extraction.urlopen', return_value=io.BytesIO(raw)):
                response = web.generate_checklist(request, assignment.id, engine='ai')
            self.assertEqual(response.status_code, 303)
            draft_id = UUID(response.headers['location'].rsplit('/', 1)[-1])
            draft = store.get_extraction_draft(assignment.id, draft_id)
            self.assertEqual(draft.result.engine, 'ai')
            self.assertEqual(draft.result.provider, 'ollama')
            self.assertEqual(len(draft.result.items), 2)
            self.assertTrue(draft.result.warnings)
            self.assertEqual(store.list_checklist(assignment.id), [])
            review = web.review_extraction(request, assignment.id, draft_id).body.decode()
            self.assertIn('AI 추출 결과', review)
            self.assertIn('AI 제안', review)
            saved = web.save_review(request, assignment.id, draft_id, selected=['0', '1'],
                                    titles=[item.title for item in draft.result.items],
                                    evidences=[item.evidence for item in draft.result.items])
            self.assertEqual(saved.status_code, 303)
            items = store.list_checklist(assignment.id)
            self.assertEqual(len(items), 2)
            self.assertTrue(all(item.extraction_engine == 'ai' for item in items))
            self.assertEqual(items[1].kind, 'suggestion')
            self.assertEqual(items[1].evidence, '')
            self.assertEqual(store.get_assignment(assignment.id).deadline_evidence, '제출 기한: 11월 18일')
            store.set_checklist_completed(assignment.id, items[0].id, True)
            self.assertTrue(store.list_checklist(assignment.id)[0].completed)

            with store.database() as connection:
                before = connection.execute('SELECT COUNT(*) AS count FROM extraction_drafts WHERE assignment_id = %s',
                                            (assignment.id,)).fetchone()['count']
            with patch('extraction.urlopen', side_effect=HTTPError('http://localhost:11434/api/chat', 404, 'missing', {}, None)):
                failure = web.generate_checklist(request, assignment.id, engine='ai')
            self.assertEqual(failure.status_code, 422)
            self.assertIn('모델이 준비되어 있지', failure.body.decode())
            self.assertEqual(len(store.list_checklist(assignment.id)), 2)
            with store.database() as connection:
                after = connection.execute('SELECT COUNT(*) AS count FROM extraction_drafts WHERE assignment_id = %s',
                                           (assignment.id,)).fetchone()['count']
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
