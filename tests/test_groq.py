import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from extraction import (ExtractionError, ModelOutput, PageRecommendationOutput, RECOMMEND_TOTAL_CHARS,
                        ai_ready, extract_notice, locate_quote, recommend_pdf_pages)


@patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'test-key', 'GROQ_MODEL': 'openai/gpt-oss-20b'})
class GroqTests(unittest.TestCase):
    @patch('extraction.urlopen')
    def test_structured_request_and_grounded_result(self, call):
        output = {'items': [
            {'title': 'PDF 준비', 'evidence': 'PDF로 제출하세요.', 'kind': 'requirement'},
            {'title': '영상 준비', 'evidence': '영상을 제출하세요.', 'kind': 'requirement'},
        ], 'deadline_evidence': ''}
        call.return_value = io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(output)}}]}).encode())
        result = extract_notice('과제', 'PDF로 제출하세요.', 'ai')
        self.assertEqual((result.provider, result.model), ('groq', 'openai/gpt-oss-20b'))
        self.assertEqual(len(result.items), 1)
        self.assertTrue(result.warnings)
        request = call.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://api.groq.com/openai/v1/chat/completions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
        self.assertEqual(request.get_header('User-agent'), 'assignment-checklist/1.0')
        self.assertEqual(payload['response_format']['json_schema']['schema'], ModelOutput.model_json_schema())
        self.assertIs(payload['response_format']['json_schema']['strict'], True)
        self.assertEqual(payload['reasoning_effort'], 'low')
        call.assert_called_once()

    @patch.dict('os.environ', {'GROQ_API_KEY': '', 'HF_TOKEN': 'old-secret'})
    @patch('extraction.urlopen')
    def test_missing_key_never_uses_old_paid_credentials(self, call):
        self.assertFalse(ai_ready())
        with self.assertRaisesRegex(ExtractionError, 'Groq API 키'):
            extract_notice('과제', 'PDF로 제출하세요.', 'ai')
        call.assert_not_called()

    @patch('extraction.urlopen')
    def test_failures_do_not_retry_or_silently_switch_provider(self, call):
        for code, hint in [(401, '키'), (403, '권한'), (429, '무료 사용 한도'), (400, '요청'), (503, '서비스')]:
            with self.subTest(status=code):
                call.reset_mock()
                call.side_effect = HTTPError('https://api.groq.com/openai/v1/chat/completions', code, 'error', {}, None)
                with self.assertRaisesRegex(ExtractionError, hint) as caught:
                    extract_notice('과제', 'PDF로 제출하세요.', 'ai')
                self.assertNotIn('test-key', str(caught.exception))
                call.assert_called_once()

    @patch('extraction.urlopen')
    def test_network_and_invalid_response_are_reported(self, call):
        call.side_effect = URLError('offline')
        with self.assertRaisesRegex(ExtractionError, '연결하지 못'):
            extract_notice('과제', 'PDF로 제출하세요.', 'ai')
        call.side_effect = None
        for raw in [b'null', b'{}', b'{', b'{"choices":[null]}', b'{"choices":[{"finish_reason":"length"}]}']:
            with self.subTest(response=raw):
                call.return_value = io.BytesIO(raw)
                with self.assertRaises(ExtractionError):
                    extract_notice('과제', 'PDF로 제출하세요.', 'ai')

    @patch.dict('os.environ', {'GROQ_MODEL': 'unsupported/model'})
    @patch('extraction.urlopen')
    def test_unsupported_model_does_not_make_request(self, call):
        with self.assertRaisesRegex(ExtractionError, '모델'):
            extract_notice('과제', 'PDF로 제출하세요.', 'ai')
        call.assert_not_called()


def groq_reply(content):
    return io.BytesIO(json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}]}).encode())


@patch.dict('os.environ', {'AI_PROVIDER': 'groq', 'GROQ_API_KEY': 'test-key', 'GROQ_MODEL': 'openai/gpt-oss-20b'})
class PdfPageRecommendationTests(unittest.TestCase):
    pages = [(1, '1주차 수업 소개입니다.'), (2, '과제 안내\n보고서는 PDF로 제출하세요.\n배포 주소를 포함하세요.'),
             (3, ''), (4, '참고 문헌 목록')]

    @patch('extraction.urlopen')
    def test_only_real_pages_with_verified_quotes_are_recommended(self, call):
        call.return_value = groq_reply({'pages': [
            {'number': 2, 'summary': '보고서 PDF와 배포 주소 제출 조건', 'evidence': '보고서는 PDF로 제출하세요.'},
            {'number': 4, 'summary': '지어낸 조건', 'evidence': '영상을 제출하세요.'},
            {'number': 9, 'summary': '없는 페이지', 'evidence': '보고서는 PDF로 제출하세요.'},
            {'number': 2, 'summary': '중복', 'evidence': '배포 주소를 포함하세요.'},
        ]})
        result = recommend_pdf_pages(self.pages)
        self.assertEqual([(page.number, page.evidence, page.verified) for page in result.pages],
                         [(2, '보고서는 PDF로 제출하세요.', True), (4, '영상을 제출하세요.', False)])
        self.assertEqual(len(result.warnings), 2)
        self.assertTrue(any('4페이지' in warning for warning in result.warnings))
        payload = json.loads(call.call_args.args[0].data)
        sent = json.loads(payload['messages'][1]['content'])['pages']
        self.assertEqual([page['number'] for page in sent], [1, 2, 4])  # empty page 3 is never sent
        self.assertEqual(payload['response_format']['json_schema']['schema'], PageRecommendationOutput.model_json_schema())
        # Stay inside Groq Free's 8,000 tokens per minute: short page text and a small answer budget.
        self.assertEqual(payload['max_completion_tokens'], 1536)
        call.assert_called_once()

    def test_quotes_tolerate_line_joins_quotes_and_final_period(self):
        page = '과제 안내\n보고서는 PDF로 제출하세요.\n배포 주소를 포함하세요.'
        for quote in ['보고서는 PDF로 제출하세요. 배포 주소를 포함하세요.', '“보고서는 PDF로 제출하세요.”',
                      '보고서는 PDF로 제출하세요', '보고서는  PDF로\n제출하세요.']:
            with self.subTest(quote=quote):
                self.assertIsNotNone(locate_quote(page, quote))
        self.assertEqual(locate_quote(page, '보고서는 PDF로 제출하세요. 배포 주소를 포함하세요.'),
                         '보고서는 PDF로 제출하세요.\n배포 주소를 포함하세요.')
        for quote in ['보고서는 워드로 제출하세요.', 'PDF', '']:
            with self.subTest(quote=quote):
                self.assertIsNone(locate_quote(page, quote))

    @patch('extraction.urlopen')
    def test_long_pages_are_shortened_with_a_warning(self, call):
        call.return_value = groq_reply({'pages': []})
        pages = [(number, '가' * 2000) for number in range(1, 21)]
        result = recommend_pdf_pages(pages)
        sent = json.loads(json.loads(call.call_args.args[0].data)['messages'][1]['content'])['pages']
        self.assertLessEqual(sum(len(page['text']) for page in sent), RECOMMEND_TOTAL_CHARS)
        self.assertIn('앞부분', result.warnings[0])
        self.assertEqual(result.pages, [])

    @patch.dict('os.environ', {'GROQ_API_KEY': ''})
    @patch('extraction.urlopen')
    def test_missing_key_and_failures_do_not_call_or_retry(self, call):
        with self.assertRaisesRegex(ExtractionError, 'Groq API 키'):
            recommend_pdf_pages(self.pages)
        call.assert_not_called()

    @patch('extraction.urlopen')
    def test_request_too_large_is_explained(self, call):
        call.side_effect = HTTPError('https://api.groq.com/openai/v1/chat/completions', 413, 'error', {}, None)
        with self.assertRaisesRegex(ExtractionError, '8,000토큰'):
            recommend_pdf_pages(self.pages)
        call.assert_called_once()

    @patch('extraction.urlopen')
    def test_rate_limit_is_reported_once(self, call):
        call.side_effect = HTTPError('https://api.groq.com/openai/v1/chat/completions', 429, 'error', {}, None)
        with self.assertRaisesRegex(ExtractionError, '무료 사용 한도'):
            recommend_pdf_pages(self.pages)
        call.assert_called_once()
