import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from extraction import (Candidate, ExtractionError, ModelOutput, extract_notice, validate_output)


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
        self.assertEqual(extract_notice('공지', body, 'basic').items, [])

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

    @patch.dict('os.environ', {'AI_GATEWAY_API_KEY': '', 'AI_GATEWAY_MODEL': ''})
    def test_ai_without_config_does_not_pretend_to_be_ai(self):
        with self.assertRaisesRegex(ExtractionError, '연결 설정'):
            extract_notice('공지', '보고서를 제출하세요.', 'ai')

    @patch.dict('os.environ', {'AI_GATEWAY_API_KEY': 'test-secret', 'AI_GATEWAY_MODEL': 'test/model'})
    @patch('extraction.urlopen')
    def test_gateway_payload_and_grounded_response(self, call):
        output = {'items': [{'title': '보고서 준비', 'evidence': '보고서를 제출하세요.', 'kind': 'requirement'}], 'deadline_evidence': ''}
        call.return_value = io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}).encode())
        result = extract_notice('공지', '보고서를 제출하세요.', 'ai')
        self.assertEqual(result.engine, 'ai')
        request = call.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(payload['response_format']['type'], 'json_schema')
        self.assertEqual(payload['model'], 'test/model')
        self.assertEqual(json.loads(payload['messages'][1]['content'])['body'], '보고서를 제출하세요.')

    @patch.dict('os.environ', {'AI_GATEWAY_API_KEY': 'test-secret', 'AI_GATEWAY_MODEL': 'test/model'})
    @patch('extraction.urlopen')
    def test_gateway_failure_never_silently_falls_back(self, call):
        for error in [URLError('offline'), HTTPError('https://example.com', 401, 'unauthorized', {}, None),
                      HTTPError('https://example.com', 429, 'limited', {}, None)]:
            with self.subTest(error=type(error).__name__):
                call.side_effect = error
                with self.assertRaises(ExtractionError) as caught:
                    extract_notice('공지', '보고서를 제출하세요.', 'ai')
                self.assertNotIn('test-secret', str(caught.exception))

    @patch.dict('os.environ', {'AI_GATEWAY_API_KEY': 'test-secret', 'AI_GATEWAY_MODEL': 'test/model'})
    @patch('extraction.urlopen')
    def test_invalid_or_partial_gateway_results_are_not_saved(self, call):
        for raw in [b'not json', json.dumps({'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}).encode()]:
            with self.subTest(raw=raw):
                call.side_effect = None
                call.return_value = io.BytesIO(raw)
                with self.assertRaises(ExtractionError):
                    extract_notice('공지', '보고서를 제출하세요.', 'ai')


if __name__ == '__main__':
    unittest.main()
