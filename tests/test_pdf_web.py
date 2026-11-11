"""HTTP PDF flow against the disposable CHECKLIST_TEST_URL server."""
import json
import http.client
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from pdf_fixture import make_pdf
import test_web as web_helpers


@unittest.skipUnless(os.getenv('CHECKLIST_TEST_URL'), 'requires isolated CHECKLIST_TEST_URL')
class PdfWebTests(unittest.TestCase):
    request = web_helpers.WebFlowTests.request
    generate = web_helpers.WebFlowTests.generate
    detail = web_helpers.WebFlowTests.detail

    def upload(self, data, filename='공지.pdf'):
        boundary = 'checklist-pdf-test-boundary'
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                'Content-Type: application/pdf\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
        request = Request(os.environ['CHECKLIST_TEST_URL'] + '/notices/pdf', data=body,
                          headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        try:
            response = build_opener(web_helpers.NoRedirect).open(request, timeout=35)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read().decode(), response.headers

    def new_import(self):
        code, html, headers = self.upload(make_pdf(['보고서는 PDF로 제출하세요.', '', '배포 주소를 포함하세요.']))
        self.assertEqual(code, 303, html)
        return urlsplit(headers['Location']).path

    def save_import(self, route, data=None):
        return self.request(route + '/save', data if data is not None else [('title', 'PDF 과제 안내'), ('selected', '1'), ('selected', '3')])

    def test_upload_preview_select_extract_review_save_and_reload(self):
        before = json.loads(self.request('/assignments')[1])
        route = self.new_import()
        code, html, _ = self.request(route)
        self.assertEqual(code, 200, html)
        self.assertIn('PDF 본문 확인', html)
        self.assertIn('2페이지에서 텍스트를 읽지 못했습니다', html)
        self.assertEqual(len(json.loads(self.request('/assignments')[1])), len(before))
        code, html, headers = self.save_import(route)
        self.assertEqual(code, 303, html)
        notice_route = urlsplit(headers['Location']).path
        notice_id = int(notice_route.rsplit('/', 1)[1])
        saved = json.loads(self.request('/assignments/' + str(notice_id))[1])
        self.assertEqual(saved['source_filename'], '공지.pdf')
        self.assertEqual([page['number'] for page in saved['source_pages']], [1, 3])
        self.assertEqual(saved['body'], '보고서는 PDF로 제출하세요.\n\n배포 주소를 포함하세요.')
        review_route, review, values = self.generate(notice_id)
        self.assertIn('href="#pdf-page-1"', review)
        self.assertIn('href="#pdf-page-3"', review)
        self.assertEqual(self.request(review_route + '/save', values)[0], 303)
        detail = self.detail(notice_id)
        self.assertEqual(detail.count('class="checklist-item"'), 2)
        self.assertIn('href="#pdf-page-3"', detail)
        self.assertIn('미기재', detail)
        self.assertEqual(self.request(route)[0], 303)
        self.assertEqual(self.save_import(route)[2]['Location'], headers['Location'])

    def test_selection_errors_keep_title_and_do_not_create_notice(self):
        route = self.new_import()
        before = len(json.loads(self.request('/assignments')[1]))
        for values in [[('title', '수정한 제목')], [('title', '수정한 제목'), ('selected', '2')],
                       [('title', '수정한 제목'), ('selected', '99')], [('title', ' '), ('selected', '1')]]:
            code, html, _ = self.save_import(route, values)
            self.assertEqual(code, 422, html)
            self.assertIn('PDF 본문 확인', html)
        self.assertEqual(len(json.loads(self.request('/assignments')[1])), before)
        code, _, headers = self.save_import(route, [('title', '하나만 저장'), ('selected', '3')])
        self.assertEqual(code, 303)
        saved = json.loads(self.request('/assignments/' + headers['Location'].rsplit('/', 1)[-1])[1])
        self.assertEqual([page['number'] for page in saved['source_pages']], [3])
        self.assertNotIn('보고서', saved['body'])

    def test_concurrent_import_save_creates_one_notice(self):
        route = self.new_import()
        before = len(json.loads(self.request('/assignments')[1]))
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.save_import(route), range(2)))
        self.assertEqual([response[0] for response in responses], [303, 303])
        self.assertEqual(responses[0][2]['Location'], responses[1][2]['Location'])
        self.assertEqual(len(json.loads(self.request('/assignments')[1])), before + 1)

    def test_bad_scan_and_oversized_files_have_recovery_messages(self):
        for data, message in [(b'not PDF', 'PDF 파일을 선택'), (make_pdf(['']), 'OCR'),
                              (b'%PDF-' + b'x' * (5 * 1024 * 1024 + 100_000), '5MB')]:
            code, html, _ = self.upload(data)
            self.assertIn(code, (413, 422))
            self.assertIn(message, html)

    def test_streamed_upload_limit_and_file_limit(self):
        # The request ceiling must also work without a Content-Length header.
        address = urlsplit(os.environ['CHECKLIST_TEST_URL'])
        connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
        try:
            chunks = [b'x' * 16384] * 326
            connection.request('POST', '/notices/pdf', body=iter(chunks), encode_chunked=True,
                               headers={'Content-Type': 'multipart/form-data; boundary=test'})
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn('5MB', response.read().decode())
        finally:
            connection.close()
        # A file can exceed 5MB while its request is still below the framing ceiling.
        code, html, _ = self.upload(b'%PDF-' + b'x' * (5 * 1024 * 1024 + 1000))
        self.assertEqual(code, 422)
        self.assertIn('5MB', html)

    def test_oversized_selected_text_is_rejected_without_truncation(self):
        code, html, headers = self.upload(make_pdf(['A' * 16000, 'B' * 16000]))
        self.assertEqual(code, 303, html)
        route = urlsplit(headers['Location']).path
        values = [('title', '긴 자료'), ('selected', '1'), ('selected', '2')]
        code, html, _ = self.save_import(route, values)
        self.assertEqual(code, 422, html)
        self.assertIn('30,000자', html)
        self.assertEqual(self.save_import(route, [('title', '짧은 자료'), ('selected', '2')])[0], 303)


if __name__ == '__main__':
    unittest.main()
