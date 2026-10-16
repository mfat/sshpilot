; sshPilot Windows installer (NSIS 3, Unicode). Built by build.ps1 -Installer:
;   makensis /DVERSION=x.y.z /DSOURCE=<dist\sshPilot> /DOUTFILE=<setup.exe> installer.nsi
;
; Per-user install into %LOCALAPPDATA%\Programs\sshPilot: no administrator
; rights, nothing machine-wide. The daemon is stopped before files change.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"

!ifndef VERSION
  !error "pass /DVERSION=..."
!endif
!ifndef SOURCE
  !error "pass /DSOURCE=<the PyInstaller sshPilot folder>"
!endif
!ifndef OUTFILE
  !define OUTFILE "sshPilot-${VERSION}-setup.exe"
!endif

!define APP_NAME "sshPilot"
!define PUBLISHER "sshPilot contributors"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_NAME}"

Name "${APP_NAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_NAME}"
InstallDirRegKey HKCU "Software\${APP_NAME}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ManifestDPIAware true

!define MUI_ICON "${__FILEDIR__}\sshpilot.ico"
!define MUI_UNICON "${__FILEDIR__}\sshpilot.ico"
!define MUI_FINISHPAGE_RUN "$INSTDIR\sshPilot.exe"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${__FILEDIR__}\..\..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP_NAME}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "FileDescription" "${APP_NAME} installer"
VIAddVersionKey "LegalCopyright" "GPL-3.0-or-later"

!macro StopDaemon
  ; A running daemon holds files in $INSTDIR open; ask it to stop, then wait.
  IfFileExists "$INSTDIR\sshpilot-cli.exe" 0 +3
    nsExec::Exec '"$INSTDIR\sshpilot-cli.exe" --daemon stop'
    Sleep 1500
!macroend

!macro CheckFileFree FILE BUSY
  ; Windows refuses write access to a running exe or a loaded DLL. FileOpen
  ; "a" would create a missing file, so only test files that exist.
  IfFileExists "${FILE}" 0 +5
    ClearErrors
    FileOpen $0 "${FILE}" a
    IfErrors ${BUSY}
    FileClose $0
!macroend

!macro EnsureNotRunning
  ; Files in use would be skipped silently, leaving old and new files mixed,
  ; and an open window restarts the daemon. Stop the daemon, then wait for the
  ; user to close sshPilot; a silent run fails instead (exit code 2).
  !define _ENR_ID ${__LINE__}
  retry_${_ENR_ID}:
    !insertmacro StopDaemon
    !insertmacro CheckFileFree "$INSTDIR\sshPilot.exe" busy_${_ENR_ID}
    !insertmacro CheckFileFree "$INSTDIR\sshpilot-cli.exe" busy_${_ENR_ID}
    !insertmacro CheckFileFree "$INSTDIR\_internal\OpenConsole.exe" busy_${_ENR_ID}
    Goto free_${_ENR_ID}
  busy_${_ENR_ID}:
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION \
      "${APP_NAME} is running. Close it, then click Retry." \
      /SD IDCANCEL IDRETRY retry_${_ENR_ID}
    DetailPrint "${APP_NAME} is running; close it and run setup again."
    SetErrorLevel 2
    Abort
  free_${_ENR_ID}:
  !undef _ENR_ID
!macroend

Section "sshPilot" SecMain
  SectionIn RO
  !insertmacro EnsureNotRunning
  SetOutPath "$INSTDIR"
  ; Replace the previous version completely: stale files must not linger.
  RMDir /r "$INSTDIR\_internal"
  File /r "${SOURCE}\*.*"

  CreateShortcut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\sshPilot.exe"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKCU "Software\${APP_NAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayName" "${APP_NAME}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "DisplayIcon" "$INSTDIR\sshPilot.exe"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTALL_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINSTALL_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTALL_KEY}" "EstimatedSize" "$0"
SectionEnd

Section "Uninstall"
  !insertmacro EnsureNotRunning
  Delete "$SMPROGRAMS\${APP_NAME}.lnk"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\sshPilot.exe"
  Delete "$INSTDIR\sshpilot-cli.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "Software\${APP_NAME}"
  ; Settings, connections and logs in %APPDATA% / %LOCALAPPDATA%\sshpilot stay.
SectionEnd
