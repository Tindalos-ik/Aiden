#requires -Version 5.1

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$backend = Join-Path $root 'backend'
$frontend = Join-Path $root 'frontend'
$mysqlEnv = Join-Path $root 'deploy\.env.mysql'
$backendEnv = Join-Path $backend '.env'
$composeFile = Join-Path $root 'deploy\compose.mysql.yml'
$python = Join-Path $backend '.venv\Scripts\python.exe'
$alembic = Join-Path $backend '.venv\Scripts\alembic.exe'
$logDir = Join-Path $backend '.tmp'

function Read-DotEnvValue {
    param([string]$Path, [string]$Name)

    $prefix = "$Name="
    foreach ($line in [System.IO.File]::ReadAllLines($Path)) {
        $candidate = $line.TrimStart()
        if (-not $candidate.StartsWith($prefix, [System.StringComparison]::Ordinal)) { continue }
        $value = $candidate.Substring($prefix.Length).Trim()
        if ($value.Length -ge 2 -and (
            ($value[0] -eq '"' -and $value[-1] -eq '"') -or
            ($value[0] -eq "'" -and $value[-1] -eq "'"))) {
            return $value.Substring(1, $value.Length - 2)
        }
        return $value
    }
    return ''
}

if (-not (Test-Path -LiteralPath $mysqlEnv -PathType Leaf)) {
    throw 'Missing deploy/.env.mysql. Create it with MYSQL_ROOT_PASSWORD, MYSQL_DATABASE, MYSQL_USER, MYSQL_PASSWORD and DATABASE_URL.'
}
if (-not (Test-Path -LiteralPath $backendEnv -PathType Leaf)) {
    throw 'Missing backend/.env. Copy backend/.env.example and configure DATABASE_URL and model credentials.'
}

$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $dockerCommand) { throw 'Docker CLI is unavailable. Install and start Docker Desktop.' }
if (-not $npmCommand) { throw 'npm.cmd is unavailable. Install Node.js and npm.' }

$mysqlPassword = Read-DotEnvValue $mysqlEnv 'MYSQL_PASSWORD'
$rootPassword = Read-DotEnvValue $mysqlEnv 'MYSQL_ROOT_PASSWORD'
$mysqlUrl = Read-DotEnvValue $mysqlEnv 'DATABASE_URL'
$backendUrl = Read-DotEnvValue $backendEnv 'DATABASE_URL'
$modelKey = if ($env:OPENAI_API_KEY) { $env:OPENAI_API_KEY } else { Read-DotEnvValue $backendEnv 'OPENAI_API_KEY' }
$modelName = if ($env:OPENAI_MODEL) { $env:OPENAI_MODEL } else { Read-DotEnvValue $backendEnv 'OPENAI_MODEL' }

if (-not $mysqlPassword -or -not $rootPassword -or $mysqlPassword -match '^(change_me|your_|<)' -or $rootPassword -match '^(change_me|your_|<)') {
    throw 'Set real local MySQL passwords in deploy/.env.mysql.'
}
if (-not $mysqlUrl -or -not $backendUrl -or $mysqlUrl -ne $backendUrl) {
    throw 'DATABASE_URL must be set to the same value in deploy/.env.mysql and backend/.env.'
}
if (-not $modelKey -or -not $modelName) {
    throw 'Set OPENAI_API_KEY and OPENAI_MODEL in backend/.env (or the current process environment).'
}
$env:DATABASE_URL = $backendUrl

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $basePython = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $basePython) { throw 'Python is unavailable. Install Python 3.10 or newer.' }
    & $basePython.Source -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer is required.' }
    Write-Host 'Creating backend virtual environment...'
    & $basePython.Source -m venv (Join-Path $backend '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create backend virtual environment.' }
}

& $python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'backend/.venv must use Python 3.10 or newer.' }

& $python -c 'import fastapi, uvicorn, sqlalchemy, alembic, langgraph, langchain_openai, pymysql' *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing backend dependencies...'
    & $python -m pip install -r (Join-Path $backend 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependency installation failed.' }
}
if (-not (Test-Path -LiteralPath $alembic -PathType Leaf)) {
    throw 'Alembic CLI is missing from backend/.venv. Reinstall backend/requirements.txt.'
}

if (-not (Test-Path -LiteralPath (Join-Path $frontend 'node_modules\vite\bin\vite.js') -PathType Leaf)) {
    Write-Host 'Installing frontend dependencies...'
    Push-Location $frontend
    try {
        & $npmCommand.Source ci
        if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    } finally {
        Pop-Location
    }
}

$composeArgs = @('compose', '--env-file', $mysqlEnv, '-f', $composeFile)
Write-Host 'Starting MySQL...'
& $dockerCommand.Source @composeArgs up -d mysql
if ($LASTEXITCODE -ne 0) { throw 'MySQL failed to start. Check Docker Desktop and deploy/.env.mysql.' }

$containerId = (& $dockerCommand.Source @composeArgs ps -q mysql | Select-Object -First 1)
if (-not $containerId) { throw 'MySQL container was not found after startup.' }
$containerId = $containerId.Trim()
$mysqlReady = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $health = (& $dockerCommand.Source inspect --format '{{.State.Health.Status}}' $containerId 2>$null | Select-Object -First 1)
    if ($health -eq 'healthy') { $mysqlReady = $true; break }
    if ($health -eq 'unhealthy') { throw 'MySQL health check failed. Inspect the container logs.' }
    Start-Sleep -Seconds 2
}
if (-not $mysqlReady) { throw 'MySQL did not become healthy within two minutes.' }

Push-Location $backend
try {
    Write-Host 'Applying database migrations...'
    & $alembic -c alembic.ini upgrade head
    if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }

    Write-Host 'Ensuring local demo users exist...'
    & $python -m scripts.init_demo_users
    if ($LASTEXITCODE -ne 0) { throw 'Demo user initialization failed.' }
} finally {
    Pop-Location
}

if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8000 is already in use. Stop the existing service before running start.ps1.'
}

New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$apiStdout = Join-Path $logDir 'api.stdout.log'
$apiStderr = Join-Path $logDir 'api.stderr.log'
$apiProcess = $null

try {
    Write-Host 'Starting FastAPI...'
    $apiProcess = Start-Process -FilePath $python `
        -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000') `
        -WorkingDirectory $backend -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $apiStdout -RedirectStandardError $apiStderr

    $apiReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Seconds 1
        $apiProcess.Refresh()
        if ($apiProcess.HasExited) { break }
        try {
            $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { $apiReady = $true; break }
        } catch {
            # The API may still be starting.
        }
    }
    if (-not $apiReady) { throw "FastAPI did not start. Check $apiStderr" }

    $env:VITE_API_MODE = 'remote'
    $env:VITE_API_BASE_URL = '/api'
    Write-Host 'FastAPI: http://127.0.0.1:8000/api/health'
    Write-Host 'Frontend: http://127.0.0.1:5173'
    Write-Host "FastAPI logs: $apiStdout and $apiStderr"
    Write-Host 'Press Ctrl+C to stop the frontend and backend. MySQL will keep running.'

    Push-Location $frontend
    try {
        & $npmCommand.Source run dev -- --host 127.0.0.1 --port 5173 --strictPort
        if ($LASTEXITCODE -ne 0) { throw 'Frontend exited with an error.' }
    } finally {
        Pop-Location
    }
} finally {
    if ($apiProcess) {
        $apiProcess.Refresh()
        if (-not $apiProcess.HasExited) {
            Stop-Process -Id $apiProcess.Id -Force -ErrorAction SilentlyContinue
        }
    }
}
