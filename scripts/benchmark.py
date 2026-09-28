"""Mede HTTP real. Mantém usuários, catálogo, estratégia e paralelismo iguais nas duas fases."""
import argparse
import asyncio
import csv
import json
import math
import platform
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx


async def benchmark(args):
    users = ["demo-ana", "demo-bruno", "demo-carla", "demo-diego", "demo-elisa",
             "demo-fabio", "demo-gabi", "demo-hugo", "demo-iris", "demo-joao"]
    samples, summaries = [], []
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=60, limits=limits, trust_env=False) as client:
        async def get(path):
            result = await client.get(path)
            result.raise_for_status()
            return result
        def route(user, use_cache):
            return f"/users/{user}/recommendations?limit=10&strategy=hybrid&use_cache={str(use_cache).lower()}"
        await get("/health")
        # Aquecimento do driver, plano Cypher e page cache, excluído das medições.
        expected = {}
        for user in users:
            expected[user] = (await get(route(user, False))).json()["recommendations"]
        for phase, enabled in [("sem_cache", False), ("com_cache", True)]:
            if enabled:
                for user in users:
                    await get(route(user, True))
            before = (await get("/metrics")).json()
            gate = asyncio.Semaphore(args.concurrency)
            async def request(index):
                user = users[index % len(users)]
                async with gate:
                    start = perf_counter()
                    try:
                        response = await client.get(route(user, enabled))
                        elapsed = (perf_counter() - start) * 1000
                        equivalent = response.status_code == 200 and response.json()["recommendations"] == expected[user]
                        return {"phase":phase,"index":index,"user":user,"elapsed_ms":round(elapsed,3),
                                "status":response.status_code,"cache":response.headers.get("x-cache","ERROR"),
                                "equivalent":equivalent,"error":""}
                    except (httpx.HTTPError, ValueError, KeyError) as exc:
                        return {"phase":phase,"index":index,"user":user,"elapsed_ms":round((perf_counter()-start)*1000,3),
                                "status":0,"cache":"ERROR","equivalent":False,"error":type(exc).__name__}
            start = perf_counter()
            results = await asyncio.gather(*(request(i) for i in range(args.requests)))
            duration = perf_counter() - start
            after = (await get("/metrics")).json()
            samples.extend(results)
            timings = sorted(x["elapsed_ms"] for x in results)
            summary = {"phase":phase,"requests":args.requests,"concurrency":args.concurrency,
                       "users":len(users),"errors":sum(x["status"] != 200 for x in results),
                       "result_mismatches":sum(not x["equivalent"] for x in results),
                       "mean_ms":round(statistics.mean(timings),3),"median_ms":round(statistics.median(timings),3),
                       "p95_ms":timings[math.ceil(len(timings)*0.95)-1],
                       "throughput_requests_s":round(args.requests/duration,2),
                       "wall_seconds":round(duration,3),"cache_status":dict(Counter(x["cache"] for x in results)),
                       "neo4j_queries":after["neo4j_queries"]-before["neo4j_queries"],
                       "recommendation_computations":after["recommendation_computations"]-before["recommendation_computations"]}
            summaries.append(summary)
            print(json.dumps(summary, ensure_ascii=False))
    uncached, cached = summaries
    report = {"generated_at":datetime.now(timezone.utc).isoformat(), "environment":args.environment,
              "platform":platform.platform(),"python":platform.python_version(),"base_url":args.base_url,
              "method":"HTTP real; 10 usuários; catálogo constante; aquecimento excluído; 2 fases sequenciais; mesmo limite/estratégia/paralelismo. Métricas compartilhadas em Redis: executar sem outras cargas simultâneas.",
              "phases":summaries,
              "mean_speedup":round(uncached["mean_ms"]/cached["mean_ms"],2) if cached["mean_ms"] else None,
              "graph_query_reduction_percent":round(100*(1-cached["neo4j_queries"]/uncached["neo4j_queries"]),2) if uncached["neo4j_queries"] else None,
              "samples":samples}
    output = Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    with output.with_suffix(".csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(samples[0]))
        writer.writeheader()
        writer.writerows(samples)
    if any(x["errors"] or x["result_mismatches"] for x in summaries):
        raise SystemExit("Benchmark registrou erros; confira o JSON antes de usar os resultados.")
    print(f"Resultados gravados em {output} e {output.with_suffix('.csv')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--environment", default="Docker local do usuário")
    parser.add_argument("--output", default="evidencias/benchmark.json")
    args = parser.parse_args()
    if args.requests < 1 or not 1 <= args.concurrency <= 100:
        parser.error("requests >= 1 e concurrency entre 1 e 100")
    asyncio.run(benchmark(args))
