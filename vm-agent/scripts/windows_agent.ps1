# PowerShell helper to run VM agent without hardcoded install paths.

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentDir = if ($env:VLM_AGENT_DIR) { $env:VLM_AGENT_DIR } else { Resolve-Path (Join-Path $scriptDir "..") }
$venvDir = if ($env:VLM_AGENT_VENV) { $env:VLM_AGENT_VENV } else { Join-Path $agentDir ".venv" }
$hostBind = if ($env:VLM_AGENT_HOST) { $env:VLM_AGENT_HOST } else { "0.0.0.0" }
$port = if ($env:VLM_AGENT_PORT) { $env:VLM_AGENT_PORT } else { "8081" }

if (-not (Test-Path $venvDir)) {
    py -m venv $venvDir
}

$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$pipExe = Join-Path $venvDir "Scripts\pip.exe"

& $pythonExe -m pip install --upgrade pip wheel
& $pipExe install -r (Join-Path $agentDir "requirements.txt")

Set-Location $agentDir
& (Join-Path $venvDir "Scripts\uvicorn.exe") src.agent:app --host $hostBind --port $port