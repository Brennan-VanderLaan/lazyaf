from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(settings.database_url, echo=True)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        # Migration: Add missing columns to step_executions if they don't exist
        # SQLite doesn't support IF NOT EXISTS for columns, so we check first
        try:
            result = await conn.execute(text("PRAGMA table_info(step_executions)"))
            columns = {row[1] for row in result.fetchall()}

            if columns:
                # All columns that may be missing from step_executions
                step_exec_columns = {
                    "error": "TEXT",
                    "progress": "TEXT",
                    "last_heartbeat": "DATETIME",
                    "timeout_at": "DATETIME",
                    "started_at": "DATETIME",
                    "completed_at": "DATETIME",
                    "created_at": "DATETIME",
                    "container_id": "VARCHAR(64)",
                }
                for col_name, col_type in step_exec_columns.items():
                    if col_name not in columns:
                        await conn.execute(text(f"ALTER TABLE step_executions ADD COLUMN {col_name} {col_type}"))
        except Exception:
            pass  # Table doesn't exist yet, create_all will handle it

        # Migration: Add steps_graph column to pipelines table (Graph Creep Phase 1)
        try:
            result = await conn.execute(text("PRAGMA table_info(pipelines)"))
            columns = {row[1] for row in result.fetchall()}

            if columns and "steps_graph" not in columns:
                await conn.execute(text("ALTER TABLE pipelines ADD COLUMN steps_graph TEXT"))
        except Exception:
            pass  # Table doesn't exist yet, create_all will handle it

        # Migration: Add graph execution tracking columns to pipeline_runs
        try:
            result = await conn.execute(text("PRAGMA table_info(pipeline_runs)"))
            columns = {row[1] for row in result.fetchall()}

            if columns:
                if "active_step_ids" not in columns:
                    await conn.execute(text("ALTER TABLE pipeline_runs ADD COLUMN active_step_ids TEXT DEFAULT '[]'"))
                if "completed_step_ids" not in columns:
                    await conn.execute(text("ALTER TABLE pipeline_runs ADD COLUMN completed_step_ids TEXT DEFAULT '[]'"))
        except Exception:
            pass  # Table doesn't exist yet, create_all will handle it

        # Migration: Add step_id column to step_runs for graph execution
        try:
            result = await conn.execute(text("PRAGMA table_info(step_runs)"))
            columns = {row[1] for row in result.fetchall()}

            if columns and "step_id" not in columns:
                await conn.execute(text("ALTER TABLE step_runs ADD COLUMN step_id VARCHAR(64)"))
        except Exception:
            pass  # Table doesn't exist yet, create_all will handle it

        await conn.run_sync(Base.metadata.create_all)
