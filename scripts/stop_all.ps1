# Stop every xau-agent process started by start_all.ps1 (and their venv child processes).
#   powershell -ExecutionPolicy Bypass -File scripts\stop_all.ps1
# Open positions keep their broker-side stop loss; nothing is closed by stopping the bots.
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
foreach ($name in 'agent', 'swing', 'swing_btcusd', 'dashboard', 'webdash', 'awake') {
    $f = "logs\$name.pid"
    if (-not (Test-Path $f)) { continue }
    $id = Get-Content $f | Select-Object -First 1
    if (-not $id) { continue }
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $parent = (Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue).ParentProcessId
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    # awake.pid holds the inner python; stop its venv launcher too
    if ($parent) { $pp = Get-CimInstance Win32_Process -Filter "ProcessId=$parent" -ErrorAction SilentlyContinue
        if ($pp -and $pp.CommandLine -match 'xau_agent') { Stop-Process -Id $parent -Force -ErrorAction SilentlyContinue } }
    Write-Host "stopped $name ($id)"
}
