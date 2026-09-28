import asyncio
import os
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from app.config import Settings
from app.main import create_app

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.getenv("RUN_INTEGRATION") != "1", reason="Exige Neo4j e Redis reais")]


@pytest_asyncio.fixture
async def system():
    prefix = f"t_{uuid4().hex[:12]}"
    settings = Settings(cache_prefix=f"test:{prefix}", cache_ttl_seconds=2)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            async def send(method, path, **kwargs):
                response = await client.request(method, path, **kwargs)
                response.raise_for_status()
                return response
            users = [f"{prefix}_u{i}" for i in range(3)]
            movies = [f"{prefix}_m{i}" for i in range(4)]
            genres = [f"{prefix}_acao", f"{prefix}_drama"]
            for i, user in enumerate(users):
                await send("POST", "/users", json={"id":user,"name":f"Pessoa {i}"})
            for i, movie in enumerate(movies):
                await send("POST", "/movies", json={"id":movie, "title":f"Filme {i}", "genres":[genres[0] if i < 3 else genres[1]]})
            yield client, send, users, movies, genres, app
        # Somente os registros criados por este teste; não toca dados do usuário.
        async with app.state.repository.driver.session(database=settings.neo4j_database) as session:
            result = await session.run("MATCH (n) WHERE n.id STARTS WITH $prefix OR (n:Genre AND n.name STARTS WITH $prefix) DETACH DELETE n", prefix=prefix)
            await result.consume()
        keys = [key async for key in app.state.cache.client.scan_iter(match=f"test:{prefix}:*")]
        if keys:
            await app.state.cache.client.delete(*keys)


def path(user, **params):
    query = "&".join(f"{k}={v}" for k,v in params.items())
    return f"/users/{user}/recommendations" + (f"?{query}" if query else "")


async def test_health_crud_and_validation(system):
    client, send, u, m, g, app = system
    assert (await send("GET", "/health")).json()["status"] == "ok"
    assert (await send("GET", f"/users/{u[0]}")).json()["id"] == u[0]
    assert (await send("GET", f"/movies/{m[0]}")).json()["genres"] == [g[0]]
    await send("POST", "/genres", json={"name":g[0]})
    assert (await send("GET", "/users?limit=1")).json()
    assert (await client.put(f"/users/{u[0]}/ratings/{m[0]}", json={"score":6})).status_code == 422
    assert (await client.get(path("nao_existe"))).status_code == 404
    assert (await client.post(f"/users/{u[0]}/watched/nao_existe")).status_code == 404
    assert (await client.get(path(u[0], limit=0))).status_code == 422


async def test_miss_hit_no_graph_queries_on_hit(system):
    client, send, u, m, g, app = system
    await send("POST", f"/users/{u[0]}/watched/{m[0]}")
    first = await send("GET", path(u[0], strategy="genres"))
    before = (await send("GET", "/metrics")).json()
    second = await send("GET", path(u[0], strategy="genres"))
    after = (await send("GET", "/metrics")).json()
    assert first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT"
    assert first.json()["recommendations"] == second.json()["recommendations"]
    assert first.json()["recommendations"][0]["score"] == 1
    assert m[0] not in [x["id"] for x in first.json()["recommendations"]]
    assert before["neo4j_queries"] == after["neo4j_queries"]


async def test_expiration_uses_real_redis_ttl(system):
    client, send, u, m, g, app = system
    await send("GET", path(u[0]))
    info = (await send("GET", f"/cache/recommendations/{u[0]}")).json()
    assert 0 < info["ttl_seconds"] <= 2
    await asyncio.sleep(2.1)
    assert (await send("GET", path(u[0]))).headers["x-cache"] == "MISS"


async def test_ratings_change_order_and_not_double_counted(system):
    client, send, u, m, g, app = system
    await send("POST", f"/users/{u[0]}/watched/{m[0]}")
    await send("GET", path(u[0], strategy="genres"))
    await send("PUT", f"/users/{u[0]}/ratings/{m[0]}", json={"score":5})
    positive = await send("GET", path(u[0], strategy="genres"))
    assert positive.headers["x-cache"] == "MISS"
    target = next(x for x in positive.json()["recommendations"] if x["id"] == m[1])
    assert target["score"] == 2  # Sem +1 duplicado de WATCHED.
    await send("PUT", f"/users/{u[0]}/ratings/{m[0]}", json={"score":1})
    negative = (await send("GET", path(u[0], strategy="genres"))).json()["recommendations"]
    assert m[1] not in [x["id"] for x in negative]
    history = (await send("GET", f"/users/{u[0]}/history")).json()
    assert len(history) == 1 and history[0]["rating"] == 1


async def test_collaborative_invalidates_other_users(system):
    client, send, u, m, g, app = system
    await send("POST", f"/users/{u[0]}/watched/{m[0]}")
    await send("POST", f"/users/{u[1]}/watched/{m[0]}")
    await send("GET", path(u[0], strategy="collaborative"))
    await send("PUT", f"/users/{u[1]}/ratings/{m[3]}", json={"score":5})
    result = await send("GET", path(u[0], strategy="collaborative"))
    assert result.headers["x-cache"] == "MISS"
    top = result.json()["recommendations"][0]
    assert top["id"] == m[3] and top["similar_users"] == 1
    assert top["reason"] == "usuarios_semelhantes"


async def test_manual_invalidation_and_cache_variants(system):
    client, send, u, m, g, app = system
    for user in u[:2]:
        await send("GET", path(user))
    await send("GET", path(u[0], limit=1))
    await send("GET", path(u[0], strategy="genres"))
    assert (await send("DELETE", f"/cache/recommendations/{u[0]}")).json()["invalidated"] == 3
    assert (await send("GET", path(u[1]))).headers["x-cache"] == "HIT"
    assert (await send("GET", path(u[0]))).headers["x-cache"] == "MISS"
    assert (await send("GET", path(u[0], use_cache="false"))).headers["x-cache"] == "BYPASS"


async def test_concurrent_cold_cache(system):
    client, send, u, m, g, app = system
    await send("POST", f"/users/{u[0]}/watched/{m[0]}")
    before = (await send("GET", "/metrics")).json()
    results = await asyncio.gather(*[send("GET", path(u[0])) for _ in range(20)])
    after = (await send("GET", "/metrics")).json()
    assert [r.headers["x-cache"] for r in results].count("MISS") == 1
    assert after["recommendation_computations"] - before["recommendation_computations"] == 1
    assert all(r.json()["recommendations"] == results[0].json()["recommendations"] for r in results)


async def test_movie_update_and_deletion_invalidate(system):
    client, send, u, m, g, app = system
    await send("POST", f"/users/{u[0]}/watched/{m[0]}")
    await send("GET", path(u[0], strategy="genres"))
    await send("POST", "/movies", json={"id":m[1], "title":"Alterado", "genres":[g[1]]})
    result = await send("GET", path(u[0], strategy="genres", limit=100))
    assert result.headers["x-cache"] == "MISS"
    assert next(x for x in result.json()["recommendations"] if x["id"] == m[1])["score"] == 0
    assert (await send("GET", f"/movies/{m[1]}")).json()["genres"] == [g[1]]
    await send("DELETE", f"/movies/{m[1]}")
    assert (await client.get(f"/movies/{m[1]}")).status_code == 404
    await send("DELETE", f"/users/{u[0]}")
    assert (await client.get(path(u[0]))).status_code == 404


async def test_empty_recommendations_are_cached(system):
    client, send, u, m, g, app = system
    # Assistir a todo o catálogo existente, inclusive dados de demonstração.
    offset = 0
    while True:
        movies = (await send("GET", f"/movies?offset={offset}&limit=100")).json()
        for movie in movies:
            await send("POST", f"/users/{u[0]}/watched/{movie['id']}")
        if len(movies) < 100:
            break
        offset += 100
    first = await send("GET", path(u[0]))
    second = await send("GET", path(u[0]))
    assert first.json()["recommendations"] == []
    assert second.headers["x-cache"] == "HIT"
