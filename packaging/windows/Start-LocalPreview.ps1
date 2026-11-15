$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$envPath = Join-Path $repoRoot ".env"
$composeFiles = @(
    "-f", (Join-Path $repoRoot "docker-compose.yml"),
    "-f", (Join-Path $repoRoot "docker-compose.local.yml")
)

function New-Secret([int]$ByteCount = 32) {
    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

$os = Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -lt 22000) {
    throw "RUSA AI Local Preview requires Windows 11 (build 22000 or later)."
}
if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "RUSA AI Local Preview currently supports Windows x64 only."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop is not installed. Install Docker Desktop, enable its WSL2 backend, restart Windows, then rerun this preview launcher."
}
docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is required."
}

try {
    docker info --format "{{.ServerVersion}}" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Docker daemon is not running." }
} catch {
    $dockerDesktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerDesktop) {
        Start-Process -FilePath $dockerDesktop
    }
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 2
        docker info --format "{{.ServerVersion}}" *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    }
    if (-not $ready) {
        throw "Docker Desktop did not become ready. Complete its first-run WSL2 setup and rerun this script."
    }
}

if (-not (Test-Path $envPath)) {
    $dbPassword = New-Secret
    $setupToken = New-Secret
    $webSecret = New-Secret
    $auditKey = New-Secret
    $metricsKey = New-Secret
    $content = @"
ENVIRONMENT=development
POSTGRES_PASSWORD=$dbPassword
RUSA_API_KEYS={}
RUSA_API_KEYS_JSON={}
RUSA_LOCAL_INSTALL_MODE=true
RUSA_LOCAL_API_KEYS_DB=/var/lib/rusa/local-api-keys.sqlite3
RUSA_LOCAL_SETUP_TOKEN=$setupToken
RUSA_WEB_SESSION_SECRET=$webSecret
RUSA_AUDIT_HMAC_KEY=$auditKey
RUSA_METRICS_API_KEY=$metricsKey
RUSA_DEFAULT_TENANT=local
RUSA_TENANT_REPOSITORIES={"local":["StrategyG/RusaAi"]}
RUSA_MODEL_NAME=rusaai-local-unprovisioned
RUSA_MODEL_BASE_URL=
RUSA_MODEL_API_KEY=
RUSA_RAG_ENABLED=false
"@
    Set-Content -Path $envPath -Value $content -Encoding ASCII
}

$envContent = Get-Content -Path $envPath -Raw
if ($envContent -notmatch "(?m)^RUSA_LOCAL_SETUP_TOKEN=.{32,}$") {
    throw "The existing .env has no valid RUSA_LOCAL_SETUP_TOKEN. Create a valid local .env first; the preview launcher will not overwrite existing secrets or data."
}
if ($envContent -notmatch "(?m)^RUSA_LOCAL_INSTALL_MODE=true$") {
    throw "The existing .env is not configured for local install mode. Create a local .env with RUSA_LOCAL_INSTALL_MODE=true before rerunning this launcher."
}

# Apply restrictive ACLs to both newly created and pre-existing local secrets.
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $envPath /inheritance:r /grant:r "${identity}:(F)" "SYSTEM:(F)" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not restrict access to the local .env file." }

Push-Location $repoRoot
try {
    docker compose @composeFiles config --quiet
    if ($LASTEXITCODE -ne 0) { throw "Docker Compose configuration validation failed." }

    docker compose @composeFiles up --build -d
    if ($LASTEXITCODE -ne 0) { throw "RUSA AI local control-plane startup failed." }

    $apiReady = $false
    for ($i = 0; $i -lt 90; $i++) {
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 3
            $apiReady = $true
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $apiReady) { throw "RUSA AI API did not become healthy. Inspect 'docker compose logs api'." }

    $webReady = $false
    for ($i = 0; $i -lt 60; $i++) {
        try {
            $null = Invoke-WebRequest -Uri "http://127.0.0.1:3000" -UseBasicParsing -TimeoutSec 3
            $webReady = $true
            break
        } catch {
            Start-Sleep -Seconds 2
        }
    }
    if (-not $webReady) { throw "RUSA AI web dashboard did not become healthy. Inspect 'docker compose logs web'." }

    Start-Process "http://127.0.0.1:3000/setup"
    Write-Host ""
    Write-Host "RUSA AI local control plane is running." -ForegroundColor Green
    Write-Host "First-run setup: http://127.0.0.1:3000/setup"
    Write-Host "API endpoint:    http://127.0.0.1:8000/v1"
    Write-Host ""
    Write-Warning "This is a local control-plane preview. Model inference is intentionally disabled until an approved RusaModels model artifact and runtime are bundled."
    Write-Host "To stop: docker compose $($composeFiles -join ' ') down"
} finally {
    Pop-Location
}
