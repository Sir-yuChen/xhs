# 设计变更 · LLM 网关服务商种子数据 + 默认业务点映射

> **状态**：🟡 **设计锁定**（2026-10-13 立；R4-A 产出，实现见 R4-B）
> **性质**：**种子数据级变更**（新增服务商种子文件 + 注册期播种 + 默认业务点映射）
> **上游**：2026-10-13 用户口径 ——
> *「大模型网关中的服务商中，默认将 deepseek、千问、豆包、火山、智谱 等等 —— 调研一下
> 将国内常见的 6 个服务商作为种子数据配置到服务列表中，用户安装登录后只需要配置 key
> 并检测后即可使用；另外小红书页的大模型配置页，也需要默认指定一个服务商的模型
> （你来调用后配置一下），这些都作为种子配置数据。」*
> **本文档是代码变更的发起件**（AGENTS.md §3.1「文档先行」）。

---

## 0. 一句话

新用户注册 ⇒ 自动播种 **6 家国内主流 OpenAI 兼容服务商**（不含 key）+ 小红书各业务点的
**默认服务商/模型映射**；用户登录后只需给服务商**填 key + 点检测**即可用，不必手工建服务商。

---

## 1. 现状（2026-10-13 实测）

| 件 | 现状 |
|---|---|
| 服务商表 | `llm_gateway_configs`（唯一键 `(user_id, name)` + `(user_id, provider_key)`）；`provider_key` 由 `api/llm_gateway.py::_generate_provider_key` 生成 |
| 模型表 | `llm_gateway_models`（`model_name` / `model_type` / `platform`(JSON 多值) / `enabled` / `model_key`） |
| 业务点映射 | `llm_business_providers`（唯一键 `(user_id, platform, business_key)`；`provider_key` + `model_name` 均必填） |
| 已有种子 | `seed_builtin_jobs_for_user` / `seed_settings_for_user` / `seed_builtin_prompt_templates` —— 都在 `api/auth.py` 注册流程播种；启动播种只覆盖「启动时已存在的用户」 |
| 已有 initial_data | `scheduled_jobs.json` / `system_settings.json` / `prompt_templates.json` / `llm_gateway_alerts.json` |
| **缺口** | **新用户注册后服务商列表为空** —— 用户必须手工逐个新建 6 家服务商 + 逐个登记模型，才谈得上配 key 使用 |

⚠️ **`resolve_business_provider` 映射缺失不回落**（设计 17 §13.2.3）：业务点没配映射就是
抛 `LLM_BUSINESS_PROVIDER_NOT_CONFIGURED`，**不会**自动指向任何服务商 ⇒ 默认映射必须**显式写**。

---

## 2. 选定的 6 家服务商（调研结论）

用户原话列了「deepseek、千问、豆包、火山、智谱 等等」—— 其中 **豆包 = 火山方舟的模型**
（豆包是字节的模型品牌，火山方舟/Ark 是承载它的平台）⇒ 二者合一为「火山方舟」一家，
凑齐 6 家不重复的服务商。另补国内主流且 OpenAI 兼容的两家：月之暗面 Kimi、MiniMax。

| # | 名称（种子 `name`） | OpenAI 兼容 base_url | 代表模型（种子登记） | 依据 |
|---|---|---|---|---|
| 1 | DeepSeek | `https://api.deepseek.com/v1` | deepseek-chat / deepseek-reasoner（text） | https://api-docs.deepseek.com/ |
| 2 | 阿里通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-plus / qwen-max（text）、qwen-vl-max（multimodal）、wanx2.1-t2i-turbo（image） | 阿里云百炼 OpenAI 兼容文档 |
| 3 | 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | doubao-seed-1-6-250615（multimodal）、doubao-seed-1-6-flash-250615（text）、doubao-seedream-3-0-t2i-250415（image） | 火山方舟快速入门 |
| 4 | 智谱GLM | `https://open.bigmodel.cn/api/paas/v4` | glm-4-plus / glm-4-flash（text）、glm-4v-plus（multimodal）、cogview-3-plus（image） | 智谱 BigModel OpenAI 兼容文档 |
| 5 | 月之暗面Kimi | `https://api.moonshot.cn/v1` | moonshot-v1-8k / -32k / -128k（text） | Moonshot 平台文档（国内站） |
| 6 | MiniMax | `https://api.minimaxi.com/v1` | MiniMax-Text-01 / abab6.5s-chat（text） | MiniMax 国内站 OpenAI 兼容文档 |

### 2.1 调研要点（与草稿的差异，均为**实测订正**）

- ⚠️ **MiniMax base_url 订正**：初稿写 `https://api.minimax.chat/v1` —— 该域名**已不是**国内
  官方 OpenAI 兼容入口。实测官方文档给的是 `https://api.minimaxi.com/v1`（国内站）/ `api.minimax.io`（国际站）。
  ⇒ 种子改用 `api.minimaxi.com/v1`。
- **DeepSeek `base_url`**：官方文档标 `https://api.deepseek.com`（`/v1` 为等价别名）；
  本项目 base_url 语义 = **模型调用地址本身**（设计 17 §7.1.0，`/v1` 由用户地址自带）
  ⇒ 种子写 `https://api.deepseek.com/v1`（与 `…/v1/chat/completions` 拼接口径一致）。
- **火山方舟模型 ID**：`doubao-seed-1-6` 系列为**多态**（支持图文理解 + 对话）⇒ `model_type=multimodal`
  （初稿标 text，按模型实际能力订正）。模型 ID 形如 `doubao-seed-1-6-250615`（带日期快照）；
  火山方舟也要求「推理接入点 ID（ep-xxx）」，用户需按控制台实际派生 —— 种子登记**公开模型 ID**，
  用户若用接入点可自行改（播种不覆盖用户改动）。
- **模型清单只登记「稳定在售」代表模型**，不是全量目录：用户点「测试连接」时后端会从
  `{base_url}/models` 拉真实目录（设计 17 §3.8），种子只是**开箱可用的起点**。

### 2.2 边界（不做）

- **不真机调用各服务商**（要 key / 要钱）—— 种子只登记「连接信息 + 模型名」，
  连通性由用户在 UI 点「测试连接」验。
- **不塞任何 key/token** —— `access_token=""`，用户登录后自填。
- **不登记视频类模型**（`generate_video` 业务点暂无默认模型，见 §4 舍弃项）。

---

## 3. 种子播种设计

### 3.1 种子文件

新增 `config/initial_data/llm_gateway_providers.json`：

```json
{
  "version": 1,
  "providers": [
    { "name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
      "doc": "https://api-docs.deepseek.com/", "remark": "…",
      "models": [ { "model_name": "deepseek-chat", "model_type": "text" }, … ] },
    …
  ]
}
```

- 读取器 `core/initial_data.py::load_initial_gateway_providers()` —— 与既有
  `load_initial_gateway_alerts` **同纪律**：文件缺失 / 坏 ⇒ 返回空结构、`warning`，**不臆造默认值**。
  逐条规范化：`name`/`base_url` 非空才收；`models` 里 `model_type` 必须 ∈ `MODEL_TYPES`，否则丢弃该行。
- ⚠️ **默认业务点映射不放在本 JSON 里**，而是 `llm_gateway_seed_service.py::DEFAULT_BUSINESS_MAP`
  的**代码常量**（与 `services/business_provider_keys.py::BUSINESS_KEYS` 同纪律）——
  映射键是「代码与数据库之间的契约」，两处各放一份就是 DEF-044 型第二真相源。
  JSON 只描述「有哪些服务商 / 模型」（数据），代码描述「哪个业务点默认指谁」（契约）。

### 3.2 播种函数 `seed_gateway_providers_for_user(db, user_id)`

位置：`backend/app/services/llm_gateway_seed_service.py`（新文件，与 monitor/usage 服务平级）。

- **幂等**：按 `(user_id, name)` / `(user_id, provider_key)` 判重 —— 已存在的服务商
  **一律跳过**（用户改过 / 删过的不还魂）。同一用户重复调用 ⇒ 0 新增。
- `provider_key` **复用** `api/llm_gateway.py::_generate_provider_key`（**不绕过**生成逻辑，
  否则与「创建服务商」端点两套 key 生成 = 两份真相源）。
- 服务商行：`access_token=""`（**不带 key**）、`enabled=True`、`healthy=False`（未探测）。
- 模型行：按种子登记，`model_key` 复用 `api/llm_gateway.py::_next_model_key` 递增逻辑，
  `platform` 落 `["xhs"]`（一期只服务小红书）。
- **失败只 warning 不阻塞注册**（与既有三个种子同纪律）。
- **存量用户不主动补种**（决策）：倾向「注册时播」——避免替存量用户建一堆没配 key 的
  空服务商。若某存量用户想用种子，可在服务商页手动「新建」或后续加一次性迁移（本轮不做）。

### 3.3 播种时机

`api/auth.py` 注册流程，与既有三个种子**并列**（顺序：jobs → settings → prompt templates → **gateway providers**）。

### 3.4 打包

- `packaging/build.ps1` 的文件清单须带上 `config/initial_data/llm_gateway_providers.json`
  （历史上漏带过 `system_settings` 种子，有教训）。
- `packaging/smoke-test.ps1` 补一条断言：包内该种子文件存在且 `providers` 长度 ≥ 1。

---

## 4. 默认业务点映射（小红书模型配置页）

### 4.1 决策

用户口径「小红书页的大模型配置页，也需要默认指定一个服务商的模型」
⇒ 在**播种服务商之后**，为 `platform=xhs` 的各业务点写默认映射：

| 业务点 | kind | 默认服务商 | 默认模型 |
|---|---|---|---|
| generate_note / rewrite_note / generate_title / generate_tags / polish_text / polish_product_title / generate_selling_point | text | DeepSeek | deepseek-chat |
| generate_cover / generate_image / generate_product_detail_image | image | 阿里通义千问 | wanx2.1-t2i-turbo |
| describe_image | image | 阿里通义千问 | qwen-vl-max |

- **只在该业务点尚无映射时写**（唯一键 `(user_id, platform, business_key)`）——
  用户改过 / 删过的不覆盖。
- **kind 必须等于所选模型行的 `model_type`**（设计 17 §13.2.9.6 类型匹配校验）
  ⇒ 播种时按 `MODEL_TYPE_IMAGE` 的 kind 挑图像模型、`MODEL_TYPE_TEXT` 挑文本模型；
  种子里配错类型 ⇒ 该条**跳过不写**（不写一条会 400 的映射）。
- **删除的业务点初始化映射不写入**，只写种子 `business_defaults` 显式列出的那些。
  （`generate_video` 视频类暂无默认 —— 文本/图像种子可覆盖前 11 个业务点，
  视频类留空由用户自配，如实不编。）
- **没配 key 时检测失败不阻塞**：映射写入 ≠ 可用；用户点检测失败只是提示「该服务商未配 key / 不可达」，
  业务调用时才报 `resolve_business_provider` 的结构化错。**播种阶段不发任何网络请求**。

### 4.2 与「未配映射不回落」的关系

默认映射**消除了**新用户「业务点没配 ⇒ 一调用就报未配置」的首次体验缺口，
但**不改变**「未配不回落」这条纪律：映射是**显式写入的行**，不是代码里的兜底。
用户删掉某行映射后，该业务点回到「未配置」错误 —— 与纪律一致。

---

## 4.3 「使用平台」列的口径修正（2026-10-14）

### 4.3.1 缺陷（用户报的原话）

> 「大模型服务商的种子数据有问题，**模型没有在平台被配置使用，但是使用平台列依旧被设置成了小红书**」
> 「**除非默认被配置到了业务点使用才应配置正确的使用列**」

### 4.3.2 根因

`llm_gateway_seed_service.seed_gateway_provider_for_user` 在写模型行时**无条件**填
`platform = {"xhs": 1}`。而这一列的权威语义（设计 17 §3.x / v2.28 §13.2.9.7c）是：

> **`platform` = 本模型真的被哪些平台的业务点使用了**，值是「该平台下指向该模型的
> **业务点个数**」—— 它是**状态**，不是「预设」。

⇒ 后果：用户**刚注册、一个业务点都没配**，模型列表就显示「使用平台 = 小红书」；
而且因为它成了已有事实，后续 `merge_platforms` 会把它当基数继续累积。

同一份代码里另外两条路径本来就是对的（**只有这条错了**）：

| 路径 | 取值 |
|---|---|
| 用户手建模型（`api/llm_gateway.py` 创建服务商） | `item.platform` —— 用户显式选的平台（可空） |
| 同步模型目录（`sync-models`） | 只在 `platform` 有值时才写 / 并入 |
| **种子播种（本条）** | ~~无条件 `{"xhs": 1}`~~ ⇒ **改为空** |

### 4.3.3 修法（两半，缺一不可）

1. **播种模型时 `platform` 留空** —— 种子只**登记条目**，不断言任何平台。
2. **业务点映射播种时回写 `platform`** —— 这是「真的被用了」的**唯一事实来源**：
   每写一条默认映射，就把该模型的 `platform` 里 `xhs` 计数 **+1**
   （走公共层的 `bump_platform` / `parse_platforms` / `dump_platforms`）。

⇒ 播种完成后的正确状态：**只有 `DeepSeek/deepseek-chat` 与
`阿里通义千问/wanx2.1-t2i-turbo` 的「使用平台」是小红书**（前者被 7 个文本业务点用、
后者被 4 个图片业务点用 —— 计数即业务点数），其余 16 个模型**空白**。

⚠️ 只做第 1 半会让「被默认映射引用的模型」也显示空白（该写的没写）；
只做第 2 半则治不了根（播种那一步仍然在说假话）。

### 4.3.4 顺带修掉的依赖方向问题

`parse_platforms` / `dump_platforms` / `bump_platform` / `merge_platforms`
原先住在 `api/llm_gateway.py`，于是 `services/llm_gateway_seed_service.py`
为了写一个模型行**反向 import API 层**。本次下沉到
**`services/llm_platforms.py`**（单一实现），API 层保留同名 re-export
（含旧的 `_` 前缀名）⇒ 既有调用点、迁移脚本、测试都不用改。

### 4.3.5 守卫

* `test_seeded_models_do_not_claim_a_platform` —— 播种后模型 `platform` 必须**全空**；
* `test_business_map_seed_writes_the_platform_of_the_models_it_uses` ——
  **反向**：被映射引用的模型计数 == 业务点数、未被引用的仍空、且**重复播种不累加**
  （计数是状态不是事件）。
* 另有 `test_scheduler_sync` 的 lifespan 桩清单守卫（见 §4.3.6）。

### 4.3.6 同轮修掉的三个「守卫与实现脱节」

这三条都是**全量回归报出来的既有红**（非本轮功能引入），根因同属一类 ——
**守卫还在按旧口径判，而实现已经变了**：

| 现象 | 根因 | 修法 |
|---|---|---|
| `test_scheduler_sync` 的桩清单守卫报「`seed_gateway_providers_for_all_users` 没被桩掉」 | 10-13 加播种时把它写成 **`lifespan` 内的函数级 import** ⇒ `main` 模块**没有这个属性**，`monkeypatch.setattr(main_module, …)` 打不上（AttributeError）；而读 AST 的守卫**照样绿**（桩清单的真相源是源码文本，能力却是运行时属性） | 提到**模块级导入**（与另两条种子同形），并补桩 |
| `test_filter_cache` 3 条 | 「无流量」筛选随 `55e0924`（数据中心独立成页）迁走了，但 `filter-cache.ts` 的 `myNotes.noTrafficThresholds` / `noTrafficQuery` **两个死键没删**，`persisted_filters.cjs` 的页签期望也还留着已删的 `goods` / `normal` | 删死键 + 同步守卫期望（**删掉不存在的期望，不是放宽判据**） |
| `test_note_sync::test_ark_note_category_refreshes_on_existing_row` | 统计行 upsert 键含 `stat_date`，生产取 `shanghai_now()`，而**夹具写死 `2026-10-12`** ⇒ 隔天不再命中、服务插了第二行 ⇒ `.one()` 抛 `MultipleResultsFound` | 夹具改用 `shanghai_now()` **同源**取当天；断言改 `.all()` + 显式「只有一行」，把「多出一行」暴露成业务断言而非 SQLAlchemy 异常 |

⚠️ 教训：**「夹具/守卫写死了一个会过期的值」是静默变红的常见形态** ——
写死日期、写死计数、写死「新用户什么都没有」这类前提，都会在某天或某次产品决策后
变成假红，而报错信息往往指向别处（SQLAlchemy 异常、AttributeError）。

## 5. 数字变化登记

| 项 | 变化 |
|---|---|
| 数据表 | **不变**（无新表、无新列；纯种子数据） |
| 端点 / path | **不变**（复用既有创建服务商 / 模型 / 业务点映射端点；播种走注册流程） |
| 请求函数 | **不变**（无新前端请求） |
| initial_data 文件 | **+1**（`llm_gateway_providers.json`） |
| 迁移 | **无**（种子在注册时播，不改存量数据） |

---

## 6. 验收判据（给用户过目）

1. 新注册用户 ⇒ 服务商列表**已有 6 家**（DeepSeek / 阿里通义千问 / 火山方舟 / 智谱GLM /
   月之暗面Kimi / MiniMax），均**不含 key**。
2. 小红书模型配置页各业务点**已有默认服务商 + 模型**（文本类 → DeepSeek，图像类 → 通义千问）。
3. 用户给某服务商填 key + 点检测 ⇒ 即可用（种子不阻塞检测失败提示）。
4. 重复播种 / 用户改过服务商名或映射 ⇒ **不覆盖**。
5. `pytest`（播种幂等 / 不覆盖 / 坏种子 / 唯一键 / 映射类型匹配）+ 打包静态断言全绿。
