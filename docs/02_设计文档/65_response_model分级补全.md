# 设计 65 · response_model 分级补全（A6 续）

> 用户口径（2026-10-25）：「**你按照级别分区，在后续的开发过程中慢慢补全**」。
>
> 本文档取代设计 61 §A6 的「一次性铺满 413 个端点」做法 —— 那个做法预估 8–12 小时，
> 且后 300 个大多是低频端点，收益递减。改为**按级别分区、随开发顺手补**。

## §1 现状（2026-10-25 实测）

| 项 | 值 |
|---|---|
| 端点总数（5 种方法） | **413** |
| 已声明 response_model | **37**（9.0%） |
| 守卫基线 | `RESPONSE_MODEL_BASELINE = 37`（只升不降） |

**为什么要做**：端点没有 `response_model` ⇒ OpenAPI 的 200 schema 为空 ⇒ 前端
300+ 个请求函数的返回类型全靠手写、零校验。后端改字段名，前端要到运行时才炸，
甚至不炸（静默取到 `undefined`）。

## §2 分区标准（唯一判据，不看文件大小）

判定顺序：**先看「改错了会不会静默丢数据」→ 再看「前端消费频率」**。

| 级别 | 判据 | 处理要求 |
|---|---|---|
| **L1 必须做** | ① 写端点（POST/PUT/PATCH/DELETE）且返回**业务对象**（前端据它渲染/跳转）；② 认证、权限、配置这类**改错了会锁死人**的读端点 | **随开发立即补**，不排期 |
| **L2 应该做** | 前端高频消费的读端点：列表/详情/统计（内容库、账号、订单、商品、数据洞察） | **按域补齐**，每域一批一提交 |
| **L3 顺手做** | 低频读端点；返回结构简单（扁平 5 字段内） | 改到那个文件时顺手补 |
| **L4 明确不做** | ① **平台原始字段透传**（`raw_json` / `_normalize_*` 的产出）；② 纯动作回执（`{"ok": true}`）且前端不读返回；③ 形状随平台改动而变的探测端点 | 在代码里写**为什么不接**（见 §4） |

## §3 当前分区结果

### ✅ L1 已完成（本批之前）

| 域 | 端点 |
|---|---|
| 配置 | `GET /settings` · `/settings/effective` · `/settings/{key}` |
| 系统运维 | `GET /system/deps` · `/deps/progress` · `/system/version` · `/update/check` · `POST download`/`install` |
| 通知 | `GET /notifications` · `/unread-count` · `POST {id}/read` · `/read-all` |
| 业务日志 | `GET /system/logs/business` |
| 清理 | `GET /pc/users/no-traffic/cleanup/active` · `/{task_id}` · `/cleanup-logs` |
| 调度 | `GET /schedule/jobs` · `/jobs/{id}` · `/targets` · `/logs` |

### 🔜 L2 待补（按域，每域一批）

| 优先级 | 域 | 端点数 | 理由 |
|---|---|---|---|
| 1 | `accounts.py` | 12 | 账号矩阵是入口页，前端消费最多 |
| 2 | `notes.py` | 27 | 内容库/我的笔记，核心读链路 |
| 3 | `analytics.py` | 13 | 数据洞察 |
| 4 | `products` 相关（`product_notes` 14） | 14 | 商品线 |
| 5 | `qianfan` 订单/商品（`goods` 25 · `publish` 14） | 39 | 店铺线高频 |

### ⏸ L3 顺手做

`llm_gateway_monitor`(11) · `tasks` / `queue` 类 · `topics`(4) · `crawl`(4) ·
`platforms/registry`(1) · `schedule` 剩余写端点 …

### 🚫 L4 明确不做（附理由）

| 文件 | 端点数 | 不做理由 |
|---|---|---|
| `pc.py` 的采集类 | ~15 | 产出是 `_normalize_*()` 的平台原始归一化结果（搜索结果/笔记详情/评论树），字段随平台变。**写死模型 = 把「前端取到 undefined」换成「后端 500」**，是拿静默问题换硬故障 |
| `violation.py` | 16 | 整片透传千帆后端原始结构（已验证「一比一复刻」口径），我方不定义契约 |
| `picture_space.py` | 16 | 同上，且含平台文件树 |

> ⚠️ L4 不是「永远不做」。若某个 L4 端点的**前端消费点**从「原样透传」变成
> 「我方据字段做判断」，它就升级成 L2 —— 判据是「我方代码是否读了这个字段」。

## §4 代码里的标注要求

L4 不接的端点，必须在函数 docstring 里写一行：

```
⚠️ L4 不接 response_model：<理由>（设计 65 §3）
```

原因：半年后有人看到「这个端点没模型」，会以为是漏了而顺手补 —— 补完就埋了个雷
（平台改字段 ⇒ 后端 500）。**把「不做」也写成显式结论**，与文档口径一致。

## §5 施工要求（每一批）

1. 模型字段**逐字段抄自实现**（`_serialize_*` / `return {...}`），不凭设计文档猜；
2. 数据库**可空列**必须 `Optional[...] = None` —— 写成 `str = ""` 会在真数据上
   `ResponseValidationError`（本批实测踩过两次，见 §6）；
3. 每批跑该域的测试文件 + `test_arch_dependency_direction`；
4. 抬 `RESPONSE_MODEL_BASELINE`（只升不降）；
5. 独立提交，commit message 写明本批端点清单与实测数。

## §6 本轮实测踩到的坑（防重蹈）

| 现象 | 根因 |
|---|---|
| `UpdateInstallResponse` 字段写成 `launched` | 实际是 `launching`。按「更自然的英文」改名 ⇒ 前端 `result.launched` 恒 `undefined`，**静默失效不报错** |
| `EffectiveSettingItem` 多写了 `description`/`requires_restart` | 那些在列表端点给，级联视图只回答「值是什么、哪来的」。凭感觉补字段 = 契约与实现脱节 |
| `ScheduledJobLogItem` 的 `result` 写成 `str` | 实际是 `dict`（`log.result or {}`）。**7 条测试同时报 `ResponseValidationError`** —— 这是跑测试救回来的 |
| `ScheduledJobItem` 的 `last_status`/`misfire_policy` 等写成 `str = ""` | 数据库可空列真值是 `None` ⇒ 该校验失败。**凡可空列一律 `Optional`** |
| `notifications` 列表我以为是裸数组 | 实际是 `{total, page, page_size, items}`（`paginated_with_total`） |
| `NoTrafficCleanupLogItem` 少了 `account_name`/`thresholds`/`query` | 只看端点 return 不够，要追到 `Model.to_dict()` |

> **判据**：写完模型必须**用真实响应/真实行数据跑一遍**。本批 4 处错误里
> 只有 1 处是静态检查（OpenAPI 有无 schema）能发现的，另外 3 处必须真跑。
