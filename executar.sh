#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
# Mesmo projeto Compose da primeira versão: mantém volumes existentes.
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-recomendador-api}"
mkdir -p evidencias
printf '\nIniciando API, Neo4j e Redis...\n'
docker compose up --build -d --wait --wait-timeout 240
printf '\nCarregando dados sintéticos...\n'
docker compose exec -T api python scripts/seed.py
printf '\nExecutando testes, inclusive expiração real de TTL...\n'
docker compose --profile test run --build --rm tests
printf '\nValidando HTTP e concorrência entre os processos da API...\n'
docker compose exec -T api python scripts/smoke.py --output /tmp/validacao-http-mac.json
docker compose cp api:/tmp/validacao-http-mac.json evidencias/validacao-http-mac.json
printf '\nComparando desempenho com e sem cache...\n'
docker compose exec -T api python scripts/benchmark.py --output /tmp/benchmark-mac.json
docker compose cp api:/tmp/benchmark-mac.json evidencias/benchmark-mac.json
docker compose cp api:/tmp/benchmark-mac.csv evidencias/benchmark-mac.csv
printf '\nConcluído. API: http://localhost:8000/docs | Resultados: pasta evidencias\n'
