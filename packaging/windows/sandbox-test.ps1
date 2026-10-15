# Test the built installer on a clean Windows in Windows Sandbox.
#
#   powershell -ExecutionPolicy Bypass -File packaging\windows\sandbox-test.ps1 [-Gpu] [-NoLaunch]
#
# Needs build\windows\sshPilot-<version>-setup.exe (build.ps1 -Installer) and
# the Windows Sandbox feature (Windows 10/11 Pro or Enterprise). Writes
# build\windows\sshpilot-sandbox.wsb, which maps build\windows and this folder
# read-only and build\windows\sandbox-results writable, and runs
# sandbox-run.ps1 at logon. The sandbox has no GPU unless -Gpu is given, so
# GTK's software rendering path is what gets tested.

param([switch]$Gpu, [switch]$NoLaunch)

$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$build = Join-Path $root "build\windows"
$results = Join-Path $build "sandbox-results"

if (-not (Get-ChildItem $build -Filter "sshPilot-*-setup.exe" -ErrorAction SilentlyContinue)) {
    throw "no installer in ${build}: run build.ps1 -Installer first"
}
if (Test-Path $results) { Remove-Item -LiteralPath $results -Recurse -Force }
New-Item -ItemType Directory -Force $results | Out-Null

$vgpu = if ($Gpu) { "Enable" } else { "Disable" }
$wsb = Join-Path $build "sshpilot-sandbox.wsb"
@"
<Configuration>
  <vGPU>$vgpu</vGPU>
  <Networking>Enable</Networking>
  <MemoryInMB>4096</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>$build</HostFolder>
      <SandboxFolder>C:\sshpilot\build</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$PSScriptRoot</HostFolder>
      <SandboxFolder>C:\sshpilot\scripts</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>$results</HostFolder>
      <SandboxFolder>C:\sshpilot\results</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\sshpilot\scripts\sandbox-run.ps1</Command>
  </LogonCommand>
</Configuration>
"@ | Set-Content -Encoding utf8 $wsb
"Wrote $wsb"

if (-not $NoLaunch) {
    $exe = Join-Path $env:SystemRoot "System32\WindowsSandbox.exe"
    if (-not (Test-Path $exe)) {
        throw "Windows Sandbox is not enabled: turn on the 'Windows Sandbox' optional feature and restart"
    }
    Start-Process $wsb
    "Sandbox starting; the report appears in $results\report.txt"
}
