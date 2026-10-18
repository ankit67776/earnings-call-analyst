import os
import uuid

import pytest

from app import db

# Database tests run only when this is set. Each test gets a fresh schema that is
# dropped afterwards, so pointing it at a dev database leaves no trace.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def pool():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = await db.connect(TEST_DATABASE_URL)
    # Extensions are per database; installed in public, every test schema can use them.
    await admin.execute("create extension if not exists vector schema public")
    await admin.execute(f'create schema "{schema}"')
    pool = await db.create_pool(TEST_DATABASE_URL, schema=schema)
    try:
        async with pool.acquire() as conn:
            # Never let a test write outside its own schema.
            assert await conn.fetchval("show search_path") == f"{schema}, public"
            await db.migrate(conn)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'drop schema "{schema}" cascade')
        await admin.close()
