from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.i18n import COOKIE_MAX_AGE
from app.jobs import registry
from app.models import Analysis, AnalysisJob, AnalysisStatus, Review
from app.services.analyzer import run_analysis_job
from app.templating import render

router = APIRouter()

MODEL_COOKIE = "analysis_model"


@router.get("/analyze")
async def analyze_page(request: Request, session: AsyncSession = Depends(get_session)):
    total = (await session.execute(select(func.count(Review.id)))).scalar() or 0
    unanalyzed = (
        await session.execute(
            select(func.count(Review.id)).outerjoin(Analysis, Analysis.review_id == Review.id).where(Analysis.id.is_(None))
        )
    ).scalar() or 0
    failed = (
        await session.execute(select(func.count(Analysis.id)).where(Analysis.status == AnalysisStatus.failed))
    ).scalar() or 0

    last_model = request.cookies.get(MODEL_COOKIE) or settings.ANTHROPIC_MODEL

    return render(
        request,
        "analyze.html",
        counts={"total": total, "unanalyzed": unanalyzed, "failed": failed},
        allowed_models=settings.allowed_models,
        last_model=last_model,
    )


@router.post("/analyze")
async def start_analysis(
    request: Request,
    background_tasks: BackgroundTasks,
    mode: str = Form("unanalyzed"),
    model: str = Form(""),
    summary_lang: str = Form("en"),
    session: AsyncSession = Depends(get_session),
):
    model = model or settings.ANTHROPIC_MODEL
    if model not in settings.allowed_models:
        model = settings.ANTHROPIC_MODEL

    aj = AnalysisJob(status="running", model=model)
    session.add(aj)
    await session.commit()
    await session.refresh(aj)

    job = registry.create("analysis")
    job.db_id = aj.id
    job.status = "pending"

    background_tasks.add_task(run_analysis_job, job.id, aj.id, mode, model, summary_lang)
    registry.prune()

    response = render(request, "partials/job_progress.html", job=job)
    response.set_cookie(MODEL_COOKIE, model, max_age=COOKIE_MAX_AGE, samesite="lax")
    return response
