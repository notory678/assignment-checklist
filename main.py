from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Path as PathParam, status
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

import store
from store import Assignment, AssignmentCreate, AssignmentSummary
from web import router
from pdf_input import MAX_PDF_BYTES


class _PdfUploadTooLarge(Exception):
    pass


class PdfUploadLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['path'] != '/notices/pdf' or scope['method'] != 'POST':
            return await self.app(scope, receive, send)
        received = 0
        limit = MAX_PDF_BYTES + 64 * 1024  # Allow normal multipart framing.

        try:
            messages = []
            while True:
                message = await receive()
                if message['type'] == 'http.disconnect':
                    return
                received += len(message.get('body', b''))
                if received > limit:
                    raise _PdfUploadTooLarge()
                messages.append(message)
                if not message.get('more_body', False):
                    break

            async def replay_receive():
                return messages.pop(0) if messages else await receive()

            await self.app(scope, replay_receive, send)
        except _PdfUploadTooLarge:
            response = HTMLResponse(
                '<!doctype html><html lang="ko"><meta charset="utf-8"><title>PDF 업로드 안내</title>'
                '<main><h1>PDF는 5MB 이하로 올려 주세요.</h1><p>필요한 페이지만 별도 PDF로 저장해 다시 시도할 수 있습니다.</p>'
                '<a href="/">공지 목록으로 돌아가기</a></main></html>', status_code=413,
            )
            await response(scope, receive, send)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.initialize_database()
    yield


app = FastAPI(title="과제 제출 누락 방지 도우미", lifespan=lifespan)
app.add_middleware(PdfUploadLimitMiddleware)
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
