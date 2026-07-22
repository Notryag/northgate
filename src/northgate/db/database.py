from collections.abc import Callable

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

InvalidationListener = Callable[[BaseException | None], None]


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self._invalidation_listeners: list[InvalidationListener] = []
        event.listen(self.engine.sync_engine.pool, "invalidate", self._connection_invalidated)

    def add_invalidation_listener(self, listener: InvalidationListener) -> None:
        if listener not in self._invalidation_listeners:
            self._invalidation_listeners.append(listener)

    def _connection_invalidated(
        self,
        _dbapi_connection: object,
        _connection_record: object,
        exception: BaseException | None,
    ) -> None:
        for listener in self._invalidation_listeners:
            listener(exception)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def close(self) -> None:
        await self.engine.dispose()
