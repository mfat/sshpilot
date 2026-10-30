# Temporary launch capture for GitHub issue #1357 (remove with the repro workflow).
# Starts a frozen sshPilot binary, waits a bounded time, and writes stdout/stderr,
# exit code, app log dirs, Event Log errors, and a small bundle inventory.

param(
    [Parameter(Mandatory = $true)][string]$AppDir,
    [Parameter(Mandatory = $true)][string]$OutDir,
    [int]$TimeoutSeconds = 25,
    [string]$Label = "launch"
)

$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

function Write-Text($Name, $Text) {
    Set-Content -Path (Join-Path $OutDir $Name) -Value $Text -Encoding utf8
}

$started = Get-Date
$os = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$osInfo = [ordered]@{
    label            = $Label
    startedUtc       = $started.ToUniversalTime().ToString("o")
    computerName     = $env:COMPUTERNAME
    osProductName    = $os.ProductName
    osDisplayVersion = $os.DisplayVersion
    osCurrentBuild   = $os.CurrentBuild
    osUbr            = $os.UBR
    osBuildLab       = $os.BuildLabEx
    appDir           = $AppDir
}
$osInfo | ConvertTo-Json | Set-Content (Join-Path $OutDir "os.json") -Encoding utf8
Write-Text "os.txt" @(
    "ProductName=$($os.ProductName)"
    "DisplayVersion=$($os.DisplayVersion)"
    "CurrentBuild=$($os.CurrentBuild).$($os.UBR)"
    "BuildLabEx=$($os.BuildLabEx)"
    "AppDir=$AppDir"
)

$inventory = Join-Path $OutDir "bundle-inventory.txt"
$patterns = @(
    "sshPilot.exe", "sshpilot-cli.exe",
    "girepository*.dll", "gio-*.dll", "glib-*.dll", "gobject-*.dll",
    "gtk-4*.dll", "adwaita*.dll", "libgirepository*.dll"
)
$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("AppDir=$AppDir exists=$(Test-Path $AppDir)")
foreach ($name in @("sshPilot.exe", "sshpilot-cli.exe")) {
    $path = Join-Path $AppDir $name
    $lines.Add("$name exists=$(Test-Path $path)")
}
$internal = Join-Path $AppDir "_internal"
$lines.Add("_internal exists=$(Test-Path $internal)")
if (Test-Path $internal) {
    foreach ($rel in @(
            "gi_typelibs",
            "lib\girepository-1.0",
            "lib\girepository-2.0",
            "girepository-1.0"
        )) {
        $dir = Join-Path $internal $rel
        $count = 0
        if (Test-Path $dir) {
            $count = @(Get-ChildItem $dir -Filter "*.typelib" -ErrorAction SilentlyContinue).Count
        }
        $lines.Add("typelib dir ${rel}: exists=$(Test-Path $dir) typelibs=$count")
    }
    Get-ChildItem $internal -Recurse -Include $patterns -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName |
        Sort-Object |
        ForEach-Object { $lines.Add($_) }
}
$lines | Set-Content $inventory -Encoding utf8

function Copy-TreeIfPresent($Source, $DestName) {
    if (Test-Path $Source) {
        $dest = Join-Path $OutDir $DestName
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        Copy-Item -Path $Source -Destination $dest -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$cli = Join-Path $AppDir "sshpilot-cli.exe"
$gui = Join-Path $AppDir "sshPilot.exe"
$exe = $cli
if (-not (Test-Path $exe)) { $exe = $gui }
if (-not (Test-Path $exe)) {
    Write-Text "summary.txt" "FAIL: neither sshpilot-cli.exe nor sshPilot.exe in $AppDir"
    Write-Text "exit-code.txt" "missing-exe"
    exit 2
}

$stdout = Join-Path $OutDir "stdout.txt"
$stderr = Join-Path $OutDir "stderr.txt"
New-Item -ItemType File -Force -Path $stdout | Out-Null
New-Item -ItemType File -Force -Path $stderr | Out-Null

$env:PYTHONFAULTHANDLER = "1"
$env:PYTHONUNBUFFERED = "1"
$env:G_ENABLE_DIAGNOSTIC = "1"
$env:G_MESSAGES_DEBUG = "all"

# Match a double-click as closely as we can while still capturing stdio:
# sshpilot-cli.exe is the console build of the same run.py entry.
$proc = Start-Process -FilePath $exe -ArgumentList "--verbose" `
    -WorkingDirectory $AppDir -PassThru -NoNewWindow `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr

$timedOut = -not $proc.WaitForExit($TimeoutSeconds * 1000)
$killed = $false
if ($timedOut) {
    try {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Wait-Process -Id $proc.Id -Timeout 5 -ErrorAction SilentlyContinue
        $killed = $true
    } catch {}
}

$exitCode = $null
try { $proc.Refresh(); $exitCode = $proc.ExitCode } catch { $exitCode = "unknown" }

$eventLog = Join-Path $OutDir "event-log.txt"
try {
    $events = Get-WinEvent -FilterHashtable @{
        LogName   = "Application"
        StartTime = $started.AddMinutes(-1)
        Level     = 2
    } -ErrorAction SilentlyContinue
    if ($events) {
        $events | Format-List TimeCreated, ProviderName, Id, Message |
            Out-File -FilePath $eventLog -Encoding utf8
    } else {
        Write-Text "event-log.txt" "No Application Error events since $($started.ToUniversalTime().ToString('o'))"
    }
} catch {
    Write-Text "event-log.txt" "Get-WinEvent failed: $_"
}

Copy-TreeIfPresent (Join-Path $env:APPDATA "sshpilot") "appdata-sshpilot"
Copy-TreeIfPresent (Join-Path $env:LOCALAPPDATA "sshpilot") "localappdata-sshpilot"
$crashDumps = Join-Path $env:LOCALAPPDATA "CrashDumps"
if (Test-Path $crashDumps) {
    Get-ChildItem $crashDumps -Filter "sshPilot*" -ErrorAction SilentlyContinue |
        Copy-Item -Destination $OutDir -Force -ErrorAction SilentlyContinue
    Get-ChildItem $crashDumps -Filter "sshpilot-cli*" -ErrorAction SilentlyContinue |
        Copy-Item -Destination $OutDir -Force -ErrorAction SilentlyContinue
}

$stdoutText = Get-Content $stdout -Raw -ErrorAction SilentlyContinue
$stderrText = Get-Content $stderr -Raw -ErrorAction SilentlyContinue
if ($null -eq $stdoutText) { $stdoutText = "" }
if ($null -eq $stderrText) { $stderrText = "" }

$reproduced = ($stderrText -match "must be an interface") -or ($stdoutText -match "must be an interface")
$startedOk = $timedOut -and -not $reproduced
$crashed = (-not $timedOut) -and ($exitCode -ne 0)

$summary = @"
label=$Label
exe=$exe
timedOut=$timedOut
killedAfterTimeout=$killed
exitCode=$exitCode
crashed=$crashed
startedWithoutImmediateExit=$startedOk
reproducedMustBeAnInterface=$reproduced
stdoutBytes=$($stdoutText.Length)
stderrBytes=$($stderrText.Length)
"@
Write-Text "summary.txt" $summary
Write-Text "exit-code.txt" "$exitCode"

$json = [ordered]@{
    label                           = $Label
    exe                             = $exe
    timedOut                        = $timedOut
    killedAfterTimeout              = $killed
    exitCode                        = $exitCode
    crashed                         = $crashed
    startedWithoutImmediateExit     = $startedOk
    reproducedMustBeAnInterface     = $reproduced
    stdoutBytes                     = $stdoutText.Length
    stderrBytes                     = $stderrText.Length
}
$json | ConvertTo-Json | Set-Content (Join-Path $OutDir "summary.json") -Encoding utf8

Write-Host $summary
if ($stderrText) {
    Write-Host "----- stderr (first 4000 chars) -----"
    Write-Host $stderrText.Substring(0, [Math]::Min(4000, $stderrText.Length))
}

# 0 = process still running at timeout (likely started)
# 1 = process exited (crash or clean early exit)
if ($timedOut) { exit 0 }
exit 1
