<#
.SYNOPSIS
    Start/reuse Blender through the named MCP launcher with duplicate-process guards.
.DESCRIPTION
    GUI by default. -Background uses the bootstrap pump. Never closes Blender.
    Factory startup is opt-in. Token content is handled solely by the MCP client.
    A busy/unauthorized bridge or an existing Blender without a bridge blocks startup.
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
    [int]$WaitSeconds = 30,
    [string[]]$ExtraArgs = @()
)
$ErrorActionPreference = 'Stop'
if ($Force) { throw '-Force is no longer supported: reuse a healthy bridge or enable bootstrap in the existing Blender.' }
if ($ExtraArgs.Count -gt 0) { throw 'Use -PumpSeconds for a bounded headless run. Bridge configuration overrides via ExtraArgs are rejected.' }
$taskMcpDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskInterpreter = Join-Path $taskMcpDirectory '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskInterpreter)) { throw "MCP interpreter missing: $taskInterpreter" }
$taskLauncherArguments = @('-m', 'blender_mcp.launch_cli', '--host', $BridgeHost, '--port', "$Port", '--wait-seconds', "$WaitSeconds", '--pump-seconds', "$PumpSeconds")
if ($Background) { $taskLauncherArguments += '--background' }
if ($FactoryStartup) { $taskLauncherArguments += '--factory-startup' }
if ($IsolateConfig) { $taskLauncherArguments += '--isolate-config' }
if ($BlenderPath) { $taskLauncherArguments += @('--blender-path', $BlenderPath) }
Push-Location -LiteralPath $taskMcpDirectory
try {
    & $taskInterpreter @taskLauncherArguments
    $taskLauncherExitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $taskLauncherExitCode
