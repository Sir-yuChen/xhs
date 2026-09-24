; =============================================================================
; SpiderXHS 安装脚本（Inno Setup 6，设计 14 §7）
;
; 由 packaging/build.ps1 调用：
;   ISCC installer.iss /DAppVersion=1.0.0 /DAppPackageVersion=1.0.0.0
; 产物：spider-xhs-setup-<版本>.exe，输出到【项目根目录】。
;
; 关键口径：
;  * PrivilegesRequired=lowest —— 免 UAC；数据目录就在安装目录里，必须可写；
;  * 安装目录用户自选（默认 {localappdata}\Programs\SpiderXHS），
;    data/、backend/app/storage/、logs/ 全部落在其中；
;  * 卸载时询问是否连带删除数据（默认否）。
; =============================================================================

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef AppPackageVersion
  #define AppPackageVersion "1.0.0.0"
#endif

#define AppName "SpiderXHS"
#define AppExeName "SpiderXHS.exe"
#define AppPublisher "SpiderXHS"
; 固定 AppId：跨版本必须一致，否则每次升级都被当成新应用（旧数据接不上）。
; ⚠️ 必须用经典双花括号转义形式直接写在这里——经 #define 替换后 `{{` 会被
; ISPP 吃掉，单花括号又会被当成内联常量引用（Unknown constant 编译失败）。

[Setup]
AppId={{8F2A7C4E-3B5D-4E6A-9C1F-2D4B6A8E0F3C}}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#AppPackageVersion}
VersionInfoProductVersion={#AppPackageVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
OutputDir={#SourcePath}\..
OutputBaseFilename=spider-xhs-setup-{#AppVersion}
SetupIconFile={#SourcePath}\assets\app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName} {#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标:"

[Files]
; PyInstaller onedir 产物（含 _internal/、runtime/node、frontend/dist、config/）
Source: "{#SourcePath}\dist\SpiderXHS\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; \
    Flags: nowait postinstall skipifsilent

; 卸载语义：**只删程序文件，数据永远保留**。
; 早期设计是卸载末弹 Yes/No 问「是否连带删数据」——实测 /VERYSILENT 静默卸载时
; [Code] 里的 MsgBox **不会**被自动应答，卸载器堵死在弹窗上（冒烟测试挂死 16 分钟）。
; 且「默认删用户数据库」本身也是危险默认。改为：data/、backend/app/storage/、logs/
; 均为运行期生成、不在卸载记录里 ⇒ 自然保留；要彻底清除就删整个安装目录。
