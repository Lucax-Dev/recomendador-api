from fastapi import HTTPException
from neo4j import unit_of_work
from .queries import RECOMMEND


class GraphRepository:
    def __init__(self, driver, database: str, cache):
        self.driver, self.database, self.cache = driver, database, cache

    async def run(self, query: str, parameters=None, *, write=False):
        @unit_of_work(timeout=10)
        async def transaction(tx):
            await self.cache.count("neo4j_queries")
            result = await tx.run(query, parameters or {})
            return await result.data()
        async with self.driver.session(database=self.database) as session:
            return await (session.execute_write(transaction) if write else session.execute_read(transaction))

    async def initialize(self):
        for label, prop in [("User", "id"), ("Movie", "id"), ("Genre", "name")]:
            async with self.driver.session(database=self.database) as session:
                result = await session.run(f"CREATE CONSTRAINT {label.lower()}_{prop} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
                await result.consume()

    async def user_exists(self, user_id):
        rows = await self.run("MATCH (u:User {id:$id}) RETURN u.id AS id", {"id": user_id})
        if not rows:
            raise HTTPException(404, "Usuário não encontrado")

    async def save_user(self, data):
        rows = await self.run("MERGE (u:User {id:$id}) SET u.name=$name RETURN u.id AS id, u.name AS name", data, write=True)
        return rows[0]

    async def save_genre(self, data):
        rows = await self.run("MERGE (g:Genre {name:$name}) RETURN g.name AS name", data, write=True)
        return rows[0]

    async def save_movie(self, data):
        # MERGE, remoção dos vínculos antigos e novos gêneros no mesmo commit.
        rows = await self.run("""
            MERGE (m:Movie {id:$id}) SET m.title=$title, m.year=$year
            WITH m OPTIONAL MATCH (m)-[old:IN_GENRE]->(:Genre)
            DELETE old
            WITH DISTINCT m
            UNWIND $genres AS name
            MERGE (g:Genre {name:name}) MERGE (m)-[:IN_GENRE]->(g)
            RETURN m.id AS id, m.title AS title, m.year AS year, collect(g.name) AS genres
        """, data, write=True)
        rows[0]["genres"].sort()
        return rows[0]

    async def list_items(self, kind, offset, limit):
        queries = {
            "users": "MATCH (u:User) RETURN u.id AS id, u.name AS name ORDER BY id SKIP $offset LIMIT $limit",
            "genres": "MATCH (g:Genre) RETURN g.name AS name ORDER BY name SKIP $offset LIMIT $limit",
            "movies": "MATCH (m:Movie) OPTIONAL MATCH (m)-[:IN_GENRE]->(g:Genre) RETURN m.id AS id, m.title AS title, m.year AS year, collect(g.name) AS genres ORDER BY id SKIP $offset LIMIT $limit",
        }
        return await self.run(queries[kind], {"offset": offset, "limit": limit})

    async def get_user(self, user_id):
        rows = await self.run("MATCH (u:User {id:$id}) RETURN u.id AS id, u.name AS name", {"id": user_id})
        if not rows:
            raise HTTPException(404, "Usuário não encontrado")
        return rows[0]

    async def get_movie(self, movie_id):
        rows = await self.run("MATCH (m:Movie {id:$id}) OPTIONAL MATCH (m)-[:IN_GENRE]->(g) RETURN m.id AS id, m.title AS title, m.year AS year, collect(g.name) AS genres", {"id": movie_id})
        if not rows:
            raise HTTPException(404, "Filme não encontrado")
        return rows[0]

    async def watch(self, user_id, movie_id):
        rows = await self.run("""
            MATCH (u:User {id:$user_id}), (m:Movie {id:$movie_id})
            MERGE (u)-[w:WATCHED]->(m) ON CREATE SET w.at=datetime()
            RETURN u.id AS user_id, m.id AS movie_id
        """, {"user_id": user_id, "movie_id": movie_id}, write=True)
        if not rows:
            raise HTTPException(404, "Usuário ou filme não encontrado")
        return rows[0]

    async def rate(self, user_id, movie_id, score):
        rows = await self.run("""
            MATCH (u:User {id:$user_id}), (m:Movie {id:$movie_id})
            MERGE (u)-[w:WATCHED]->(m) ON CREATE SET w.at=datetime()
            MERGE (u)-[r:RATED]->(m) SET r.score=$score, r.at=datetime()
            RETURN u.id AS user_id, m.id AS movie_id, r.score AS score
        """, {"user_id": user_id, "movie_id": movie_id, "score": score}, write=True)
        if not rows:
            raise HTTPException(404, "Usuário ou filme não encontrado")
        return rows[0]

    async def history(self, user_id):
        await self.user_exists(user_id)
        return await self.run("""
            MATCH (u:User {id:$user_id})-[:WATCHED|RATED]->(m:Movie)
            WITH DISTINCT u, m
            OPTIONAL MATCH (u)-[r:RATED]->(m)
            RETURN m.id AS id, m.title AS title, r.score AS rating ORDER BY id
        """, {"user_id": user_id})

    async def delete_item(self, kind, identifier):
        label = {"users": "User", "movies": "Movie"}[kind]
        rows = await self.run(f"MATCH (n:{label} {{id:$id}}) DETACH DELETE n RETURN count(*) AS deleted", {"id": identifier}, write=True)
        if not rows or not rows[0]["deleted"]:
            raise HTTPException(404, "Registro não encontrado")
        return {"deleted": identifier}

    async def recommend(self, user_id, limit, strategy):
        await self.cache.count("recommendation_computations")
        rows = await self.run(RECOMMEND, {"user_id": user_id, "limit": limit, "strategy": strategy})
        if not rows:
            raise HTTPException(404, "Usuário não encontrado")
        items = rows[0]["recommendations"]
        for item in items:
            item["genres"].sort()
        return items
