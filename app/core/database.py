from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

# Sized for the 4-core / 8 GB VDS: every connection is a Postgres process of
# its own, and Postgres allows 60. Thirty at most here leaves room for the
# price-intel service (8), the backups and a psql session. Five plus five
# (the old 1 GB server's size) ran out twice in November 2026 under ordinary
# load. A request that finds them all busy waits ten seconds and then fails,
# rather than queueing without end; a connection is checked before use and
# replaced every five minutes, so one Postgres dropped is never handed out.
POOL = {"pool_size": 15, "max_overflow": 15, "pool_timeout": 10, "pool_recycle": 300, "pool_pre_ping": True}

engine = create_async_engine(settings.DATABASE_URL, echo=settings.DEBUG, **POOL)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    async with async_session() as session:
        yield session
