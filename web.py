from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from extraction import Candidate, ExtractionError, ai_provider, ai_ready, local_ai_ready, extract_notice
from pdf_input import MAX_PDF_BYTES, PdfInputError, evidence_page_numbers, extract_pdf

from store import (
    Assignment,
    AssignmentCreate,
    add_checklist_item,
    create_assignment,
    get_assignment,
    list_assignments,
    list_checklist,
    set_checklist_completed,
    create_extraction_draft,
    get_extraction_draft,
    save_extraction_review,
    create_pdf_import,
    get_pdf_import,
    save_pdf_import,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))
templates.env.globals['evidence_pages'] = evidence_page_numbers


def render_home(request: Request, *, title="", body="", error=None, pdf_error=None):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"assignments": list_assignments(), "title": title, "body": body, "error": error, "pdf_error": pdf_error},
        status_code=422 if error or pdf_error else 200,
    )


def render_detail(request: Request, assignment: Assignment, *, title="", evidence="", error=None,
                  extraction_error=None):
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
            "extraction_error": extraction_error,
            "ai_ready": ai_ready(),
            "ai_provider": ai_provider(),
            "local_ai_ready": local_ai_ready(),
        },
        status_code=422 if error or extraction_error else 200,
    )


def render_review(request: Request, assignment: Assignment, draft, *, titles=None, evidences=None,
                  selected=None, error=None):
    items = draft.result.items
    return templates.TemplateResponse(
        request, 'review.html', {
            'assignment': assignment, 'draft': draft,
            'titles': titles if titles is not None else [item.title for item in items],
            'evidences': evidences if evidences is not None else [item.evidence for item in items],
            'selected': selected if selected is not None else {str(i) for i, item in enumerate(items) if item.kind == 'requirement'},
            'error': error,
        }, status_code=422 if error else 200,
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


@router.post('/notices/pdf', response_class=HTMLResponse)
def upload_pdf(request: Request, file: Annotated[UploadFile | None, File()] = None):
    if file is None:
        return render_home(request, pdf_error='먼저 PDF 파일을 선택해 주세요.')
    try:
        document = extract_pdf(file.file.read(MAX_PDF_BYTES + 1), file.filename or '')
    except PdfInputError as exc:
        return render_home(request, pdf_error=str(exc))
    finally:
        file.file.close()
    import_id = create_pdf_import(document)
    return RedirectResponse(request.url_for('preview_pdf', import_id=import_id), status_code=303)


def render_pdf_preview(request: Request, draft, *, title=None, selected=None, error=None):
    return templates.TemplateResponse(request, 'pdf_preview.html', {
        'draft': draft,
        'title': title if title is not None else Path(draft.filename).stem[:200],
        'selected': selected if selected is not None else {str(page.number) for page in draft.pages if page.text},
        'error': error,
    }, status_code=422 if error else 200)


@router.get('/pdf-imports/{import_id}', response_class=HTMLResponse)
def preview_pdf(request: Request, import_id: UUID):
    draft = get_pdf_import(import_id)
    if draft.assignment_id is not None:
        return RedirectResponse(request.url_for('notice_detail', assignment_id=draft.assignment_id), status_code=303)
    return render_pdf_preview(request, draft)


@router.post('/pdf-imports/{import_id}/save', response_class=HTMLResponse)
def save_pdf_notice(request: Request, import_id: UUID,
                    title: Annotated[str, Form()] = '', selected: Annotated[list[str], Form()] = []):
    draft = get_pdf_import(import_id)
    try:
        assignment_id = save_pdf_import(import_id, title, selected)
    except ValueError as exc:
        return render_pdf_preview(request, draft, title=title, selected=set(selected), error=str(exc))
    return RedirectResponse(request.url_for('notice_detail', assignment_id=assignment_id), status_code=303)


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


@router.post('/notices/{assignment_id}/extract', response_class=HTMLResponse)
def generate_checklist(request: Request, assignment_id: int, engine: Annotated[str, Form()] = 'basic'):
    assignment = get_assignment(assignment_id)
    try:
        result = extract_notice(assignment.title, assignment.body, engine)
    except ExtractionError as exc:
        return render_detail(request, assignment, extraction_error=str(exc))
    draft_id = create_extraction_draft(assignment_id, result)
    return RedirectResponse(request.url_for('review_extraction', assignment_id=assignment_id, draft_id=draft_id), status_code=303)


@router.get('/notices/{assignment_id}/extractions/{draft_id}', response_class=HTMLResponse)
def review_extraction(request: Request, assignment_id: int, draft_id: UUID):
    assignment = get_assignment(assignment_id)
    draft = get_extraction_draft(assignment_id, draft_id)
    if draft.saved_at:
        return RedirectResponse(request.url_for('notice_detail', assignment_id=assignment_id), status_code=303)
    return render_review(request, assignment, draft)


@router.post('/notices/{assignment_id}/extractions/{draft_id}/save', response_class=HTMLResponse)
def save_review(
    request: Request, assignment_id: int, draft_id: UUID,
    selected: Annotated[list[str], Form()] = [],
    titles: Annotated[list[str], Form()] = [],
    evidences: Annotated[list[str], Form()] = [],
):
    assignment = get_assignment(assignment_id)
    draft = get_extraction_draft(assignment_id, draft_id)
    if draft.saved_at:
        return RedirectResponse(request.url_for('notice_detail', assignment_id=assignment_id), status_code=303)
    try:
        if len(titles) != len(draft.result.items) or len(evidences) != len(titles):
            raise ValueError('입력 항목을 읽지 못했습니다. 결과 화면을 다시 열어 주세요.')
        allowed = {str(i) for i in range(len(titles))}
        if set(selected) - allowed:
            raise ValueError('저장할 항목을 확인한 뒤 다시 선택해 주세요.')
        reviewed = [Candidate(title=titles[i], evidence=evidences[i], kind=item.kind)
                    for i, item in enumerate(draft.result.items) if str(i) in set(selected)]
        save_extraction_review(assignment, draft_id, reviewed)
    except (ValueError, ValidationError) as exc:
        error = '항목 이름은 1~200자, 근거는 4,000자 이내로 입력해 주세요.' if isinstance(exc, ValidationError) else str(exc)
        if len(titles) != len(draft.result.items) or len(evidences) != len(draft.result.items):
            titles = evidences = None
        return render_review(request, assignment, draft, titles=titles, evidences=evidences,
                             selected=set(selected), error=error)
    return RedirectResponse(request.url_for('notice_detail', assignment_id=assignment_id), status_code=303)
