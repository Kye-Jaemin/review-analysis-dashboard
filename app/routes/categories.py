from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.models.category import Category
from app.templating import render

router = APIRouter()


class CategoryIn(BaseModel):
    name: str
    description: str = ""
    parent_id: Optional[int] = None


class CategoryPatch(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None


async def _compute_path(session: AsyncSession, cat: Category) -> str:
    parts = [cat.name]
    cur = cat
    while cur.parent_id is not None:
        cur = await session.get(Category, cur.parent_id)
        if cur is None:
            break
        parts.append(cur.name)
    return " > ".join(reversed(parts))


async def _refresh_subtree_paths(session: AsyncSession, node_id: int) -> None:
    node = await session.get(Category, node_id)
    if node is None:
        return
    node.path = await _compute_path(session, node)
    result = await session.execute(select(Category).where(Category.parent_id == node_id))
    for child in result.scalars().all():
        await _refresh_subtree_paths(session, child.id)


def _build_tree(rows):
    by_id = {r.id: {"id": r.id, "name": r.name, "description": r.description, "path": r.path, "parent_id": r.parent_id, "children": []} for r in rows}
    roots = []
    for r in rows:
        node = by_id[r.id]
        if r.parent_id and r.parent_id in by_id:
            by_id[r.parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


@router.get("/categories")
async def categories_page(request: Request, session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Category).order_by(Category.path))
    rows = result.scalars().all()
    tree = _build_tree(rows)
    return render(request, "categories.html", tree=tree)


@router.get("/api/categories")
async def categories_api(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(Category).order_by(Category.path))
    rows = result.scalars().all()
    return {"tree": _build_tree(rows)}


@router.post("/categories")
async def create_category(payload: CategoryIn, session: AsyncSession = Depends(get_session)):
    cat = Category(name=payload.name.strip(), description=payload.description or "", parent_id=payload.parent_id)
    session.add(cat)
    await session.flush()
    cat.path = await _compute_path(session, cat)
    await session.commit()
    return {"id": cat.id, "path": cat.path}


@router.patch("/categories/{cat_id}")
async def update_category(cat_id: int, payload: CategoryPatch, session: AsyncSession = Depends(get_session)):
    cat = await session.get(Category, cat_id)
    if cat is None:
        raise HTTPException(404, "category not found")
    if payload.name is not None:
        cat.name = payload.name.strip()
    if payload.description is not None:
        cat.description = payload.description
    if payload.parent_id is not None:
        if payload.parent_id == cat_id:
            raise HTTPException(400, "cannot set self as parent")
        cat.parent_id = payload.parent_id
    await session.flush()
    await _refresh_subtree_paths(session, cat.id)
    await session.commit()
    return {"id": cat.id, "path": cat.path}


@router.delete("/categories/{cat_id}")
async def delete_category(cat_id: int, session: AsyncSession = Depends(get_session)):
    cat = await session.get(Category, cat_id)
    if cat is None:
        raise HTTPException(404, "category not found")
    await session.delete(cat)
    await session.commit()
    return {"ok": True}
