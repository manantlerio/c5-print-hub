# C5 Print Hub one-time setup for Windows. Run it from this folder:
#   powershell -ExecutionPolicy Bypass -File setup.ps1                  (phones on your home Wi-Fi)
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Tailscale       (also your own Tailscale devices, from anywhere)
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -HttpsPort 8443  (also open the HTTPS port in the firewall)
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Uninstall       (remove the start-at-login task and firewall rules)
# What it does: finds Python 3.10 or newer, starts the hub at every login (and now), restarting it if it crashes, adds a
# Windows Firewall rule so only your home network (Private profile, local subnet) reaches the hub's port, then opens
# the one-time setup link in your browser. Nothing is installed outside this folder except the task and the rules.
param([switch]$Tailscale, [switch]$Uninstall, [int]$Port = 0, [int]$HttpsPort = 0)
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$task = "C5 Print Hub"
$rule = "C5 Print Hub"

function Invoke-Admin([string]$cmd) {
    # Firewall rules need an administrator: run just that one command elevated (Windows asks once).
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) { Invoke-Expression $cmd; return $true }
    try {
        $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList "-NoProfile -ExecutionPolicy Bypass -Command $cmd"
        return ($p.ExitCode -eq 0)
    } catch { return $false }
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Invoke-Admin "Remove-NetFirewallRule -DisplayName '$rule' -ErrorAction SilentlyContinue; Remove-NetFirewallRule -DisplayName '$rule HTTPS' -ErrorAction SilentlyContinue" | Out-Null
    Write-Host "Removed the '$task' task and its firewall rules. Your config.json and jobs folder are still here."
    exit 0
}

function Find-Python {
    # The py launcher first, then python on PATH. The Microsoft Store stub in WindowsApps is skipped: it only opens the Store.
    $found = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $p = & py -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $found += $p.Trim() }
    }
    foreach ($c in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        if ($c.Source -notlike "*\WindowsApps\*") { $found += $c.Source }
    }
    foreach ($p in $found) {
        $ok = & $p -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null
        if ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq "1") { return $p }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host "Python 3.10 or newer was not found."
    Write-Host "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this again."
    exit 1
}
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "Python: $python"

$cfg = Join-Path $dir "config.json"
if ($Tailscale) {
    # The hub's home port then also answers Tailscale addresses (100.64.0.0/10), so your own tailnet devices reach it
    # directly at this PC's Tailscale address. Written to a temp file and moved, so config.json is never half written.
    & $python -I -c "import json,os,sys; p=sys.argv[1]; c=json.load(open(p)) if os.path.exists(p) else {}; c['tailscale']=True; json.dump(c,open(p+'.tmp','w'),indent=1); os.replace(p+'.tmp',p)" $cfg
}
if ($Port -eq 0) {
    $Port = 8765
    if (Test-Path $cfg) {
        $p = & $python -I -c "import json,sys; print(json.load(open(sys.argv[1])).get('port', 8765))" $cfg
        if ($LASTEXITCODE -eq 0) { $Port = [int]$p }
    }
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-I `"$dir\hub.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $task -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description "C5 Print Hub, phone page for the Creator 5 Pro ($dir)" | Out-Null

# Firewall: the hub's port for Python only, on the Private profile only, from the local subnet only (plus Tailscale's
# range with -Tailscale). On cafe or hotel Wi-Fi (Public profile) nothing gets in. The hub itself also refuses any
# address that is not a home-network one on this port.
$remote = if ($Tailscale) { "LocalSubnet,100.64.0.0/10" } else { "LocalSubnet" }
$fw = "Remove-NetFirewallRule -DisplayName '$rule' -ErrorAction SilentlyContinue; " +
      "New-NetFirewallRule -DisplayName '$rule' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port " +
      "-Program '$pythonw' -Profile Private -RemoteAddress $remote | Out-Null"
if ($HttpsPort -gt 0) {
    # The HTTPS port takes the internet (your router forwards it here); the hub asks a passphrase and a code on it.
    $fw += "; Remove-NetFirewallRule -DisplayName '$rule HTTPS' -ErrorAction SilentlyContinue; " +
           "New-NetFirewallRule -DisplayName '$rule HTTPS' -Direction Inbound -Action Allow -Protocol TCP " +
           "-LocalPort $HttpsPort -Program '$pythonw' -Profile Private | Out-Null"
}
if (Invoke-Admin $fw) {
    Write-Host "Firewall: port $Port open to your home network only (Private profile, $remote)."
} else {
    Write-Host "Firewall rule not added (no administrator). When Windows asks whether Python may use networks, allow Private networks only."
}

Start-ScheduledTask -TaskName $task
Write-Host "Task '$task': $((Get-ScheduledTask -TaskName $task).State)"

# The hub writes a one-time setup link (setup-url.txt) at its first start; it stops working once setup is done.
$link = Join-Path $dir "setup-url.txt"
for ($i = 0; $i -lt 20 -and -not (Test-Path $link); $i++) { Start-Sleep -Milliseconds 500 }
Write-Host ""
if (Test-Path $link) {
    $url = (Get-Content $link -TotalCount 1).Trim()
    Write-Host "Opening the one-time setup link (also in setup-url.txt): $url"
    Start-Process $url
} else {
    Write-Host "Setup is already done (or the hub has not started yet). Opening http://127.0.0.1:$Port/"
    Start-Process "http://127.0.0.1:$Port/"
}
