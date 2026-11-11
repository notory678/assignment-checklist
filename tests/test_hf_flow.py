"""Exercise Hugging Face extraction through web handlers and an isolated real DB.

Only enable CHECKLIST_DB_TESTS in a disposable test stack. No live HF calls occur.
"""

import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from uuid import UUID


@unittest.skipUnless(os.getenv('CHECKLIST_DB_TESTS') == '1', 'requires isolated CHECKLIST_DB_TESTS=1')
class HuggingFaceFlowTests(unittest.TestCase):
    def test_hf_result_review_save_completion_and_failure(self):
        import store
        import web
        from main import app
        from starlette.requests import Request

        store.initialize_database()
        assignment = store.create_assignment(store.AssignmentCreate(
            title='Hugging Face 연동 검증', body='보고서를 PDF로 제출하세요.\n제출 기한: 11월 18일',
        ))
        request = Request({'type': 'http', 'method': 'POST', 'scheme': 'http', 'path': '/',
                           'headers': [], 'server': ('localhost', 8000), 'router': app.router})
        output = {'items': [
            {'title': '보고서 PDF 준비', 'evidence': '보고서를 PDF로 제출하세요.', 'kind': 'requirement'},
            {'title': '영상 제출', 'evidence': '영상을 제출하세요.', 'kind': 'requirement'},
            {'title': '파일 확인', 'evidence': '', 'kind': 'suggestion'},
        ], 'deadline_evidence': '제출 기한: 11월 18일'}
        raw = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}).encode()
        with patch.dict('os.environ', {'HF_TOKEN': 'hf-test-token', 'HF_MODEL': 'test/model:provider'}):
            detail = web.render_detail(request, assignment).body.decode()
            self.assertIn('AI 자동 추출 · Hugging Face', detail)
            self.assertIn('선택한 모델 제공업체에 전송', detail)
            self.assertNotIn('hf-test-token', detail)

            with patch('extraction.urlopen', return_value=io.BytesIO(raw)):
                response = web.generate_checklist(request, assignment.id, engine='ai')
            self.assertEqual(response.status_code, 303)
            draft_id = UUID(response.headers['location'].rsplit('/', 1)[-1])
            draft = store.get_extraction_draft(assignment.id, draft_id)
            self.assertEqual(draft.result.engine, 'ai')
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
            with patch('extraction.urlopen', side_effect=HTTPError('https://router.huggingface.co/v1/chat/completions', 403, 'forbidden', {}, None)):
                failure = web.generate_checklist(request, assignment.id, engine='ai')
            self.assertEqual(failure.status_code, 422)
            self.assertIn('호출 권한', failure.body.decode())
            self.assertEqual(len(store.list_checklist(assignment.id)), 2)
            with store.database() as connection:
                after = connection.execute('SELECT COUNT(*) AS count FROM extraction_drafts WHERE assignment_id = %s',
                                           (assignment.id,)).fetchone()['count']
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
