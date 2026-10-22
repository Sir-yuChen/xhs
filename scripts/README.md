# scripts/ —— 本地运维脚本

不是业务代码，是**让服务在这台机器上稳定跑起来**的东西。
之所以放进仓库（而不是 `.hermes/`）：`.hermes/` 被 `.gitignore` 整体忽略，
放那里的脚本换机器就没了，而这些恰恰是最需要复用、最容易忘记重建的部分。

| 脚本 | 作用 |
|---|---|
| `run-app.py` | 常驻 launcher：监督后端 uvicorn(8000) 与前端 vite(5173)，子进程崩了自动重启 |
| `register-task.ps1` | 把 launcher 注册成 Windows 计划任务，登录自启 + 每 5 分钟兜底 |

## 日常操作

```powershell
# 重建计划任务（换机器 / 重装系统后执行一次；普通权限即可）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1

# 删除计划任务
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Unregister

# 手工前台启动（不经过计划任务，调试用）
"C:\Users\admin\AppData\Local\Programs\Python\Python312\pythonw.exe" scripts\run-app.py
```

日志：`.hermes/logs/app.log`（launcher + 后端）、`.hermes/logs/vite.log`。

## 为什么是「计划任务 + launcher」两层

- **计划任务**：登录后自动起、且每 5 分钟兜底一次 —— 解决「launcher 被系统回收 /
  被杀之后没人再拉起来」。
- **launcher**：管子进程死活 —— 解决「uvicorn 崩了不会自己回来」。
- **单实例锁**（launcher 内的 `msvcrt.locking`）：兜底触发会反复命中，若允许并行
  就会起两个 launcher 抢 8000，抢不到的那个每 5 秒崩一次、把日志刷爆。

⚠️ 不要用 `main.py --with-frontend` 当常驻服务：它是一次性阻塞、无监督循环，
父进程一退 uvicorn/vite 立刻变孤儿（占着端口但没有父进程管）。

## 两个环境坑（都实际踩过）

**① `.ps1` 必须带 UTF-8 BOM。**
PowerShell 5.1 读无 BOM 的 `.ps1` 会按系统 ANSI（中文机器上是 GBK）解码，
中文注释里的全角标点被拆成非法字节 ⇒ 报
`TerminatorExpectedAtEndOfString: 字符串缺少终止符`。
新建/改写这类脚本时用 `encoding="utf-8-sig"` 写，或写完补 `\xef\xbb\xbf`。

**② `.ps1` 的续行反引号后面不能有空格。**
`` ` `` 必须是该行**最后一个字符**，否则续行失效、整句被截断。
