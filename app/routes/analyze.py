from datetime import datetime
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
from app.services.auto_analyzer import run_auto_analysis_job
from app.services.themes import autogen_theme_snapshots
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
    classification_mode: str = Form("auto"),
    separate_user_tier: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    model = model or settings.ANTHROPIC_MODEL
    if model not in settings.allowed_models:
        model = settings.ANTHROPIC_MODEL

    parsed_roots = [v for v in (_parse_int(s) for s in root_ids) if v is not None]
    parsed_sources = [v for v in (_parse_int(s) for s in source_ids) if v is not None]

    label = (investigation_label or "").strip()
    inv: Investigation | None = None
    if label:
        inv = Investigation(
            label=label[:200],
            source_ids=parsed_sources,
            root_ids=parsed_roots,
        )
        session.add(inv)
        await session.commit()
        await session.refresh(inv)

    aj = AnalysisJob(status="running", model=model)
    session.add(aj)
    await session.commit()
    await session.refresh(aj)

    job = registry.create("analysis")
    job.db_id = aj.id
    job.status = "pending"

    clamped_conf = max(0.0, min(1.0, float(min_confidence or 0.0)))

    use_auto = classification_mode == "auto"
    if use_auto:
        if inv is None:
            # Auto mode needs an Investigation row to attach categories to.
            inv = Investigation(
                label=f"Auto · {datetime.utcnow():%Y-%m-%d %H:%M}",
                source_ids=parsed_sources,
                root_ids=[],
            )
            session.add(inv)
            await session.commit()
            await session.refresh(inv)
        tier_flag = (separate_user_tier or "").lower() in ("on", "true", "1", "yes")
        background_tasks.add_task(
            run_auto_analysis_job,
            job.id,
            aj.id,
            inv.id,
            mode,
            model,
            summary_lang,
            parsed_sources or None,
            clamped_conf,
            tier_flag,
        )
    else:
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

    # Once analysis is done, auto-generate the 5 sentiment-band mind maps for
    # the investigation. Runs as a second sequential background task — only
    # makes sense when there's a card to attach them to.
    if inv is not None:
        background_tasks.add_task(
            autogen_theme_snapshots, inv.id, model, summary_lang,
        )

    registry.prune()

    response = render(request, "partials/job_progress.html", job=job)
    response.set_cookie(MODEL_COOKIE, model, max_age=COOKIE_MAX_AGE, samesite="lax")
    return response
