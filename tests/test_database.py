import asyncio

import pytest

from northgate.db.database import Database
from northgate.metrics import Metrics


@pytest.mark.anyio
async def test_database_pool_invalidation_exposes_bounded_reason_metric() -> None:
    database = Database("postgresql+asyncpg://northgate:northgate@127.0.0.1:5433/northgate")
    metrics = Metrics("test")
    database.add_invalidation_listener(metrics.observe_database_connection_invalidation)

    try:
        database.engine.sync_engine.pool.dispatch.invalidate(None, None, asyncio.CancelledError())
        database.engine.sync_engine.pool.dispatch.invalidate(None, None, RuntimeError("lost"))
        database.engine.sync_engine.pool.dispatch.invalidate(None, None, None)
    finally:
        await database.close()

    samples = {
        (sample.name, sample.labels.get("reason")): sample.value
        for metric in metrics.registry.collect()
        for sample in metric.samples
    }
    assert samples[("northgate_database_connection_invalidations_total", "cancelled")] == 1
    assert samples[("northgate_database_connection_invalidations_total", "error")] == 1
    assert samples[("northgate_database_connection_invalidations_total", "unspecified")] == 1


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
