"""Integration test for the criterion → vendor-grouped reviews endpoint.

Seeds a VendorReasonCard (with review_ids) + the underlying reviews, then
hits POST /competitive-v3/criterion-reviews and asserts the per-vendor
grouping and cross-reason dedup.
"""
import json
from datetime import datetime

import pytest


@pytest.mark.asyncio
async def test_criterion_reviews_groups_and_dedups(app_client):
    from app.db import AsyncSessionLocal
    from app.models import Review, Source, SourceType, VendorReasonCard

    async with AsyncSessionLocal() as s:
        src = Source(type=SourceType.google_play, label="t", config={})
        s.add(src)
        await s.flush()
        ids = []
        for i in range(1, 4):
            r = Review(
                source_id=src.id,
                external_id=f"e{i}",
                text=f"review {i}",
                posted_at=datetime(2024, 1, 1),
            )
            s.add(r)
            await s.flush()
            ids.append(r.id)
        rid0, rid1, rid2 = ids
        s.add(VendorReasonCard(
            vendor_key="acme",
            vendor_display="Acme",
            category_name="UX",
            band="positive",
            label="UX",
            sample_size=3,
            source_ids_snapshot=[src.id],
            reasons=[
                {"reason": "빠른 입력", "count": 2, "examples": [], "review_ids": [rid0, rid1]},
                # overlaps rid1 → must be deduped at the vendor level
                {"reason": "정확한 인식", "count": 2, "examples": [], "review_ids": [rid1, rid2]},
            ],
        ))
        await s.commit()

    # Both reasons live under the SAME top category here → one category
    # block, one vendor, 3 distinct reviews (rid1 shared, deduped).
    descriptors = [
        {"top_category": "건강 데이터 추적", "vendor_key": "acme", "vendor_display": "Acme",
         "category_name": "UX", "band": "positive", "reason_text": "빠른 입력"},
        {"top_category": "건강 데이터 추적", "vendor_key": "acme", "vendor_display": "Acme",
         "category_name": "UX", "band": "positive", "reason_text": "정확한 인식"},
    ]
    r = await app_client.post(
        "/competitive-v3/criterion-reviews",
        data={"descriptors_json": json.dumps(descriptors)},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["categories"]) == 1
    cat = data["categories"][0]
    assert cat["name"] == "건강 데이터 추적"
    assert len(cat["vendors"]) == 1
    v = cat["vendors"][0]
    assert v["key"] == "acme"
    assert v["display"] == "Acme"
    # 3 distinct reviews despite rid1 appearing in both reasons.
    assert v["review_count"] == 3
    assert {rev["id"] for rev in v["reviews"]} == {rid0, rid1, rid2}
    assert cat["review_count"] == 3
    assert data["matched_reasons"] == 2
    assert data["missing_cards"] == 0
    # Feedback summary = the matched reasons (text + count), no LLM.
    assert {rs["text"] for rs in v["reasons"]} == {"빠른 입력", "정확한 인식"}
    assert all(rs["count"] == 2 for rs in v["reasons"])


@pytest.mark.asyncio
async def test_criterion_reviews_splits_by_top_category(app_client):
    """Two reasons in the same VendorReasonCard assigned to DIFFERENT top
    categories must land in separate category blocks."""
    from app.db import AsyncSessionLocal
    from app.models import Review, Source, SourceType, VendorReasonCard

    async with AsyncSessionLocal() as s:
        src = Source(type=SourceType.app_store, label="t2", config={})
        s.add(src)
        await s.flush()
        ids = []
        for i in range(1, 3):
            r = Review(source_id=src.id, external_id=f"s{i}", text=f"r{i}",
                       posted_at=datetime(2024, 1, 1))
            s.add(r)
            await s.flush()
            ids.append(r.id)
        a, b = ids
        s.add(VendorReasonCard(
            vendor_key="beta", vendor_display="Beta", category_name="기능",
            band="positive", label="기능", sample_size=2, source_ids_snapshot=[src.id],
            reasons=[
                {"reason": "칼로리 자동 계산", "count": 1, "examples": [], "review_ids": [a]},
                {"reason": "운동 자동 동기화", "count": 1, "examples": [], "review_ids": [b]},
            ],
        ))
        await s.commit()

    descriptors = [
        {"top_category": "칼로리·매크로·영양소 추적", "vendor_key": "beta", "vendor_display": "Beta",
         "category_name": "기능", "band": "positive", "reason_text": "칼로리 자동 계산"},
        {"top_category": "운동·수면·건강 데이터 추적", "vendor_key": "beta", "vendor_display": "Beta",
         "category_name": "기능", "band": "positive", "reason_text": "운동 자동 동기화"},
    ]
    r = await app_client.post(
        "/competitive-v3/criterion-reviews",
        data={"descriptors_json": json.dumps(descriptors)},
    )
    data = r.json()
    names = [c["name"] for c in data["categories"]]
    assert names == ["칼로리·매크로·영양소 추적", "운동·수면·건강 데이터 추적"]  # first-seen order
    assert all(len(c["vendors"]) == 1 and c["vendors"][0]["key"] == "beta" for c in data["categories"])


@pytest.mark.asyncio
async def test_criterion_reviews_feedback_only_no_review_ids(app_client):
    """A reason with NO review_ids still surfaces its feedback summary;
    the vendor appears with review_count 0 but a non-empty reasons list."""
    from app.db import AsyncSessionLocal
    from app.models import Source, SourceType, VendorReasonCard

    async with AsyncSessionLocal() as s:
        src = Source(type=SourceType.reddit, label="t3", config={})
        s.add(src)
        await s.flush()
        s.add(VendorReasonCard(
            vendor_key="gamma", vendor_display="Gamma", category_name="가격",
            band="positive", label="가격", sample_size=0, source_ids_snapshot=[src.id],
            reasons=[
                {"reason": "무료로 충분히 쓸 만함", "count": 9, "examples": [], "review_ids": []},
            ],
        ))
        await s.commit()

    descriptors = [
        {"top_category": "무료·가격 가치", "vendor_key": "gamma", "vendor_display": "Gamma",
         "category_name": "가격", "band": "positive", "reason_text": "무료로 충분히 쓸 만함"},
    ]
    r = await app_client.post(
        "/competitive-v3/criterion-reviews",
        data={"descriptors_json": json.dumps(descriptors)},
    )
    data = r.json()
    assert len(data["categories"]) == 1
    v = data["categories"][0]["vendors"][0]
    assert v["key"] == "gamma"
    assert v["review_count"] == 0
    assert v["reviews"] == []
    # The feedback summary is still present.
    assert [rs["text"] for rs in v["reasons"]] == ["무료로 충분히 쓸 만함"]
    assert v["reason_count"] == 1
    assert data["categories"][0]["reason_count"] == 1


@pytest.mark.asyncio
async def test_criterion_reviews_no_card_uses_file_counts(app_client):
    """No saved card → still surface the reason + its file count so the
    numbers add up (just no raw review bodies). This is the WeightWatchers
    case: present in the upload, but never reason-analyzed on /vendors."""
    descriptors = [
        {"top_category": "무료·가격 가치", "vendor_key": "weightwatchers",
         "vendor_display": "Weight Watchers", "category_name": "가격",
         "band": "positive", "reason_text": "구독 가치가 높음", "count": 14},
    ]
    r = await app_client.post(
        "/competitive-v3/criterion-reviews",
        data={"descriptors_json": json.dumps(descriptors)},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["missing_cards"] == 1
    assert len(data["categories"]) == 1
    cat = data["categories"][0]
    assert cat["feedback_total"] == 14
    v = cat["vendors"][0]
    assert v["display"] == "Weight Watchers"
    assert v["review_count"] == 0          # no bodies
    assert v["reviews"] == []
    assert v["feedback_total"] == 14       # number still adds up
    assert [rs["text"] for rs in v["reasons"]] == ["구독 가치가 높음"]
    assert data["total_feedback"] == 14


@pytest.mark.asyncio
async def test_criterion_reviews_sorted_by_feedback_desc(app_client):
    """Vendors rank by feedback_total desc even when the bigger number
    has no raw review bodies (no saved card)."""
    from app.db import AsyncSessionLocal
    from app.models import Review, Source, SourceType, VendorReasonCard

    async with AsyncSessionLocal() as s:
        src = Source(type=SourceType.web, label="t4", config={})
        s.add(src)
        await s.flush()
        # Small vendor WITH review bodies (2 reviews, count 2).
        small_ids = []
        for i in range(1, 3):
            rv = Review(source_id=src.id, external_id=f"w{i}", text=f"x{i}",
                        posted_at=datetime(2024, 1, 1))
            s.add(rv)
            await s.flush()
            small_ids.append(rv.id)
        s.add(VendorReasonCard(
            vendor_key="small", vendor_display="Small", category_name="가격",
            band="positive", label="가격", sample_size=2, source_ids_snapshot=[src.id],
            reasons=[{"reason": "저렴함", "count": 2, "examples": [], "review_ids": small_ids}],
        ))
        await s.commit()

    descriptors = [
        # Big number, NO card → bodies absent but count 30.
        {"top_category": "무료·가격 가치", "vendor_key": "big", "vendor_display": "Big",
         "category_name": "가격", "band": "positive", "reason_text": "가성비 최고", "count": 30},
        # Small number, WITH bodies.
        {"top_category": "무료·가격 가치", "vendor_key": "small", "vendor_display": "Small",
         "category_name": "가격", "band": "positive", "reason_text": "저렴함", "count": 2},
    ]
    r = await app_client.post(
        "/competitive-v3/criterion-reviews",
        data={"descriptors_json": json.dumps(descriptors)},
    )
    data = r.json()
    vendors = data["categories"][0]["vendors"]
    # Big (30, no bodies) ranks above Small (2, with bodies).
    assert [v["display"] for v in vendors] == ["Big", "Small"]
    assert [v["feedback_total"] for v in vendors] == [30, 2]
    assert vendors[0]["review_count"] == 0
    assert vendors[1]["review_count"] == 2


@pytest.mark.asyncio
async def test_criterion_reviews_bad_payload(app_client):
    r = await app_client.post(
        "/competitive-v3/criterion-reviews",
        data={"descriptors_json": "not json"},
    )
    assert r.status_code == 422
    r = await app_client.post(
        "/competitive-v3/criterion-reviews",
        data={"descriptors_json": "{}"},
    )
    assert r.status_code == 422
