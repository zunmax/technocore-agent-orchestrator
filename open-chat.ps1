[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^run_[a-z0-9][a-z0-9_-]{7,63}$')]
    [string]$RunId
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
    throw 'workflow.toml is missing. Copy examples\workflow.example.toml to workflow.toml, then configure it before opening a run.'
}
$arguments = @(
    'run',
    '--project', $PSScriptRoot,
    'technocore-orchestrator',
    'view', $RunId,
    '--config', $profile
    '--startup-timeout', '120'
)

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $uv
$startInfo.UseShellExecute = $false
$startInfo.CreateNoWindow = $true
$startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
foreach ($argument in $arguments) {
    $startInfo.ArgumentList.Add($argument)
}
$viewer = [System.Diagnostics.Process]::Start($startInfo)
if ($null -eq $viewer) {
    throw 'Unable to start the private Technocore conversation viewer.'
}
Write-Host "Opening the private Technocore conversation UI for $RunId (viewer process $($viewer.Id))."
