// SpiderXHS 独立窗口宿主（Electron 主进程）。
//
// ## 为什么要有它（2026-10-23）
//
// 之前用「Edge/Chrome 的 --app=」开独立窗口，实测在真机上失败：
//   ① 用户电脑可能根本没有 Edge/Chrome（只装了 360）；
//   ② 即使有，Edge 在**已有 app-profile 会话**时会打印「正在现有浏览器会话中打开」
//      并把请求转交给已有实例，**自己立即退出（code 0）** ⇒ 调用方误判为失败；
//   ③ 各家浏览器对 `--app=` 的支持程度不同（360/QQ/搜狗都可能吞掉参数变普通标签页）。
// 用户口径：「不再依赖不同电脑的不同环境，无法确认」。
//
// 本宿主自带 Chromium（Electron），因此**不读用户任何浏览器**，行为在所有电脑上一致：
//   * 同一个可执行文件、同一个内核版本、同一个窗口形态；
//   * 只依赖本进程连的 `--url`（由 Python 启动器在服务就绪后传入）。
//
// ## 用法（由 `packaging/launcher.py` 调用）
//
//     SpiderXHSWindow.exe --url=http://127.0.0.1:8000 --profile=<dir>
//
// ## 退出语义
//
//   * 用户关掉窗口 ⇒ 进程退出（启动器据此把后端也停掉）；
//   * 启动器（托盘「退出」）只杀本进程，后端由它自己停。
//   * ⚠️ 不做「无窗口常驻」：进程活着就必须有窗口，避免出现「进程在但看不见」的
//     那类僵尸态（正是本文件要根治的毛病）。
'use strict';

const { app, BrowserWindow, shell } = require('electron');

//: 单实例：第二次启动直接退出（由第一次负责把 URL 窗口带到前台）。
//: ⚠️ 必须开启 —— 否则用户双击两次会出现两个窗口、两个 profile 锁冲突。
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

/** 解析 `--name=value` 形式的参数（Electron 会把它们放在 argv 里）。 */
function argValue(name, fallback) {
  const prefix = `--${name}=`;
  const hit = process.argv.find((a) => a.startsWith(prefix));
  return hit ? hit.slice(prefix.length) : fallback;
}

const TARGET_URL = argValue('url', 'http://127.0.0.1:8000');
const PROFILE_DIR = argValue('profile', '');

//: profile 由启动器指定（安装目录下 runtime/app-profile），不污染用户目录。
if (PROFILE_DIR) {
  app.setPath('userData', PROFILE_DIR);
}

//: 关掉不需要的 Chromium 能力，减少启动耗时与攻击面。
app.commandLine.appendSwitch('disable-features', 'HardwareMediaKeyHandling');
app.commandLine.appendSwitch('no-first-run');
app.commandLine.appendSwitch('no-default-browser-check');

let mainWindow = null;
//: 启动横幅：内核起来到页面渲染完成之间给用户一个可见反馈，
//: 避免「点了没反应」的观感（真机反馈的问题之一）。
let splash = null;

function createSplash() {
  splash = new BrowserWindow({
    width: 460,
    height: 260,
    frame: false,
    resizable: false,
    show: false,
    backgroundColor: '#1f1f1f',
    webPreferences: { nodeIntegration: false, contextIsolation: true },
  });
  const html = `<!doctype html><html><head><meta charset="utf-8"><style>
    body{margin:0;height:100vh;display:flex;flex-direction:column;align-items:center;
    justify-content:center;background:#1f1f1f;color:#e8e8e8;
    font:14px/1.6 "Microsoft YaHei",system-ui,sans-serif;-webkit-user-select:none}
    .t{font-size:16px;margin-bottom:10px;letter-spacing:.5px}
    .d{opacity:.65;font-size:12px}
    .bar{width:180px;height:3px;background:#3a3a3a;border-radius:2px;margin-top:18px;overflow:hidden}
    .bar i{display:block;width:40%;height:100%;background:#4a9eff;border-radius:2px;
    animation:m 1.1s ease-in-out infinite}
    @keyframes m{0%{transform:translateX(-100%)}100%{transform:translateX(350%)}}
  </style></head><body>
    <div class="t">SpiderXHS 正在启动…</div>
    <div class="d">正在连接本地服务，请稍候</div>
    <div class="bar"><i></i></div>
  </body></html>`;
  splash.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(html));
  splash.once('ready-to-show', () => splash && splash.show());
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    show: false,
    backgroundColor: '#ffffff',
    autoHideMenuBar: true,
    title: 'SpiderXHS Operations',
    webPreferences: {
      //: 页面是我们自己的本地前端；不开放 node 能力（最小权限）。
      nodeIntegration: false,
      contextIsolation: true,
      //: 允许前端用 localStorage / cookie 存登录态（默认就允许，显式写出便于审阅）。
      webSecurity: true,
    },
  });

  mainWindow.setMenuBarVisibility(false);
  mainWindow.loadURL(TARGET_URL);

  mainWindow.once('ready-to-show', () => {
    if (splash) {
      splash.destroy();
      splash = null;
    }
    mainWindow.show();
    mainWindow.focus();
    //: 显式置前：真机上出现过「窗口建了但在别的窗口后面」被当成没打开。
    mainWindow.setAlwaysOnTop(true);
    mainWindow.setAlwaysOnTop(false);
  });

  //: 外链走系统浏览器，不在应用窗口里开（避免用户以为应用坏了）。
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(TARGET_URL)) {
      shell.openExternal(url);
      return { action: 'deny' };
    }
    return { action: 'allow' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
    app.quit();
  });
}

//: 第二次启动：把已有窗口带到前台（单实例语义里最容易被忘的一步）。
app.on('second-instance', () => {
  if (mainWindow) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  }
});

app.whenReady().then(() => {
  createSplash();
  createWindow();
});

//: 所有窗口关闭即退出（这是桌面应用的正常语义，也保证启动器能感知到「用户关了界面」）。
app.on('window-all-closed', () => app.quit());
