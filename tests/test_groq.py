import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from extraction import ExtractionError, ModelOutput, ai_ready, extract_notice


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
