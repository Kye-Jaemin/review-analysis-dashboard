from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.i18n import COOKIE_MAX_AGE
from app.jobs import registry
from app.models import Analysis, AnalysisJob, AnalysisStatus, Category, Review
from app.routes.reviews import _parse_int
from app.services.analyzer import run_analysis_job
from app.templating import render

router = APIRouter()

MODEL_COOKIE = "analysis_model"


@router.get("/analyze")
async def analyze_page(request: Request, session: AsyncSession = Depends(get_session)):
    total = (await session.execute(select(func.count(Review.id)))).scalar() or 0
    unanalyzed = (
        await session.execute(
            select(func.count(Review.id))
            .outerjoin(Analysis, Analysis.review_id == Review.id)
            .where(Analysis.id.is_(None))
        )
    ).scalar() or 0
    failed = (
        await session.execute(
            select(func.count(Analysis.id)).where(Analysis.status == AnalysisStatus.failed)
        )
    ).scalar() or 0

    last_model = request.cookies.get(MODEL_COOKIE) or settings.ANTHROPIC_MODEL

    root_rows = (
        await session.execute(
            select(Category).where(Category.parent_id.is_(None)).order_by(Category.name)
        )
    ).scalars().all()
    roots = [{"id": c.id, "name": c.name, "description": c.description} for c in root_rows]

    return render(
        request,
        "analyze.html",
        counts={"total": total, "unanalyzed": unanalyzed, "failed": failed},
        allowed_models=settings.allowed_models,
        last_model=last_model,
        roots=roots,
    )


@router.post("/analyze")
async def start_analysis(
    request: Request,
    background_tasks: BackgroundTasks,
    mode: str = Form("unanalyzed"),
    model: str = Form(""),
    summary_lang: str = Form("en"),
    root_ids: List[str] = Form(default_factory=list),
    session: AsyncSession = Depends(get_session),
):
    model = model or settings.ANTHROPIC_MODEL
    if model not in settings.allowed_models:
        model = settings.ANTHROPIC_MODEL

    parsed_roots = [v for v in (_parse_int(s) for s in root_ids) if v is not None]

    aj = AnalysisJob(status="running", model=model)
    session.add(aj)
    await session.commit()
    await session.refresh(aj)

    job = registry.create("analysis")
    job.db_id = aj.id
    job.status = "pending"

    background_tasks.add_task(
        run_analysis_job, job.id, aj.id, mode, model, summary_lang, parsed_roots or None
    )
    registry.prune()

    response = render(request, "partials/job_progress.html", job=job)
    response.set_cookie(MODEL_COOKIE, model, max_age=COOKIE_MAX_AGE, samesite="lax")
    return response
