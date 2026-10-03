<#
.SYNOPSIS
    Run every Blender MCP test suite with bounded waits and write the reports.

.DESCRIPTION
    Three suites, each independently runnable (see README):
      1. unit/protocol   - pure Python, no Blender               -> reports\unit_report.json
      2. blender         - real Blender, evaluated assertions    -> reports\blender_integration_report.json
      3. mcp stdio e2e   - real MCP stdio client + real Blender  -> reports\stdio_report.json

    Blender runs use an isolated BLENDER_USER_CONFIG inside MCP\.runtime so the
    user profile is never written, and every child process this script starts is
    torn down by the test that started it (the stdio test sends the bridge
    'shutdown' command and only then, if needed, terminates its own PID).
#>
[CmdletBinding()]
param(
    [switch]$SkipUnit,
    [switch]$SkipIntegration,
    [switch]$SkipStdio,
    [string]$BlenderPath = $env:BLENDER_MCP_BLENDER,
    [int]$Port = 0
)

$ErrorActionPreference = 'Continue'
$TestsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MCPDir = Split-Path -Parent $TestsDir
$Workspace = Split-Path -Parent $MCPDir
$Python = Join-Path $MCPDir '.venv\Scripts\python.exe'
$Runtime = Join-Path $MCPDir '.runtime'
$Reports = Join-Path $TestsDir 'reports'

New-Item -ItemType Directory -Force -Path $Runtime, $Reports, (Join-Path $Runtime 'tmp') | Out-Null
$env:TMP = Join-Path $Runtime 'tmp'
$env:TEMP = $env:TMP
$env:BLENDER_USER_CONFIG = Join-Path $Runtime 'blender_config'
$env:BLENDER_MCP_TEST_PORT = "$Port"
New-Item -ItemType Directory -Force -Path $env:BLENDER_USER_CONFIG | Out-Null

function Resolve-BlenderExecutable {
    if ($BlenderPath -and (Test-Path -LiteralPath $BlenderPath)) { return (Resolve-Path -LiteralPath $BlenderPath).Path }
    $versionDir = Join-Path $Workspace 'Version'
    if (Test-Path -LiteralPath $versionDir) {
        $found = @(
            Get-ChildItem -LiteralPath $versionDir -Directory |
                Sort-Object -Property Name -Descending |
                ForEach-Object { Join-Path $_.FullName 'blender.exe' } |
                Where-Object { Test-Path -LiteralPath $_ }
        )
        if ($found.Count -gt 0) { return (Resolve-Path -LiteralPath $found[0]).Path }
    }
    throw 'No Blender executable found; pass -BlenderPath or set BLENDER_MCP_BLENDER.'
}

$summary = @()

if (-not $SkipUnit) {
    Write-Host '=== 1/3 unit + protocol ========================================='
    & $Python (Join-Path $TestsDir 'test_unit_validation.py')
    $summary += [pscustomobject]@{ suite = 'unit'; exit = $LASTEXITCODE }
    & $Python (Join-Path $TestsDir 'test_review_transport.py')
    $summary += [pscustomobject]@{ suite = 'transport_review'; exit = $LASTEXITCODE }
}

if (-not $SkipIntegration) {
    Write-Host '=== 2/3 Blender integration ===================================='
    $blender = Resolve-BlenderExecutable
    & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $TestsDir 'blender_integration.py')
    $summary += [pscustomobject]@{ suite = 'blender_integration'; exit = $LASTEXITCODE }
    if (Test-Path -LiteralPath (Join-Path $TestsDir 'blender_review_regressions.py')) {
        & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $TestsDir 'blender_review_regressions.py')
        $summary += [pscustomobject]@{ suite = 'blender_review'; exit = $LASTEXITCODE }
    }
}

if (-not $SkipStdio) {
    Write-Host '=== 3/3 MCP stdio end-to-end ==================================='
    & $Python (Join-Path $TestsDir 'test_mcp_stdio.py')
    $summary += [pscustomobject]@{ suite = 'mcp_stdio'; exit = $LASTEXITCODE }
}

Write-Host ''
Write-Host '=== summary ===================================================='
$summary | Format-Table -AutoSize
foreach ($report in @('unit_report.json', 'blender_integration_report.json', 'stdio_report.json')) {
    $path = Join-Path $Reports $report
    if (Test-Path -LiteralPath $path) {
        $data = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        Write-Host ("{0}: ok={1} checks={2} failed={3}" -f $report, $data.ok, $data.checks_total, ($data.checks_failed -join ','))
    }
}

$failed = @($summary | Where-Object { $_.exit -ne 0 })
if ($failed.Count -gt 0) { exit 1 }
exit 0
