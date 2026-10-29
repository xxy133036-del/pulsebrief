param(
    [ValidateRange(1, 10000)]
    [int]$Subscriptions = 10000,
    [ValidateRange(60, 3600)]
    [int]$TimeoutSeconds = 1200,
    [ValidateRange(1, 32)]
    [int]$WorkerConcurrency = 4
)

$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$apiImage = "pulsebrief-api:latest"
$workerImage = "pulsebrief-worker:latest"
$testImage = "pulsebrief-test:latest"
$runId = [guid]::NewGuid().ToString("N")
$network = "pulsebrief-capacity-$runId"
$dbContainer = "pulsebrief-capacity-db-$runId"
$redisContainer = "pulsebrief-capacity-redis-$runId"
$workerContainer = "pulsebrief-capacity-worker-$runId"
$probeContainer = "pulsebrief-capacity-probe-$runId"
$dbPassword = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$authSecret = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$databaseUrl = "postgresql+psycopg://capacity:$dbPassword@db:5432/daily_ai_news_capacity"
$redisUrl = "redis://redis:6379/0"
$artifactDir = Join-Path $repoRoot "artifacts/operations"
$logPath = Join-Path $artifactDir "10k-celery-capacity-$((Get-Date).ToString('yyyyMMdd-HHmmss')).log"

New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null

foreach ($image in @($apiImage, $workerImage, $testImage)) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -ne 0) {
        docker compose build api worker test
        if ($LASTEXITCODE -ne 0) {
            throw "Could not build required project images."
        }
        break
    }
}

$exitCode = 1
try {
    docker network create $network | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not create isolated Docker network." }

    docker run -d --name $dbContainer --network $network --network-alias db `
        -e "POSTGRES_DB=daily_ai_news_capacity" `
        -e "POSTGRES_USER=capacity" `
        -e "POSTGRES_PASSWORD=$dbPassword" postgres:16-alpine | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not start isolated PostgreSQL." }

    docker run -d --name $redisContainer --network $network --network-alias redis `
        redis:7-alpine redis-server --save "" --appendonly no | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not start isolated Redis." }

    $databaseReady = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        docker exec $dbContainer pg_isready -U capacity -d daily_ai_news_capacity *> $null
        if ($LASTEXITCODE -eq 0) {
            $databaseReady = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $databaseReady) { throw "Isolated PostgreSQL did not become ready." }

    docker exec $redisContainer redis-cli ping *> $null
    if ($LASTEXITCODE -ne 0) { throw "Isolated Redis did not become ready." }

    docker run --rm --network $network `
        -e "APP_ENV=development" -e "DEMO_MODE=true" -e "CELERY_ENABLED=true" `
        -e "AUTH_SECRET=$authSecret" -e "DATABASE_URL=$databaseUrl" -e "REDIS_URL=$redisUrl" `
        $apiImage alembic upgrade head
    if ($LASTEXITCODE -ne 0) { throw "Isolated database migration failed." }

    docker run -d --name $workerContainer --network $network --network-alias worker `
        -e "APP_ENV=development" -e "DEMO_MODE=true" -e "CELERY_ENABLED=true" `
        -e "AUTH_SECRET=$authSecret" -e "DATABASE_URL=$databaseUrl" -e "REDIS_URL=$redisUrl" `
        -e "TOOL_TASK_ROOT=/tmp/capacity-workspaces" `
        $workerImage celery -A app.celery_app.celery_app worker --loglevel=WARNING `
            "--concurrency=$WorkerConcurrency" --prefetch-multiplier=1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Isolated Celery Worker failed to start." }

    $workerReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        docker exec $workerContainer celery -A app.celery_app.celery_app inspect ping --timeout=2 *> $null
        if ($LASTEXITCODE -eq 0) {
            $workerReady = $true
            break
        }
        Start-Sleep -Seconds 2
    }
    if (-not $workerReady) { throw "Isolated Celery Worker did not answer ping." }

    docker run --name $probeContainer --network $network --workdir /srv/backend `
        -e "APP_ENV=development" -e "DEMO_MODE=true" -e "CELERY_ENABLED=true" `
        -e "AUTH_SECRET=$authSecret" -e "DATABASE_URL=$databaseUrl" -e "REDIS_URL=$redisUrl" `
        -e "TOOL_TASK_ROOT=/tmp/capacity-workspaces" `
        $testImage python scripts/quality-evaluation/benchmark_10k_celery_pipeline.py `
            --subscriptions $Subscriptions --timeout-seconds $TimeoutSeconds `
            --redis-url $redisUrl --match-demo-candidates *> $logPath
    $exitCode = $LASTEXITCODE
    Get-Content -LiteralPath $logPath -Tail 40
}
finally {
    if (Test-Path -LiteralPath $logPath) {
        docker logs $workerContainer 2>&1 | Add-Content -LiteralPath $logPath
    }
    foreach ($container in @($probeContainer, $workerContainer, $redisContainer, $dbContainer)) {
        docker rm -f $container *> $null
    }
    docker network rm $network *> $null
}

Write-Output "Benchmark log: $logPath"
exit $exitCode
