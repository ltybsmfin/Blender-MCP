<#
.SYNOPSIS
    Start (hidden) the local Blender MCP audit-log dashboard and print its URL.

.DESCRIPTION
    The dashboard is a read-only, loopback-only HTTP page served by
    ``blender_mcp.log_viewer`` (Python standard library, no CDN, no build step).
    It shows every audited MCP operation: tool name, sanitised parameters,
    outcome (成功 / 错误 / 进行中 / 已回滚), duration, batch steps and the linked
    Blender bridge command.

    The process is started hidden (``-WindowStyle Hidden``); it never prints
    anything to the caller's console except the URL line, and it never needs
    administrator rights. Tokens are never part of the log, so nothing secret can
    appear in the page.

.PARAMETER Open
    Also open the dashboard in the default browser.

.PARAMETER Reuse
    Reuse an already-running dashboard for the same log directory instead of
    starting another one (default: on).

.PARAMETER Stop
    Stop the dashboard(s) this script previously started (matched by the PID in
    the per-process metadata file *and* by the process command line, so an
    unrelated process that reused the PID is never killed).

.PARAMETER Port
    0 (default) lets the OS pick a free port, which is what makes concurrent
    launches race-safe. An explicit busy port is reported as an error.

.PARAMETER Seconds
    Stop the viewer after N seconds (0 = run until stopped/killed).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File MCP\launch_log_viewer.ps1 -Open

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File MCP\launch_log_viewer.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Open,
    [switch]$Reuse = $true,
    [switch]$Stop,
    [string]$Root,
    [int]$Port = 0,
    [int]$Seconds = 0,
    [string]$Python
)

$ErrorActionPreference = 'Stop'
$MCPDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = Join-Path $MCPDir '.runtime'
$LogsDir = Join-Path $Runtime 'logs'
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
if (-not $Root) { $Root = $LogsDir }
$Root = [System.IO.Path]::GetFullPath($Root)

if (-not $Python) {
    $candidate = Join-Path $MCPDir '.venv\Scripts\python.exe'
    $Python = if (Test-Path -LiteralPath $candidate) { $candidate } else { 'python' }
}

function Get-ViewerMetadata {
    if (-not (Test-Path -LiteralPath $Root)) { return @() }
    Get-ChildItem -LiteralPath $Root -Filter 'viewer-*.json' -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            $file = $_.FullName
            try {
                $obj = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
                Add-Member -InputObject $obj -NotePropertyName '_file' -NotePropertyValue $file -Force
                $obj
            } catch { $null }
        } | Where-Object { $_ -and $_.service -eq 'blender-mcp-log-viewer' }
}

function Test-ViewerAlive {
    param($Meta)
    if (-not $Meta.url) { return $false }
    try {
        $response = Invoke-WebRequest -Uri ("{0}api/health" -f $Meta.url) -TimeoutSec 2 -UseBasicParsing
        if ($response.StatusCode -ne 200) { return $false }
        $health = $response.Content | ConvertFrom-Json
        return ($health.service -eq 'blender-mcp-log-viewer' -and $health.pid -eq $Meta.pid)
    } catch {
        return $false
    }
}

if ($Stop) {
    $stopped = @()
    foreach ($meta in Get-ViewerMetadata) {
        $pidValue = [int]$meta.pid
        if (-not (Test-ViewerAlive -Meta $meta)) { continue }
        # Prefer a command-line check so a reused PID is never killed by accident.
        # In a restricted sandbox CIM may be denied; the health probe above already
        # proved this PID is our viewer process, so fall back to that.
        $verified = $true
        try {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $pidValue" -ErrorAction Stop
            if ($proc -and $proc.CommandLine -and $proc.CommandLine -notmatch 'log_viewer') {
                Write-Warning "PID $pidValue does not look like the log viewer; leaving it alone."
                continue
            }
            if (-not $proc) { continue }
        } catch {
            $verified = 'health-probe-only'
        }
        Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue
        if ($meta._file) { Remove-Item -LiteralPath $meta._file -Force -ErrorAction SilentlyContinue }
        $stopped += $pidValue
        Write-Host ("stopped log viewer pid {0} (verified: {1})" -f $pidValue, $verified)
    }
    if ($stopped.Count -gt 0) { Write-Host ("stopped log viewer pid(s): {0}" -f ($stopped -join ', ')) }
    else { Write-Host 'no log viewer started by this script is running' }
    exit 0
}

if ($Reuse) {
    foreach ($meta in Get-ViewerMetadata) {
        if (Test-ViewerAlive -Meta $meta) {
            Write-Host ("LOG_VIEWER_URL {0}" -f $meta.url)
            Write-Host ("[log-viewer] reusing pid {0} (root {1})" -f $meta.pid, $meta.root)
            if ($Open) { Start-Process $meta.url | Out-Null }
            exit 0
        }
    }
}

$outFile = Join-Path $LogsDir 'log_viewer.out'
$errFile = Join-Path $LogsDir 'log_viewer.err'
$args = @('-m', 'blender_mcp.log_viewer', '--root', $Root, '--port', "$Port", '--quiet')
if ($Seconds -gt 0) { $args += @('--seconds', "$Seconds") }

$proc = Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $MCPDir `
    -WindowStyle Hidden -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru

$url = $null
$deadline = (Get-Date).AddSeconds(20)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
    if (Test-Path -LiteralPath $outFile) {
        $line = Select-String -LiteralPath $outFile -Pattern '^LOG_VIEWER_URL\s+(\S+)' -ErrorAction SilentlyContinue |
            Select-Object -Last 1
        if ($line) { $url = $line.Matches[0].Groups[1].Value; break }
    }
    if ($proc.HasExited) { break }
}

if (-not $url) {
    $detail = if (Test-Path -LiteralPath $errFile) { (Get-Content -LiteralPath $errFile -Raw) } else { '' }
    Write-Error ("the log viewer did not report a URL. stderr: {0}" -f $detail.Trim())
    exit 1
}

Write-Host ("LOG_VIEWER_URL {0}" -f $url)
Write-Host ("[log-viewer] pid {0} | root {1} | 只读本地看板，仅回环监听" -f $proc.Id, $Root)
if ($Open) { Start-Process $url | Out-Null }
exit 0
