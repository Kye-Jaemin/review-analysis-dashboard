from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.i18n import COOKIE_MAX_AGE
from app.jobs import registry
from app.models import Analysis, AnalysisJob, AnalysisStatus, Category, Investigation, Review, Source
from app.routes.reviews import _parse_int
from app.services.analyzer import _select_review_ids, run_analysis_job
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

    source_rows = (
        await session.execute(select(Source).order_by(Source.label))
    ).scalars().all()
    sources = [
        {
            "id": s.id,
            "label": s.label,
            "type": s.type.value if hasattr(s.type, "value") else str(s.type),
            "icon_url": s.icon_url,
        }
        for s in source_rows
    ]

    return render(
        request,
        "analyze.html",
        counts={"total": total, "unanalyzed": unanalyzed, "failed": failed},
        allowed_models=settings.allowed_models,
        last_model=last_model,
        roots=roots,
        sources=sources,
    )


@router.get("/api/analyze/count")
async def analyze_count(
    mode: str = Query("unanalyzed"),
    source_ids: List[str] = Query(default_factory=list),
    session: AsyncSession = Depends(get_session),
):
    s_ids = [v for v in (_parse_int(s) for s in source_ids) if v is not None]
    ids = await _select_review_ids(session, mode, source_ids=s_ids or None)
    return {"count": len(ids), "mode": mode, "source_ids": s_ids}


@router.post("/analyze")
async def start_analysis(
    request: Request,
    background_tasks: BackgroundTasks,
    mode: str = Form("unanalyzed"),
    model: str = Form(""),
    summary_lang: str = Form("en"),
    root_ids: List[str] = Form(default_factory=list),
    source_ids: List[str] = Form(default_factory=list),
    investigation_label: str = Form(""),
    min_confidence: float = Form(0.0),
    session: AsyncSession = Depends(get_session),
):
    model = model or settings.ANTHROPIC_MODEL
    if model not in settings.allowed_models:
        model = settings.ANTHROPIC_MODEL

    parsed_roots = [v for v in (_parse_int(s) for s in root_ids) if v is not None]
    parsed_sources = [v for v in (_parse_int(s) for s in source_ids) if v is not None]

    # If the user provided a label, persist (source_ids, root_ids) as a
    # dashboard card. Empty label means "just run, no card".
    label = (investigation_label or "").strip()
    if label:
        inv = Investigation(
            label=label[:200],
            source_ids=parsed_sources,
            root_ids=parsed_roots,
        )
        session.add(inv)
        await session.commit()

    aj = AnalysisJob(status="running", model=model)
    session.add(aj)
    await session.commit()
    await session.refresh(aj)

    job = registry.create("analysis")
    job.db_id = aj.id
    job.status = "pending"

    # Clamp confidence to [0, 1] just in case the form was tampered with.
    clamped_conf = max(0.0, min(1.0, float(min_confidence or 0.0)))

    background_tasks.add_task(
        run_analysis_job,
        job.id,
        aj.id,
        mode,
        model,
        summary_lang,
        parsed_roots or None,
        parsed_sources or None,
        clamped_conf,
    )
    registry.prune()

    response = render(request, "partials/job_progress.html", job=job)
    response.set_cookie(MODEL_COOKIE, model, max_age=COOKIE_MAX_AGE, samesite="lax")
    return response
