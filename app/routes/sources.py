from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal, get_session
from app.jobs import registry
from app.models import CollectionJob, CollectionStatus, Investigation, Review, Source, SourceType
from app.services.collectors import COLLECTORS, get_collector
from app.templating import render

router = APIRouter()


class SourceIn(BaseModel):
    type: SourceType
    label: str
    display_name: Optional[str] = None
    icon_url: Optional[str] = None
    config: dict = {}


@router.get("/sources")
async def list_sources(request: Request, session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Source).order_by(Source.id.desc()))).scalars().all()
    last_runs: dict[int, CollectionJob] = {}
    for src in rows:
        last = (
            await session.execute(
                select(CollectionJob)
                .where(CollectionJob.source_id == src.id)
                .order_by(CollectionJob.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if last:
            last_runs[src.id] = last

    # Total collected reviews per source — surfaced prominently next to each
    # row so the user sees how big each source actually is at a glance.
    count_rows = (
        await session.execute(
            select(Review.source_id, func.count(Review.id))
            .group_by(Review.source_id)
        )
    ).all()
    review_counts: dict[int, int] = {sid: cnt for sid, cnt in count_rows}

    return render(
        request,
        "sources.html",
        sources=rows,
        last_runs=last_runs,
        review_counts=review_counts,
    )


@router.get("/sources/search")
async def search_candidates(
    type: SourceType = Query(...),
    query: str = Query(..., min_length=1),
    country: str = Query("us"),
    lang: str = Query("en"),
):
    cls = COLLECTORS[type]
    try:
        results = await cls.search(query=query, country=country, lang=lang)
    except Exception as e:
        return JSONResponse({"error": str(e), "results": []}, status_code=200)
    return {"results": results}


@router.post("/sources")
async def create_source(payload: SourceIn, session: AsyncSession = Depends(get_session)):
    src = Source(
        type=payload.type,
        label=payload.label.strip() or payload.type.value,
        display_name=payload.display_name,
        icon_url=payload.icon_url,
        config=payload.config or {},
    )
    session.add(src)
    await session.commit()
    return {"id": src.id}


async def _prune_source_from_investigations(session: AsyncSession, src_id: int) -> None:
    """Investigation.source_ids is a JSON int array, not a FK, so deleting
    a Source doesn't automatically clean up references. Without this any
    card that pointed at the deleted source shows review_count = 0 on the
    dashboard, because the list endpoint can't resolve the dead id back
    to a Source row and silently drops it. Scrub references here before
    the Source row goes away."""
    invs = (await session.execute(select(Investigation))).scalars().all()
    for inv in invs:
        ids = list(inv.source_ids or [])
        if src_id in ids:
            inv.source_ids = [x for x in ids if x != src_id]


@router.post("/sources/{src_id}/delete")
async def delete_source_form(src_id: int, session: AsyncSession = Depends(get_session)):
    src = await session.get(Source, src_id)
    if src:
        await _prune_source_from_investigations(session, src_id)
        await session.delete(src)
        await session.commit()
    return RedirectResponse(url="/sources", status_code=303)


@router.delete("/sources/{src_id}")
async def delete_source(src_id: int, session: AsyncSession = Depends(get_session)):
    src = await session.get(Source, src_id)
    if not src:
        raise HTTPException(404)
    await _prune_source_from_investigations(session, src_id)
    await session.delete(src)
    await session.commit()
    return {"ok": True}


async def _run_collection(job_id: str, source_id: int, db_job_id: int) -> None:
    job = registry.get(job_id)
    if not job:
        return
    job.status = "running"
    try:
        async with AsyncSessionLocal() as session:
            src = await session.get(Source, source_id)
            if not src:
                raise RuntimeError("source not found")
            collector = get_collector(src)

            fetched = 0
            new_count = 0
            async for item in collector.collect():
                fetched += 1
                exists = (
                    await session.execute(
                        select(Review.id).where(
                            Review.source_id == src.id, Review.external_id == item.external_id
                        )
                    )
                ).scalar_one_or_none()
                if exists:
                    job.processed = fetched
                    continue
                # Normalize to naive UTC — DB column is TIMESTAMP WITHOUT TIME ZONE.
                # Apple RSS / Google Play occasionally hand back tz-aware values.
                posted_at = item.posted_at
                if posted_at is not None and posted_at.tzinfo is not None:
                    posted_at = posted_at.astimezone(timezone.utc).replace(tzinfo=None)
                review = Review(
                    source_id=src.id,
                    external_id=item.external_id,
                    author=item.author,
                    posted_at=posted_at,
                    rating=item.rating,
                    text=item.text,
                    url=item.url,
                    raw=item.raw or {},
                )
                session.add(review)
                try:
                    await session.flush()
                    new_count += 1
                except IntegrityError:
                    await session.rollback()
                job.processed = fetched
                job.new_count = new_count
                job.total = max(fetched, job.total)
                job.progress = min(99, int(fetched / max(job.total, 1) * 100))
                job.message = f"fetched {fetched}, new {new_count}"

            db_job = await session.get(CollectionJob, db_job_id)
            if db_job:
                db_job.status = CollectionStatus.succeeded
                db_job.finished_at = datetime.utcnow()
                db_job.fetched_count = fetched
                db_job.new_count = new_count
            await session.commit()

        job.status = "succeeded"
        job.progress = 100
        job.finished_at = datetime.utcnow()
    except Exception as e:
        job.status = "failed"
        job.error = str(e)
        job.finished_at = datetime.utcnow()
        try:
            async with AsyncSessionLocal() as session:
                db_job = await session.get(CollectionJob, db_job_id)
                if db_job:
                    db_job.status = CollectionStatus.failed
                    db_job.finished_at = datetime.utcnow()
                    db_job.error = str(e)[:2000]
                    await session.commit()
        except Exception:
            pass


@router.post("/sources/{src_id}/collect")
async def start_collection(
    src_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    src = await session.get(Source, src_id)
    if not src:
        raise HTTPException(404)

    db_job = CollectionJob(source_id=src.id, status=CollectionStatus.running)
    session.add(db_job)
    await session.commit()
    await session.refresh(db_job)

    job = registry.create("collection")
    job.db_id = db_job.id
    job.status = "pending"
    job.total = int(src.config.get("max_count") or src.config.get("max_submissions") or 100)

    background_tasks.add_task(_run_collection, job.id, src.id, db_job.id)
    registry.prune()

    return render(request, "partials/job_progress.html", job=job)


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, request: Request):
    job = registry.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    if request.headers.get("hx-request") or "text/html" in request.headers.get("accept", ""):
        return render(request, "partials/job_progress.html", job=job)
    return job.as_dict()
