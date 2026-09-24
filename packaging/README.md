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

## 流程（脚本自动完成）

1. 前置检查：Python 3.12（可用 `SPIDER_XHS_PYTHON` 指定）、node/npm、Inno Setup（ISCC）；
2. 派生构建 venv 到 `packaging/.venv-build`（隔离，不污染系统 Python）；
3. `npm ci && npm run build` → `frontend/dist`；
4. 生成 `version_info.txt`（exe 版本信息）与 `assets/app.ico`（源自 `frontend/public/logo.jpg`）；
5. 暂存 node 运行时：`node.exe` + 仅 `crypto-js` 一棵 `node_modules`；
6. PyInstaller onedir → `packaging/dist/SpiderXHS/`；
7. 组装：`frontend/dist`、`config/default.yaml`、`runtime/node`；
8. ISCC → `spider-xhs-setup-<版本>.exe`（项目根目录）。

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

- [ ] `packaging/build.ps1 -Version <x.y.z>` 全绿；
- [ ] 静默安装到测试目录，`/api/health` 200、`/` 出 index.html；
- [ ] 安装目录下 `data/`、`storage/`、`logs/` 均生成；
- [ ] 占住 8000 后启动，实例自动改端口且浏览器开到实际端口；
- [ ] 卸载：程序文件清掉，`data/` 按语义保留（要全删就删安装目录）。

## 注意

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
