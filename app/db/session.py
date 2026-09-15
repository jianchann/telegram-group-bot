from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class Database:
    def __init__(self, url: str) -> None:
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=2,
            pool_timeout=10,
            connect_args={"prepare_threshold": None},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self) -> None:
        await self.engine.dispose()
