; Inno Setup script for AK Direct Print
; Produces: dist\AKDirectPrint_Setup.exe
;
; Requirements:
;   Inno Setup 6 (https://jrsoftware.org/isdl.php) installed on Windows.
;
; Build:
;   From agent\ directory:
;     "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build\windows\ak_direct_print.iss
;   Or use build_installer_win.bat which does everything automatically.

; ---------------------------------------------------------------------------
; App identity
; ---------------------------------------------------------------------------
[Setup]
AppId={{6A3F2B1C-84D7-4E9A-BF32-1C5D8E9A0F47}
AppName=AK Direct Print
AppVersion=1.0.0
AppPublisher=Aktiv Software
AppPublisherURL=https://www.aktivsoftware.com
AppSupportURL=https://www.aktivsoftware.com
AppUpdatesURL=https://www.aktivsoftware.com
AppCopyright=Copyright (C) 2025 Aktiv Software

; ---------------------------------------------------------------------------
; Install location
; ---------------------------------------------------------------------------
; PrivilegesRequired=lowest + PrivilegesRequiredOverridesAllowed=dialog lets the
; installer run without UAC for per-user installs, or elevate for all-users.
; {autopf} resolves to Program Files for admin, AppData\Local\Programs for non-admin.
DefaultDirName={autopf}\AK Direct Print
DefaultGroupName=AK Direct Print
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
AllowNoIcons=yes

; ---------------------------------------------------------------------------
; Output
; ---------------------------------------------------------------------------
; Path is relative to the .iss file location (build\windows\)
; Two levels up = agent\, then into dist\
OutputDir=..\..\dist
OutputBaseFilename=AKDirectPrint_Setup
Compression=lzma2/ultra64
SolidCompression=yes

; ---------------------------------------------------------------------------
; Appearance
; ---------------------------------------------------------------------------
WizardStyle=modern
WizardResizable=no
DisableWelcomePage=no
DisableDirPage=no
DisableProgramGroupPage=yes

; ---------------------------------------------------------------------------
; Architecture
; ---------------------------------------------------------------------------
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; ---------------------------------------------------------------------------
; Uninstall
; ---------------------------------------------------------------------------
UninstallDisplayName=AK Direct Print
UninstallDisplayIcon={app}\AKDirectPrint.exe
CloseApplications=yes
RestartApplications=no

; ---------------------------------------------------------------------------
; Languages
; ---------------------------------------------------------------------------
[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

; ---------------------------------------------------------------------------
; Optional tasks shown on the "Select Additional Tasks" wizard page
; ---------------------------------------------------------------------------
[Tasks]
; "Start on Login" is ticked by default — user can uncheck it.
; Note: tasks are checked by default in Inno Setup; no flag needed for checked state.
Name: "startlogin";   Description: "Start AK Direct Print automatically when Windows starts"
; Desktop shortcut is opt-in.
Name: "desktopicon";  Description: "Create a &desktop shortcut"; Flags: unchecked

; ---------------------------------------------------------------------------
; Files
; ---------------------------------------------------------------------------
[Files]
; Path relative to .iss location → agent\dist\AKDirectPrint.exe
Source: "..\..\dist\AKDirectPrint.exe"; DestDir: "{app}"; Flags: ignoreversion

; ---------------------------------------------------------------------------
; Shortcuts
; ---------------------------------------------------------------------------
[Icons]
Name: "{group}\AK Direct Print";        Filename: "{app}\AKDirectPrint.exe"
Name: "{group}\Uninstall AK Direct Print"; Filename: "{uninstallexe}"
Name: "{commondesktop}\AK Direct Print"; Filename: "{app}\AKDirectPrint.exe"; Tasks: desktopicon

; ---------------------------------------------------------------------------
; Registry — Start on Login (HKCU, no admin required)
; ---------------------------------------------------------------------------
[Registry]
Root: HKCU; \
    Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; \
    ValueName: "AK Direct Print"; \
    ValueData: """{app}\AKDirectPrint.exe"""; \
    Flags: uninsdeletevalue; \
    Tasks: startlogin

; ---------------------------------------------------------------------------
; Post-install: launch the agent so the tray icon appears immediately
; ---------------------------------------------------------------------------
[Run]
Filename: "{app}\AKDirectPrint.exe"; \
    Description: "Launch AK Direct Print now"; \
    Flags: nowait postinstall skipifsilent shellexec

; ---------------------------------------------------------------------------
; Pre-uninstall: kill the running agent so files can be deleted
; ---------------------------------------------------------------------------
[UninstallRun]
Filename: "taskkill"; Parameters: "/F /IM AKDirectPrint.exe"; \
    Flags: runhidden waituntilterminated; RunOnceId: "KillAgent"
