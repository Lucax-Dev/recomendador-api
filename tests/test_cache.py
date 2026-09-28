import asyncio
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import pytest_asyncio
from redis.exceptions import ConnectionError

from app.cache import RecommendationCache
from app.config import Settings
from app.schemas import Movie, Rating, User
from pydantic import ValidationError


@pytest_asyncio.fixture
async def cached():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield RecommendationCache(client, Settings(cache_ttl_seconds=1))
    await client.aclose()


class Repo:
    def __init__(self):
        self.calls = 0
        self.items = [{"id": "m2"}]

    async def recommend(self, *args):
        self.calls += 1
        await asyncio.sleep(0.04)
        return list(self.items)


async def test_cold_concurrent_requests_compute_once(cached):
    repo = Repo()
    results = await asyncio.gather(*[cached.recommendations(repo, "u1", 10, "hybrid") for _ in range(20)])
    assert repo.calls == 1
    assert [r[1] for r in results].count("MISS") == 1
    assert [r[1] for r in results].count("HIT") == 19


async def test_cache_empty_list_and_real_ttl(cached):
    repo = Repo()
    repo.items = []
    assert (await cached.recommendations(repo, "u", 10, "hybrid"))[1] == "MISS"
    assert (await cached.recommendations(repo, "u", 10, "hybrid"))[1] == "HIT"
    await asyncio.sleep(1.1)
    assert (await cached.recommendations(repo, "u", 10, "hybrid"))[1] == "MISS"


async def test_variants_and_user_invalidation(cached):
    repo = Repo()
    for user, limit, strategy in [("u1", 10, "hybrid"), ("u1", 1, "hybrid"), ("u1", 10, "genres"), ("u2", 10, "hybrid")]:
        assert (await cached.recommendations(repo, user, limit, strategy))[1] == "MISS"
    assert (await cached.invalidate_user("u1"))["invalidated"] == 3
    assert (await cached.recommendations(repo, "u2", 10, "hybrid"))[1] == "HIT"
    assert (await cached.recommendations(repo, "u1", 10, "hybrid"))[1] == "MISS"


async def test_inflight_read_does_not_repopulate_after_mutation(cached):
    started, release = asyncio.Event(), asyncio.Event()
    repo = Repo()

    async def old_read(*args):
        started.set()
        await release.wait()
        return [{"id": "old"}]

    repo.recommend = old_read
    pending = asyncio.create_task(cached.recommendations(repo, "u1", 10, "hybrid"))
    await started.wait()
    async with cached.writing():
        pass
    release.set()
    result = await pending
    assert result[2] == 0  # Cálculo antigo não armazenado.
    assert not (await cached.info("u1", 10, "hybrid"))["exists"]
    repo.recommend = AsyncMock(return_value=[{"id": "new"}])
    assert (await cached.recommendations(repo, "u1", 10, "hybrid"))[0] == [{"id": "new"}]


async def test_writing_bypasses_cache_and_finalizes_on_error(cached):
    repo = Repo()
    with pytest.raises(RuntimeError):
        async with cached.writing():
            assert (await cached.recommendations(repo, "u1", 10, "hybrid"))[1] == "BYPASS"
            raise RuntimeError("rollback")
    assert not (await cached.info("u1", 10, "hybrid"))["write_in_progress"]
    assert (await cached.recommendations(repo, "u1", 10, "hybrid"))[1] == "MISS"


async def test_redis_unavailable_prevents_write(cached):
    cached.client.eval = AsyncMock(side_effect=ConnectionError("offline"))
    entered = False
    with pytest.raises(ConnectionError):
        async with cached.writing():
            entered = True
    assert not entered


@pytest.mark.parametrize("payload", [{"score":0}, {"score":6}, {"score":2.5}, {"score":True}, {"score":"5"}])
def test_rating_validation(payload):
    with pytest.raises(ValidationError):
        Rating(**payload)


def test_input_validation():
    with pytest.raises(ValidationError):
        User(id="*", name="teste")
    with pytest.raises(ValidationError):
        User(id="u1", name="   ")
    with pytest.raises(ValidationError):
        Movie(id="m1", title="X", genres=["  "])
    assert Movie(id="m1", title=" X ", genres=["Ação", "Ação"]).genres == ["Ação"]
