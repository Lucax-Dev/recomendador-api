import asyncio
import json
import logging
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

logger = logging.getLogger(__name__)

# Versões evitam que um cálculo iniciado antes de uma alteração republique cache antigo.
# O marcador de escrita impede leitura/preenchimento de cache durante o commit.
STATE = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[3], '-inf', now)
local global = redis.call('GET', KEYS[1])
if not global then global = ARGV[1]; redis.call('SET', KEYS[1], global) end
return {global, redis.call('GET', KEYS[2]) or '0', redis.call('ZCARD', KEYS[3])}
"""
BEGIN_WRITE = """
local clock = redis.call('TIME')
local now = clock[1] * 1000 + math.floor(clock[2] / 1000)
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZADD', KEYS[2], now + 120000, ARGV[2])
return 1
"""
END_WRITE = """
redis.call('SET', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[2])
return 1
"""
PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if (redis.call('GET', KEYS[2]) or '0') ~= ARGV[2] then return 0 end
if redis.call('ZCARD', KEYS[3]) > 0 then return 0 end
if redis.call('GET', KEYS[5]) ~= ARGV[5] then return 0 end
redis.call('SET', KEYS[4], ARGV[3], 'EX', ARGV[4])
return 1
"""
UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""


class RecommendationCache:
    def __init__(self, client, settings):
        self.client, self.settings = client, settings
        self.prefix = settings.cache_prefix
        self.global_key = f"{self.prefix}:generation"
        self.writers_key = f"{self.prefix}:writers"
        self.metrics_key = f"{self.prefix}:metrics"

    def user_key(self, user_id):
        return f"{self.prefix}:user-generation:{user_id}"

    async def count(self, field):
        await self.client.hincrby(self.metrics_key, field, 1)

    async def metrics(self):
        values = await self.client.hgetall(self.metrics_key)
        return {key: int(values.get(key, 0)) for key in
                ["hit", "miss", "bypass", "invalidations", "neo4j_queries", "recommendation_computations"]}

    async def snapshot(self, user_id, limit, strategy):
        generation, user_generation, busy = await self.client.eval(
            STATE, 3, self.global_key, self.user_key(user_id), self.writers_key, uuid4().hex)
        key = f"{self.prefix}:recommendations:{user_id}:{generation}:{user_generation}:{strategy}:{limit}"
        return key, generation, user_generation, bool(busy)

    async def lookup(self, key):
        async with self.client.pipeline(transaction=True) as pipe:
            value, ttl = await pipe.get(key).ttl(key).execute()
        if value is None or ttl < 0:
            return None
        try:
            return json.loads(value), ttl
        except (ValueError, TypeError):
            await self.client.delete(key)
            return None

    @asynccontextmanager
    async def writing(self):
        token = uuid4().hex
        # Se Redis estiver indisponível, a gravação nem começa.
        await self.client.eval(BEGIN_WRITE, 2, self.global_key, self.writers_key, uuid4().hex, token)
        try:
            yield
        finally:
            await self.client.eval(END_WRITE, 2, self.global_key, self.writers_key, uuid4().hex, token)
            await self.count("invalidations")

    async def invalidate_user(self, user_id):
        await self.client.set(self.user_key(user_id), uuid4().hex)
        deleted = 0
        async for key in self.client.scan_iter(match=f"{self.prefix}:recommendations:{user_id}:*"):
            deleted += await self.client.delete(key)
        await self.count("invalidations")
        return {"user_id": user_id, "invalidated": deleted, "status": "invalidated"}

    async def invalidate_all(self):
        await self.client.set(self.global_key, uuid4().hex)
        await self.count("invalidations")
        return {"status": "invalidated", "scope": "all"}

    async def info(self, user_id, limit, strategy):
        key, _, _, busy = await self.snapshot(user_id, limit, strategy)
        hit = None if busy else await self.lookup(key)
        return {"key": key, "exists": hit is not None, "ttl_seconds": hit[1] if hit else -2,
                "configured_ttl_seconds": self.settings.cache_ttl_seconds, "write_in_progress": busy}

    async def recommendations(self, repository, user_id, limit, strategy, use_cache=True):
        if not use_cache:
            items = await repository.recommend(user_id, limit, strategy)
            await self.count("bypass")
            return items, "BYPASS", 0
        deadline = monotonic() + self.settings.cache_wait_seconds
        while True:
            key, generation, user_generation, busy = await self.snapshot(user_id, limit, strategy)
            if busy:
                items = await repository.recommend(user_id, limit, strategy)
                await self.count("bypass")
                return items, "BYPASS", 0
            hit = await self.lookup(key)
            if hit:
                await self.count("hit")
                logger.info("CACHE HIT user=%s strategy=%s limit=%s", user_id, strategy, limit)
                return hit[0], "HIT", hit[1]
            lock_key = f"{key}:lock"
            token = uuid4().hex
            acquired = await self.client.set(lock_key, token, nx=True, ex=self.settings.cache_lock_seconds)
            if acquired:
                try:
                    # Um outro processo pode ter preenchido a chave entre GET e SET NX.
                    hit = await self.lookup(key)
                    if hit:
                        await self.count("hit")
                        return hit[0], "HIT", hit[1]
                    items = await repository.recommend(user_id, limit, strategy)
                    stored = await self.client.eval(PUBLISH, 5,
                        self.global_key, self.user_key(user_id), self.writers_key, key, lock_key,
                        generation, user_generation, json.dumps(items, ensure_ascii=False),
                        self.settings.cache_ttl_seconds, token)
                    await self.count("miss")
                    logger.info("CACHE MISS user=%s stored=%s", user_id, stored)
                    return items, "MISS", self.settings.cache_ttl_seconds if stored else 0
                finally:
                    await self.client.eval(UNLOCK, 1, lock_key, token)
            if monotonic() >= deadline:
                items = await repository.recommend(user_id, limit, strategy)
                await self.count("bypass")
                return items, "BYPASS", 0
            await asyncio.sleep(0.025)
