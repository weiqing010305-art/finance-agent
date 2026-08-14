param(
    [Parameter(Position = 0)]
    [ValidateSet("init", "up", "status", "logs", "bootstrap", "down", "test")]
    [string]$Action = "status",
    [string]$Email = "owner@example.com",
    [string]$TenantName = "FinScope Local"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot

function New-SafeSecret {
    $bytes = [byte[]]::new(32)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Initialize-Secrets {
    $secretDirectory = Join-Path $ProjectRoot "secrets"
    New-Item -ItemType Directory -Path $secretDirectory -Force | Out-Null
    foreach ($name in @("postgres_admin_password", "postgres_app_password", "postgres_worker_password", "minio_root_access_key", "minio_root_secret_key", "minio_app_access_key", "minio_app_secret_key", "minio_worker_access_key", "minio_worker_secret_key", "milvus_minio_access_key", "milvus_minio_secret_key", "jwt_signing_key", "backup_key")) {
        $target = Join-Path $secretDirectory $name
        if (Test-Path -LiteralPath $target -PathType Container) {
            $children = @(Get-ChildItem -LiteralPath $target -Force)
            if ($children.Count -ne 0) {
                throw "Secret path is a non-empty directory and will not be replaced: $target"
            }
            Remove-Item -LiteralPath $target
        }
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            [IO.File]::WriteAllText($target, (New-SafeSecret), [Text.UTF8Encoding]::new($false))
        }
        if ((Get-Item -LiteralPath $target).Length -eq 0) {
            throw "Secret file is empty: $target"
        }
    }
    Write-Host "Local secrets are ready in the ignored secrets directory."
}

function Assert-DockerReady {
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    docker info *> $null
    $dockerExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($dockerExitCode -ne 0) {
        throw "Docker Desktop is not running. Start Docker Desktop, wait for the engine, then retry."
    }
}

switch ($Action) {
    "init" { Initialize-Secrets }
    "up" {
        Initialize-Secrets
        Assert-DockerReady
        docker compose --profile core up -d --build
        if ($LASTEXITCODE -ne 0) { throw "Docker Compose startup failed" }
        Write-Host "FinScope: https://localhost:8443/  Mailpit: http://127.0.0.1:8025/"
    }
    "status" {
        Assert-DockerReady
        docker compose --profile core ps
    }
    "logs" {
        Assert-DockerReady
        docker compose --profile core logs --tail 200 -f api worker dispatcher
    }
    "bootstrap" {
        Assert-DockerReady
        $securePassword = Read-Host "Owner password" -AsSecureString
        $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        try {
            $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
            $plainPassword | docker compose --profile core run --rm -T migrate python scripts/container_entrypoint.py python -m scripts.bootstrap_admin --password-stdin --email $Email --tenant-name $TenantName
            if ($LASTEXITCODE -ne 0) { throw "Owner bootstrap failed" }
        }
        finally {
            $plainPassword = $null
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
    }
    "down" {
        Assert-DockerReady
        docker compose --profile core down
    }
    "test" {
        & ".\.venv\Scripts\python.exe" -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
    }
}
