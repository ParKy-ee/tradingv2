# One-time setup of the runtime PC for remote work over Tailscale. Run in an ELEVATED PowerShell on the runtime PC:
#   powershell -ExecutionPolicy Bypass -File scripts\runtime_setup.ps1     # authorizes every keys\*.pub in the repo
#   (or -PublicKeyFile C:\path\id_ed25519.pub for a key that is not in the repo)
# What it does (nothing is opened to the internet):
#   1. installs and starts Windows OpenSSH Server (Tailscale SSH does not run on Windows)
#   2. firewall: SSH (22) and the dashboard web port (8765) accepted ONLY from Tailscale addresses 100.64.0.0/10;
#      the default "allow SSH from anywhere" rule is disabled
#   3. installs your public key and turns password login OFF (key only) — skipped if no key is found
#   4. makes PowerShell the SSH shell
#   5. scheduled tasks: "xau-start" (at logon, in your desktop session, so MT5 works) and "xau-stop"; from SSH run
#      `schtasks /run /tn xau-start` or `schtasks /run /tn xau-stop`
param([string]$PublicKeyFile, [int]$DashPort = 8765)
$ErrorActionPreference = 'Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this in an elevated (Administrator) PowerShell' }
$root = Split-Path -Parent $PSScriptRoot

# 1. OpenSSH Server
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*'
if ($cap.State -ne 'Installed') { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
Set-Service sshd -StartupType Automatic
Start-Service sshd

# 2. Firewall: Tailscale range only
Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule
foreach ($r in @(@{n = 'xau-ssh-tailscale'; p = 22}, @{n = 'xau-dashboard-tailscale'; p = $DashPort})) {
    Get-NetFirewallRule -Name $r.n -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -Name $r.n -DisplayName $r.n -Direction Inbound -Protocol TCP -LocalPort $r.p `
        -RemoteAddress '100.64.0.0/10' -Action Allow | Out-Null
}

# 3. Key-only login (keys: -PublicKeyFile, or every keys\*.pub in the repo)
$cfg = "$env:ProgramData\ssh\sshd_config"
$keyFiles = if ($PublicKeyFile) { @($PublicKeyFile) } else { @(Get-ChildItem "$root\keys\*.pub" -ErrorAction SilentlyContinue | ForEach-Object FullName) }
if ($keyFiles.Count) {
    $isAdmin = (Get-LocalGroupMember -SID 'S-1-5-32-544' | Where-Object { $_.Name -like "*\$env:USERNAME" })
    foreach ($kf in $keyFiles) {
        $key = (Get-Content $kf -Raw).Trim()
        if ($key -notmatch '^ssh-(ed25519|rsa) ') { throw "$kf does not look like a public key (.pub)" }
        if ($isAdmin) {
            $ak = "$env:ProgramData\ssh\administrators_authorized_keys"   # Windows OpenSSH uses this for admins
            if (-not (Test-Path $ak) -or -not (Select-String -Path $ak -SimpleMatch $key -Quiet)) { Add-Content $ak $key }
            icacls $ak /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
        } else {
            $dir = "$env:USERPROFILE\.ssh"; New-Item -ItemType Directory -Force $dir | Out-Null
            $ak = "$dir\authorized_keys"
            if (-not (Test-Path $ak) -or -not (Select-String -Path $ak -SimpleMatch $key -Quiet)) { Add-Content $ak $key }
        }
        Write-Host "authorized key: $kf"
    }
    $text = Get-Content $cfg -Raw
    $text = $text -replace '(?m)^#?\s*PasswordAuthentication\s+\w+', 'PasswordAuthentication no'
    if ($text -notmatch '(?m)^PasswordAuthentication no') { $text = "PasswordAuthentication no`r`n" + $text }
    Set-Content $cfg $text -Encoding ascii
    Write-Host 'key installed; password login disabled'
} else {
    Write-Warning 'No public key found (keys\*.pub or -PublicKeyFile): password login is still ON.'
}

# 4. PowerShell as the SSH shell
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -PropertyType String -Force `
    -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null
Restart-Service sshd

# 5. Scheduled tasks in the interactive desktop session
$user = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0
foreach ($t in @(@{n = 'xau-start'; f = 'start_all.ps1'; trig = (New-ScheduledTaskTrigger -AtLogOn -User $user)},
                 @{n = 'xau-stop'; f = 'stop_all.ps1'; trig = $null})) {
    $act = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $root `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\scripts\$($t.f)`""
    $args = @{TaskName = $t.n; Action = $act; Principal = $principal; Settings = $settings; Force = $true}
    if ($t.trig) { $args.Trigger = $t.trig }
    Register-ScheduledTask @args | Out-Null
}
Write-Host "done. SSH: ssh $env:USERNAME@<tailscale-ip>  |  dashboard: http://<tailscale-ip>:$DashPort/"
