"""Alembic uses the process environment, never reads credential files."""

import asyncio
import os

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.db.models import Base


def database_url() -> str:
    url = os.environ["DATABASE_URL"]
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=database_url(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
