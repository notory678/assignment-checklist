import subprocess
import unittest
from unittest.mock import patch

from pdf_fixture import make_pdf
from pdf_input import MAX_PDF_BYTES, PdfInputError, PdfPage, evidence_page_numbers, extract_pdf


class PdfInputTests(unittest.TestCase):
    def test_korean_pages_and_empty_page_keep_original_numbers(self):
        document = extract_pdf(make_pdf(['보고서는 PDF로 제출하세요.', '', '배포 주소를 포함하세요.']), '../공지.PDF')
        self.assertEqual(document.filename, '공지.PDF')
        self.assertEqual([page.number for page in document.pages], [1, 2, 3])
        self.assertEqual(document.pages[0].text, '보고서는 PDF로 제출하세요.')
        self.assertEqual(document.pages[2].text, '배포 주소를 포함하세요.')
        self.assertIn('2페이지', document.warnings[0])

    def test_wrong_extension_header_and_size(self):
        for data, filename in [(b'plain text', 'notice.pdf'), (make_pdf(['hello']), 'notice.txt'),
                               (b'%PDF-' + b'0' * MAX_PDF_BYTES, 'notice.pdf')]:
            with self.subTest(filename=filename, length=len(data)), self.assertRaises(PdfInputError):
                extract_pdf(data, filename)

    def test_scan_encryption_corruption_and_page_limit(self):
        for data, message in [(make_pdf(['', '']), 'OCR'), (make_pdf(['hello'], encrypted=True), '암호화'),
                              (b'%PDF-1.7\nbroken', '읽지 못'), (make_pdf(['hello'] * 51), '50페이지')]:
            with self.subTest(message=message), self.assertRaisesRegex(PdfInputError, message):
                extract_pdf(data, 'notice.pdf')

    def test_parser_timeout_has_recovery_message(self):
        with patch('pdf_input.subprocess.run', side_effect=subprocess.TimeoutExpired('parser', 25)):
            with self.assertRaisesRegex(PdfInputError, '시간이 초과'):
                extract_pdf(b'%PDF-1.7', 'notice.pdf')

    def test_evidence_locations_repeated_cross_page_missing_and_no_quote(self):
        pages = [PdfPage(number=1, text='보고서 PDF 제출'), PdfPage(number=3, text='배포 주소 포함'),
                 PdfPage(number=5, text='보고서 PDF 제출')]
        self.assertEqual(evidence_page_numbers(pages, '보고서 PDF 제출'), [1, 5])
        self.assertEqual(evidence_page_numbers(pages, 'PDF 제출\n\n배포 주소'), [1, 3])
        self.assertEqual(evidence_page_numbers(pages, '없는 문장'), [])
        self.assertEqual(evidence_page_numbers(pages, ''), [])


if __name__ == '__main__':
    unittest.main()
