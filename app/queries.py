# Cada filme visto contribui uma vez por gênero. A nota substitui esse peso:
# notas 1..5 => -2, -1, 0, 1, 2. Avaliar e assistir não duplica interesse.
RECOMMEND = """
MATCH (u:User {id: $user_id})
CALL (u) {
    MATCH (m:Movie)
    WHERE NOT EXISTS { MATCH (u)-[:WATCHED|RATED]->(m) }
    CALL (u, m) {
        OPTIONAL MATCH (u)-[:WATCHED|RATED]->(seen:Movie)-[:IN_GENRE]->(g:Genre)<-[:IN_GENRE]-(m)
        WITH DISTINCT u, seen, g
        OPTIONAL MATCH (u)-[r:RATED]->(seen)
        RETURN coalesce(sum(CASE WHEN seen IS NULL THEN 0.0
            WHEN r IS NULL THEN 1.0 ELSE toFloat(r.score) - 3.0 END), 0.0) AS genre_score
    }
    CALL (u, m) {
        OPTIONAL MATCH (u)-[:WATCHED|RATED]->(common:Movie)<-[:WATCHED|RATED]-(peer:User)-[like:RATED]->(m)
        WHERE peer <> u AND like.score >= 4
          AND NOT EXISTS { MATCH (u)-[bad:RATED]->(common) WHERE bad.score < 3 }
          AND NOT EXISTS { MATCH (peer)-[bad:RATED]->(common) WHERE bad.score < 3 }
        RETURN count(DISTINCT peer) AS similar_users
    }
    CALL (m) {
        OPTIONAL MATCH (:User)-[r:RATED]->(m)
        RETURN coalesce(avg(toFloat(r.score)), 0.0) AS average_rating
    }
    CALL (m) {
        OPTIONAL MATCH (viewer:User)-[:WATCHED]->(m)
        RETURN count(DISTINCT viewer) AS watched_count
    }
    WITH m, genre_score, similar_users, average_rating, watched_count,
         CASE $strategy WHEN 'genres' THEN genre_score
              WHEN 'collaborative' THEN 2.0 * similar_users
              ELSE genre_score + 2.0 * similar_users END AS score
    WHERE score >= 0
    ORDER BY score DESC, average_rating DESC, watched_count DESC, m.title, m.id
    LIMIT $limit
    RETURN collect({id: m.id, title: m.title,
        genres: [(m)-[:IN_GENRE]->(g:Genre) | g.name], score: score,
        genre_score: genre_score, similar_users: similar_users,
        average_rating: average_rating, watched_count: watched_count,
        reason: CASE WHEN score = 0 THEN 'popularidade'
                     WHEN $strategy = 'genres' THEN 'generos'
                     WHEN $strategy = 'collaborative' THEN 'usuarios_semelhantes'
                     WHEN genre_score > 0 AND similar_users > 0 THEN 'generos_e_usuarios'
                     WHEN similar_users > 0 THEN 'usuarios_semelhantes'
                     ELSE 'generos' END}) AS recommendations
}
RETURN recommendations
"""
