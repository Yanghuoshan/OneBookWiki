# OneBookWiki server startup script (Windows PowerShell)
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# ---- Load .env if present ----
if (Test-Path .env) {
    Write-Host "[onebookwiki] Loading environment from .env"
    Get-Content .env | ForEach-Object {
        $line = $_.Trim()
        if ($line -and ($line -notmatch '^\s*#')) {
            $parts = $line -split '=', 2
            if ($parts.Count -eq 2) {
                $name = $parts[0].Trim()
                $value = $parts[1].Trim()
                [Environment]::SetEnvironmentVariable($name, $value, "Process")
            }
        }
    }
}

# ---- Detect Python ----
$pythonCmd = if ($env:ONEBOOKWIKI_PYTHON) { $env:ONEBOOKWIKI_PYTHON } else { "python" }
try {
    $pyVersion = & $pythonCmd --version 2>&1
    Write-Host "Python: $pyVersion"
} catch {
    Write-Host "Error: Python not found. Install Python 3.10+ and try again." -ForegroundColor Red
    exit 1
}

# ---- Start server ----
$Port = if ($env:ONEBOOKWIKI_PORT) { $env:ONEBOOKWIKI_PORT } else { "8000" }
$HostAddr = if ($env:ONEBOOKWIKI_HOST) { $env:ONEBOOKWIKI_HOST } else { "0.0.0.0" }

Write-Host "Starting OneBookWiki server on http://${HostAddr}:${Port} ..."
& $pythonCmd -m uvicorn server.main:app --host $HostAddr --port $Port
