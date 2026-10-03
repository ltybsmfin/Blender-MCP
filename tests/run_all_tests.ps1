<#
.SYNOPSIS
    Run every Blender MCP test suite with bounded waits and write the reports.

.DESCRIPTION
    Six suites, each independently runnable (see README):
      1. unit/protocol        - pure Python, no Blender               -> reports\unit_report.json
      2. transport review     - bridge client edges, no Blender       -> reports\review_report.json
      3. feature unit         - audit log + dashboard + tool schemas  -> reports\feature_unit_report.json
      4. blender              - real Blender, evaluated assertions    -> reports\blender_integration_report.json
      5. blender feature      - transactional edits + preview capture -> reports\blender_feature_report.json
      6. mcp stdio e2e        - real MCP stdio client + real Blender  -> reports\stdio_report.json
      7. mcp stdio feature    - new tools over the official protocol  -> reports\stdio_feature_report.json

    Blender runs use an isolated BLENDER_USER_CONFIG inside MCP\.runtime so the
    user profile is never written, and every child process this script starts is
    torn down by the test that started it (the stdio tests send the bridge
    'shutdown' command and only then, if needed, terminate their own PID).

    Note for sandboxed runs: this build environment denies os.chmod in the
    platform temp area and denies writes outside MCP\, so the three pre-existing
    unit checks that build fixtures with tempfile.TemporaryDirectory() or write
    into the sibling Project\ directory cannot pass here. They are unrelated to
    the feature code; see tests\reports\TEST_REPORT.md.
#>
[CmdletBinding()]
param(
    [switch]$SkipUnit,
    [switch]$SkipIntegration,
    [switch]$SkipStdio,
    [switch]$SkipFeature,
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
    Write-Host '=== 1/7 unit + protocol ========================================='
    & $Python (Join-Path $TestsDir 'test_unit_validation.py')
    $summary += [pscustomobject]@{ suite = 'unit'; exit = $LASTEXITCODE }
    & $Python (Join-Path $TestsDir 'test_review_transport.py')
    $summary += [pscustomobject]@{ suite = 'transport_review'; exit = $LASTEXITCODE }
}

if (-not $SkipFeature) {
    Write-Host '=== 2/7 feature unit (audit + dashboard + schemas) =============='
    & $Python (Join-Path $TestsDir 'test_audit_features.py')
    $summary += [pscustomobject]@{ suite = 'feature_unit'; exit = $LASTEXITCODE }
}

if (-not $SkipIntegration) {
    Write-Host '=== 3/7 Blender integration ===================================='
    $blender = Resolve-BlenderExecutable
    & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $TestsDir 'blender_integration.py')
    $summary += [pscustomobject]@{ suite = 'blender_integration'; exit = $LASTEXITCODE }
    if (Test-Path -LiteralPath (Join-Path $TestsDir 'blender_review_regressions.py')) {
        & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $TestsDir 'blender_review_regressions.py')
        $summary += [pscustomobject]@{ suite = 'blender_review'; exit = $LASTEXITCODE }
    }
}

if (-not $SkipFeature) {
    Write-Host '=== 4/7 Blender feature (transaction + preview + audit) ========='
    $blender = Resolve-BlenderExecutable
    & $blender --background --factory-startup --python-exit-code 1 --python (Join-Path $TestsDir 'blender_feature_integration.py')
    $summary += [pscustomobject]@{ suite = 'blender_feature'; exit = $LASTEXITCODE }
}

if (-not $SkipStdio) {
    Write-Host '=== 5/7 MCP stdio end-to-end ==================================='
    & $Python (Join-Path $TestsDir 'test_mcp_stdio.py')
    $summary += [pscustomobject]@{ suite = 'mcp_stdio'; exit = $LASTEXITCODE }
}

if (-not $SkipFeature -and -not $SkipStdio) {
    Write-Host '=== 6/7 MCP stdio feature (new tools) =========================='
    & $Python (Join-Path $TestsDir 'test_mcp_stdio_features.py')
    $summary += [pscustomobject]@{ suite = 'mcp_stdio_features'; exit = $LASTEXITCODE }
}

Write-Host ''
Write-Host '=== summary ===================================================='
$summary | Format-Table -AutoSize
foreach ($report in @('unit_report.json', 'review_report.json', 'feature_unit_report.json',
                      'blender_integration_report.json', 'blender_feature_report.json',
                      'stdio_report.json', 'stdio_feature_report.json')) {
    $path = Join-Path $Reports $report
    if (-not (Test-Path -LiteralPath $path)) { continue }
    # PowerShell's ConvertFrom-Json is case-insensitive, so a payload that carries
    # both 'localhost' and 'LOCALHOST' cannot be parsed; fall back to a text scan.
    try {
        $data = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
        $total = if ($data.PSObject.Properties.Name -contains 'tests_run') { $data.tests_run } else { $data.checks_total }
        $failed = @()
        if ($data.PSObject.Properties.Name -contains 'checks_failed') { $failed += @($data.checks_failed) }
        if ($data.PSObject.Properties.Name -contains 'errors') { $failed += @($data.errors | ForEach-Object { "error: $_" }) }
        if ($data.PSObject.Properties.Name -contains 'failures') { $failed += @($data.failures) }
        $failed = @($failed | Where-Object { $_ }) -join '; '
        Write-Host ("{0}: ok={1} checks={2} failed={3}" -f $report, $data.ok, $total, $failed)
    } catch {
        $text = Get-Content -LiteralPath $path -Raw
        $okMatch = [regex]::Match($text, '"ok"\s*:\s*(true|false)')
        $countMatch = [regex]::Match($text, '"(tests_run|checks_total)"\s*:\s*(\d+)')
        Write-Host ("{0}: ok={1} checks={2} (raw scan; {3})" -f $report,
            $(if ($okMatch.Success) { $okMatch.Groups[1].Value } else { '?' }),
            $(if ($countMatch.Success) { $countMatch.Groups[2].Value } else { '?' }),
            $_.Exception.Message.Split([Environment]::NewLine)[0])
    }
}

$failed = @($summary | Where-Object { $_.exit -ne 0 })
if ($failed.Count -gt 0) { exit 1 }
exit 0
