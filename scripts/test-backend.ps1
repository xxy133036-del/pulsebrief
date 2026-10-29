$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    docker compose --profile test run --build --rm test
    if ($LASTEXITCODE -ne 0) {
        throw "Backend tests or Ruff checks failed."
    }
}
finally {
    Pop-Location
}
