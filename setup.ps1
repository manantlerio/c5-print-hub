# C5 Print Hub one-time setup for Windows. Run it from this folder:
#   powershell -ExecutionPolicy Bypass -File setup.ps1              (LAN only: phones on your Wi-Fi)
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Tailscale   (only your own Tailscale devices)
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -Uninstall   (remove the start-at-login task)
# What it does: finds Python 3.10 or newer, starts the hub at every login (and now), restarting it if it crashes, then
# opens the setup page in your browser. Nothing is installed outside this folder except the scheduled task.
param([switch]$Tailscale, [switch]$Uninstall, [int]$Port = 0)
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$task = "C5 Print Hub"

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed the '$task' task. Your config.json and jobs folder are still here."
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
    # The hub then listens on this PC only; tailscale serve passes your own devices through to it.
    & $python -I -c "import json,os,sys; p=sys.argv[1]; c=json.load(open(p)) if os.path.exists(p) else {}; c['host']='127.0.0.1'; json.dump(c,open(p,'w'),indent=1)" $cfg
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
Start-ScheduledTask -TaskName $task
Start-Sleep -Seconds 3
Write-Host "Task '$task': $((Get-ScheduledTask -TaskName $task).State)"

if ($Tailscale) {
    $ts = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
    if (-not (Test-Path $ts)) {
        $t = Get-Command tailscale -ErrorAction SilentlyContinue
        $ts = if ($t) { $t.Source } else { $null }
    }
    if ($ts) {
        # serve = your own tailnet devices only. Never use 'tailscale funnel': that puts the hub on the open internet.
        & $ts serve --bg "http://127.0.0.1:$Port"
        & $ts serve status
    } else {
        Write-Host "Tailscale not found. Install it from https://tailscale.com/download, sign in, then run this again with -Tailscale."
    }
}

Write-Host ""
Write-Host "Opening the setup page: http://127.0.0.1:$Port/setup"
Write-Host "If Windows asks whether Python may use networks, allow Private networks only."
Start-Process "http://127.0.0.1:$Port/setup"
