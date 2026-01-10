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

            if columns and "error" not in columns:
                await conn.execute(text("ALTER TABLE step_executions ADD COLUMN error TEXT"))
        except Exception:
            pass  # Table doesn't exist yet, create_all will handle it

        await conn.run_sync(Base.metadata.create_all)
