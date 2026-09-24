---
name: xhs-frontend-guards
description: 给 XHS_ALL_IN_ONE 的「前端行为」加源码级守卫并做变异验证。本项目没有前端测试框架，所以凡涉及「必须做到 / 不得出现」的前端约束（不得弹全局提示、置灰而非隐藏、禁止持久化令牌、轮询间隔与结束判据、缺值渲染 `-` 而非 `0`、长文案不得换行等），一律用源码断言钉住。当你要改 `frontend/src/` 下的页面或请求层、要复核既有守卫是否真的有效、或要判断某条守卫是「有效」还是「空守卫」时，使用本技能。
agent_created: true
---

# XHS_ALL_IN_ONE 前端源码级守卫 + 变异验证

## 0. 一句话

**本项目没有前端测试框架，所以「前端必须做到的事」用源码断言钉住；而源码断言必须做变异验证才可信。**

改完跑两条：

```bash
# ① 守卫本身是绿的
py -3.12 -m pytest tests/backend/test_page_optimizations.py -q

# ② 把 bug 注回去，同一套断言必须变红（这是唯一能识破「空守卫」的手段）
py -3.12 .workbuddy/scripts/mutate_verify_t807.py
```

## 1. 守卫放哪

| 位置 | 用途 |
|---|---|
| `tests/backend/test_page_optimizations.py` | 页面级行为守卫（R7–R9、账号卡片、长文案溢出、同步入口…） |
| `tests/backend/test_login_panel_identity.py` | 登录面板身份不变式 |
| `tests/backend/test_login_state.py` | 登录态三层判据 + 扫码续期（设计 13 §18） |
| `tests/frontend/*.cjs` | 打包**真实**源码后**真实驱动**它（esbuild；`detail_message.cjs` 需要 antd 桩，`login_state.cjs` 是纯模块**不需要 jsdom**） |

⚠️ **`.cjs` 脚本本身不计入 pytest 数，但从 2026-09-21 起它们是「被 pytest 驱动的」**：
`test_page_optimizations.py::test_structured_detail_reaches_the_toast_via_a_real_bundle` 与
`test_login_state.py::TestFrontendBehaviourScripts` 会 `subprocess.run(["node", …])` 并断言
`N PASS / 0 FAIL`，**所以那份「14 PASS」是被全量回归守着的** —— 脚本改坏了会红。
（本行原先写「不计入 pytest 数」，容易被读成「没人跑」，已更正。）

源码断言靠 `_read(PATH)` 读文件再 `assert "…" in source`，随 `pytest tests/` 一起跑。

## 2. 写守卫的四条硬规则

1. **锚点必须唯一**。`assert 'account.sub_type === "creator"' in source` 是**空守卫** —— 同一个字符串在文件里出现第二次时，把处理函数的分支取反它照样是绿的（2026-09-19 实测抓到）。
   要断言**函数体内**那段（完整到能区分方向，如 `if (account.sub_type === "creator") {`），并先确认它在文件里只出现一次。
   需要限定范围时，用索引切片取函数体：
   ```python
   start = source.index("async function pollSyncTasks")
   end = source.index("async function handleDelete", start)
   body = source[start:end]
   code = re.sub(r"//[^\n]*", "", body)   # 注释里为解释坑因会提到关键字，结构判断只看代码
   ```
2. **每条断言都必须能区分「正确写法」与「错误写法」**。断言前先自问：把它改坏，这条会红吗？答不上来就是空守卫。
3. **「不要做某事」也要写成守卫**（`"EventSource" not in source`、`"加载更多" not in source`）。这类要求最容易在重构里被改回去，而且改回去不会有别的断言变红。
4. **「同一份实现」要用 `ast` 查形态，不能查值**（2026-09-21 实证）。
   `assert capabilities[...]["reason"] == _web_session_required_detail(...)["message"]`
   这类**值相等**断言守不住「第二份实现」：把整段文案**逐字抄一遍**，它**照样绿** ——
   而逐字抄写正是最阴的形态：今天一模一样，**明天改一处就漂移，那一刻没有任何断言会变红**。
   要查**赋值右侧的形态**：
   ```python
   def _reused_from(node):
       if isinstance(node, ast.Subscript):   # ← 别漏！f(...)["message"] 是 Subscript 不是 Call
           node = node.value
       if not isinstance(node, ast.Call):
           return None
       return getattr(node.func, "id", None) or getattr(node.func, "attr", None)

   assert _reused_from(assign.value) == "_web_session_required_detail"
   ```
   ⚠️ 先写成「必须是 `ast.Call`」会**误红**（真实写法是 `f(...)["message"]`）——
   收紧类断言第一版报红时，先确认**是代码错了还是断言写窄了**。

## 3. 变异验证（不允许跳过）

脚本：`.workbuddy/scripts/mutate_verify_*.py`（**常驻脚本，别当临时件清理**；每轮一个，
如 `mutate_verify_t807.py` / `mutate_verify_def058.py` / `mutate_verify_login_state.py`）。
做法：把每条变异写成一个元组，逐条写回文件→跑对应用例→必须失败→最后**复原并确认基线仍是绿的**。

```python
# (说明, 文件, 原文, 替换为, 期望变红的用例列表)
MUTATIONS = [
    ("轮询间隔被改成 3 秒", PAGE, "SYNC_POLL_INTERVAL_MS = 5000", "SYNC_POLL_INTERVAL_MS = 3000",
     ["test_accounts_sync_polling_uses_five_seconds_and_stops_on_unmount"]),
]
```

要点：
- 变异要**尽量保持语法有效**（例如把删除语句裹进 `finally` 时，要同时补上配对的 `}`），否则你可能只是在测语法错误。
- **锚点唯一性先数出现次数**（与 §2 规则 1 同源）：`target_account_id=state.target_account_id,`
  在文件里出现 2 次时，`replace(..., 1)` 会默默改错那一处 ⇒ 把**前一行注释**一起纳入锚点。
- 脚本必须**在 `finally` 里复原原文件**，并在结尾**重跑一次基线**确认自己没把文件改坏。
- 输出格式固定：`[红 ✅]` / `[还是绿的 ❌（守卫无效）]`，末尾打印 `N/N 条按预期变红`。

### ⚠️ 判定口径：**一律以 `--junitxml` 为准**（2026-09-21 实测踩坑）

**变异脚本自己也会骗人。** 第一版 `mutate_verify_login_state.py` 按
「退出码 ≠ 0 **且** 输出尾部 900 字符里有 `failed`」判红，结果把
**已经变红的 7 条注入判成「守卫是摆设」**（假阴性），报出 7/14。

真因：pytest 收尾清理系统 temp 会被沙箱的**批量删除守卫**拦下，在正常汇总行**之后**追加
`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":391,...}` ⇒
① 退出码**假性变 1**、② 汇总行被挤出「尾部 N 字符」窗口。

```python
# 唯一可信的判据
subprocess.run([PY, "-m", "pytest", *cases, "-q", "--junitxml", str(xml)], ...)
tests, failures, errors = parse(xml)          # 收集数 / 失败数 / 错误数
red = tests > 0 and (failures + errors) > 0    # 收集数=0 说明是用法/导入错误，**不算变红**
```

顺带解决第二个坑：pytest 用 **5** 表示「没收集到用例」、**4** 表示「用法错误」，两者都非 0，
只看看退出码会把它们误判成「按预期变红」。**与「校验脚本自己也会骗人」（DEF-046）同类。**

## 4. 这一套抓到过什么（证明它不是形式主义）

| 轮次 | 发现 | 说明 |
|---|---|---|
| T-807 | **1 条假绿** | 锚点在文件里出现第二次 → 对「分支取反」不敏感 |
| T-807 | **1 条真缺陷** | 轮询的「摘除账号」写在 `try/finally` 的 `finally` 里；`running` 分支是 `return`，`finally` 对 `return` 同样生效 → **第一轮轮询即停摆**（按钮仍显示「同步中」，进度行永久冻结）。已改为 `try/catch` 之后摘除，并新增守卫 `..._keeps_running_until_terminal` |
| 2026-09-21 · §18 | **1 条真缺陷（DEF-059）** | **通道判定读不懂 JSON 形态的 jar**：`channel_for_cookies()` 只按 `;`/`=` 切分，而**所有登录路径**落的 jar 是 JSON ⇒ `cookie_names()` **恒为空集** ⇒ 每份登录来的 jar 都落进 `www` 兜底 ⇒ 「一个账号两条通道」**在登录路径上从未生效**。**既有用例全部用 `k=v` 串写夹具 ⇒ 那条分支零覆盖**（「夹具少一种形态」＝同型事故） |
| 2026-09-21 · §18 | **1 条假绿（守卫太弱）** | 「用户维度原因复用同一份」原用**值相等**断言 ⇒ 逐字副本照样绿。**成对注入 ⑦（逐字）/ ⑧（漂移）** 才看出来；补 §2 规则 4 的 `ast` 形态断言 |
| 2026-09-21 · §18 | **1 条假绿（脚本自己）** | 变异脚本按「退出码 + 尾部字符串」判红 ⇒ **7 条已变红的注入被判成「守卫是摆设」**。见 §3 判定口径 |

> 一句话：**报告「守卫是摆设」之前，先确认脚本自己没瞎。**

## 5. 配套动作（缺一不可）

- **文档先行**：守卫要钉的行为，必须先在 `docs/02_设计文档/NN_*.md` 里有明文（含「实现坑」小节更好）；被守卫钉住的实现坑要写进设计与 `MEMORY.md`。
- **用例簿记**：新增守卫要在 `docs/04_测试文档/03_测试用例库.md` 有对应 `TC-XXX-NN`（并跑 §6 的自洽脚本）。
  ⚠️ **自动化用例 ≠ 用例库条目**：`pytest` 涨了 32 条时，用例库（268 条）/ 完成度（51.9%）**可以不变** ——
  别顺手把它们一起改，那会造出新的漂移。
- **台账回写**：任务卡的「实施记录」要写明守卫条数、变异验证通过数、以及有没有抓到东西。

## 6. 收尾自检

```bash
py -3.12 -m pytest tests/backend/test_page_optimizations.py -q     # 守卫绿
py -3.12 .workbuddy/scripts/mutate_verify_<本轮>.py                # N/N 变红 + 基线绿
cd frontend && node node_modules/typescript/bin/tsc --noEmit       # 类型干净
cd frontend && node node_modules/vite/bin/vite.js build            # 8000 吃的是构建产物
py -3.12 .workbuddy/skills/xhs-doc-stats-verify/verify_doc_stats.py # OVERALL: PASS（表内自洽）
py -3.12 .workbuddy/skills/xhs-doc-stats-verify/verify_doc_counts.py# OVERALL: PASS（文档↔实测）
```

⚠️ 全量回归的结果**只看 `--junitxml`，不看退出码**；**禁止 `--basetemp` 指向工作区内**（逐用例 `SystemExit: 1`）。详见 `.workbuddy/memory/MEMORY.md` §4。

⚠️ **改了 `verify_doc_*.py` 的比对逻辑，必须跑 `mutate_verify_docstats.py`（3 类全红）**；只改文档数字不必跑。

⚠️ 改完前端要**核对 8000 直出的是不是新产物**：`curl -s http://127.0.0.1:8000/ | grep -o 'assets/index-[^"]*\.js'`
必须等于 `ls frontend/dist/assets/` 里那个 hash。详见技能 `xhs-live-server-freshness`。
