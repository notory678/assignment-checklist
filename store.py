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
from pydantic import BaseModel, Field, field_validator

from extraction import Candidate, ExtractionResult, normalize


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200, examples=["클라우드 프로그래밍 프로젝트"])
    body: str = Field(min_length=1, examples=["배포 주소를 포함하세요.\n발표 자료를 준비하세요."])

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


class Assignment(AssignmentSummary):
    body: str
    deadline_evidence: str = ''
    extraction_reviewed: bool = False


class ChecklistItem(BaseModel):
    id: int
    title: str
    evidence: str
    completed: bool
    kind: str = 'requirement'
    extraction_engine: str = 'manual'


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
            "SELECT id, title, created_at FROM assignments ORDER BY id DESC"
        ).fetchall()
    return [AssignmentSummary.model_validate(row) for row in rows]


def get_assignment(assignment_id: int) -> Assignment:
    with database() as connection:
        row = connection.execute(
            "SELECT id, title, body, created_at, deadline_evidence, extraction_reviewed FROM assignments WHERE id = %s",
            (assignment_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "공지를 찾을 수 없습니다.")
    return Assignment.model_validate(row)


def list_checklist(assignment_id: int) -> list[ChecklistItem]:
    with database() as connection:
        rows = connection.execute(
            """
            SELECT id, title, evidence, completed, kind, extraction_engine FROM checklist_items
            WHERE assignment_id = %s ORDER BY id
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
    if item.kind == 'requirement' and (not evidence or evidence not in normalize(assignment.body)):
        raise ValueError('공지에 명시된 항목의 근거는 원문에서 그대로 복사해 주세요.')
    if item.kind == 'suggestion':
        evidence = ''
    return Candidate(title=title, evidence=evidence, kind=item.kind)


def save_extraction_review(assignment: Assignment, draft_id: UUID, items: list[Candidate]) -> None:
    if not items:
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
