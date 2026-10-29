$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$compose = Join-Path $projectRoot 'compose.yaml'
$url = 'http://127.0.0.1:8080'
$requiredDockerRoot = 'F:\Resources'

try {
    $envFile = Join-Path $projectRoot '.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $envFile
    }
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Docker Desktop is not installed. Install it and set its disk image location to F:\Resources first.'
    }

    $dockerSettingsPath = Join-Path $env:APPDATA 'Docker\settings-store.json'
    if (-not (Test-Path -LiteralPath $dockerSettingsPath)) {
        $dockerSettingsPath = Join-Path $env:APPDATA 'Docker\settings.json'
    }
    if (-not (Test-Path -LiteralPath $dockerSettingsPath)) {
        throw 'Cannot verify Docker Desktop disk image location. Set it to F:\Resources in Docker Desktop Settings before starting the project.'
    }
    $dockerSettings = Get-Content -LiteralPath $dockerSettingsPath -Raw | ConvertFrom-Json
    $dockerDataRoot = [string]$dockerSettings.CustomWslDistroDir
    $dockerDataRootNormalized = if ([string]::IsNullOrWhiteSpace($dockerDataRoot)) {
        ''
    } else {
        [System.IO.Path]::GetFullPath($dockerDataRoot).TrimEnd('\')
    }
    $requiredDockerRootNormalized = $requiredDockerRoot.TrimEnd('\')
    if ([string]::IsNullOrWhiteSpace($dockerDataRootNormalized) -or
        ($dockerDataRootNormalized -ne $requiredDockerRootNormalized -and
            -not $dockerDataRootNormalized.StartsWith(
                $requiredDockerRootNormalized + '\',
                [System.StringComparison]::OrdinalIgnoreCase))) {
        throw "Docker Desktop currently stores its disk image at '$dockerDataRoot'. Move it to F:\Resources in Docker Desktop Settings before downloading project images."
    }

    try { docker info --format '{{.ServerVersion}}' *> $null } catch { }
    if ($LASTEXITCODE -ne 0) {
        $dockerCommand = Get-Command docker
        $dockerBin = Split-Path -Parent $dockerCommand.Source
        $dockerResources = Split-Path -Parent $dockerBin
        $dockerInstall = Split-Path -Parent $dockerResources
        $desktopCandidates = @(
            (Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'),
            (Join-Path $dockerInstall 'Docker Desktop.exe')
        )
        $desktop = $desktopCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if (-not $desktop) { throw 'Docker Desktop could not be located beside the Docker CLI. Open it manually, then retry.' }
        Start-Process -FilePath $desktop -WindowStyle Hidden
        $ready = $false
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Seconds 2
            docker info --format '{{.ServerVersion}}' *> $null
            if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        }
        if (-not $ready) { throw 'Docker Engine did not start. Open Docker Desktop and check its status.' }
    }

    Push-Location $projectRoot
    try {
        $tunnelConfigured = $false
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match '^\s*CLOUDFLARE_TUNNEL_TOKEN\s*=\s*(.*?)\s*$') {
                $tokenValue = $Matches[1].Trim().Trim('"').Trim("'")
                if (-not [string]::IsNullOrWhiteSpace($tokenValue)) {
                    if ($tokenValue -match '\s|--token|docker\s+run') {
                        throw 'CLOUDFLARE_TUNNEL_TOKEN must contain only the secret value after --token, not the full Docker command. The token was not displayed.'
                    }
                    $tunnelConfigured = $true
                    break
                }
            }
        }

        if ($tunnelConfigured) {
            docker compose --profile tunnel -f $compose up --build -d
        } else {
            docker compose -f $compose up --build -d
        }
        if ($LASTEXITCODE -ne 0) { throw 'The project containers could not be started.' }
    } finally { Pop-Location }

    $apiHealthy = $false
    $websiteHealthy = $false
    for ($i = 0; $i -lt 120; $i++) {
        try {
            $response = Invoke-RestMethod -Uri "$url/api/v1/health" -TimeoutSec 3
            $apiHealthy = $response.status -eq 'ok'
        } catch { }
        if ($apiHealthy) {
            try {
                $page = Invoke-WebRequest -Uri $url -TimeoutSec 3 -UseBasicParsing
                $websiteHealthy = $page.StatusCode -eq 200
            } catch { }
        }
        if ($apiHealthy -and $websiteHealthy) { break }
        Start-Sleep -Seconds 5
    }
    if (-not ($apiHealthy -and $websiteHealthy)) {
        $serviceStatus = docker compose -f $compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Health}}' 2>&1 | Out-String
        throw "The project did not become ready within 10 minutes. API healthy: $apiHealthy; website healthy: $websiteHealthy.`n`nContainer status:`n$serviceStatus"
    }
    Start-Process $url
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, '每日 AI 新闻助手', 'OK', 'Error') | Out-Null
}
