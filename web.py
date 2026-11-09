from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from store import (
    Assignment,
    AssignmentCreate,
    add_checklist_item,
    create_assignment,
    get_assignment,
    list_assignments,
    list_checklist,
    set_checklist_completed,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))


def render_home(request: Request, *, title="", body="", error=None):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"assignments": list_assignments(), "title": title, "body": body, "error": error},
        status_code=422 if error else 200,
    )


def render_detail(request: Request, assignment: Assignment, *, title="", evidence="", error=None):
    items = list_checklist(assignment.id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "assignment": assignment,
            "items": items,
            "completed_count": sum(item.completed for item in items),
            "title": title,
            "evidence": evidence,
            "error": error,
        },
        status_code=422 if error else 200,
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render_home(request)


@router.post("/notices", response_class=HTMLResponse)
def save_notice(
    request: Request,
    title: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
):
    try:
        payload = AssignmentCreate(title=title, body=body)
    except ValidationError:
        return render_home(
            request, title=title, body=body,
            error="제목은 1~200자로 입력하고, 본문에는 공지 내용을 붙여넣어 주세요.",
        )
    assignment = create_assignment(payload)
    return RedirectResponse(request.url_for("notice_detail", assignment_id=assignment.id), status_code=303)


@router.get("/notices/{assignment_id}", response_class=HTMLResponse)
def notice_detail(request: Request, assignment_id: int):
    return render_detail(request, get_assignment(assignment_id))


@router.post("/notices/{assignment_id}/checklist", response_class=HTMLResponse)
def save_checklist_item(
    request: Request,
    assignment_id: int,
    title: Annotated[str, Form()] = "",
    evidence: Annotated[str, Form()] = "",
):
    assignment = get_assignment(assignment_id)
    try:
        add_checklist_item(assignment, title, evidence)
    except ValueError as exc:
        return render_detail(request, assignment, title=title, evidence=evidence, error=str(exc))
    return RedirectResponse(request.url_for("notice_detail", assignment_id=assignment_id), status_code=303)


@router.post("/notices/{assignment_id}/checklist/{item_id}/completion")
def update_completion(
    request: Request,
    assignment_id: int,
    item_id: int,
    completed: Annotated[bool, Form()],
):
    set_checklist_completed(assignment_id, item_id, completed)
    return RedirectResponse(request.url_for("notice_detail", assignment_id=assignment_id), status_code=303)
