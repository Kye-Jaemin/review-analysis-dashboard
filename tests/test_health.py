import pytest


@pytest.mark.asyncio
async def test_health(app_client):
    resp = await app_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_dashboard_page_renders(app_client):
    resp = await app_client.get("/")
    assert resp.status_code == 200
    assert "Review Dashboard" in resp.text or "리뷰" in resp.text


@pytest.mark.asyncio
async def test_set_language_cookie(app_client):
    resp = await app_client.get("/set-language/ko", follow_redirects=False)
    assert resp.status_code == 303
    assert "lang=ko" in resp.headers.get("set-cookie", "")
