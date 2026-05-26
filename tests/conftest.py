import asyncio
import os
import tempfile

import pytest
import pytest_asyncio

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")


@pytest_asyncio.fixture
async def app_client():
    from httpx import ASGITransport, AsyncClient

    from app.db import init_db
    from app.main import app

    await init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
