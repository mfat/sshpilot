# Runs inside Windows Sandbox at logon (started by sandbox-test.ps1): installs
# the built setup.exe on a clean Windows and user profile, launches the app,
# and writes a report to C:\sshpilot\results (the host's
# build\windows\sandbox-results). The app is left open to try by hand.

$ErrorActionPreference = "Continue"
$build = "C:\sshpilot\build"
$results = "C:\sshpilot\results"
$inst = "$env:LOCALAPPDATA\Programs\sshPilot"
Start-Transcript -Path "$results\report.txt" -Force | Out-Null

function Check($label, $ok, $detail = "") {
    $mark = if ($ok) { "PASS" } else { "FAIL" }
    "[$mark] $label $detail"
}

"== Environment"
$os = Get-CimInstance Win32_OperatingSystem
"Windows: $($os.Caption) $($os.Version) (build $([Environment]::OSVersion.Version.Build))"
"GPU: $((Get-CimInstance Win32_VideoController).Name -join ', ')"
Check "OpenSSH client present" (Test-Path "$env:SystemRoot\System32\OpenSSH\ssh.exe")
Check "fresh profile (no sshPilot settings)" (-not (Test-Path "$env:APPDATA\sshpilot") -and -not (Test-Path "$env:LOCALAPPDATA\sshpilot"))

"`n== Install"
$setup = Get-ChildItem $build -Filter "sshPilot-*-setup.exe" | Sort-Object LastWriteTime | Select-Object -Last 1
"Installer: $($setup.Name) ($([math]::Round($setup.Length / 1MB, 1)) MiB, built $($setup.LastWriteTime))"
$p = Start-Process $setup.FullName -ArgumentList "/S" -PassThru -Wait
Check "silent install exit code 0" ($p.ExitCode -eq 0) "(got $($p.ExitCode))"
Check "sshPilot.exe installed" (Test-Path "$inst\sshPilot.exe")
Check "Start-menu shortcut" (Test-Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\sshPilot.lnk")
Check "Settings > Apps entry" (Test-Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\sshPilot")

"`n== First launch (--verbose)"
$g = Start-Process "$inst\sshPilot.exe" -ArgumentList "--verbose" -PassThru
$deadline = (Get-Date).AddSeconds(60)
do { Start-Sleep 2; $title = (Get-Process -Id $g.Id -ErrorAction SilentlyContinue).MainWindowTitle } until ($title -or $g.HasExited -or (Get-Date) -gt $deadline)
Check "GUI running with a window" ((-not $g.HasExited) -and $title) "(title: '$title')"
if ($g.HasExited) { "GUI exit code: $($g.ExitCode)" }
Start-Sleep 5
$status = & "$inst\sshpilot-cli.exe" --daemon status 2>&1 | Out-String
Check "daemon ready" ($status -match '"state":\s*"ready"')
$outside = Get-Process -Id $g.Id -ErrorAction SilentlyContinue | ForEach-Object { $_.Modules } |
    Where-Object { $_.FileName -notlike "$inst\*" -and $_.FileName -notlike "$env:SystemRoot\*" -and $_.FileName -notlike "$env:ProgramData\Microsoft\Windows Defender\*" }
Check "no DLLs loaded from outside the app and Windows" (-not $outside) ($outside.FileName -join ", ")

"`n== Errors in logs"
$logs = Get-ChildItem "$env:LOCALAPPDATA\sshpilot\state\*.log" -ErrorAction SilentlyContinue
$bad = $logs | Select-String -Pattern "CRITICAL|ERROR|Traceback" | ForEach-Object { "  $($_.Filename): $($_.Line)" }
Check "no ERROR/CRITICAL/Traceback lines" (-not $bad)
$bad | Select-Object -First 40
New-Item -ItemType Directory -Force "$results\logs" | Out-Null
$logs | Copy-Item -Destination "$results\logs" -ErrorAction SilentlyContinue

"`nsshPilot is left open: try a local terminal tab and an SSH connection, then"
"uninstall from Settings > Apps. Logs: build\windows\sandbox-results\logs."
Stop-Transcript | Out-Null
