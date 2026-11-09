from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Path as PathParam, status
from fastapi.staticfiles import StaticFiles

import store
from store import Assignment, AssignmentCreate, AssignmentSummary
from web import router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.initialize_database()
    yield


app = FastAPI(title="과제 제출 누락 방지 도우미", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
app.include_router(router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/assignments", status_code=status.HTTP_201_CREATED, summary="공지 등록")
def create_assignment(payload: AssignmentCreate) -> Assignment:
    return store.create_assignment(payload)


@app.get("/assignments", summary="공지 목록 조회")
def list_assignments() -> list[AssignmentSummary]:
    return store.list_assignments()


@app.get("/assignments/{assignment_id}", summary="공지 상세 조회")
def get_assignment(assignment_id: Annotated[int, PathParam(gt=0)]) -> Assignment:
    return store.get_assignment(assignment_id)
