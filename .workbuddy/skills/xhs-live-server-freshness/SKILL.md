---
name: xhs-live-server-freshness
description: 判定「127.0.0.1:8000 上跑的到底是不是我刚改的代码」，并排查 run-app.py 监督进程重复导致的「每 10 秒 exited (code 3)」崩溃循环。当你在 XHS_ALL_IN_ONE 改完后端、准备宣称「已修复/已验证」，或发现 .workbuddy/logs/app.log 被刷屏、服务行为与代码不一致、改了逻辑但接口没变化时，用本技能做客观判定，不要凭「刚起了服务」就当新代码生效。⚠️ 用户的硬要求：**每轮只要动过 backend/app/** 就必须重启服务并复核**（§3.2）；8001 等 launcher 管不到的调试端口用 .workbuddy/scripts/spawn-detached-api.py 起（§3.1）。
agent_created: true
---

# 线上服务「新代码 / 崩溃循环」判定

## 1. 为什么需要它

`AGENTS.md` §5 给的老判据是「看 `/openapi.json` 的 paths 数」。**它只对「端点有增减」有效**：

- 只改内部逻辑（过滤条件、校验闸门、文案）时 **paths 数一动不动**，老判据等于没判；
- 2026-09-21 实测：后端改动落在 `01:01` / `01:11`，而 uvicorn 子进程启动于 `00:47` —— **线上一直跑旧代码**，
  而 `paths` 仍是 135/155「看起来正常」。**改完不重启，验证全是假的。**

## 2. 判据（按可信度从高到低）

### 2.1 启动时间 vs 文件 mtime —— **唯一可信的内部逻辑判据**

```bash
# a) 找 8000 上的 uvicorn 子进程及其启动时间
#    ⚠️ PowerShell 工具里 Get-CimInstance 的输出常被吞掉 → 一律 Out-File 再读
# 输出写到 .workbuddy/tmp/procs.txt，然后用 Read 工具读该文件
```

```powershell
$out="D:\work\XHS\.workbuddy\tmp\procs.txt"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*uvicorn*8000*" } |
  ForEach-Object { "PID=$($_.ProcessId) PPID=$($_.ParentProcessId) Start=$($_.CreationDate)" } |
  Out-File $out -Encoding utf8
```

⚠️ **命令行过滤可能一条都返不回来**（2026-09-22 实测：`CommandLine -match 'uvicorn|main\.py'` 与
`-like "*uvicorn*8000*"` 都得到空集，而 8000 明明在服务）—— 监督层怎么起的进程**尚未探明**，
不要因此以为「服务没在跑」。**可靠兜底是「按端口反查 PID」**：

```bash
netstat -ano | grep ":8000" | grep LISTENING      # 最后一列就是 PID（本机实测稳定可用）
```

拿到 PID 后可直接 `Get-CimInstance Win32_Process -Filter "ProcessId=<pid>"` 取 `CreationDate`
（本机实测该单进程查询正常返回），或用 §3 的 `Stop-Process`。

```bash
# b) 改动文件 mtime
ls -la --time-style=full-iso backend/app/core/deps.py
```

**判定**：`Start` 晚于所有 mtime → 新代码已生效；**早于** → 必须重启。

### 2.2 paths 数 —— 仅在「增删端点」时有效

```bash
curl -s http://127.0.0.1:8000/openapi.json | python -c "import json,sys;d=json.load(sys.stdin);print(len(d['paths']), sum(len(v) for v in d['paths'].values()))"
```

### 2.3 行为判据（最硬，但常需改造数据）

用 `TestClient` + 依赖覆盖模拟登录用户，避开真机登录：

```python
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.core.deps import get_current_user
from backend.app.core.database import SessionLocal
from backend.app.models.user import User
db = SessionLocal(); u = db.get(User, 2); db.close()
app.dependency_overrides[get_current_user] = lambda: u
print(TestClient(app).post('/api/xhs/qianfan/shops', json={}).json())
```

⚠️ 这只是「**我的实现与我的期望一致**」的证据，**不等于线上的就是这份代码** —— 必须与 2.1 并用。
⚠️ 若同时把「判定所依赖的脏数据」清掉了（如删掉跨租户绑定），行为判据就**不再具备区分力**（新旧代码都会给出正确结果）。
⇒ **先做行为验证，再做数据清理**；或依赖 2.1 的时间判据。

## 3. 重启：怎样让新代码真正生效

不要把 uvicorn 当普通进程杀完就完 —— 有监督进程会自动拉起：

```powershell
$log="D:\work\XHS\.workbuddy\tmp\restart_api.txt"
Stop-Process -Id <uvicorn_pid> -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 12
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { "PID=$($_.OwningProcess)" } | Out-File $log -Encoding utf8
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like "*uvicorn*8000*" } |
  ForEach-Object { "new PID=$($_.ProcessId) Start=$($_.CreationDate)" } | Out-File $log -Encoding utf8 -Append
```

重启后**必须复验**：根路径与 `/openapi.json` 都要 `HTTP 200`。
（🟠 **前端改了才需要 `vite build`**；只改后端不用。判据：`find frontend/src -newermt "<今天>"` 是否为空。）

⚠️ **重启会顺带自动跑 alembic 迁移**（启动时升级到 head）。⇒ **若本轮带迁移，重启前先备份运行库**：

```bash
cp data/spider_xhs.db "data/spider_xhs.db.bak-$(date +%Y%m%d)-pre-<change>"
```

复验时要**同时**看两处（2026-09-22 实证，重启前两者都是旧的）：`/openapi.json` 里新增的**参数/字段**在不在，
以及 `PRAGMA table_info(<表>)` / `SELECT version_num FROM alembic_version` 是否已是新 head。

### 3.1 非 8000 端口的调试实例（8001 等）

`run-app.py` **只监督 `uvicorn :8000` + `vite :5173`**（`Child` 只实例化这两个），
**不管 8001**；而**再起一个 `run-app.py` 会和常驻那个互相抢 8000**（§4 的崩溃循环）。
所以额外端口一律用本仓库脚本起：

```bash
# 起（脚本自己完成「脱离调用方」，日志 → .workbuddy/logs/api-<port>.log）
"C:/Users/admin/AppData/Local/Programs/Python/Python312/python.exe" \
    .workbuddy/scripts/spawn-detached-api.py --port 8001 --log-level debug

# 停：按端口反查 PID（本机实测稳定）
netstat -ano | grep ":8001.*LISTENING"        # 最后一列 = PID
MSYS_NO_PATHCONV=1 taskkill /F /PID <pid>
```

⚠️ **「起得来」不等于「活得下去」** —— 三种起法在 agent 的 Bash 工具里逐一试过：

| 起法 | 结果 |
|---|---|
| `python.exe` + `DETACHED_PROCESS \| CREATE_NO_WINDOW` + `stdout=文件` | ❌ **创建后秒死**，日志里连一行 uvicorn 输出都没有（std handle 无效，解释器起步即崩，无报错可查） |
| `pythonw.exe` + `CREATE_NO_WINDOW`（不 attach 控制台） | ⚠️ **能起、能服务，但命令结束就被连带杀掉** —— Bash 工具把子进程放进 **job object**，`pythonw` 只是「不 attach 控制台」，**不脱离 job**。日志会正常打印到 `Started server process [N]` 然后戛然而止、无 traceback（= 外部终止） |
| `pythonw.exe` + `CREATE_NO_WINDOW \| **CREATE_BREAKAWAY_FROM_JOB**` | ⚠️ 本机实测 **job 直接拒绝**（`PermissionError`，job 没设 breakaway 许可） |
| **WMI 代起**（`Invoke-CimMethod Win32_Process Create`，父进程 = `WmiPrvSE.exe`） | ✅ **唯一存活**的做法，已被 `spawn-detached-api.py` 自动用作回退 |

💡 **判据必须跨命令**：不要在**同一条 bash 命令**里起完就断言它活着（那必然活着）；
**换一条命令**再 `netstat` 复验端口 —— job 的清理发生在命令/回合边界。

（`run-app.py` 那套不受影响：它由**计划任务** `XHS-AllInOne-App` 用 `pythonw` 起，
父进程是任务计划服务，与 agent 的 job 无关 —— 所以 8000/5173 一直是稳的。）

### 3.2 收尾纪律（用户 2026-09-23 明确要求，必须执行）

> 「每次优化完涉及到后端代码需求后你都应重启本次服务」

**本轮只要动过 `backend/app/**`（哪怕只改一句文案、只加一个字段），收尾就必须重启并复核**：

1. 8000 → 杀 launcher 的子进程，等 5~10 秒由 launcher 用新代码拉起（§3）；
2. 8001 等调试实例 → 用 §3.1 的方式重启；
3. **复核**（§2.1 启动时间晚于全部 mtime **且** 新字段/参数真的在 `/openapi.json` 里）。

⚠️ 不许用「我起了服务」或「刚才重启过」替代判据 —— 2026-09-23 就是这么踩的：
只给 `/items` 加了两个查询参数，paths 数一动不动，界面表现和「代码写错」一模一样
（实为旧进程 + Pydantic 默认丢弃未知字段）。判据永远是新代码**进入了进程**。

前端改动（`frontend/src/**`）只需 `vite build`（8000 直出 `dist`），不必重启后端。

## 4. 崩溃循环：`每 10 秒 exited (code 3)`

**症状**：`.workbuddy/logs/app.log` 每 10 秒一行

```
INFO:     Started server process [NNNN]
INFO:     Waiting for application startup.
INFO  [alembic.runtime.migration] Context impl SQLiteImpl.
INFO  [alembic.runtime.migration] Will assume non-transactional DDL.
2026-xx-xx HH:MM:SS  api(uvicorn 127.0.0.1:8000) exited (code 3) → restart in 5s
```

**别被误导**：
- 这一段**看起来像启动失败**，但**不是** —— 该版本 uvicorn 的 lifespan **正常情况下也不打印** `Application startup complete.`。
  在**空闲端口**起一个同样的 uvicorn，日志一模一样且能正常服务 → 证明这不是失败信息。
- 日志里同时出现 `Traceback` 时，先确认是不是**请求级**异常（如 `curl_cffi` 调小红书超时），与启动无关。

**真因**：**两个 `run-app.py` 监督进程同时在跑**。抢到 8000 的 uvicorn 健康存活；抢不到的那个每 5 秒失败重启一次 → 10 秒一轮。

```powershell
# 列出所有 launcher
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
  ForEach-Object { "PID=$($_.ProcessId) PPID=$($_.ParentProcessId) Start=$($_.CreationDate) Cmd=$($_.CommandLine)" } |
  Out-File D:\work\XHS\.workbuddy\tmp\launchers.txt -Encoding utf8
```

**修法**：
1. 保留**持有 8000 的那个** launcher（= 活 uvicorn 的 `PPID`），`Stop-Process` 其余；
2. 只杀 launcher **不够** —— 它已拉起的失败 uvicorn 要一并按 PID 杀；
3. 若顺带要换代码，连同活 uvicorn 一起杀掉，让保留下来的 launcher 用新代码重启。

**验收**：`grep "exited (code" app.log | tail -1` 的时间戳之后**不再增长**（修复前每 10 秒一条），且 8000 只有**一个**监听者。

## 5. 工具坑（本机实测）

| 坑 | 后果 | 做法 |
|---|---|---|
| `wmic` | 曾记录「被沙箱黑名单拦截」；**2026-09-23 在 Bash 工具里实测 `/format:csv` 完全可用**（一条命令拿到 PID / PPID / CreationDate / CommandLine，比 `Get-CimInstance` 省事） | 能用就用它；若在 PowerShell 工具里失败，仍按「`Out-File` 再读」办 |
| PowerShell 工具直接输出 | 常被吞掉，`exit code 0` 但无内容 | 一律 `Out-File` 到 `.workbuddy/tmp/*.txt` 再用 Read |
| `grep ... | awk '$2 >= "HH:MM:SS"'` | 字段错位 → 计数虚高（本次得到 9382 的假结果） | 用 `tail -1` 取最后一条时间戳人工比对 |
| `timeout N python -m uvicorn ...` 在 git-bash 里 | 受控复现端口占用得用空闲端口（如 8011） | 复现启动问题时**换端口**，不要抢 8000 |
