import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from extraction import (Candidate, ExtractionError, ModelOutput, ai_ready, extract_notice, validate_output)


class ExtractionTests(unittest.TestCase):
    def test_four_submission_requirements_with_exact_quotes(self):
        body = '보고서는 PDF로 제출하고, 배포 주소를 포함하세요.\r\n정상·실패 사례를 시연하세요.\r\nAI 사용 내역도 제출하세요.'
        result = extract_notice('프로젝트', body, 'basic')
        requirements = [item for item in result.items if item.kind == 'requirement']
        self.assertEqual([item.title for item in requirements], [
            '보고서 PDF 준비', '배포 주소 포함', '정상·실패 시연 준비', 'AI 사용 내역 준비'])
        self.assertTrue(all(item.evidence in body.replace('\r\n', '\n') for item in requirements))
        self.assertEqual(result.deadline_evidence, '')
        self.assertEqual(result.items[-1].kind, 'suggestion')
        self.assertEqual(result.items[-1].evidence, '')

    def test_optional_negative_and_example_are_not_requirements(self):
        body = '보고서 제출은 필요 없습니다.\n영상은 선택 제출입니다.\n예시: 배포 주소를 포함하세요.\nAI 내역은 제출하지 않아도 됩니다.'
        items = extract_notice('공지', body, 'basic').items
        # R02: optional/example sentences are never requirements; they are kept as notes with the exact quote.
        self.assertEqual([item.kind for item in items], ['note'] * 4)
        self.assertEqual([item.evidence for item in items], body.split('\n'))

    def test_mixed_sentence_becomes_note_and_requirements_stay_separate(self):
        body = '보고서는 PDF로 제출하세요.\n예시 코드를 참고하여 구현하고, 배포 주소를 포함하세요.\n마감은 추후 안내합니다.\n수업 소개입니다.'
        items = extract_notice('공지', body, 'basic').items
        self.assertEqual([(item.kind, item.evidence) for item in items if item.kind != 'suggestion'], [
            ('requirement', '보고서는 PDF로 제출하세요.'),
            ('note', '예시 코드를 참고하여 구현하고, 배포 주소를 포함하세요.'),
            ('note', '마감은 추후 안내합니다.'),
        ])

    def test_ungrounded_ai_note_is_removed(self):
        result = validate_output(ModelOutput(items=[
            Candidate(title='영상은 선택', evidence='영상은 선택입니다.', kind='note'),
            Candidate(title='없는 비고', evidence='원문에 없는 문장', kind='note'),
        ], deadline_evidence=''), '영상은 선택입니다.', 'ai')
        self.assertEqual([(item.kind, item.evidence) for item in result.items], [('note', '영상은 선택입니다.')])
        self.assertTrue(result.warnings)

    def test_unrecognized_action_remains_a_quote(self):
        body = '파일명에 학번을 기재하세요.'
        self.assertEqual(extract_notice('과제', body, 'basic').items[0].evidence, body)

    def test_pdf_does_not_imply_an_unmentioned_report(self):
        result = extract_notice('공지', '발표 자료는 PDF로 제출하세요.', 'basic')
        self.assertNotIn('보고서 PDF 준비', [item.title for item in result.items])
        self.assertIn('PDF 파일 준비', [item.title for item in result.items])

    def test_deadline_is_not_invented_or_inferred_from_event_date(self):
        for body in ['발표일: 2026-11-18\n보고서를 제출하세요.', '마감은 추후 안내합니다.', '11월 18일에 수업합니다.']:
            with self.subTest(body=body):
                self.assertEqual(extract_notice('공지', body, 'basic').deadline_evidence, '')

    def test_deadline_raw_date_is_preserved(self):
        for body in ['제출 기한: 2026. 11. 18. 23:59', '11월 18일까지 제출하세요.', '마감: 내일']:
            with self.subTest(body=body):
                result = extract_notice('공지', body, 'basic')
                self.assertEqual(result.deadline_evidence, body)
                self.assertEqual(result.items[0].kind, 'requirement')

    def test_ungrounded_ai_requirement_and_deadline_removed(self):
        result = validate_output(ModelOutput(items=[
            Candidate(title='PDF 준비', evidence='보고서는 PDF로 제출', kind='requirement'),
            Candidate(title='영상 제출', evidence='영상을 제출하세요.', kind='requirement'),
            Candidate(title='링크 확인', evidence='보고서는 PDF로 제출', kind='suggestion'),
        ], deadline_evidence='11월 20일까지 제출하세요.'), '보고서는 PDF로 제출', 'ai')
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[-1].evidence, '')
        self.assertEqual(result.deadline_evidence, '')
        self.assertEqual(len(result.warnings), 2)

    def test_length_limit_and_no_implicit_truncation(self):
        with self.assertRaises(ExtractionError):
            extract_notice('과제', '가' * 30001, 'basic')

    def test_repeated_sentence_is_deduplicated(self):
        result = extract_notice('과제', '보고서는 PDF로 제출하세요.\n보고서는 PDF로 제출하세요.', 'basic')
        self.assertEqual(len(result.items), 2)

    @patch.dict('os.environ', {'GROQ_API_KEY': ''})
    @patch('extraction.urlopen')
    def test_disabled_ai_never_calls_any_api(self, call):
        self.assertFalse(ai_ready())
        with self.assertRaisesRegex(ExtractionError, '연결 설정'):
            extract_notice('공지', '보고서를 제출하세요.', 'ai')
        call.assert_not_called()

    @patch.dict('os.environ', {'GROQ_API_KEY': 'test-key', 'OLLAMA_MODEL': 'qwen3:4b', 'AI_PROVIDER': 'ollama'})
    @patch('extraction.urlopen')
    def test_local_ai_engine_is_removed(self, call):
        # The local Ollama option was removed; old settings or form values cannot reach it.
        with self.assertRaisesRegex(ExtractionError, '지원하지 않는 추출 방식'):
            extract_notice('공지', '보고서를 제출하세요.', 'local')
        call.assert_not_called()

    @patch.dict('os.environ', {'GROQ_API_KEY': '', 'HF_TOKEN': 'old-test-key', 'HF_MODEL': 'old/model',
                              'AI_GATEWAY_API_KEY': 'old-test-key', 'AI_GATEWAY_MODEL': 'old/model'})
    @patch('extraction.urlopen')
    def test_old_paid_settings_do_not_enable_ai_or_make_requests(self, call):
        self.assertFalse(ai_ready())
        with self.assertRaises(ExtractionError):
            extract_notice('공지', '보고서를 제출하세요.', 'ai')
        call.assert_not_called()

    def test_old_local_drafts_remain_readable(self):
        from extraction import ExtractionResult
        result = ExtractionResult.model_validate({'items': [], 'deadline_evidence': '', 'engine': 'ai', 'provider': 'ollama'})
        self.assertEqual(result.provider, 'ollama')

    def test_old_drafts_remain_readable(self):
        from extraction import ExtractionResult
        result = ExtractionResult.model_validate({'items': [], 'deadline_evidence': '', 'engine': 'ai'})
        self.assertIsNone(result.provider)


if __name__ == '__main__':
    unittest.main()
