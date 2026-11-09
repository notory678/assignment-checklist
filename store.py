from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from os import getenv
from pathlib import Path
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, field_validator


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


class ChecklistItem(BaseModel):
    id: int
    title: str
    evidence: str
    completed: bool


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
            RETURNING id, title, body, created_at
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
            "SELECT id, title, body, created_at FROM assignments WHERE id = %s",
            (assignment_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "공지를 찾을 수 없습니다.")
    return Assignment.model_validate(row)


def list_checklist(assignment_id: int) -> list[ChecklistItem]:
    with database() as connection:
        rows = connection.execute(
            """
            SELECT id, title, evidence, completed FROM checklist_items
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
