# Start every xau-agent process detached, the way they run on the dev PC.
#   powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1
# Skips a process whose pid file points at a live process. Logs: logs\<name>-<time>.out.log / .err.log
# M5 agent uses MODE from .env (advisory = analyse only). Swing bots run on the DEMO account (MODE=demo);
# they refuse to start on a real account unless ALLOW_REAL_ACCOUNT=true is set.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path '.env')) { throw '.env not found: copy your .env to this folder first (never put it in git)' }
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw '.venv not found: run  python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt' }
New-Item -ItemType Directory -Force logs | Out-Null

function Test-Alive($pidFile) {
    if (-not (Test-Path $pidFile)) { return $false }
    $id = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    return [bool]($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue))
}

function Start-Bot($name, $pidName, $argList, $envs) {
    $pidFile = "logs\$pidName.pid"
    if (Test-Alive $pidFile) { Write-Host "$name already running (pid $(Get-Content $pidFile))"; return }
    foreach ($k in $envs.Keys) { Set-Item "Env:$k" $envs[$k] }
    $ts = Get-Date -Format yyyyMMdd-HHmm
    $p = Start-Process -FilePath $py -ArgumentList $argList -WorkingDirectory $root -WindowStyle Hidden `
        -RedirectStandardOutput "logs\$name-$ts.out.log" -RedirectStandardError "logs\$name-$ts.err.log" -PassThru
    foreach ($k in $envs.Keys) { Remove-Item "Env:$k" -ErrorAction SilentlyContinue }
    if ($pidName -ne 'awake') { $p.Id | Set-Content $pidFile -Encoding ascii }  # awake.py writes its own pid
    Write-Host "started $name (pid $($p.Id))"
}

Start-Bot 'awake'     'awake'        @('-u', '-m', 'xau_agent.awake') @{}
Start-Bot 'run'       'agent'        @('-u', '-m', 'xau_agent.main') @{}
Start-Bot 'swing'     'swing'        @('-u', '-m', 'xau_agent.swing_bot') @{ MODE = 'demo' }
Start-Bot 'swingbtc'  'swing_btcusd' @('-u', '-m', 'xau_agent.swing_bot') @{ MODE = 'demo'; SYMBOL = 'BTCUSD'; SWING_MAGIC = '260514'; SWING_MAX_SPREAD_POINTS = '10000' }
Start-Bot 'dashboard' 'dashboard'    @('-u', '-m', 'xau_agent.dashboard', '--watch') @{}
if ((Get-Command tailscale -ErrorAction SilentlyContinue) -or (Test-Path 'C:\Program Files\Tailscale\tailscale.exe')) {
    Start-Bot 'webdash' 'webdash' @('-u', '-m', 'xau_agent.webdash') @{}   # dashboard over Tailscale only
}
Start-Sleep 20
Get-ChildItem logs\*.err.log | Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-1) -and $_.Length -gt 0 } |
    ForEach-Object { Write-Warning "$($_.Name):"; Get-Content $_.FullName -Tail 5 }
Write-Host "dashboard: $root\dashboard.html"
