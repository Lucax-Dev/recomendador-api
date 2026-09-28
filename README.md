# Recomendador — execução

Pré-requisito: Docker Desktop em execução. Na pasta do projeto:

```bash
bash executar.sh
```

O comando sobe os serviços, carrega 10 usuários e 24 filmes sintéticos, executa testes com Neo4j/Redis reais e compara desempenho com/sem cache. Resultados em `evidencias/`. Não remove os volumes existentes. A documentação acadêmica fica a cargo do autor.

- API e contratos gerados automaticamente: http://localhost:8000/docs
- Neo4j Browser: http://localhost:7474 — `neo4j` / `senha-local-123`
- Exemplo: http://localhost:8000/users/demo-ana/recommendations
- Estado/TTL: http://localhost:8000/cache/recommendations/demo-ana
- Métricas: http://localhost:8000/metrics
- Parar preservando dados: `docker compose stop`

Copie `.env.example` para `.env` caso queira alterar TTL ou portas. Para reaplicar: `docker compose up -d --build --wait`. A senha de um volume Neo4j já inicializado deve continuar correspondente à senha desse banco.

`POST /users` e `POST /movies` fazem cadastro/atualização pelo ID (idempotentes). Avaliações de 1 a 5; avaliar também registra como assistido. O parâmetro `strategy` aceita `hybrid`, `genres` e `collaborative`. `use_cache=false` permite medir a consulta sem cache.

Testes de integração criam IDs aleatórios e apagam somente seus próprios registros. A carga `demo-*` é sintética e repetível. Os arquivos com `ambiente-assistente` foram gerados fora do Docker do Mac; os arquivos `*-mac` e `testes-docker.xml` são gerados localmente pelo comando acima.
