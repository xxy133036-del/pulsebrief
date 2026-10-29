param(
    [switch]$VerifyRestore,
    [switch]$VerifyApi,
    [string]$BackupDirectory = "F:\Resources\pulsebrief\backups"
)

$ErrorActionPreference = "Stop"
$allowedRoot = [System.IO.Path]::GetFullPath("F:\Resources")
$allowedRootPrefix = $allowedRoot.TrimEnd('\') + '\'
$resolvedBackupDirectory = [System.IO.Path]::GetFullPath($BackupDirectory)
if (-not $resolvedBackupDirectory.StartsWith($allowedRootPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Database backups must stay under F:\Resources."
}
if ($VerifyApi) {
    $VerifyRestore = $true
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
$restoreContainer = ""
$apiSmokeContainer = ""
$containerDump = ""
$backupPath = ""
$tables = @(
    "alembic_version",
    "briefings",
    "delivery_attempts",
    "email_outbox",
    "feedback",
    "generation_tasks",
    "news_articles",
    "outbox_events",
    "read_states",
    "subscriptions",
    "users",
    "verification_codes"
)
$tableCounts = [ordered]@{}
$backupStartedAt = Get-Date
try {
    New-Item -ItemType Directory -Path $resolvedBackupDirectory -Force | Out-Null

    $databaseContainer = (docker compose ps -q db).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $databaseContainer) {
        throw "The PostgreSQL container is not running."
    }

    foreach ($table in $tables) {
        $count = (docker compose exec -T db psql -U daily_ai_news -d daily_ai_news -Atc `
            "SELECT count(*) FROM $table").Trim()
        if ($LASTEXITCODE -ne 0 -or $count -notmatch '^\d+$') {
            throw "Could not read the row count for $table."
        }
        $tableCounts[$table] = [long]$count
    }
    $nonEmptyUsageCount = (docker compose exec -T db psql -U daily_ai_news -d daily_ai_news -Atc `
        "SELECT count(*) FROM generation_tasks WHERE token_usage::text <> '{}'" ).Trim()
    if ($LASTEXITCODE -ne 0 -or $nonEmptyUsageCount -notmatch '^\d+$') {
        throw "Could not read generation task token usage."
    }

    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupName = "daily-ai-news-$stamp.dump"
    $containerDump = "/tmp/$backupName"
    $backupPath = Join-Path $resolvedBackupDirectory $backupName

    docker compose exec -T db pg_dump -U daily_ai_news -d daily_ai_news -Fc -f $containerDump
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL backup creation failed."
    }

    docker compose exec -T db pg_restore --list $containerDump *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "The backup archive failed its integrity check."
    }

    docker cp "${databaseContainer}:$containerDump" $backupPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Copying the backup to F:\Resources failed."
    }

    $restoreResult = "not requested"
    $apiSmokeResult = "not requested"
    $restoreSeconds = $null
    if ($VerifyRestore) {
        $restoreStartedAt = Get-Date
        $restoreContainer = "daily-ai-news-restore-check-$stamp"
        docker run --rm -d --name $restoreContainer `
            -e POSTGRES_USER=restore `
            -e POSTGRES_PASSWORD=restore-only-test `
            -e POSTGRES_DB=restore_check `
            postgres:16-alpine | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "The temporary restore database failed to start."
        }

        $ready = $false
        $stableReadyChecks = 0
        for ($attempt = 0; $attempt -lt 60; $attempt++) {
            docker exec $restoreContainer pg_isready -U restore -d restore_check *> $null
            if ($LASTEXITCODE -eq 0) {
                $stableReadyChecks++
                if ($stableReadyChecks -ge 3) {
                    $ready = $true
                    break
                }
            }
            else {
                $stableReadyChecks = 0
            }
            Start-Sleep -Seconds 1
        }
        if (-not $ready) {
            throw "The temporary restore database did not become ready."
        }

        docker cp $backupPath "${restoreContainer}:/tmp/restore.dump" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Copying the archive to the temporary database failed."
        }

        docker exec $restoreContainer pg_restore -U restore -d restore_check `
            --exit-on-error --no-owner --no-privileges /tmp/restore.dump
        if ($LASTEXITCODE -ne 0) {
            throw "Restoring the archive into a temporary database failed."
        }

        $restoreCounts = [ordered]@{}
        foreach ($table in $tables) {
            $count = (docker exec $restoreContainer psql -U restore -d restore_check -Atc `
                "SELECT count(*) FROM $table").Trim()
            if ($LASTEXITCODE -ne 0 -or $count -notmatch '^\d+$') {
                throw "Could not read restored row count for $table."
            }
            $restoreCounts[$table] = [long]$count
            if ($restoreCounts[$table] -ne $tableCounts[$table]) {
                throw "Restored row count for $table does not match the source snapshot."
            }
        }
        $hasUsageColumn = (docker exec $restoreContainer psql -U restore -d restore_check -Atc `
            "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' AND table_name='generation_tasks' AND column_name='token_usage'").Trim()
        if ($LASTEXITCODE -ne 0 -or $hasUsageColumn -ne "1") {
            throw "The restored generation_tasks.token_usage column is missing."
        }
        $restoredNonEmptyUsageCount = (docker exec $restoreContainer psql -U restore -d restore_check -Atc `
            "SELECT count(*) FROM generation_tasks WHERE token_usage::text <> '{}'" ).Trim()
        if ($LASTEXITCODE -ne 0 -or $restoredNonEmptyUsageCount -ne "$nonEmptyUsageCount") {
            throw "Restored generation task token usage does not match the source snapshot."
        }
        $restoreResult = "verified $($tables.Count) tables; $($tableCounts['news_articles']) articles; $restoredNonEmptyUsageCount tasks with token usage"
        $restoreSeconds = [math]::Round(((Get-Date) - $restoreStartedAt).TotalSeconds, 2)

        if ($VerifyApi) {
            $apiServiceContainer = (docker compose ps -q api).Trim()
            if ($LASTEXITCODE -ne 0 -or -not $apiServiceContainer) {
                throw "The Compose API container is required for the restore smoke test."
            }
            $apiContainerInfo = (docker inspect $apiServiceContainer | ConvertFrom-Json)[0]
            $networkName = ($apiContainerInfo.NetworkSettings.Networks.PSObject.Properties | Select-Object -First 1).Name
            if (-not $networkName) {
                throw "Could not determine the Compose network for the API smoke test."
            }
            docker network connect $networkName $restoreContainer
            if ($LASTEXITCODE -ne 0) {
                throw "Could not connect the restored database to the Compose network."
            }

            $apiImage = (docker compose images -q api | Select-Object -First 1).Trim()
            if ($LASTEXITCODE -ne 0 -or -not $apiImage) {
                throw "Could not locate the current Compose API image."
            }
            $apiSmokeContainer = "daily-ai-news-restore-api-$stamp"
            docker run --rm -d --name $apiSmokeContainer --network $networkName `
                --read-only `
                --tmpfs "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777" `
                --cap-drop ALL `
                --security-opt no-new-privileges:true `
                -p "127.0.0.1::8000" `
                -e "APP_ENV=development" `
                -e "DEMO_MODE=true" `
                -e "DATABASE_URL=postgresql+psycopg://restore:restore-only-test@$($restoreContainer):5432/restore_check" `
                -e "AUTH_SECRET=restore-smoke-auth-secret-1234567890" `
                -e "CELERY_ENABLED=false" `
                -e "SCHEDULER_ENABLED=false" `
                -e "TOOL_TASK_ROOT=/tmp/restore-smoke-workspaces" `
                $apiImage /opt/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "The temporary API container failed to start."
            }

            $apiBinding = (docker port $apiSmokeContainer 8000/tcp | Select-Object -First 1).Trim()
            $apiPort = [int]($apiBinding.Split(':')[-1])
            $apiBaseUrl = "http://127.0.0.1:$apiPort"
            $apiReady = $false
            for ($attempt = 0; $attempt -lt 30; $attempt++) {
                try {
                    $readiness = Invoke-RestMethod -Uri "$apiBaseUrl/api/v1/health/ready" -TimeoutSec 3
                    if ($readiness.status -eq "ready") {
                        $apiReady = $true
                        break
                    }
                }
                catch {
                    Start-Sleep -Seconds 1
                }
            }
            if (-not $apiReady) {
                throw "The API did not become ready against the restored database."
            }

            $demoSession = Invoke-RestMethod -Method Post -Uri "$apiBaseUrl/api/v1/auth/demo" -TimeoutSec 5
            $headers = @{ Authorization = "Bearer $($demoSession.access_token)" }
            $briefingList = Invoke-RestMethod -Uri "$apiBaseUrl/api/v1/briefings?limit=30" `
                -Headers $headers -TimeoutSec 5
            if ($null -eq $briefingList.items) {
                throw "The API could not read the briefing list from the restored database."
            }
            $apiSmokeResult = "ready; demo login and briefing list returned successfully"
        }
    }

    $backupFile = Get-Item -LiteralPath $backupPath
    $manifest = [ordered]@{
        created_at = $backupStartedAt.ToUniversalTime().ToString("o")
        database = "daily_ai_news"
        backup_file = $backupFile.Name
        size_bytes = $backupFile.Length
        sha256 = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash
        table_counts = $tableCounts
        tasks_with_token_usage = [long]$nonEmptyUsageCount
        restore_verified = [bool]$VerifyRestore
        restore_seconds = $restoreSeconds
        api_smoke_verified = [bool]$VerifyApi
        api_smoke_result = $apiSmokeResult
    }
    $manifestPath = "$backupPath.json"
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding utf8

    Write-Output "Backup: $backupPath"
    Write-Output "Manifest: $manifestPath"
    Write-Output "Size: $($backupFile.Length) bytes"
    Write-Output "SHA-256: $($manifest.sha256)"
    Write-Output "Existing backups: preserved; no old backup was deleted."
    Write-Output "Restore verification: $restoreResult"
    Write-Output "API restore smoke: $apiSmokeResult"
    if ($restoreSeconds -ne $null) {
        Write-Output "Restore validation time: $restoreSeconds seconds"
    }
}
catch {
    if ($backupPath -and (Test-Path -LiteralPath $backupPath -PathType Leaf) -and
        -not (Test-Path -LiteralPath "$backupPath.json" -PathType Leaf)) {
        Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    }
    throw
}
finally {
    if ($apiSmokeContainer) {
        docker rm -f -v $apiSmokeContainer *> $null
    }
    if ($restoreContainer) {
        docker rm -f -v $restoreContainer *> $null
    }
    if ($containerDump) {
        docker compose exec -T db rm -f $containerDump *> $null
    }
    Pop-Location
}
