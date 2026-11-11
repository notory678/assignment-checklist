"""Bounded, local text-PDF import. Page locations are derived from saved text."""

import json
import re
import subprocess
import sys
from io import BytesIO
from pathlib import Path

from pydantic import BaseModel, Field

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_PDF_TEXT = 150_000


class PdfInputError(ValueError):
    pass


class PdfPage(BaseModel):
    number: int = Field(ge=1, le=MAX_PDF_PAGES)
    text: str


class PdfDocument(BaseModel):
    filename: str
    pages: list[PdfPage]
    warnings: list[str] = Field(default_factory=list)


def join_pages(pages: list[PdfPage]) -> str:
    return '\n\n'.join(page.text for page in pages)


def evidence_page_numbers(pages: list[PdfPage], evidence: str) -> list[int]:
    """Find every exact occurrence, including quotations spanning a page break."""
    evidence = evidence.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not evidence or not pages:
        return []
    body = join_pages(pages)
    spans, offset = [], 0
    for page in pages:
        spans.append((offset, offset + len(page.text), page.number))
        offset += len(page.text) + 2
    found, start = set(), 0
    while (position := body.find(evidence, start)) != -1:
        end = position + len(evidence)
        found.update(number for left, right, number in spans if left < end and right > position)
        start = position + 1
    return sorted(found)


def extract_pdf(data: bytes, filename: str) -> PdfDocument:
    filename = re.sub(r'[\x00-\x1f\x7f]', '', filename.replace('\\', '/').rsplit('/', 1)[-1]).strip()
    if not filename.lower().endswith('.pdf') or not data.startswith(b'%PDF-'):
        raise PdfInputError('PDF 파일을 선택해 주세요. 확장자만 바꾼 파일은 읽을 수 없습니다.')
    if len(data) > MAX_PDF_BYTES:
        raise PdfInputError('PDF는 5MB 이하로 올려 주세요.')
    # Keep malformed/complex documents from blocking the web process indefinitely.
    try:
        process = subprocess.run(
            [sys.executable, str(Path(__file__).resolve())], input=data,
            capture_output=True, timeout=25, check=False,
        )
        result = json.loads(process.stdout)
    except subprocess.TimeoutExpired as exc:
        raise PdfInputError('PDF를 읽는 시간이 초과되었습니다. 필요한 페이지만 별도 PDF로 저장해 올려 주세요.') from exc
    except (OSError, ValueError) as exc:
        raise PdfInputError('PDF를 읽지 못했습니다. 파일을 확인하거나 공지 본문을 붙여넣어 주세요.') from exc
    if process.returncode or 'error' in result:
        raise PdfInputError(result.get('error', 'PDF를 읽지 못했습니다. 더 작은 PDF로 다시 시도해 주세요.'))
    return PdfDocument(filename=filename[:255], **result)


def _read_pages(data: bytes) -> dict:
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise PdfInputError('암호화된 PDF는 지원하지 않습니다. 암호화하지 않은 파일을 올려 주세요.')
        if not 1 <= len(reader.pages) <= MAX_PDF_PAGES:
            raise PdfInputError('PDF는 1~50페이지까지 지원합니다. 필요한 페이지만 별도 PDF로 저장해 올려 주세요.')
        pages, total = [], 0
        for number, page in enumerate(reader.pages, 1):
            text = (page.extract_text() or '').replace('\r\n', '\n').replace('\r', '\n')
            text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text).strip()
            if not any(char.isalnum() for char in text):
                text = ''
            total += len(text)
            if total > MAX_PDF_TEXT:
                raise PdfInputError('PDF에서 읽은 글자가 너무 많습니다. 과제 공지가 있는 페이지만 별도 PDF로 올려 주세요.')
            pages.append(PdfPage(number=number, text=text))
        if not any(page.text for page in pages):
            raise PdfInputError('텍스트를 읽을 수 없는 PDF입니다. 스캔·이미지 PDF의 OCR은 아직 지원하지 않습니다. 공지 본문을 직접 붙여넣어 주세요.')
        warnings = []
        empty = [str(page.number) for page in pages if not page.text]
        if empty:
            warnings.append(', '.join(empty) + '페이지에서 텍스트를 읽지 못했습니다. 빈 페이지이거나 이미지일 수 있으니 원본을 확인해 주세요. 이 페이지는 분석하지 않습니다.')
        return {'pages': [page.model_dump() for page in pages], 'warnings': warnings}
    except PdfInputError:
        raise
    except Exception as exc:
        raise PdfInputError('PDF를 읽지 못했습니다. 손상되었거나 지원하지 않는 형식입니다. 본문을 직접 붙여넣을 수도 있습니다.') from exc


if __name__ == '__main__':
    # Linux containers can also enforce a memory/CPU ceiling on the parser child.
    if sys.platform.startswith('linux'):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    try:
        print(json.dumps(_read_pages(sys.stdin.buffer.read(MAX_PDF_BYTES + 1)), ensure_ascii=False))
    except PdfInputError as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
