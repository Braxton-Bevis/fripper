# FRipper Web setup (run on the PC that will do the downloads, as an administrator).
# Starts web.py at logon, allows it only from your tailnet and home network, and
# prints the addresses to open on an iPad or phone. Safe to run again.
#
#   web-setup.ps1 [-Port 8787] [-Pin 123456] [-TailnetOnly] [-Remove]
param([int]$Port = 8787, [string]$Pin = '', [switch]$TailnetOnly, [switch]$Remove)

$ErrorActionPreference = 'Stop'
$Root = $PSScriptRoot
$TaskName = 'FRipper Web'
$RuleName = 'FRipper Web'
$pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
$python = Join-Path $Root '.venv\Scripts\python.exe'

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { throw 'Run this from an administrator PowerShell (it adds a firewall rule).' }

Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}
# Stop a web server left running from this folder.
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like "*$Root*web.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
if ($Remove) { Write-Host 'FRipper Web removed (task and firewall rule). Music and settings were kept.'; exit 0 }

if (-not (Test-Path -LiteralPath $pythonw)) { throw "FRipper is not installed in $Root. Run setup.ps1 first." }
if ($Pin) { & $python (Join-Path $Root 'web.py') --set-pin $Pin; if ($LASTEXITCODE -ne 0) { throw 'Could not set the PIN.' } }

# Tailscale addresses (100.64.0.0/10) always; the local network unless -TailnetOnly.
$remote = @('100.64.0.0/10')
if (-not $TailnetOnly) { $remote += 'LocalSubnet' }
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -RemoteAddress $remote -Profile Any -Description 'FRipper web queue (PIN protected).' | Out-Null

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$(Join-Path $Root 'web.py')`" --port $Port" -WorkingDirectory $Root
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name   # PC\user; USERDOMAIN can read WORKGROUP over SSH
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
# Interactive logon keeps the user's profile, so Windows can unlock the saved TIDAL session.
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'FRipper web queue for phones and tablets.' | Out-Null
Start-ScheduledTask -TaskName $TaskName

$deadline = (Get-Date).AddSeconds(20)
do {
    Start-Sleep -Milliseconds 500
    $up = Test-NetConnection -ComputerName 127.0.0.1 -Port $Port -InformationLevel Quiet -WarningAction SilentlyContinue
} until ($up -or (Get-Date) -gt $deadline)
if (-not $up) { throw "FRipper Web did not start. See $(Join-Path $Root '.local\web.log')." }

Write-Host "`nFRipper Web is running." -ForegroundColor Green
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
if (Test-Path $tailscale) {
    $ip = (& $tailscale ip -4 2>$null | Select-Object -First 1)
    $name = (& $tailscale status --json 2>$null | ConvertFrom-Json).Self.DNSName.TrimEnd('.')
    if ($ip) { Write-Host "  Tailnet:  http://$($ip):$Port" }
    if ($name) { Write-Host "            http://$($name):$Port" }
}
if (-not $TailnetOnly) {
    Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp, Manual -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike '169.254*' -and $_.InterfaceAlias -notlike '*Tailscale*' } |
        ForEach-Object { Write-Host "  Wi-Fi:    http://$($_.IPAddress):$Port" }
}
$pinFile = Join-Path $Root '.local\web-pin.txt'
if (Test-Path $pinFile) { Write-Host "  PIN:      $((Get-Content $pinFile -First 1) -replace '^FRipper web PIN: ', '')" }
Write-Host "`nOn the iPad, open the address in Safari, sign in, then Share > Add to Home Screen."
