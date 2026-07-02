"""Vendor categories — user-named groups of existing Investigation cards,
used to scope /vendors (and, via its Excel export, /competitive-v3).

Covers: CRUD roundtrip, the source-id union across member investigations
(including a multi-vendor card), DB-level filtering on /vendors, the
list_vendors() backward-compat guard, self-heal of dead investigation
ids, an empty-category scope, reorder, and a registration regression
guard (see app/db.py's init_db() import list — a model missing there
makes this 200 turn into a 500 on a fresh DB).
"""
import pytest


@pytest.mark.asyncio
async def test_vendor_category_crud(app_client):
    from app.db import AsyncSessionLocal
    from app.models import Investigation

    async with AsyncSessionLocal() as s:
        inv = Investigation(label="Card A", source_ids=[], root_ids=[], display_order=1)
        s.add(inv)
        await s.commit()
        inv_id = inv.id

    r = await app_client.post(
        "/api/vendor-categories", json={"label": "헬스", "investigation_ids": [inv_id]}
    )
    assert r.status_code == 200
    vc_id = r.json()["id"]

    r = await app_client.get("/api/vendor-categories")
    assert r.status_code == 200
    cats = r.json()["vendor_categories"]
    created = next(c for c in cats if c["id"] == vc_id)
    assert created["label"] == "헬스"
    assert created["investigation_count"] == 1
    assert created["investigation_ids"] == [inv_id]

    r = await app_client.patch(f"/api/vendor-categories/{vc_id}", json={"label": "헬스케어"})
    assert r.status_code == 200

    r = await app_client.patch(
        f"/api/vendor-categories/{vc_id}/visibility", json={"hidden": True}
    )
    assert r.status_code == 200

    r = await app_client.get("/api/vendor-categories")
    assert all(c["id"] != vc_id for c in r.json()["vendor_categories"])
    r = await app_client.get("/api/vendor-categories?include_hidden=true")
    hidden_cat = next(c for c in r.json()["vendor_categories"] if c["id"] == vc_id)
    assert hidden_cat["label"] == "헬스케어"
    assert hidden_cat["hidden"] is True

    r = await app_client.delete(f"/api/vendor-categories/{vc_id}")
    assert r.status_code == 200
    r = await app_client.get("/api/vendor-categories?include_hidden=true")
    assert all(c["id"] != vc_id for c in r.json()["vendor_categories"])


@pytest.mark.asyncio
async def test_resolve_source_ids_unions_across_investigations(app_client):
    from app.db import AsyncSessionLocal
    from app.models import Investigation, Source, SourceType
    from app.services.vendor_categories import (
        create_vendor_category,
        resolve_vendor_category_source_ids,
    )

    async with AsyncSessionLocal() as s:
        s1 = Source(type=SourceType.google_play, label="s1", display_name="UnionAlpha", config={})
        s2 = Source(type=SourceType.google_play, label="s2", display_name="UnionBeta", config={})
        s3 = Source(type=SourceType.app_store, label="s3", display_name="UnionGamma", config={})
        s.add_all([s1, s2, s3])
        await s.flush()
        # Investigation A targets one vendor; Investigation B is a
        # multi-vendor card spanning the other two sources.
        inv_a = Investigation(label="A", source_ids=[s1.id], root_ids=[], display_order=1)
        inv_b = Investigation(label="B", source_ids=[s2.id, s3.id], root_ids=[], display_order=2)
        s.add_all([inv_a, inv_b])
        await s.flush()
        vc = await create_vendor_category(
            s, label="Union test", investigation_ids=[inv_a.id, inv_b.id]
        )
        vc_id, s1_id, s2_id, s3_id = vc.id, s1.id, s2.id, s3.id

    async with AsyncSessionLocal() as s:
        source_ids, resolved_vc = await resolve_vendor_category_source_ids(s, vc_id)
        assert source_ids == {s1_id, s2_id, s3_id}
        assert resolved_vc is not None
        assert resolved_vc.id == vc_id


@pytest.mark.asyncio
async def test_vendors_page_filters_by_category(app_client):
    from datetime import datetime

    from app.db import AsyncSessionLocal
    from app.models import Investigation, Review, Source, SourceType

    async with AsyncSessionLocal() as s:
        alpha = Source(type=SourceType.google_play, label="a", display_name="ZzAlphaVendor", config={})
        beta = Source(type=SourceType.google_play, label="b", display_name="ZzBetaVendor", config={})
        s.add_all([alpha, beta])
        await s.flush()
        # list_vendors() skips vendors with zero reviews, so each source
        # needs at least one to actually show up on the page.
        s.add(Review(source_id=alpha.id, external_id="a1", text="great", posted_at=datetime(2024, 1, 1)))
        s.add(Review(source_id=beta.id, external_id="b1", text="great", posted_at=datetime(2024, 1, 1)))
        await s.flush()
        inv = Investigation(label="Alpha only", source_ids=[alpha.id], root_ids=[], display_order=1)
        s.add(inv)
        await s.commit()
        inv_id = inv.id

    r = await app_client.post(
        "/api/vendor-categories", json={"label": "AlphaCat", "investigation_ids": [inv_id]}
    )
    vc_id = r.json()["id"]

    r = await app_client.get(f"/vendors?vendor_category_id={vc_id}")
    assert r.status_code == 200
    assert "ZzAlphaVendor" in r.text
    assert "ZzBetaVendor" not in r.text

    # Unfiltered view still shows both.
    r = await app_client.get("/vendors")
    assert r.status_code == 200
    assert "ZzAlphaVendor" in r.text
    assert "ZzBetaVendor" in r.text


@pytest.mark.asyncio
async def test_list_vendors_positional_call_still_unfiltered(app_client):
    from datetime import datetime

    from app.db import AsyncSessionLocal
    from app.models import Review, Source, SourceType
    from app.services.vendors import list_vendors

    async with AsyncSessionLocal() as s:
        src = Source(type=SourceType.web, label="w1", display_name="ZzWebVendor", config={})
        s.add(src)
        await s.flush()
        s.add(Review(source_id=src.id, external_id="w1r1", text="ok", posted_at=datetime(2024, 1, 1)))
        await s.commit()

    async with AsyncSessionLocal() as s:
        vendors = await list_vendors(s)  # positional-only, no source_ids kwarg
        assert any(v["display"] == "ZzWebVendor" for v in vendors)


@pytest.mark.asyncio
async def test_self_heal_removes_dead_investigation_ids(app_client):
    from app.db import AsyncSessionLocal
    from app.models import Investigation

    async with AsyncSessionLocal() as s:
        inv = Investigation(label="Temp", source_ids=[], root_ids=[], display_order=1)
        s.add(inv)
        await s.commit()
        inv_id = inv.id

    r = await app_client.post(
        "/api/vendor-categories", json={"label": "Heals", "investigation_ids": [inv_id]}
    )
    vc_id = r.json()["id"]

    # Delete the investigation directly, simulating a card deletion that
    # doesn't know about (or care about) vendor categories referencing it.
    async with AsyncSessionLocal() as s:
        inv = await s.get(Investigation, inv_id)
        await s.delete(inv)
        await s.commit()

    r = await app_client.get("/api/vendor-categories")
    assert r.status_code == 200
    cat = next(c for c in r.json()["vendor_categories"] if c["id"] == vc_id)
    assert cat["investigation_ids"] == []
    assert cat["investigation_count"] == 0


@pytest.mark.asyncio
async def test_empty_category_scopes_to_zero_vendors_without_500(app_client):
    r = await app_client.post(
        "/api/vendor-categories", json={"label": "EmptyCat", "investigation_ids": []}
    )
    assert r.status_code == 200
    vc_id = r.json()["id"]

    r = await app_client.get(f"/vendors?vendor_category_id={vc_id}")
    assert r.status_code == 200
    assert "EmptyCat" in r.text


@pytest.mark.asyncio
async def test_reorder_vendor_categories(app_client):
    r1 = await app_client.post("/api/vendor-categories", json={"label": "ReorderFirst"})
    r2 = await app_client.post("/api/vendor-categories", json={"label": "ReorderSecond"})
    id1, id2 = r1.json()["id"], r2.json()["id"]

    r = await app_client.post("/api/vendor-categories/reorder", json={"ids": [id2, id1]})
    assert r.status_code == 200
    assert r.json()["count"] == 2

    r = await app_client.get("/api/vendor-categories")
    cats = r.json()["vendor_categories"]
    ordered = sorted((c for c in cats if c["id"] in (id1, id2)), key=lambda c: c["display_order"])
    assert [c["id"] for c in ordered] == [id2, id1]


@pytest.mark.asyncio
async def test_registration_guard_fresh_db_accepts_creation(app_client):
    """If VendorCategory is ever dropped from app/models/__init__.py or
    app/db.py's init_db() import list, the table won't exist on a fresh
    DB and this 200 silently becomes a 500 — exactly the bug that hit
    the competitive-v3 cards feature once."""
    r = await app_client.post("/api/vendor-categories", json={"label": "Smoke"})
    assert r.status_code == 200
    assert r.json()["label"] == "Smoke"
