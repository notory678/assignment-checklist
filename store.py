from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from os import getenv
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, HttpUrl, field_validator

from extraction import Candidate, ExtractionResult, MAX_BODY_LENGTH, PdfRecommendation, normalize
from pdf_input import PdfDocument, PdfPage, join_pages


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200, examples=["클라우드 프로그래밍 프로젝트"])
    body: str = Field(min_length=1, max_length=MAX_BODY_LENGTH, examples=["배포 주소를 포함하세요.\n발표 자료를 준비하세요."])

    @field_validator("title", "body")
    @classmethod
    def validate_notice_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("제목과 본문에는 공백 외의 글자가 필요합니다.")
        if "\x00" in value:
            raise ValueError("제목과 본문에는 널 문자를 사용할 수 없습니다.")
        return value


class AssignmentSummary(BaseModel):
    id: int
    title: str
    created_at: datetime
    source_filename: str = ''


class AppendedPdf(BaseModel):
    filename: str
    heading: str
    pages: list[PdfPage]


class Assignment(AssignmentSummary):
    body: str
    deadline_evidence: str = ''
    extraction_reviewed: bool = False
    source_pages: list[PdfPage] = Field(default_factory=list)
    source_warnings: list[str] = Field(default_factory=list)
    appended_pdfs: list[AppendedPdf] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    id: int
    title: str
    evidence: str
    completed: bool
    kind: str = 'requirement'
    extraction_engine: str = 'manual'
    submission_url: str = ''
    preparation_note: str = ''


@contextmanager
def database() -> Iterator[psycopg.Connection[dict[str, Any]]]:
    database_url = getenv("DATABASE_URL", "")
    if not database_url and not getenv("PGHOST"):
        raise HTTPException(503, "데이터베이스 접속 설정이 필요합니다. Docker Compose로 실행해 주세요.")
    try:
        with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=3) as connection:
            yield connection
    except psycopg.OperationalError as exc:
        raise HTTPException(503, "데이터베이스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.") from exc


def initialize_database() -> None:
    if getenv("DATABASE_URL") or getenv("PGHOST"):
        schema = (Path(__file__).parent / "db" / "init.sql").read_text(encoding="utf-8")
        with database() as connection:
            connection.execute(schema)


def create_assignment(payload: AssignmentCreate) -> Assignment:
    with database() as connection:
        row = connection.execute(
            """
            INSERT INTO assignments (title, body) VALUES (%s, %s)
            RETURNING id, title, body, created_at, deadline_evidence, extraction_reviewed
            """,
            (payload.title, payload.body),
        ).fetchone()
        return Assignment.model_validate(row)


def list_assignments() -> list[AssignmentSummary]:
    with database() as connection:
        rows = connection.execute(
            "SELECT id, title, created_at, source_filename FROM assignments ORDER BY id DESC"
        ).fetchall()
    return [AssignmentSummary.model_validate(row) for row in rows]


def get_assignment(assignment_id: int) -> Assignment:
    with database() as connection:
        row = connection.execute(
            """SELECT id, title, body, created_at, deadline_evidence, extraction_reviewed,
                      source_filename, source_pages, source_warnings, appended_pdfs FROM assignments WHERE id = %s""",
            (assignment_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "공지를 찾을 수 없습니다.")
    return Assignment.model_validate(row)


class PdfImport(PdfDocument):
    id: UUID
    assignment_id: int | None = None
    recommendation: PdfRecommendation | None = None


def create_pdf_import(document: PdfDocument) -> UUID:
    import_id = uuid4()
    with database() as connection:
        connection.execute("DELETE FROM pdf_imports WHERE assignment_id IS NULL AND created_at < CURRENT_TIMESTAMP - INTERVAL '1 day'")
        connection.execute(
            'INSERT INTO pdf_imports (id, filename, pages, warnings) VALUES (%s, %s, %s, %s)',
            (import_id, document.filename, Jsonb([page.model_dump() for page in document.pages]), Jsonb(document.warnings)),
        )
    return import_id


def get_pdf_import(import_id: UUID) -> PdfImport:
    with database() as connection:
        row = connection.execute(
            """SELECT id, filename, pages, warnings, assignment_id, recommendation FROM pdf_imports
               WHERE id = %s AND (assignment_id IS NOT NULL OR created_at > CURRENT_TIMESTAMP - INTERVAL '1 day')""",
            (import_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, 'PDF 미리보기를 찾을 수 없거나 보관 기간이 지났습니다. 파일을 다시 올려 주세요.')
    return PdfImport.model_validate(row)


def save_pdf_import(import_id: UUID, title: str, selected: list[str]) -> int:
    """Persist immutable page text, a canonical body, and its source in one transaction."""
    with database() as connection:
        row = connection.execute(
            """SELECT id, filename, pages, warnings, assignment_id, recommendation FROM pdf_imports
               WHERE id = %s AND (assignment_id IS NOT NULL OR created_at > CURRENT_TIMESTAMP - INTERVAL '1 day') FOR UPDATE""",
            (import_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, 'PDF 미리보기를 찾을 수 없습니다. 파일을 다시 올려 주세요.')
        draft = PdfImport.model_validate(row)
        if draft.assignment_id is not None:
            return draft.assignment_id
        allowed = {str(page.number) for page in draft.pages if page.text}
        if not selected or set(selected) - allowed:
            raise ValueError('텍스트가 있는 페이지를 하나 이상 선택해 주세요.')
        pages = [page for page in draft.pages if str(page.number) in set(selected)]
        body = join_pages(pages)
        if len(body) > MAX_BODY_LENGTH:
            raise ValueError('선택한 본문은 30,000자까지 저장할 수 있습니다. 과제 공지가 있는 페이지만 골라 주세요.')
        try:
            payload = AssignmentCreate(title=title.strip(), body=body)
        except ValueError as exc:
            raise ValueError('공지 제목을 1~200자로 입력해 주세요.') from exc
        assignment = connection.execute(
            """INSERT INTO assignments (title, body, source_filename, source_pages, source_warnings)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (payload.title, payload.body, draft.filename, Jsonb([page.model_dump() for page in pages]), Jsonb(draft.warnings)),
        ).fetchone()
        connection.execute('UPDATE pdf_imports SET assignment_id = %s WHERE id = %s', (assignment['id'], import_id))
    return assignment['id']


def save_pdf_recommendation(import_id: UUID, recommendation: PdfRecommendation) -> None:
    with database() as connection:
        result = connection.execute(
            """UPDATE pdf_imports SET recommendation = %s
               WHERE id = %s AND assignment_id IS NULL AND created_at > CURRENT_TIMESTAMP - INTERVAL '1 day'""",
            (Jsonb(recommendation.model_dump()), import_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, 'PDF 미리보기를 찾을 수 없거나 이미 공지로 저장했습니다.')


APPENDIX_MARK = '[추가 공지]'


def append_notice(assignment_id: int, title: str, body: str) -> None:
    """Append a follow-up notice to an existing notice body. Existing items stay valid quotes."""
    title, body = title.strip(), normalize_newlines(body).strip()
    if not body or '\x00' in body or '\x00' in title or len(title) > 200:
        raise ValueError('추가 공지 본문을 입력해 주세요. 제목은 200자 이내로 적을 수 있습니다.')
    with database() as connection:
        _append_body(connection, assignment_id, f'{APPENDIX_MARK} {title}' if title else APPENDIX_MARK, body)


def _append_body(connection, assignment_id: int, heading: str, body: str) -> None:
    row = connection.execute('SELECT body FROM assignments WHERE id = %s FOR UPDATE', (assignment_id,)).fetchone()
    if row is None:
        raise HTTPException(404, '공지를 찾을 수 없습니다.')
    combined = row['body'] + '\n\n' + heading + '\n' + body
    if len(combined) > MAX_BODY_LENGTH:
        raise ValueError('기존 공지와 합친 본문은 30,000자까지 저장할 수 있습니다. 필요한 부분만 골라 주세요.')
    connection.execute('UPDATE assignments SET body = %s WHERE id = %s', (combined, assignment_id))


def append_pdf_import(import_id: UUID, assignment_id: int, title: str, selected: list[str]) -> int:
    """Append chosen PDF pages to an existing notice as a follow-up notice, once per preview."""
    title = title.strip()
    if '\x00' in title or len(title) > 200:
        raise ValueError('추가 공지 제목은 200자 이내로 적어 주세요.')
    with database() as connection:
        row = connection.execute(
            """SELECT id, filename, pages, warnings, assignment_id, recommendation FROM pdf_imports
               WHERE id = %s AND (assignment_id IS NOT NULL OR created_at > CURRENT_TIMESTAMP - INTERVAL '1 day') FOR UPDATE""",
            (import_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, 'PDF 미리보기를 찾을 수 없습니다. 파일을 다시 올려 주세요.')
        draft = PdfImport.model_validate(row)
        if draft.assignment_id is not None:
            return draft.assignment_id  # repeated or concurrent submit: already attached
        allowed = {str(page.number) for page in draft.pages if page.text}
        if not selected or set(selected) - allowed:
            raise ValueError('텍스트가 있는 페이지를 하나 이상 선택해 주세요.')
        pages = [page for page in draft.pages if str(page.number) in set(selected)]
        numbers = '·'.join(str(page.number) for page in pages)
        source = f'({draft.filename} {numbers}페이지)'
        heading = f'{APPENDIX_MARK} {title} {source}' if title else f'{APPENDIX_MARK} {source}'
        _append_body(connection, assignment_id, heading, join_pages(pages))
        appended = AppendedPdf(filename=draft.filename, heading=heading, pages=pages)
        connection.execute('UPDATE assignments SET appended_pdfs = appended_pdfs || %s WHERE id = %s',
                           (Jsonb([appended.model_dump()]), assignment_id))
        connection.execute('UPDATE pdf_imports SET assignment_id = %s WHERE id = %s', (assignment_id, import_id))
    return assignment_id


def list_checklist(assignment_id: int) -> list[ChecklistItem]:
    with database() as connection:
        rows = connection.execute(
            """
            SELECT id, title, evidence, completed, kind, extraction_engine,
                   submission_url, preparation_note FROM checklist_items
            WHERE assignment_id = %s ORDER BY completed, id
            """,
            (assignment_id,),
        ).fetchall()
    return [ChecklistItem.model_validate(row) for row in rows]


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def add_checklist_item(assignment: Assignment, title: str, evidence: str) -> None:
    title = title.strip()
    evidence = normalize_newlines(evidence).strip()
    if not 1 <= len(title) <= 200 or "\x00" in title:
        raise ValueError("항목 이름을 1~200자로 입력해 주세요.")
    if not evidence or evidence not in normalize_newlines(assignment.body):
        raise ValueError("근거 문구는 위 공지 본문에서 그대로 복사해 주세요.")
    with database() as connection:
        connection.execute(
            "INSERT INTO checklist_items (assignment_id, title, evidence) VALUES (%s, %s, %s)",
            (assignment.id, title, evidence),
        )


class ExtractionDraft(BaseModel):
    id: UUID
    assignment_id: int
    result: ExtractionResult
    saved_at: datetime | None = None


def create_extraction_draft(assignment_id: int, result: ExtractionResult) -> UUID:
    draft_id = uuid4()
    with database() as connection:
        connection.execute(
            'INSERT INTO extraction_drafts (id, assignment_id, result) VALUES (%s, %s, %s)',
            (draft_id, assignment_id, Jsonb(result.model_dump())),
        )
    return draft_id


def get_extraction_draft(assignment_id: int, draft_id: UUID) -> ExtractionDraft:
    with database() as connection:
        row = connection.execute(
            'SELECT id, assignment_id, result, saved_at FROM extraction_drafts WHERE id = %s AND assignment_id = %s',
            (draft_id, assignment_id),
        ).fetchone()
    if row is None:
        raise HTTPException(404, '이 공지의 추출 결과를 찾을 수 없습니다.')
    return ExtractionDraft.model_validate(row)


def validate_review_item(assignment: Assignment, item: Candidate) -> Candidate:
    title, evidence = item.title.strip(), normalize(item.evidence).strip()
    if not title or len(title) > 200 or '\x00' in title:
        raise ValueError('선택한 항목 이름을 1~200자로 입력해 주세요.')
    if item.kind in ('requirement', 'note') and (not evidence or evidence not in normalize(assignment.body)):
        raise ValueError('공지에 명시된 항목의 근거는 원문에서 그대로 복사해 주세요.')
    if item.kind == 'suggestion':
        evidence = ''
    return Candidate(title=title, evidence=evidence, kind=item.kind)


def save_extraction_review(assignment: Assignment, draft_id: UUID, items: list[Candidate],
                           allow_empty: bool = False) -> None:
    """allow_empty: the candidates are already in the checklist, so saving nothing just closes the review."""
    if not items and not allow_empty:
        raise ValueError('저장할 항목을 하나 이상 선택해 주세요.')
    validated = [validate_review_item(assignment, item) for item in items]
    with database() as connection:
        # Serializes saves for this notice, including separate extraction drafts.
        connection.execute('SELECT id FROM assignments WHERE id = %s FOR UPDATE', (assignment.id,))
        row = connection.execute(
            'SELECT id, assignment_id, result, saved_at FROM extraction_drafts WHERE id = %s AND assignment_id = %s FOR UPDATE',
            (draft_id, assignment.id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, '이 공지의 추출 결과를 찾을 수 없습니다.')
        draft = ExtractionDraft.model_validate(row)
        if draft.saved_at:
            return
        rows = connection.execute(
            'SELECT title, evidence, kind FROM checklist_items WHERE assignment_id = %s', (assignment.id,),
        ).fetchall()
        existing = {(item['title'].strip().casefold(), normalize(item['evidence']).strip(), item['kind']) for item in rows}
        for item in validated:
            key = (item.title.casefold(), item.evidence, item.kind)
            if key in existing:
                continue
            connection.execute(
                '''INSERT INTO checklist_items (assignment_id, title, evidence, kind, extraction_engine)
                   VALUES (%s, %s, %s, %s, %s)''',
                (assignment.id, item.title, item.evidence, item.kind, draft.result.engine),
            )
            existing.add(key)
        if draft.result.deadline_evidence:
            connection.execute('UPDATE assignments SET deadline_evidence = %s WHERE id = %s',
                               (draft.result.deadline_evidence, assignment.id))
        connection.execute('UPDATE assignments SET extraction_reviewed = TRUE WHERE id = %s', (assignment.id,))
        connection.execute('UPDATE extraction_drafts SET saved_at = CURRENT_TIMESTAMP WHERE id = %s', (draft_id,))


def update_checklist_item(assignment: Assignment, item_id: int, title: str, evidence: str) -> None:
    """Edit a saved item's name and quote. The kind stays; quotes are re-checked against the notice."""
    with database() as connection:
        row = connection.execute(
            'SELECT kind FROM checklist_items WHERE assignment_id = %s AND id = %s FOR UPDATE',
            (assignment.id, item_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, '이 공지에서 해당 체크리스트 항목을 찾을 수 없습니다.')
        item = validate_review_item(assignment, Candidate.model_construct(title=title, evidence=evidence, kind=row['kind']))
        connection.execute('UPDATE checklist_items SET title = %s, evidence = %s WHERE assignment_id = %s AND id = %s',
                           (item.title, item.evidence, assignment.id, item_id))


def delete_checklist_item(assignment_id: int, item_id: int) -> None:
    with database() as connection:
        result = connection.execute('DELETE FROM checklist_items WHERE assignment_id = %s AND id = %s',
                                    (assignment_id, item_id))
        if result.rowcount == 0:
            raise HTTPException(404, '이 공지에서 해당 체크리스트 항목을 찾을 수 없습니다.')


def delete_assignment(assignment_id: int) -> None:
    """Delete a notice with its checklist, review drafts and PDF preview (ON DELETE CASCADE)."""
    with database() as connection:
        result = connection.execute('DELETE FROM assignments WHERE id = %s', (assignment_id,))
        if result.rowcount == 0:
            raise HTTPException(404, '공지를 찾을 수 없습니다.')


def set_checklist_completed(assignment_id: int, item_id: int, completed: bool) -> None:
    with database() as connection:
        result = connection.execute(
            """
            UPDATE checklist_items SET completed = %s
            WHERE assignment_id = %s AND id = %s
            """,
            (completed, assignment_id, item_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, "이 공지에서 해당 체크리스트 항목을 찾을 수 없습니다.")


def save_checklist_preparation(assignment_id: int, item_id: int, submission_url: str, preparation_note: str) -> None:
    submission_url = submission_url.strip()
    preparation_note = normalize_newlines(preparation_note).strip()
    if len(submission_url) > 2048:
        raise ValueError('제출 주소는 2,048자 이내로 입력해 주세요.')
    if submission_url:
        if not submission_url.lower().startswith(('http://', 'https://')) or '\\' in submission_url:
            raise ValueError('제출 주소는 http:// 또는 https://로 시작하는 올바른 웹 주소로 입력해 주세요.')
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in submission_url):
            raise ValueError('제출 주소에는 공백이나 줄바꿈을 넣을 수 없습니다.')
        try:
            address = HttpUrl(submission_url)
        except ValueError as exc:
            raise ValueError('제출 주소는 http:// 또는 https://로 시작하는 올바른 웹 주소로 입력해 주세요.') from exc
        if address.username is not None or address.password is not None:
            raise ValueError('제출 주소에 아이디나 비밀번호를 포함하지 말아 주세요.')
        # Use the validated representation for both storage and the clickable link.
        submission_url = str(address)
        if len(submission_url) > 2048:
            raise ValueError('제출 주소는 2,048자 이내로 입력해 주세요.')
    if len(preparation_note) > 4000:
        raise ValueError('준비 메모는 4,000자 이내로 입력해 주세요.')
    if '\x00' in preparation_note:
        raise ValueError('준비 메모에 사용할 수 없는 문자가 있습니다. 내용을 확인해 주세요.')
    with database() as connection:
        result = connection.execute(
            '''UPDATE checklist_items SET submission_url = %s, preparation_note = %s
               WHERE assignment_id = %s AND id = %s''',
            (submission_url, preparation_note, assignment_id, item_id),
        )
        if result.rowcount == 0:
            raise HTTPException(404, '이 공지에서 해당 체크리스트 항목을 찾을 수 없습니다.')
