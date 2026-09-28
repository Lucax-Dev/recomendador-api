@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
title Recomendador API - Inicializacao

cd /d "%~dp0"

echo.
echo ============================================================
echo  RECOMENDADOR API - INICIALIZACAO WINDOWS
echo ============================================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Docker nao foi encontrado no PATH.
    echo Instale e abra o Docker Desktop antes de executar este arquivo.
    pause
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo [ERRO] O Docker Desktop nao esta em execucao.
    echo Abra o Docker Desktop, aguarde iniciar e execute novamente.
    pause
    exit /b 1
)

docker compose version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] O comando "docker compose" nao esta disponivel.
    echo Atualize o Docker Desktop para uma versao com Docker Compose v2.
    pause
    exit /b 1
)

set "COMPOSE_FILE="
for %%F in (docker-compose.yml docker-compose.yaml compose.yml compose.yaml) do (
    if exist "%%F" set "COMPOSE_FILE=%%F"
)

if not defined COMPOSE_FILE (
    echo [ERRO] Nenhum arquivo docker-compose.yml ou compose.yml foi encontrado.
    echo Coloque executar.bat na pasta raiz do projeto e tente novamente.
    pause
    exit /b 1
)

echo [1/4] Arquivo de composicao encontrado: %COMPOSE_FILE%
echo [2/4] Construindo e iniciando os containers...
docker compose up --build -d

if errorlevel 1 (
    echo.
    echo [ERRO] Falha ao iniciar os containers.
    echo Execute "docker compose logs" para consultar os logs.
    pause
    exit /b 1
)

echo.
echo [3/4] Status dos containers:
docker compose ps

echo.
echo [4/4] Aguardando a API responder em http://localhost:8000/health ...

set "API_OK=0"
for /L %%I in (1,1,60) do (
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://localhost:8000/health' -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }" >nul 2>&1

    if not errorlevel 1 (
        set "API_OK=1"
        goto :API_READY
    )

    timeout /t 2 /nobreak >nul
)

:API_READY
echo.
if "%API_OK%"=="1" (
    echo [OK] API respondeu com sucesso.
) else (
    echo [AVISO] Os containers subiram, mas a API ainda nao respondeu no /health.
    echo Consulte os logs com: docker compose logs -f api
)

echo.
echo ============================================================
echo  ACESSOS
echo ============================================================
echo  API:        http://localhost:8000
echo  Swagger:    http://localhost:8000/docs
echo  Neo4j:      http://localhost:7474
echo  Neo4j Bolt: bolt://localhost:7687
echo  Redis:      localhost:6379
echo ============================================================
echo.

echo Credencial configurada para o Neo4j no arquivo %COMPOSE_FILE%:
findstr /I "NEO4J_AUTH" "%COMPOSE_FILE%"
if errorlevel 1 (
    echo [INFO] A linha NEO4J_AUTH nao foi localizada automaticamente.
    echo Abra %COMPOSE_FILE% e procure por NEO4J_AUTH.
)

echo.
echo Comandos uteis:
echo   docker compose ps
echo   docker compose logs -f
echo   docker compose down
echo.
echo Ambiente iniciado.
pause
exit /b 0
