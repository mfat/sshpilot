; sshPilot Windows installer (NSIS 3, Unicode). Built by build.ps1 -Installer:
;   makensis /DVERSION=x.y.z /DSOURCE=<dist\sshPilot> /DOUTFILE=<setup.exe> installer.nsi
;
; Per-user install into %LOCALAPPDATA%\Programs\sshPilot: no administrator
; rights, nothing machine-wide. The daemon is stopped before files change.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "nsDialogs.nsh"
!include "LogicLib.nsh"

!ifndef VERSION
  !error "pass /DVERSION=..."
!endif
!ifndef SOURCE
  !error "pass /DSOURCE=<the PyInstaller sshPilot folder>"
!endif
!ifndef OUTFILE
  !define OUTFILE "SSHPilot-${VERSION}-windows-setup.exe"
!endif

; APP_ID names the install folder and registry keys; renaming it would orphan
; existing installs. APP_NAME is the name people see.
!define APP_ID "sshPilot"
!define APP_NAME "SSH Pilot"
!define PUBLISHER "SSH Pilot contributors"
!define UNINSTALL_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP_ID}"
!define PRIVACY_URL "https://sshpilot.app/privacy.html"

; 1 when the app should check for updates on startup. The app copies it into
; its config on the next launch (installer_update_choice.py) and mirrors later
; Preferences changes back, so a rerun of setup starts from the current choice.
Var CheckUpdates
Var CheckUpdatesBox

Name "${APP_NAME} ${VERSION}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP_ID}"
InstallDirRegKey HKCU "Software\${APP_ID}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ManifestDPIAware true

!define MUI_ICON "${__FILEDIR__}\sshpilot.ico"
!define MUI_UNICON "${__FILEDIR__}\sshpilot.ico"
; 164x314 side panel of the welcome and finish pages.
!define MUI_WELCOMEFINISHPAGE_BITMAP "${__FILEDIR__}\welcome.bmp"
!define MUI_UNWELCOMEFINISHPAGE_BITMAP "${__FILEDIR__}\welcome.bmp"
; 150x57 banner on the right of every inner page's header.
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_RIGHT
!define MUI_HEADERIMAGE_BITMAP "${__FILEDIR__}\header.bmp"
!define MUI_HEADERIMAGE_UNBITMAP "${__FILEDIR__}\header.bmp"
!define MUI_FINISHPAGE_RUN "$INSTDIR\sshPilot.exe"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "${__FILEDIR__}\..\..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
; SignPath Foundation requires the privacy notice and an opt-out during setup.
Page custom UpdateCheckPage UpdateCheckPageLeave
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

Function .onInit
  StrCpy $CheckUpdates 1
  ClearErrors
  ReadRegDWORD $0 HKCU "Software\${APP_ID}" "CheckForUpdates"
  ${IfNot} ${Errors}
    StrCpy $CheckUpdates $0
  ${EndIf}
FunctionEnd

Function UpdateCheckPage
  !insertmacro MUI_HEADER_TEXT "Privacy" "Choose whether ${APP_NAME} checks for updates."
  nsDialogs::Create 1018
  Pop $0
  ${NSD_CreateLabel} 0 0 100% 24u "SSH Pilot checks GitHub for new versions when it starts. GitHub sees your IP address."
  Pop $0
  ${NSD_CreateCheckbox} 0 30u 100% 12u "Check for updates on startup"
  Pop $CheckUpdatesBox
  ${If} $CheckUpdates == 1
    ${NSD_Check} $CheckUpdatesBox
  ${EndIf}
  ${NSD_CreateLabel} 0 52u 100% 12u "You can change this later in Preferences."
  Pop $0
  ${NSD_CreateLink} 0 70u 100% 12u "Privacy policy"
  Pop $0
  ${NSD_OnClick} $0 OpenPrivacyPolicy
  nsDialogs::Show
FunctionEnd

Function OpenPrivacyPolicy
  Pop $0
  ExecShell "open" "${PRIVACY_URL}"
FunctionEnd

Function UpdateCheckPageLeave
  ${NSD_GetState} $CheckUpdatesBox $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CheckUpdates 1
  ${Else}
    StrCpy $CheckUpdates 0
  ${EndIf}
FunctionEnd

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
  ; user to close SSH Pilot; a silent run fails instead (exit code 2).
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

Section "SSH Pilot" SecMain
  SectionIn RO
  !insertmacro EnsureNotRunning
  SetOutPath "$INSTDIR"
  ; Replace the previous version completely: stale files must not linger.
  RMDir /r "$INSTDIR\_internal"
  File /r "${SOURCE}\*.*"

  Delete "$SMPROGRAMS\${APP_ID}.lnk" ; older installers named it after APP_ID
  CreateShortcut "$SMPROGRAMS\${APP_NAME}.lnk" "$INSTDIR\sshPilot.exe"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  WriteRegStr HKCU "Software\${APP_ID}" "InstallDir" "$INSTDIR"
  WriteRegDWORD HKCU "Software\${APP_ID}" "CheckForUpdates" $CheckUpdates
  WriteRegDWORD HKCU "Software\${APP_ID}" "CheckForUpdatesPending" 1
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
  Delete "$SMPROGRAMS\${APP_ID}.lnk"
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\sshPilot.exe"
  Delete "$INSTDIR\sshpilot-cli.exe"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTALL_KEY}"
  DeleteRegKey HKCU "Software\${APP_ID}"
  ; Settings, connections and logs in %APPDATA% / %LOCALAPPDATA%\sshpilot stay.
SectionEnd
