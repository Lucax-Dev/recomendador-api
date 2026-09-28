"""Validação HTTP externa, inclusive concorrência entre os workers da API."""
import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import httpx


async def run(args):
    prefix = f"smoke_{uuid4().hex[:10]}"
    user, peer = f"{prefix}_u", f"{prefix}_p"
    seen, candidate = f"{prefix}_m1", f"{prefix}_m2"
    records, checks = [], []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, trust_env=False) as client:
        async def send(method, path, **kwargs):
            result = await client.request(method, path, **kwargs)
            records.append({"method":method,"path":path,"status":result.status_code,
                            "x_cache":result.headers.get("x-cache"),"body":result.json()})
            result.raise_for_status()
            return result
        def check(name, condition):
            checks.append({"name":name,"passed":bool(condition)})
            if not condition:
                raise AssertionError(name)
        try:
            await send("GET", "/health")
            for uid in [user, peer]:
                await send("POST", "/users", json={"id":uid,"name":"Validação HTTP"})
            for mid in [seen, candidate]:
                await send("POST", "/movies", json={"id":mid,"title":mid,"genres":["Ação"]})
            await send("POST", f"/users/{user}/watched/{seen}")
            route = f"/users/{user}/recommendations?strategy=genres&limit=100"
            first = await send("GET", route)
            second = await send("GET", route)
            check("miss_hit", first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT")
            check("exclui_assistido", seen not in [m["id"] for m in second.json()["recommendations"]])
            state = await send("GET", f"/cache/recommendations/{user}?strategy=genres&limit=100")
            check("ttl_configurado", 0 < state.json()["ttl_seconds"] <= state.json()["configured_ttl_seconds"])
            await send("PUT", f"/users/{user}/ratings/{seen}", json={"score":5})
            rated = await send("GET", route)
            check("avaliacao_invalida", rated.headers["x-cache"] == "MISS")
            match = next(m for m in rated.json()["recommendations"] if m["id"] == candidate)
            check("nota_sem_duplicacao", match["score"] == 2)
            await send("DELETE", f"/cache/recommendations/{user}")
            before = (await send("GET", "/metrics")).json()
            simultaneous = await asyncio.gather(*[client.get(route) for _ in range(20)])
            after = (await send("GET", "/metrics")).json()
            check("20_requisicoes_ok", all(r.status_code == 200 for r in simultaneous))
            check("um_calculo_no_cache_frio", after["recommendation_computations"] - before["recommendation_computations"] == 1)
            check("19_hits_no_cache_frio", sum(r.headers.get("x-cache") == "HIT" for r in simultaneous) == 19)
            check("resultados_concorrentes_iguais", all(r.json()["recommendations"] == simultaneous[0].json()["recommendations"] for r in simultaneous))
            await send("POST", f"/users/{peer}/watched/{seen}")
            await send("PUT", f"/users/{peer}/ratings/{candidate}", json={"score":5})
            collab = await send("GET", f"/users/{user}/recommendations?strategy=collaborative&limit=100")
            item = next(m for m in collab.json()["recommendations"] if m["id"] == candidate)
            check("recomendacao_colaborativa", item["similar_users"] == 1)
        finally:
            for endpoint, values in [("users", [user, peer]), ("movies", [seen, candidate])]:
                for identifier in values:
                    result = await client.delete(f"/{endpoint}/{identifier}")
                    if result.status_code not in (200,404):
                        print(f"Limpeza pendente: {endpoint}/{identifier}: {result.status_code}")
            output = Path(args.output)
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(json.dumps({"generated_at":datetime.now(timezone.utc).isoformat(),
                "environment":args.environment,"checks":checks,"http_records":records}, ensure_ascii=False, indent=2)+"\n")
    print(f"{len(checks)} verificações HTTP aprovadas. Resultado: {args.output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--output", default="evidencias/validacao-http.json")
    parser.add_argument("--environment", default="Docker local do usuário; API com 2 workers")
    asyncio.run(run(parser.parse_args()))
