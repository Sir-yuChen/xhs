# packaging · 桌面打包（设计 14）

把 XHS_ALL_IN_ONE 打成 Windows 桌面安装程序。设计文档：
`docs/02_设计文档/14_功能设计_桌面打包与安装.md`。

## 一键打包

```powershell
# 在任意目录执行（默认版本 1.0.0）
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1

# 发新版：只改版本号
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build.ps1 -Version 1.0.1
```

产物：**项目根目录** `spider-xhs-setup-<版本>.exe`。

| 开关 | 作用 |
|---|---|
| `-Version <x.y.z>` | 版本号（默认 1.0.0），决定产物文件名与 exe 版本信息 |
| `-SkipFrontend` | 跳过 `npm ci + vite build`（dist 已最新时省时间） |
| `-SkipInstaller` | 只出 `packaging/dist/SpiderXHS/` bundle，不调 Inno Setup |
| `-Clean` | 先删 `packaging/{build,dist,.venv-build,node-runtime}` 全量重建 |

## 正式版发布工作流（一键发版）

打包只是上游；「发正式版」还要 校验 → 冒烟 → 打 tag → 建 Release 并上传安装包。
`packaging/release.ps1` 把这七步串成一条**可重复执行**的链路：

```powershell
# 完整发版（要求工作区干净）
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\release.ps1 -Version 1.0.2

# 只打包 + 冒烟，不发版
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\release.ps1 -Version 1.0.2 -SkipTag -SkipRelease

# 换 tag 名 / 指定发布说明
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\release.ps1 -Version 1.0.2 `
    -TagName v1.0.2 -ReleaseName "v1.0.2 · 正式版" -NotesFile notes.md
```

| 阶段 | 做什么 | 闸门（不过就停） |
|---|---|---|
| 1 预检 | 工作区是否干净、tag 是否已被占用、Python / Inno Setup 是否就绪 | **工作区脏 ⇒ 直接拒绝**（包必须对应确定提交，否则 tag 与发布物脱钩） |
| 2 打包 | 清 `packaging/{dist,build}` + 删旧安装包 → `build.ps1 -Version` | 构建 exit≠0 |
| 3 校验 | 产物存在且 ≥80MB、**包内 `frontend/dist/assets/index-*.js` 与源码 dist 逐名一致**、`version_info.txt` 版本号、构建自检「全部通过」且无 `[LEAK]`/`[MISSING]` | 任一不符 |
| 4 冒烟 | `smoke-test.ps1`（静默安装 → health → openapi paths → 端口回退 → 卸载） | 冒烟非「全部通过」 |
| 5 清残留 | 删 `.workbuddy/tmp/smoke-install` | 清理被拦只告警 |
| 6 打 tag | 附注 tag（正文含提交号 / 体积 / sha256）+ push | tag 已存在 / push 失败 |
| 7 发布 | `gitee_release.py`：建或复用 Release、更新说明、**同名附件 `--replace` 覆盖** | API 调用失败 |

**开关**：`-SkipFrontend` `-SkipSmoke` `-SkipTag` `-SkipRelease` `-AllowDirty`
`-Owner` `-Repo` `-Python` `-TagName` `-ReleaseName` `-NotesFile`。

**三条硬口径**：

1. **安装包不进 git 仓库** —— `.gitignore` 明确排除 `spider-xhs-setup-*.exe`，发布物一律走 **Release 附件**。
   86.7MB 二进制提交进 git 会让仓库历史永久膨胀。
2. **发布判据不看网页状态码** —— 仓库是**私有**的，未登录访问仓库页 / Release 页 / 附件直链**恒返回 403**。
   判据用 **API 返回的 assets 列表**（`gitee_release.py` 末尾会打印附件名与下载直链）。
3. **令牌从凭据管理器取** —— `gitee_release.py` 用 `git credential fill` 读 gitee 凭据（私人令牌），
   全程只在内存流转、**不落盘、不回显**；取不到时才需要手工提供。

> 实测（2026-09-29，v1.0.1）：构建 5m14s / 冒烟 27s / 附件替换上传 20s；
> 包 **90,860,466 B**、sha256 `04c6c05415450156…`；冒烟 `paths: 164` 与 `AGENTS.md` §5 声明值一致。

## 流程（脚本自动完成）

1. 前置检查：Python 3.12（可用 `SPIDER_XHS_PYTHON` 指定）、node/npm、Inno Setup（ISCC）；
2. 派生构建 venv 到 `packaging/.venv-build`（隔离，不污染系统 Python）；
3. `npm ci && npm run build` → `frontend/dist`；
4. 生成 `version_info.txt`（exe 版本信息）与 `assets/app.ico`（源自 `frontend/public/logo.jpg`）；
5. 暂存 node 运行时：`node.exe` + 仅 `crypto-js` 一棵 `node_modules`；
6. PyInstaller onedir → `packaging/dist/SpiderXHS/`；
7. 组装：`frontend/dist`、`config/default.yaml`、`runtime/node`；
8. ISCC → `spider-xhs-setup-<版本>.exe`（项目根目录）。

**启动行为（2026-09-26 起）**：launcher 健康检查通过后用 Edge / Chrome 的 `--app=` 起
**独立应用窗口**（无地址栏/标签栏，独占 profile `%LOCALAPPDATA%\SpiderXHS\app-profile`），
不再弹系统默认浏览器的标签页；两者都找不到才退回默认浏览器（设计 14 §2）。

**黑窗闪现（2026-09-26 修复）**：windowed 主进程每次起 `node.exe` 算签名都会闪一个黑色
控制台窗口（页面每有操作/每轮 5s 轮询都触发）。launcher 在 frozen 时给全部子进程注入
`CREATE_NO_WINDOW`（一处 patch `Popen.__init__` 覆盖 6 处签名调用点 + PyExecJS）。
托盘「退出」会同时树杀独立窗口进程，uvicorn 停机带 5s 超时兜底。

## 什么不该进包（2026-09-26 v1.0.1 梳理）

**结论：进包内容由白名单决定**（`SpiderXHS.spec` 的 `datas` + PyInstaller 依赖分析），
在此之上叠三道拦截 —— 不是「尽量别打包」，而是"构建当场判红"。

| 不该进包 | 为什么 | 拦在哪 |
|---|---|---|
| `docs/`、`tests/`、`pytest`、tkinter、IPython、notebook | 文档与测试链，运行期零使用（pytest 约 10MB+） | spec `excludes` + 构建自检（`tests` / `pytest` / `_pytest` / `docs`） |
| `static/`（v1 期界面截图）、`author/`（Logo / 收款码） | 与运行时无关的展示资产；`static/` 已于 2026-09-26 从仓库移除 | 构建自检 `_internal\static` / `_internal\author` |
| `data/`（开发库与历史备份）、`backend/app/storage/` | 运行期数据，安装后在用户目录生成；把开发库打进去 = 泄露真实数据 | 构建自检 `data` / `_internal\data`；两者都不在 `datas` 白名单 |
| `.git/`、`.workbuddy/` | 版本历史、本地规范与日志 | 构建自检 |
| `frontend/src/`、`frontend/node_modules/` | 前端源码与构建期依赖，只发 `frontend/dist` 产物 | 构建自检（`frontend\src` / `frontend\node_modules`） |
| crypto-js 以外的 node 依赖 | 签名运行时只需要 crypto-js | 构建自检：安装根 `node_modules` 只允许 crypto-js |
| cv2 的 `data/`（haarcascade 等模型，约 10MB） | 本仓 cv2 只用 `imdecode` / `imencode` / `VideoCapture`，无级联分类器用法 | `build.ps1` 第 7a 步直接删 |
| 旧安装包 `spider-xhs-setup-*.exe` | 上一版产物（约 90MB），不是输入 | 安装脚本 Source 只指向 `dist\SpiderXHS\*` |

**哪些看着大却不能动**：

| 依赖 | 体积 | 为什么必须带 |
|---|---|---|
| `cv2` | **112MB** | 发布链路要用：`apis/xhs_creator_apis.py` 用 `imdecode` 取图片宽高、`VideoCapture` 取视频时长与封面帧（`upload_media` → 发布/自动运营都走它） |
| `runtime/node/node.exe` | 84MB | 签名 JS 的唯一执行器（`xhs_utils/**/js`） |
| `PIL` / `numpy` / `cryptography` / `curl_cffi` | 13M / 27M / 9.5M / 3.8M | 图片处理（`image_util`） / cv2 依赖 / Fernet 加密 Cookie 与 Key / HTTP 指纹请求 |

> 每次构建尾部会自动打印 `_internal` 体积 **top 15** 并跑一遍禁止项自检 ——
> 有新东西混进包，看那张表和 `[LEAK]` 行就够了，不用人肉翻目录。

## 安装后布局（全部在用户选择的安装目录）

```
<安装目录>\
  SpiderXHS.exe        启动器（无窗口，托盘图标：打开界面/退出）
  _internal\           Python 运行时 + 依赖 + 后端代码 + 签名 JS
  node_modules\        crypto-js（安装根：文件式/管道式两条 require 路都覆盖）
  runtime\node\        node.exe
  frontend\dist\       前端生产构建
  config\default.yaml  可编辑配置
  config\initial_data\ 应用初始化数据（定时任务配置，设计 15 §4.5）
  data\                spider_xhs.db、secret.key
  backend\app\storage\ media、exports、tmp_note_publish
  logs\launcher.log    启动器 + uvicorn 日志
```

## 卸载语义

卸载**只删程序文件，数据永远保留**（`data/`、`storage/`、`logs/` 都是运行期生成、
不在卸载记录里）。要彻底清除就删整个安装目录。早期「弹窗问是否删数据」的设计已移除：
静默卸载时 [Code] 里的 MsgBox 不会被自动应答，会把卸载器堵死。

## 发版自检清单

> 2026-09-29 起，下面 5 条已由 `packaging/release.ps1` 的阶段 2–4 **自动执行**
> （构建尾部自检 + `smoke-test.ps1` 覆盖）。只有在 `-SkipSmoke` 或冒烟被跳过时，
> 才需要人工照此复核一遍。

- [ ] `packaging/build.ps1 -Version <x.y.z>` 全绿；
- [ ] 静默安装到测试目录，`/api/health` 200、`/` 出 index.html；
- [ ] 安装目录下 `data/`、`storage/`、`logs/` 均生成；
- [ ] 占住 8000 后启动，实例自动改端口且浏览器开到实际端口；
- [ ] 卸载：程序文件清掉，`data/` 按语义保留（要全删就删安装目录）。

## 注意

- **先清 `packaging/dist`、`packaging/build` 再打包**：PyInstaller 起手要删旧的
  `dist\SpiderXHS`（近千个文件），在带删除保护的受控环境里这一步会被拦 ⇒ 构建直接挂
  （v1.0.1 首轮就是这样失败的）。没有旧产物时它不会触发删除，一次过。
- **`cv2/data` 裁剪失败只告警、不中断**（2026-09-26 起）：那 10MB 是体积优化不是正确性判据，
  删不掉就让包大一点，不该让整包构建失败。日志里认 `[WARN] cv2/data trim failed` 那行。
- **⚠️ 跑完 `smoke-test.ps1` 必须先清掉它留下的静默安装残留，再手工装包验证**
  （2026-09-26 实测踩到）：冒烟是 `/VERYSILENT /DIR=.workbuddy\tmp\smoke-install` 装的，
  中途失败会 `exit` 掉、卸载步骤不执行 ⇒ **卸载表里留下一条同 `AppId` 的条目**，指向那个
  随后被清空的目录。之后手工双击安装器，Inno 判定「已安装过」就会**整页跳过
  「选择目标位置」**（`UsePreviousAppDir=yes` 的既定行为），看起来像「包坏了不能选目录」——
  其实是环境残留，安装包本身没问题。清法：删 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall`
  下 `{8F2A7C4E-…}_is1` 那一条（或跑一次残留目录里的 `unins000.exe`）。
- **编辑 `build.ps1` / `installer.iss` 后必须确认文件仍带 UTF-8 BOM**（PowerShell 5.1 与
  ISCC 对无 BOM 的 UTF-8 文件会按 GBK 误读中文，导致 tokenizer 行号错位、解析报错）。
  用 VS Code 等编辑器保存时选「带 BOM 的 UTF-8」。
- **`config/initial_data/` 必须随包**（`build.ps1` 组装 + `SpiderXHS.spec` 兜底两处都收了它，
  构建尾部自检有正向断言）：它决定**装完任务中心有没有那批定时任务**。改任务清单改这个文件，
  不要改迁移、也不要写死在代码里（设计 15 §4.5）。
- **测试代码一律不进包**：`tests/`、`pytest`、`_pytest` 都在构建自检的禁止项里，命中即**构建失败**
  （不是只印红字）；新增测试文件时无需额外配置，但不要把测试素材放进 `backend/` 或 `config/`。
- **不要**手改 `packaging/dist/` 与 `packaging/build/`（构建产物，`-Clean` 会删）；
- `node-runtime/` 是 npm 缓存目录，删了下次自动重装；
- `.venv-build/` 只在打包机本地存在，可随时删除；
- 未签名 exe 首次运行有 Windows SmartScreen 提示（「更多信息 → 仍要运行」），
  代码签名列为后续可选项。
