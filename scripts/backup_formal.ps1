param(
    [Parameter(Position = 0)]
    [ValidateSet("create", "drill")]
    [string]$Action = "create",
    [string]$BundlePath = "",
    [string]$KeyFile = "secrets/backup_key"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $ProjectRoot
$Python = Join-Path $ProjectRoot ".venv/Scripts/python.exe"
$KeyPath = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $KeyFile))
if (-not (Test-Path -LiteralPath $KeyPath -PathType Leaf)) { throw "Backup key file is missing; run local.ps1 init" }

function Assert-SafeTemporaryPath([string]$Path) {
    $root = [IO.Path]::GetFullPath((Join-Path $env:SystemDrive "finscope-backup-staging"))
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing unsafe backup staging path: $resolved"
    }
}

function Invoke-Checked([scriptblock]$Command, [string]$Message) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw $Message }
}

if (-not $BundlePath) {
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss")
    $BundlePath = Join-Path $ProjectRoot "backups/finscope-$stamp.fsbk"
}
$BundlePath = [IO.Path]::GetFullPath($BundlePath)
$stagingRoot = Join-Path $env:SystemDrive "finscope-backup-staging"
New-Item -ItemType Directory -Path $stagingRoot -Force | Out-Null
$staging = Join-Path $stagingRoot ([Guid]::NewGuid().ToString("N"))
Assert-SafeTemporaryPath $staging
New-Item -ItemType Directory -Path $staging -Force | Out-Null
$started = [DateTime]::UtcNow
$helper = "finscope-backup-$([Guid]::NewGuid().ToString('N').Substring(0, 12))"

try {
    if ($Action -eq "create") {
        Invoke-Checked { docker compose --profile core exec -T postgres /bin/sh /ops/postgres_backup_snapshot.sh /tmp/finscope-backup.dump /tmp/finscope-object-inventory.tsv /tmp/finscope-schema-revision.txt } "consistent PostgreSQL snapshot failed"
        Invoke-Checked { docker compose --profile core cp postgres:/tmp/finscope-backup.dump (Join-Path $staging "postgres.dump") } "database backup copy failed"
        $inventoryPath = Join-Path $staging "object-inventory.tsv"
        Invoke-Checked { docker compose --profile core cp postgres:/tmp/finscope-object-inventory.tsv $inventoryPath } "object inventory copy failed"
        $schemaPath = Join-Path $staging "schema-revision.txt"
        Invoke-Checked { docker compose --profile core cp postgres:/tmp/finscope-schema-revision.txt $schemaPath } "schema revision copy failed"
        $minioStage = Join-Path $staging "minio"
        New-Item -ItemType Directory -Path $minioStage -Force | Out-Null
        Invoke-Checked { docker compose --profile core run -d --name $helper --entrypoint /bin/sh minio-init -c "sleep 300" } "MinIO backup helper failed to start"
        Invoke-Checked { docker cp $inventoryPath "${helper}:/tmp/object-inventory.tsv" } "object inventory helper copy failed"
        Invoke-Checked { docker exec $helper /bin/sh /ops/minio_backup.sh collect /tmp/export /tmp/object-inventory.tsv } "MinIO backup collection failed"
        Invoke-Checked { docker cp "${helper}:/tmp/export/." $minioStage } "MinIO backup copy failed"
        docker rm -f $helper | Out-Null
        Invoke-Checked { & $Python -m scripts.backup_bundle_cli create --source $staging --bundle $BundlePath --key-file $KeyPath --schema-revision-file $schemaPath --object-inventory $inventoryPath } "backup encryption failed"
        Write-Host "Encrypted backup created: $BundlePath"
    }
    else {
        $restore = Join-Path $staging "restore"
        Invoke-Checked { & $Python -m scripts.backup_bundle_cli restore --bundle $BundlePath --destination $restore --key-file $KeyPath } "backup verification failed"
        $minioRestore = Join-Path $restore "minio"
        New-Item -ItemType Directory -Path $minioRestore -Force | Out-Null
        $database = "finscope_restore_$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
        $bucket = "finscope-restore-$([Guid]::NewGuid().ToString('N').Substring(0, 12))"
        try {
            Invoke-Checked { docker compose --profile core exec -T postgres createdb -U finscope_admin $database } "isolated restore database creation failed"
            Invoke-Checked { docker compose --profile core cp (Join-Path $restore "postgres.dump") "postgres:/tmp/finscope-restore.dump" } "restore dump copy failed"
            Invoke-Checked { docker compose --profile core exec -T postgres pg_restore -U finscope_admin -d $database --exit-on-error /tmp/finscope-restore.dump } "PostgreSQL restore failed"
            $expectedRevision = [string]((Get-Content -LiteralPath (Join-Path $restore "finscope-manifest.json") -Raw | ConvertFrom-Json).schema_revision)
            $restoredRevision = [string](docker compose --profile core exec -T postgres psql -U finscope_admin -d $database -Atqc "SELECT version_num FROM alembic_version")
            if ($LASTEXITCODE -ne 0 -or $restoredRevision.Trim() -ne $expectedRevision) {
                throw "restored schema revision does not match the backup manifest"
            }
            Invoke-Checked { docker compose --profile core run -d --name $helper --entrypoint /bin/sh minio-init -c "sleep 300" } "MinIO restore helper failed to start"
            Invoke-Checked { docker exec $helper mkdir -p /tmp/restore } "MinIO restore staging failed"
            Invoke-Checked { docker cp "$minioRestore/." "${helper}:/tmp/restore" } "MinIO restore copy failed"
            Invoke-Checked { docker cp (Join-Path $restore "object-inventory.tsv") "${helper}:/tmp/object-inventory.tsv" } "restore inventory helper copy failed"
            Invoke-Checked { docker exec $helper /bin/sh /ops/minio_backup.sh drill /tmp/restore /tmp/object-inventory.tsv $bucket } "isolated MinIO restore drill failed"
            docker rm -f $helper | Out-Null
        }
        finally {
            docker compose --profile core exec -T postgres dropdb -U finscope_admin --if-exists $database | Out-Null
        }
        $elapsed = ([DateTime]::UtcNow - $started).TotalSeconds
        Write-Host ("Restore drill passed in {0:N1} seconds" -f $elapsed)
    }
}
finally {
    docker rm -f $helper 2>$null | Out-Null
    docker compose --profile core exec -T postgres /bin/sh -c "rm -f /tmp/finscope-backup.dump /tmp/finscope-object-inventory.tsv /tmp/finscope-schema-revision.txt /tmp/finscope-restore.dump" 2>$null | Out-Null
    Assert-SafeTemporaryPath $staging
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
