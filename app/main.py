import asyncio
import logging
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated

import redis.asyncio as redis
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from neo4j import AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired
from redis.exceptions import RedisError

from .cache import RecommendationCache
from .config import Settings
from .repository import GraphRepository
from .schemas import Genre, Identifier, Movie, Recommendations, Rating, Strategy, User

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def resources(request: Request):
    return request.app.state.repository, request.app.state.cache


Resources = Annotated[tuple, Depends(resources)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def create_app(settings=None, repository=None, cache=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        if repository is not None and cache is not None:
            app.state.repository, app.state.cache = repository, cache
            yield
            return
        driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=5, connection_acquisition_timeout=10,
            max_transaction_retry_time=5, max_connection_pool_size=50)
        client = redis.from_url(settings.redis_url, decode_responses=True,
                                socket_connect_timeout=3, socket_timeout=3,
                                max_connections=100)
        app.state.cache = RecommendationCache(client, settings)
        app.state.repository = GraphRepository(driver, settings.neo4j_database, app.state.cache)
        try:
            for attempt in range(30):
                try:
                    await driver.verify_connectivity()
                    await client.ping()
                    await app.state.repository.initialize()
                    break
                except (ServiceUnavailable, SessionExpired, RedisError):
                    if attempt == 29:
                        raise
                    await asyncio.sleep(1)
            yield
        finally:
            await driver.close()
            await client.aclose()

    app = FastAPI(title="Recomendador de Filmes", version="2.0.0", lifespan=lifespan,
                  description="API acadêmica com recomendações por gêneros e usuários semelhantes, Neo4j e cache Redis.")

    async def dependency_error(request, exc):
        logger.error("Dependência indisponível: %s", type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "Neo4j ou Redis indisponível. Confira /health e os logs."})

    for error in (RedisError, ServiceUnavailable, SessionExpired):
        app.add_exception_handler(error, dependency_error)

    @app.exception_handler(Neo4jError)
    async def database_error(request, exc):
        logger.exception("Falha no Neo4j")
        return JSONResponse(status_code=500, content={"detail": "Falha ao executar a operação no banco."})

    @app.middleware("http")
    async def timing(request, call_next):
        start = perf_counter()
        response = await call_next(request)
        response.headers["X-Response-Time-Ms"] = f"{(perf_counter() - start) * 1000:.3f}"
        return response

    @app.get("/", tags=["Operação"])
    async def root():
        return {"name": "Recomendador de Filmes", "version": "2.0.0", "docs": "/docs"}

    @app.get("/health", tags=["Operação"])
    async def health(r: Resources):
        repo, cached = r
        await repo.driver.verify_connectivity()
        await cached.client.ping()
        return {"status": "ok", "neo4j": "ok", "redis": "ok"}

    @app.get("/metrics", tags=["Operação"])
    async def metrics(r: Resources):
        return await r[1].metrics()

    @app.post("/users", status_code=201, response_model=User, tags=["Usuários"])
    async def save_user(user: User, r: Resources):
        async with r[1].writing():
            return await r[0].save_user(user.model_dump())

    @app.get("/users", tags=["Usuários"])
    async def users(r: Resources, offset: Offset = 0, limit: PageSize = 100):
        return await r[0].list_items("users", offset, limit)

    @app.get("/users/{user_id}", response_model=User, tags=["Usuários"])
    async def user(user_id: Identifier, r: Resources):
        return await r[0].get_user(user_id)

    @app.delete("/users/{user_id}", tags=["Usuários"])
    async def delete_user(user_id: Identifier, r: Resources):
        async with r[1].writing():
            return await r[0].delete_item("users", user_id)

    @app.post("/genres", status_code=201, response_model=Genre, tags=["Gêneros"])
    async def save_genre(genre: Genre, r: Resources):
        async with r[1].writing():
            return await r[0].save_genre(genre.model_dump())

    @app.get("/genres", tags=["Gêneros"])
    async def genres(r: Resources, offset: Offset = 0, limit: PageSize = 100):
        return await r[0].list_items("genres", offset, limit)

    @app.post("/movies", status_code=201, response_model=Movie, tags=["Filmes"])
    async def save_movie(movie: Movie, r: Resources):
        async with r[1].writing():
            return await r[0].save_movie(movie.model_dump())

    @app.get("/movies", tags=["Filmes"])
    async def movies(r: Resources, offset: Offset = 0, limit: PageSize = 100):
        return await r[0].list_items("movies", offset, limit)

    @app.get("/movies/{movie_id}", response_model=Movie, tags=["Filmes"])
    async def movie(movie_id: Identifier, r: Resources):
        return await r[0].get_movie(movie_id)

    @app.delete("/movies/{movie_id}", tags=["Filmes"])
    async def delete_movie(movie_id: Identifier, r: Resources):
        async with r[1].writing():
            return await r[0].delete_item("movies", movie_id)

    @app.post("/users/{user_id}/watched/{movie_id}", status_code=201, tags=["Histórico"])
    async def watched(user_id: Identifier, movie_id: Identifier, r: Resources):
        async with r[1].writing():
            return await r[0].watch(user_id, movie_id)

    @app.get("/users/{user_id}/history", tags=["Histórico"])
    async def history(user_id: Identifier, r: Resources):
        return await r[0].history(user_id)

    @app.put("/users/{user_id}/ratings/{movie_id}", tags=["Histórico"])
    async def rate(user_id: Identifier, movie_id: Identifier, rating: Rating, r: Resources):
        async with r[1].writing():
            return await r[0].rate(user_id, movie_id, rating.score)

    @app.get("/users/{user_id}/recommendations", response_model=Recommendations, tags=["Recomendações"])
    async def recommendations(user_id: Identifier, response: Response, r: Resources,
                              limit: PageSize = 10, strategy: Strategy = "hybrid", use_cache: bool = True):
        items, status, ttl = await r[1].recommendations(r[0], user_id, limit, strategy, use_cache)
        response.headers["X-Cache"] = status
        response.headers["X-Cache-TTL"] = str(ttl)
        return {"user_id": user_id, "cache": status, "ttl_seconds": ttl,
                "strategy": strategy, "recommendations": items}

    @app.get("/cache/recommendations/{user_id}", tags=["Cache"])
    async def cache_info(user_id: Identifier, r: Resources, limit: PageSize = 10, strategy: Strategy = "hybrid"):
        await r[0].user_exists(user_id)
        return await r[1].info(user_id, limit, strategy)

    @app.delete("/cache/recommendations/{user_id}", tags=["Cache"])
    async def invalidate_user(user_id: Identifier, r: Resources):
        await r[0].user_exists(user_id)
        return await r[1].invalidate_user(user_id)

    @app.delete("/cache/recommendations", tags=["Cache"])
    async def invalidate_all(r: Resources):
        return await r[1].invalidate_all()

    return app


app = create_app()
