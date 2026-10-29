$ErrorActionPreference = 'Stop'
$toolRoot = 'F:\Resources\codex-tools\pulsebrief'
$pythonRoot = 'D:\python\python'
$pythonRuntime = Join-Path $pythonRoot 'cpython-3.13-windows-x86_64-none'
$pythonProjectRoot = Join-Path $pythonRoot 'pulsebrief'
$pythonEnvironment = Join-Path $pythonProjectRoot '.venv'
$npmCache = Join-Path $toolRoot 'npm-cache'
$gradleCache = 'F:\Resources\gradle-cache'
$pubCache = 'F:\Resources\pub-cache'
$androidUserHome = Join-Path $toolRoot '.android-user-home'
$androidSdk = Join-Path $toolRoot 'android-sdk'
$directories = @(
    (Join-Path $toolRoot '.tmp'),
    (Join-Path $toolRoot '.uv-cache'),
    $npmCache,
    $gradleCache,
    $pubCache,
    $androidUserHome
)
foreach ($directory in $directories) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}
New-Item -ItemType Directory -Force -Path $pythonProjectRoot | Out-Null

$env:TEMP = Join-Path $toolRoot '.tmp'
$env:TMP = $env:TEMP
$env:UV_CACHE_DIR = Join-Path $toolRoot '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = $pythonRoot
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:UV_PYTHON = Join-Path $pythonRuntime 'python.exe'
$env:UV_PROJECT_ENVIRONMENT = $pythonEnvironment
$env:PATH = "$pythonEnvironment\Scripts;$pythonRuntime;$env:PATH"
$env:npm_config_cache = $npmCache
$env:GRADLE_USER_HOME = $gradleCache
$env:PUB_CACHE = $pubCache
$env:ANDROID_USER_HOME = $androidUserHome
$env:ANDROID_HOME = $androidSdk
$env:ANDROID_SDK_ROOT = $androidSdk

Write-Host "Python runtime and virtual environment: $pythonRoot"
Write-Host "Python, npm, Flutter, Gradle, and Android caches are directed to D: and F: under $toolRoot or F:\Resources"
