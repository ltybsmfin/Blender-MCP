<#
.SYNOPSIS
    Launch Blender with the Blender MCP bridge enabled (Windows helper).

.DESCRIPTION
    Starts the bundled Blender with MCP/blender/bootstrap.py, which opens the
    token-authenticated local JSON bridge that MCP/server.py (the MCP stdio
    server) talks to.

    Behaviour that matters:
      * GUI by default (the user's normal Blender). -Background runs headless,
        which also enables the manual socket pump, because bpy.app.timers never
        fires in --background.
      * -FactoryStartup is OPT-IN and only meant for controlled test runs; a
        normal user launch keeps the user's preferences.
      * A healthy bridge already answering on the port is REUSED: nothing is
        launched. Pass -Force to start another Blender anyway.
      * This helper never terminates any Blender process - not even the one it
        starts. Stop the bridge from inside Blender with
        bpy.ops.blendermcp.stop_bridge(), or close Blender.
      * -IsolateConfig points BLENDER_USER_CONFIG at MCP/.runtime/... so a test
        run cannot touch the user profile.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File MCP\launch_blender.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File MCP\launch_blender.ps1 -Background -FactoryStartup -IsolateConfig -PumpSeconds 600
#>
[CmdletBinding()]
param(
    [switch]$Background,
    [switch]$FactoryStartup,
    [switch]$Force,
    [switch]$IsolateConfig,
    [string]$BlenderPath = $env:BLENDER_MCP_BLENDER,
    [int]$Port = $(if ($env:BLENDER_MCP_PORT) { [int]$env:BLENDER_MCP_PORT } else { 9876 }),
    [string]$BridgeHost = $(if ($env:BLENDER_MCP_HOST) { $env:BLENDER_MCP_HOST } else { '127.0.0.1' }),
    [double]$PumpSeconds = 0,
    [int]$WaitSeconds = 60,
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'
if ($BridgeHost -eq 'localhost') { $BridgeHost = '127.0.0.1' }
$taskBridgeAddress = $null
if (-not [System.Net.IPAddress]::TryParse($BridgeHost, [ref]$taskBridgeAddress) -or
    $taskBridgeAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
    -not [System.Net.IPAddress]::IsLoopback($taskBridgeAddress)) {
    throw 'BridgeHost must be an IPv4 loopback address.'
}
$MCPDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Workspace = Split-Path -Parent $MCPDir
$Bootstrap = Join-Path $MCPDir 'blender\bootstrap.py'
$TokenFile = Join-Path $MCPDir '.runtime\token'
$LogDir = Join-Path $MCPDir '.runtime\logs'

function Resolve-BlenderExecutable {
    param([string]$Explicit)
    if ($Explicit) {
        if (Test-Path -LiteralPath $Explicit) { return (Resolve-Path -LiteralPath $Explicit).Path }
        throw "Blender executable not found at '$Explicit'"
    }
    $versionDir = Join-Path $Workspace 'Version'
    if (Test-Path -LiteralPath $versionDir) {
        $candidates = @(
            Get-ChildItem -LiteralPath $versionDir -Directory |
                Sort-Object -Property Name -Descending |
                ForEach-Object { Join-Path $_.FullName 'blender.exe' } |
                Where-Object { Test-Path -LiteralPath $_ }
        )
        if ($candidates.Count -gt 0) { return (Resolve-Path -LiteralPath $candidates[0]).Path }
    }
    $onPath = Get-Command blender.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    throw 'No Blender executable found. Pass -BlenderPath or set BLENDER_MCP_BLENDER.'
}

function Test-BridgeHealth {
    param([string]$TokenPath)
    if (-not (Test-Path -LiteralPath $TokenPath)) { return $false }
    $token = (Get-Content -LiteralPath $TokenPath -Raw).Trim()
    if (-not $token) { return $false }
    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $client.Connect($BridgeHost, $Port)
        $stream = $client.GetStream()
        $payload = (@{ id = 'launch-helper'; token = $token; command = 'health'; params = @{} } | ConvertTo-Json -Compress) + "`n"
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush()
        $stream.ReadTimeout = 3000
        $reader = New-Object System.IO.StreamReader($stream)
        $line = $reader.ReadLine()
        if (-not $line) { return $false }
        $response = $line | ConvertFrom-Json
        return [bool]$response.ok
    }
    catch {
        return $false
    }
    finally {
        if ($client) { $client.Close() }
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $MCPDir '.runtime'), $LogDir | Out-Null

if (-not (Test-Path -LiteralPath $Bootstrap)) { throw "bootstrap script missing: $Bootstrap" }
$blender = Resolve-BlenderExecutable -Explicit $BlenderPath
Write-Host "[launch] Blender  : $blender"

if (-not $Force -and (Test-BridgeHealth -TokenPath $TokenFile)) {
    Write-Host "[launch] a healthy bridge already answers on ${BridgeHost}:${Port} - reusing it, nothing started."
    Write-Host "[launch] pass -Force to start another Blender anyway"
    exit 0
}

$blenderArgs = @()
if ($Background) { $blenderArgs += '--background' }
if ($FactoryStartup) { $blenderArgs += '--factory-startup' }
$blenderArgs += @('--python', $Bootstrap, '--', '--mcp-host', $BridgeHost, '--mcp-port', "$Port", '--mcp-token-file', $TokenFile)
if ($Background) {
    $blenderArgs += '--mcp-pump'
    if ($PumpSeconds -gt 0) { $blenderArgs += @('--mcp-pump-seconds', "$PumpSeconds") }
}
if ($ExtraArgs.Count -gt 0) { $blenderArgs += $ExtraArgs }

$env:BLENDER_MCP_PROJECT_DIR = Join-Path $Workspace 'Project'
if ($IsolateConfig) {
    $env:BLENDER_USER_CONFIG = Join-Path $MCPDir '.runtime\blender_config'
    New-Item -ItemType Directory -Force -Path $env:BLENDER_USER_CONFIG | Out-Null
    Write-Host "[launch] isolated config: $env:BLENDER_USER_CONFIG (user profile untouched)"
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$logFile = Join-Path $LogDir "launch_$stamp.log"

if ($Background) {
    # Headless: child output goes to a log file, then wait (bounded) for the bridge.
    $process = Start-Process -FilePath $blender -ArgumentList $blenderArgs -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput $logFile -RedirectStandardError "$logFile.err"
    Write-Host "[launch] started headless Blender pid=$($process.Id), log: $logFile"
    $deadline = (Get-Date).AddSeconds($WaitSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-BridgeHealth -TokenPath $TokenFile) {
            Write-Host "[launch] bridge is ready on ${BridgeHost}:${Port} (pid $($process.Id))"
            Write-Host "[launch] this helper will not kill it: send the 'shutdown' bridge command or stop pid $($process.Id) yourself"
            exit 0
        }
        if ($process.HasExited) {
            Write-Host "[launch] Blender exited early with code $($process.ExitCode); see $logFile"
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "[launch] WARNING: the bridge did not answer within ${WaitSeconds}s; see $logFile"
    exit 1
}

# GUI launch: detached, with output redirected to a log file so nothing can
# interfere with a caller that owns this console (an MCP stdio server must never
# share stdout with a child process).
Start-Process -FilePath $blender -ArgumentList $blenderArgs -RedirectStandardOutput $logFile `
    -RedirectStandardError "$logFile.err" | Out-Null
Write-Host "[launch] Blender GUI started (log: $logFile)"
Write-Host "[launch] inside Blender: Properties > Scene > Blender MCP Bridge, or bpy.ops.blendermcp.start_bridge()"
Write-Host "[launch] token file: $TokenFile (contents are never printed)"
exit 0
