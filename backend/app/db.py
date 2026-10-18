"""Postgres access (asyncpg) and schema migrations.

uv run python -m app.db migrate
"""

import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import asyncpg

from app.config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
# Any constant works; it just serializes concurrent migrate() calls.
_MIGRATION_LOCK = 4_172_301


async def _init(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


def _use_schema(schema: str) -> Callable[[asyncpg.Connection], Awaitable[None]]:
    """A schema other than public gives each test run its own tables.

    Set with SQL rather than as a startup parameter, which Neon's proxy drops, and on
    every pool checkout because returning a connection to the pool resets it. Public
    stays on the path, after it, for extension types like pgvector's; everything the
    migrations create goes in the first schema.
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
        raise ValueError(f"bad schema name {schema!r}")

    async def use(conn: asyncpg.Connection) -> None:
        await conn.execute(f"set search_path to {schema}, public")

    return use


async def connect(url: str | None = None, *, schema: str | None = None) -> asyncpg.Connection:
    conn = await asyncpg.connect(url or _require_url(), timeout=30)
    await _init(conn)
    if schema:
        await _use_schema(schema)(conn)
    return conn


async def create_pool(
    url: str | None = None, *, schema: str | None = None, max_size: int = 5
) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        url or _require_url(),
        min_size=0,
        max_size=max_size,
        init=_init,
        setup=_use_schema(schema) if schema else None,
        timeout=30,
    )


async def migrate(conn: asyncpg.Connection) -> list[str]:
    """Apply migrations/*.sql that haven't run yet, in name order. Returns their names."""
    applied_now = []
    async with conn.transaction():
        await conn.execute("select pg_advisory_xact_lock($1)", _MIGRATION_LOCK)
        await conn.execute(
            "create table if not exists schema_migrations ("
            " version text primary key, applied_at timestamptz not null default now())"
        )
        done = {r["version"] for r in await conn.fetch("select version from schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in done:
                continue
            await conn.execute(path.read_text(encoding="utf-8"))
            await conn.execute("insert into schema_migrations (version) values ($1)", path.stem)
            applied_now.append(path.stem)
    return applied_now


def _require_url() -> str:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return settings.database_url


async def _main(command: str) -> None:
    conn = await connect()
    try:
        if command == "migrate":
            applied = await migrate(conn)
            print("applied: " + ", ".join(applied) if applied else "already up to date")
    finally:
        await conn.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["migrate"]:
        sys.exit("usage: python -m app.db migrate")
    asyncio.run(_main("migrate"))
