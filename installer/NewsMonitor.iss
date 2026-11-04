#ifndef AppVersion
  #define AppVersion "1.1.11"
#endif
[Setup]
#ifdef SmokeTest
AppId=NewsMonitor-InstallerSmokeTest
Uninstallable=no
OutputDir=..\tmp\installer-smoke
OutputBaseFilename=NewsMonitor-Setup-Smoke
#else
AppId={{A9B11F14-790C-435B-ABD8-2305E212F621}
OutputDir=..\dist
OutputBaseFilename=NewsMonitor-Setup-{#AppVersion}
#endif
AppName=News Monitor
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\NewsMonitor
DefaultGroupName=News Monitor
PrivilegesRequired=lowest
DisableDirPage=no
DisableProgramGroupPage=yes
LicenseFile=usage-terms.txt
SetupIconFile=..\assets\news-monitor.ico
UninstallDisplayIcon={app}\NewsMonitor.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
CloseApplications=yes
RestartApplications=no
AppMutex=Local\NewsMonitor.InstalledApp

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면 바로가기 만들기"; Flags: checkedonce

[Files]
Source: "..\dist\NewsMonitor.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "usage-terms.txt"; DestDir: "{app}"

[Icons]
#ifndef SmokeTest
Name: "{userprograms}\News Monitor"; Filename: "{app}\NewsMonitor.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\News Monitor"; Filename: "{app}\NewsMonitor.exe"; WorkingDir: "{app}"; Tasks: desktopicon
#endif

[Run]
Filename: "{app}\NewsMonitor.exe"; Description: "News Monitor 실행 (기존 데이터 백업 및 이전)"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: files; Name: "{app}\NewsMonitor.exe.previous"
Type: files; Name: "{app}\migration-source.txt"

[Code]
var
  LegacyPage: TInputDirWizardPage;

procedure InitializeWizard;
var Candidate: String;
begin
  LegacyPage := CreateInputDirPage(wpSelectDir, '기존 데이터 가져오기',
    '기존 프로그램을 종료한 후 data 폴더를 확인하세요.',
    '첫 실행 시 전체 데이터를 백업하고 사용자 데이터 폴더로 복사합니다. 원본은 삭제하지 않습니다.' + #13#10 +
    '기존 봇 정보와 검색 조건을 유지하려면 settings.json이 있는 data 폴더를 선택하세요. 처음 사용하는 경우 비워 두세요.', False, '');
  LegacyPage.Add('기존 data 폴더:');
  Candidate := ExpandConstant('{src}\data');
  if not FileExists(Candidate + '\settings.json') then
    Candidate := ExpandConstant('{src}\..\data');
  if FileExists(Candidate + '\settings.json') then LegacyPage.Values[0] := Candidate;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := (PageID = LegacyPage.ID) and DirExists(ExpandConstant('{localappdata}\NewsMonitor\data'));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var Probe: String;
begin
  Result := True;
  if CurPageID = wpSelectDir then begin
    Probe := AddBackslash(WizardDirValue) + '.install-write-test';
    if not ForceDirectories(WizardDirValue) or not SaveStringToFile(Probe, 'test', False) then begin
      MsgBox('자동 업데이트를 위해 현재 사용자가 쓸 수 있는 설치 폴더를 선택하세요. 기본 설치 경로를 권장합니다.', mbError, MB_OK);
      Result := False;
    end else DeleteFile(Probe);
  end;
  if (CurPageID = LegacyPage.ID) and (LegacyPage.Values[0] <> '') and
    not FileExists(AddBackslash(LegacyPage.Values[0]) + 'settings.json') then begin
    MsgBox('settings.json이 있는 기존 data 폴더를 선택하세요.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (LegacyPage.Values[0] <> '') and
    not DirExists(ExpandConstant('{localappdata}\NewsMonitor\data')) then
    if not SaveStringToFile(ExpandConstant('{app}\migration-source.txt'), UTF8Encode(LegacyPage.Values[0]), False) then
      RaiseException('기존 데이터 경로를 저장하지 못했습니다.');
end;
