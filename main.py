from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from os import getenv
from typing import Annotated, Any

import psycopg
from fastapi import FastAPI, HTTPException, Path, status
from psycopg.rows import dict_row
from pydantic import BaseModel, Field, field_validator

app = FastAPI(title="과제 제출 누락 방지 도우미")


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/assignments", status_code=status.HTTP_201_CREATED, summary="공지 등록")
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


@app.get("/assignments", summary="공지 목록 조회")
def list_assignments() -> list[AssignmentSummary]:
    with database() as connection:
        rows = connection.execute(
            "SELECT id, title, created_at FROM assignments ORDER BY id DESC"
        ).fetchall()
    return [AssignmentSummary.model_validate(row) for row in rows]


@app.get("/assignments/{assignment_id}", summary="공지 상세 조회")
def get_assignment(assignment_id: Annotated[int, Path(gt=0)]) -> Assignment:
    with database() as connection:
        row = connection.execute(
            "SELECT id, title, body, created_at FROM assignments WHERE id = %s",
            (assignment_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "공지를 찾을 수 없습니다.")
    return Assignment.model_validate(row)
