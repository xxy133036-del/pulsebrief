$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    docker compose stop
    if ($LASTEXITCODE -ne 0) {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show('服务未能正常停止，请查看 Docker Desktop。', '每日 AI 新闻助手', 'OK', 'Warning') | Out-Null
    }
} finally { Pop-Location }
