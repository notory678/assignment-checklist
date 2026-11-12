from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from extraction import (Candidate, ExtractionError, ai_provider, ai_ready, groq_ready, local_ai_ready, extract_notice,
                        recommend_pdf_pages)
from pdf_input import MAX_PDF_BYTES, PdfInputError, evidence_page_numbers, extract_pdf, join_pages

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
    save_checklist_preparation,
    save_pdf_recommendation,
    update_checklist_item,
    append_notice,
    delete_checklist_item,
    delete_assignment,
    append_pdf_import,
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).with_name("templates"))
templates.env.globals['evidence_pages'] = evidence_page_numbers


def appended_text(assignment: Assignment) -> str:
    """Text added after a PDF notice was saved (follow-up notices)."""
    if not assignment.source_pages:
        return ''
    return assignment.body[len(join_pages(assignment.source_pages)):].strip()


templates.env.globals['appended_text'] = appended_text


def render_home(request: Request, *, title="", body="", append_to="", error=None, pdf_error=None, deleted=False):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"assignments": list_assignments(), "title": title, "body": body, "append_to": append_to,
         "error": error, "pdf_error": pdf_error, "deleted": deleted},
        status_code=422 if error or pdf_error else 200,
    )


def render_detail(request: Request, assignment: Assignment, *, title="", evidence="", error=None,
                  extraction_error=None, preparation_error=None, preparation_values=None, saved_item_id=None,
                  edit_error=None, edit_values=None, edited_item_id=None, appended=False, item_deleted=False):
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
            "preparation_error": preparation_error,
            "preparation_values": preparation_values,
            "saved_item_id": saved_item_id,
            "edit_error": edit_error,
            "edit_values": edit_values,
            "edited_item_id": edited_item_id,
            "appended": appended,
            "item_deleted": item_deleted,
            "ai_ready": ai_ready(),
            "ai_provider": ai_provider(),
            "local_ai_ready": local_ai_ready(),
        },
        status_code=422 if error or extraction_error or preparation_error or edit_error else 200,
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
def home(request: Request, deleted: int | None = None):
    return render_home(request, deleted=bool(deleted))


@router.post("/notices", response_class=HTMLResponse)
def save_notice(
    request: Request,
    title: Annotated[str, Form()] = "",
    body: Annotated[str, Form()] = "",
    append_to: Annotated[str, Form()] = "",
):
    if append_to:
        if not append_to.isdigit():
            return render_home(request, title=title, body=body, error="추가 공지를 붙일 공지를 다시 선택해 주세요.")
        assignment_id = int(append_to)
        get_assignment(assignment_id)
        try:
            append_notice(assignment_id, title, body)
        except ValueError as exc:
            return render_home(request, title=title, body=body, append_to=append_to, error=str(exc))
        url = request.url_for("notice_detail", assignment_id=assignment_id).include_query_params(appended=1)
        return RedirectResponse(str(url), status_code=303)
    try:
        payload = AssignmentCreate(title=title, body=body)
    except ValidationError:
        return render_home(
            request, title=title, body=body,
            error="제목은 1~200자로 입력하고, 본문에는 공지 내용을 30,000자 이내로 붙여넣어 주세요.",
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


def render_pdf_preview(request: Request, draft, *, title=None, selected=None, error=None, append_to=''):
    recommended = ({str(page.number) for page in draft.recommendation.pages if page.verified}
                   if draft.recommendation else None)
    if selected is None:
        selected = recommended if recommended else {str(page.number) for page in draft.pages if page.text}
    return templates.TemplateResponse(request, 'pdf_preview.html', {
        'draft': draft,
        'title': title if title is not None else Path(draft.filename).stem[:200],
        'selected': selected,
        'recommendations': {page.number: page for page in draft.recommendation.pages} if draft.recommendation else {},
        'groq_ready': groq_ready(),
        'assignments': list_assignments(),
        'append_to': append_to,
        'error': error,
    }, status_code=422 if error else 200)


@router.post('/pdf-imports/{import_id}/recommend', response_class=HTMLResponse)
def recommend_pdf(request: Request, import_id: UUID):
    draft = get_pdf_import(import_id)
    if draft.assignment_id is not None:
        return RedirectResponse(request.url_for('notice_detail', assignment_id=draft.assignment_id), status_code=303)
    try:
        recommendation = recommend_pdf_pages([(page.number, page.text) for page in draft.pages])
    except ExtractionError as exc:
        return render_pdf_preview(request, draft, error=str(exc))
    save_pdf_recommendation(import_id, recommendation)
    return RedirectResponse(request.url_for('preview_pdf', import_id=import_id), status_code=303)


@router.get('/pdf-imports/{import_id}', response_class=HTMLResponse)
def preview_pdf(request: Request, import_id: UUID):
    draft = get_pdf_import(import_id)
    if draft.assignment_id is not None:
        return RedirectResponse(request.url_for('notice_detail', assignment_id=draft.assignment_id), status_code=303)
    return render_pdf_preview(request, draft)


@router.post('/pdf-imports/{import_id}/save', response_class=HTMLResponse)
def save_pdf_notice(request: Request, import_id: UUID,
                    title: Annotated[str, Form()] = '', selected: Annotated[list[str], Form()] = [],
                    append_to: Annotated[str, Form()] = ''):
    draft = get_pdf_import(import_id)
    if append_to:
        if not append_to.isdigit():
            return render_pdf_preview(request, draft, title=title, selected=set(selected),
                                      error='추가 공지를 붙일 공지를 다시 선택해 주세요.')
        get_assignment(int(append_to))
        try:
            assignment_id = append_pdf_import(import_id, int(append_to), title, selected)
        except ValueError as exc:
            return render_pdf_preview(request, draft, title=title, selected=set(selected), error=str(exc),
                                      append_to=append_to)
        url = request.url_for('notice_detail', assignment_id=assignment_id).include_query_params(appended=1)
        return RedirectResponse(str(url), status_code=303)
    try:
        assignment_id = save_pdf_import(import_id, title, selected)
    except ValueError as exc:
        return render_pdf_preview(request, draft, title=title, selected=set(selected), error=str(exc))
    return RedirectResponse(request.url_for('notice_detail', assignment_id=assignment_id), status_code=303)


@router.get("/notices/{assignment_id}", response_class=HTMLResponse)
def notice_detail(request: Request, assignment_id: int, saved: int | None = None, edited: int | None = None,
                  appended: int | None = None, deleted: int | None = None):
    return render_detail(request, get_assignment(assignment_id), saved_item_id=saved, edited_item_id=edited,
                         appended=bool(appended), item_deleted=bool(deleted))


@router.post('/notices/{assignment_id}/delete')
def remove_notice(request: Request, assignment_id: int):
    delete_assignment(assignment_id)
    return RedirectResponse(str(request.url_for('home').include_query_params(deleted=1)), status_code=303)


@router.post('/notices/{assignment_id}/checklist/{item_id}/delete')
def remove_checklist_item(request: Request, assignment_id: int, item_id: int):
    delete_checklist_item(assignment_id, item_id)
    url = request.url_for('notice_detail', assignment_id=assignment_id).include_query_params(deleted=1)
    return RedirectResponse(str(url) + '#checklist-heading', status_code=303)


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


@router.post('/notices/{assignment_id}/checklist/{item_id}/edit', response_class=HTMLResponse)
def edit_checklist_item(
    request: Request, assignment_id: int, item_id: int,
    title: Annotated[str, Form()] = '',
    evidence: Annotated[str, Form()] = '',
):
    assignment = get_assignment(assignment_id)
    try:
        update_checklist_item(assignment, item_id, title, evidence)
    except ValueError as exc:
        message = '항목 이름은 1~200자, 근거는 4,000자 이내로 입력해 주세요.' if isinstance(exc, ValidationError) else str(exc)
        return render_detail(request, assignment, edit_error=message,
                             edit_values={'item_id': item_id, 'title': title, 'evidence': evidence})
    url = request.url_for('notice_detail', assignment_id=assignment_id).include_query_params(edited=item_id)
    return RedirectResponse(str(url) + f'#checklist-item-{item_id}', status_code=303)


@router.post("/notices/{assignment_id}/checklist/{item_id}/completion")
def update_completion(
    request: Request,
    assignment_id: int,
    item_id: int,
    completed: Annotated[bool, Form()],
):
    set_checklist_completed(assignment_id, item_id, completed)
    if request.headers.get('x-requested-with') == 'fetch':
        # In-page toggle: no reload, the page keeps its scroll position.
        items = list_checklist(assignment_id)
        return JSONResponse({'completed': completed, 'completed_count': sum(item.completed for item in items),
                             'total': len(items)})
    url = request.url_for("notice_detail", assignment_id=assignment_id)
    return RedirectResponse(str(url) + '#checklist-heading', status_code=303)


@router.post('/notices/{assignment_id}/checklist/{item_id}/preparation', response_class=HTMLResponse)
def update_preparation(
    request: Request, assignment_id: int, item_id: int,
    submission_url: Annotated[str, Form()] = '',
    preparation_note: Annotated[str, Form()] = '',
):
    assignment = get_assignment(assignment_id)
    try:
        save_checklist_preparation(assignment_id, item_id, submission_url, preparation_note)
    except ValueError as exc:
        return render_detail(request, assignment, preparation_error=str(exc), preparation_values={
            'item_id': item_id, 'submission_url': submission_url, 'preparation_note': preparation_note,
        })
    url = request.url_for('notice_detail', assignment_id=assignment_id).include_query_params(saved=item_id)
    return RedirectResponse(str(url) + f'#checklist-item-{item_id}', status_code=303)


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
