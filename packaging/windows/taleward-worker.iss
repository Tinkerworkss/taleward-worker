; Taleward Worker – Windows-Installer (Inno Setup 6). Baut der GitHub-Ablauf build.yml:
;   iscc /DVersion=0.1.0 packaging\windows\taleward-worker.iss
; Installation nur für den eigenen Benutzer (keine Administratorrechte nötig).

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{5B7C2E61-8F3A-4C8E-9C1B-7A0D4E2F9B31}
AppName=Taleward Worker
AppVersion={#Version}
AppPublisher=Taleward
AppPublisherURL=https://taleward.org
AppSupportURL=https://github.com/Tinkerworkss/taleward-worker
DefaultDirName={localappdata}\Programs\Taleward Worker
DefaultGroupName=Taleward
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\dist
OutputBaseFilename=TalewardWorker-Setup
SetupIconFile=taleward-worker.ico
UninstallDisplayIcon={app}\TalewardWorker.exe
WizardStyle=modern
WizardImageFile=installer-seite.bmp
WizardSmallImageFile=installer-klein.bmp
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
MinVersion=10.0.17763
LicenseFile=..\..\LICENSE

[Languages]
Name: "de"; MessagesFile: "compiler:Languages\German.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
de.Autostart=Mit Windows starten (empfohlen) – der Worker läuft dann unsichtbar im Hintergrund
en.Autostart=Start with Windows (recommended) – the worker then runs invisibly in the background
de.Starten=Taleward Worker jetzt starten
en.Starten=Start Taleward Worker now
de.DatenLoeschen=Auch das KI-Paket, die Sprachmodelle und die Einstellungen löschen (etwa 10 GB)?%n%nBei „Nein“ bleiben sie für eine spätere Neuinstallation erhalten.
en.DatenLoeschen=Also delete the AI package, speech models and settings (about 10 GB)?%n%nIf you choose “No”, they are kept for a later reinstall.
de.DatenRest=Einige Dateien unter %1 konnten nicht gelöscht werden (noch in Benutzung). Bitte den Ordner nach einem Neustart von Hand löschen.
en.DatenRest=Some files under %1 could not be deleted (still in use). Please delete the folder manually after a restart.
de.AlsAdmin=Das Setup läuft mit Administratorrechten. Taleward Worker wird für den Benutzer installiert, der das Setup gestartet hat – nicht für das Konto, in dem Sie gerade angemeldet sind.%n%nEmpfehlung: Abbrechen und das Setup ohne „Als Administrator ausführen“ starten.%n%nTrotzdem fortfahren?
en.AlsAdmin=Setup is running with administrator rights. Taleward Worker will be installed for the user who started Setup – not for the account you are logged in as.%n%nRecommendation: cancel and run Setup without “Run as administrator”.%n%nContinue anyway?
de.WebView2Laden=Microsoft WebView2 wird geladen (für die Oberfläche nötig) …
en.WebView2Laden=Downloading Microsoft WebView2 (needed for the user interface) …
de.WebView2Fehlt=Microsoft WebView2 konnte nicht installiert werden. Taleward Worker zeigt beim Start einen Hinweis mit Download-Link.
en.WebView2Fehlt=Microsoft WebView2 could not be installed. Taleward Worker will show a hint with a download link on start.

[Tasks]
Name: "autostart"; Description: "{cm:Autostart}"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\..\dist\TalewardWorker\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\Taleward Worker"; Filename: "{app}\TalewardWorker.exe"
Name: "{userdesktop}\Taleward Worker"; Filename: "{app}\TalewardWorker.exe"; Tasks: desktopicon

[Registry]
; Gleicher Eintrag, den die App unter Einstellungen → „Mit dem Computer starten“ setzt oder entfernt
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Taleward Worker"; \
  ValueData: """{app}\TalewardWorker.exe"" --hintergrund"; Tasks: autostart; Flags: uninsdeletevalue; Check: not WizardSilent
; (nicht bei stillen Updates: Inno würde sonst den beim ersten Mal gewählten Autostart wieder einschalten,
;  auch wenn er in der App inzwischen abgeschaltet wurde)

[Run]
Filename: "{app}\TalewardWorker.exe"; Description: "{cm:Starten}"; Flags: nowait postinstall skipifsilent runasoriginaluser
; Update aus der App (still): danach die App im Hintergrund wieder starten
Filename: "{app}\TalewardWorker.exe"; Parameters: "--hintergrund"; Flags: nowait runasoriginaluser; Check: WizardSilent

[Code]
const
  WebView2Key = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  WebView2Bootstrapper = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703';

var
  LadeSeite: TDownloadWizardPage;

function WebView2Vorhanden(): Boolean;
var
  Fassung: String;
begin
  Result := (RegQueryStringValue(HKLM, WebView2Key, 'pv', Fassung) and (Fassung <> '') and (Fassung <> '0.0.0.0'))
         or (RegQueryStringValue(HKCU, WebView2Key, 'pv', Fassung) and (Fassung <> '') and (Fassung <> '0.0.0.0'));
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  { „Als Administrator ausführen“ auf einem Standardkonto: alles landete im Profil des Administrators }
  if IsAdmin and not WizardSilent then
    Result := MsgBox(CustomMessage('AlsAdmin'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

procedure InitializeWizard();
begin
  LadeSeite := CreateDownloadPage(SetupMessage(msgWizardPreparing), CustomMessage('WebView2Laden'), nil);
end;

function AppLaeuft(): Boolean;
var
  Code: Integer;
begin
  { find liefert 0, wenn tasklist die App aufführt }
  Result := Exec(ExpandConstant('{cmd}'), '/C tasklist /FI "IMAGENAME eq TalewardWorker.exe" | find /I "TalewardWorker.exe" >nul',
                 '', SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
end;

procedure LaufendeAppBeenden();
var
  Exe: String;
  Code, Runde: Integer;
begin
  { Die App wandert beim Schließen in den Infobereich und lehnt Schließanfragen ab – darum höflich über die App
    selbst beenden (--beenden spricht die laufende Instanz an) und bis 15 s warten; was dann noch läuft
    (alte Fassungen ohne --beenden), wird samt Kindprozessen abgeschossen }
  if not AppLaeuft() then Exit;
  Exe := ExpandConstant('{app}\TalewardWorker.exe');
  if FileExists(Exe) then
  begin
    Exec(Exe, '--beenden', '', SW_HIDE, ewNoWait, Code);
    Runde := 0;
    while AppLaeuft() and (Runde < 30) do
    begin
      Sleep(500);
      Runde := Runde + 1;
    end;
  end;
  if AppLaeuft() then
    Exec(ExpandConstant('{cmd}'), '/C taskkill /IM TalewardWorker.exe /F /T', '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure WebView2Einrichten();
var
  Code: Integer;
  Datei: String;
begin
  if WebView2Vorhanden() then Exit;
  LadeSeite.Clear;
  LadeSeite.Add(WebView2Bootstrapper, 'MicrosoftEdgeWebview2Setup.exe', '');
  LadeSeite.Show;
  try
    try
      LadeSeite.Download;
      Datei := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
      { Der Evergreen-Bootstrapper installiert ohne Administratorrechte je Benutzer }
      if (not Exec(Datei, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, Code)) or (Code <> 0) then
        if not WizardSilent then MsgBox(CustomMessage('WebView2Fehlt'), mbInformation, MB_OK);
    except
      if not WizardSilent then MsgBox(CustomMessage('WebView2Fehlt'), mbInformation, MB_OK);
    end;
  finally
    LadeSeite.Hide;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  LaufendeAppBeenden();
  WebView2Einrichten();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Daten: String;
  Befehl: String;
begin
  Daten := ExpandConstant('{localappdata}\Taleward Worker');
  if CurUninstallStep = usUninstall then
  begin
    LaufendeAppBeenden();
    { KI-Paket (python.exe), ffmpeg und das eigene Ollama laufen als eigene Prozesse aus dem Datenordner weiter –
      nur die aus diesem Ordner beenden, nicht ein fremdes Ollama des Benutzers }
    Befehl := '-NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $_.Path -like ''' + Daten + '\*'' } | Stop-Process -Force"';
    Exec('powershell.exe', Befehl, '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'Taleward Worker');
    if (not UninstallSilent) and
       (MsgBox(CustomMessage('DatenLoeschen'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
      if not DelTree(Daten, True, True, True) then
        MsgBox(FmtMessage(CustomMessage('DatenRest'), [Daten]), mbInformation, MB_OK);
  end;
end;
