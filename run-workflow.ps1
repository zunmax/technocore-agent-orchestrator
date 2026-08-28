[CmdletBinding()]
param(
    [ValidatePattern('^run_[a-z0-9][a-z0-9_-]{7,63}$')]
    [string]$RunId = "run_$((Get-Date).ToString('yyyyMMdd_HHmmss_fff'))"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -lt 7) {
    throw 'PowerShell 7 or newer is required.'
}
. (Join-Path $PSScriptRoot 'scripts\launcher-tools.ps1')

$profile = Join-Path $PSScriptRoot 'workflow.toml'
$uv = Resolve-NativeApplication -Name 'uv'
if (-not (Test-Path -LiteralPath $profile -PathType Leaf)) {
    throw 'workflow.toml is missing. Copy examples\workflow.example.toml to workflow.toml, then configure the provider executables, models, and versions.'
}

function Test-TechnocoreHealth {
    try {
        Invoke-RestMethod `
            -Uri 'http://127.0.0.1:8080/healthz' `
            -TimeoutSec 2 `
            -ErrorAction Stop | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

if (-not (Test-TechnocoreHealth)) {
    $docker = Resolve-NativeApplication -Name 'docker'
    $containerName = 'technocore-workflow-local'
    & $docker container inspect $containerName *> $null
    if ($LASTEXITCODE -eq 0) {
        & $docker start $containerName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to start the existing local Technocore container.'
        }
    }
    else {
        $technocoreSource = Join-Path (Split-Path -Parent $PSScriptRoot) 'technocore-chat'
        $expectedCommit = 'd8775c2c03e4fc96c24022ffa7103cc765ea94fc'
        if (-not (Test-Path -LiteralPath $technocoreSource -PathType Container)) {
            throw "Pinned Technocore source is missing: $technocoreSource"
        }
        $git = Resolve-NativeApplication -Name 'git'
        $actualCommit = (& $git -C $technocoreSource rev-parse HEAD).Trim()
        if ($LASTEXITCODE -ne 0 -or $actualCommit -ne $expectedCommit) {
            throw "The local technocore-chat source must be pinned to $expectedCommit."
        }
        $sourceStatus = (& $git -C $technocoreSource status --porcelain --untracked-files=all)
        if ($LASTEXITCODE -ne 0 -or $sourceStatus) {
            throw 'The pinned local technocore-chat source must have a clean working tree.'
        }
        & $docker build `
            --file (Join-Path $technocoreSource 'docker\Dockerfile') `
            --tag 'technocore-chat:agent-orchestrator' `
            $technocoreSource
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to build the pinned local Technocore image.'
        }
        & $docker run `
            --detach `
            --name $containerName `
            --restart unless-stopped `
            --publish '127.0.0.1:8080:8080' `
            --volume 'technocore-workflow-data:/data' `
            'technocore-chat:agent-orchestrator' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Unable to create the local Technocore container.'
        }
    }

    $healthDeadline = (Get-Date).AddSeconds(60)
    while (-not (Test-TechnocoreHealth) -and (Get-Date) -lt $healthDeadline) {
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-TechnocoreHealth)) {
        throw 'Local Technocore did not become healthy within 60 seconds.'
    }
}

Write-Host "Starting Technocore workflow: $RunId"
& (Join-Path $PSScriptRoot 'open-chat.ps1') -RunId $RunId

& $uv run --project $PSScriptRoot technocore-orchestrator run `
    --config $profile `
    --run-id $RunId `
    --allow-model-invocations `
    --json

exit $LASTEXITCODE
