# XHS_ALL_IN_ONE · 项目总纲

> **本文件是 2026-09-17 工作会话的统一汇总文档**，涵盖：项目全景梳理、无头浏览器方案的放弃决策、以及「商品笔记自动化」的完整调研与设计方案。
>
> - **生成日期**：2026-09-17
> - **性质**：项目速览 + 决策记录 + 实施方案（**不含代码实现**）
> - **取代范围**：原 `PROJECT_MAP.md`，以及 `docs/` 下的 10、12、13、14、15、16、17、18 八份文档（已全部归档至 `docs/_archive/`，见附录 D）
> - **证据分级**（贯穿全文）：
>   - `[代码]` = 本仓库源码可验证
>   - `[公开]` = 多个独立公开来源一致，但非官方文档原文
>   - `[待实测]` = 推断或单一来源，**落地前必须抓包/实测确认**
> - **重要提醒**：`docs/11_商品AI笔记自动化方案.md` 属项目原有文档，未被本次归档，但其「商品数据靠浏览器抓取或手动导入」的结论**已被本文第三部分取代**。

---

## 目录

- **第一部分 · 项目全景**（§1–§8）
- **第二部分 · 决策记录：无头浏览器方案已放弃**（§9–§11）
- **第三部分 · 商品笔记自动化**（§12–§25）
- **第四部分 · 已知缺陷与技术债**（§26–§28）
- **附录 A–D**

---

# 第一部分 · 项目全景

## 1. 定位

**小红书一站式智能运营平台**：把「数据采集 → 内容库 → AI 改写 → 图片润色 → 一键/定时发布 → 全自动运营」串成一条闭环。

对外的两个身份：

- **Web 运营平台**：给运营人员用的可视化后台（多账号矩阵 + 内容管线）
- **XHS 底层 SDK 仓库**：`apis/` + `xhs_utils/` 是逆向签名 + HTTP 接口的封装，可独立作为能力层被上层 Agent 工具链通过 skills 复用

> ⚠️ README 明确标注：仅供学习交流，禁止商业化。本文涉及的商品笔记属商业推广内容，**用途合规性需自行评估**。

**规模**：Python 约 16,920 行（`apis/` 3,841 + `xhs_utils/` 4,808 + `backend/app/` 8,271）；前端 ts/tsx 约 11,513 行 / 37 文件；`xhs_utils/**/js/` 签名算法 16 文件 / 2,174 行 / 92.8KB。

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 后端框架 | FastAPI + Uvicorn |
| ORM / 迁移 | SQLAlchemy 2.0 + Alembic（14 个版本脚本） |
| 数据库 | SQLite（默认，`data/spider_xhs.db`）/ MySQL（生产） |
| 调度 | APScheduler `BackgroundScheduler` |
| 鉴权 | 自研 JWT（HMAC-SHA256）+ PBKDF2-SHA256 密码（26 万次迭代） |
| 加密 | Fernet 对称加密（Cookie、API Key 落库前加密） |
| HTTP 客户端 | **curl_cffi**（伪装 Chrome 146 TLS/HTTP2 指纹），非 requests |
| 签名运行时 | **Node.js 子进程**执行 JS 算法（唯一算法实现源，Python 侧不重复实现） |
| 前端 | React 19 + Vite 7 + TypeScript 5.9 + antd 6 + recharts 3 |
| 拖拽 | @dnd-kit（素材排序） |
| 部署 | 多阶段 Dockerfile（Node 构建前端 → Python 运行）+ docker-compose |

## 3. 分层架构与调用约定

```
前端 SPA
   ↓ axios (/api)
FastAPI 路由层  ── 21 个 router，约 130 个端点，全部 /api 前缀
   ↓
服务层 services/（AI 改写、调度、账号、通知、限流、素材下载）
   ↓
适配层 adapters/xhs/  ← 唯一允许碰 SDK 的地方
   ↓
SDK 层 apis/ + xhs_utils/（curl_cffi 发请求，Node 子进程算签名）
   ↓
小红书（PC 站 / 创作者平台 / 蒲公英 / 千帆）
```

**硬性约定**：

| 约定 | 说明 |
|---|---|
| **`apis/` 禁止直接改** | 属底层 SDK，上层必须经 `backend/app/adapters/` 中转 |
| **时区统一** | 全项目用 `backend/app/core/time.py::shanghai_now()`，禁用裸 `datetime.now()` |
| **签名单源** | 算法只存在于 `xhs_utils/**/js/`，Python 通过 subprocess 调 Node |
| **适配层细节** | 每个 SDK 调用包在 `direct_xhs_request_env()` 上下文里，临时摘掉 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY`（加线程锁），避免本机代理污染 |

## 4. 数据模型（21 张表）

| 域 | 表 |
|---|---|
| 用户 | `users` |
| 账号矩阵 | `platform_accounts`、`account_cookie_versions`（Cookie 多版本加密存档）、`login_sessions` |
| 内容库 | `notes`、`note_assets`、`note_comments`、`tags`、`note_tags`（多对多） |
| 草稿/AI | `ai_drafts`、`draft_assets`、`ai_generated_assets`、`model_configs` |
| 发布 | `publish_jobs`、`publish_assets` |
| 自动运营 | `auto_tasks` |
| 监控 | `monitoring_targets`、`monitoring_snapshots` |
| 系统 | `tasks`（任务审计）、`notifications`、`api_logs`、`keyword_groups`、`app_migrations` |

**两个设计要点**：

1. **账号两个正交维度**（详见 §15）：`sub_type ∈ {pc, creator}` 只表达协议链路；「有没有店铺」由 `shop_account_bindings` 的存在性推导。
2. **Cookie 不覆盖写**：往 `account_cookie_versions` 追加新版本，取用时按 `created_at desc, id desc` 取最新；内容 Fernet 加密。

**项目当前没有任何商品/店铺概念** `[代码]`——`backend/app/models/` 全部 21 张表的 `__tablename__` 已核对，无 product/goods/shop/sku。

## 5. 核心链路

### 5.1 账号接入
扫码 / 手机验证码 / Cookie 导入 → `login_sessions` 轮询会话状态 → 成功后写 `platform_accounts` + 追加 `account_cookie_versions`（加密）。

### 5.2 采集入库
选账号 → PC 适配器 `search_note` / `get_note_info` → 归一化 → `notes` + `note_assets`（素材下载到 `storage/media/`）→ 内容库可打标签、导出 JSON/CSV。

### 5.3 AI 改写
内容库深拷贝 → `ai_drafts` → 调 OpenAI 兼容接口（用户自配端点）改写正文 / 生成标题 / 生成标签 / 润色 → `draft_assets` 存图，可拖拽排序。

### 5.4 发布
草稿送入 `publish_jobs` → 素材上传 Creator（`publish_assets.creator_upload_info` 存 fileIds）→ 立即发布走 `post_note`，定时发布落 `scheduled_at` 由调度器补跑。

> **发布实现的关键点** `[代码]`：图集发布不是用 SDK 的通用 `post_note`，而是 `adapters/xhs/creator_api_adapter.py:87` 的 `_post_uploaded_image_note()` 重新组装请求——本地生成 `post_loc`（空地点时**必须删掉** `post_loc` 键，否则服务端报参数错误）、逐条解析话题并拼接 `#话题[话题]#`、再手动补 `x-rap-param` 头。这些都是在浏览器实抓基础上对齐的。

### 5.5 自动运营（无人值守管线）

```
APScheduler 每 60s 扫一次 auto_tasks
  → 取 next_run_at <= now 的 active 任务
  → 随机挑一个关键词 → PC 账号搜索
  → 按互动总量选最优笔记
  → 建草稿 → AI 改写标题+正文（非致命，失败也继续）
  → Creator 账号上传图片素材
  → 建 publish_job 并直接发布
  → 无论成败都重算 next_run_at
```

支持 `manual / daily / weekly / interval` 四种调度。

### 5.6 定时调度（四条后台任务）

| Job | 频率 | 作用 |
|---|---|---|
| `due_publish_runner` | `interval_seconds`（默认 60s） | 补跑到期的定时发布任务 |
| `monitoring_refresh_runner` | 同上 | 刷新监控目标快照 |
| `auto_tasks_runner` | 60s | 自动运营管线 |
| `cookie_health_checker` | 2h | 全量账号 Cookie 巡检，新过期发通知 |

> 默认 `scheduler.enabled=false`，不开启则所有定时能力不生效。

## 6. 签名体系（本项目最有价值的部分）

详见仓库内 `SIGNATURE_ALGORITHM_DEEP_DIVE.md`，四层结构：

1. **传输层伪装** — curl_cffi `impersonate='chrome146'`、`http_version='v2tls'`、`discard_cookies=True`，且每个场景有**严格固定的 Header 顺序**契约（PC / Creator 各 8~10 套顺序常量）。
2. **认证态** — Cookie 解析（a1、web_session）/ DS 锚点 / websectiga（执行服务端下发的 JSVMP 程序）。
3. **请求签名** — `b1`（RC4 + 自定义 Base64）、MNS 签名 `x-s`/`x-t`（三档：`mns0101_` 冷启动 Base58、`mns0201_` 安全域 XXTEA、`mns0301_` 稳态 XOR）、`x-s-common`（CRC32）、追踪头。
4. **指纹/业务扩展** — `profileData`（PC 自定义 DES-ECB / Creator CryptoJS DES-ECB）、`x-rap-param`。

**工程约束（防静默降级的护栏）** `[代码]`：Python 调 Node 后对输出做**长度/前缀/正则校验**才接受——如 b1 长度 ≥500 且是 4 的倍数、x3 必须匹配档位前缀与长度、profileData 必须是 `base64-des-ecb-zero-padding-hex` 且 10000~20000 位 hex。

> **对新增业务域的意义**：`xhs_utils/xhs_pc/runtime.py:68-106` 的 `run_signer(api, data, ...)` 签名输入**只有 path + body，host 完全不参与**。这是后文「接千帆不需要重新逆向」判断的基础（§13）。

## 7. 配置与启动

**配置优先级**：`config/default.yaml` < `CONFIG_FILE` < `.env` < 环境变量。

```yaml
database.type: "sqlite"        # sqlite | mysql
security.secret_key: "dev-only-change-me"   # 生产必改
security.fernet_key: ""        # 空则用 secret_key 派生（⚠️ 见 §28）
scheduler.enabled: false       # 定时能力总开关
frontend.serve_static: false   # Docker/生产置 true
```

```bash
python main.py --with-frontend     # 后端 :8000 + 前端 :5173
docker compose up -d               # 单容器，前端静态内嵌
```

**首次启动自动初始化**：`init_db()` 跑 Alembic；若库里已有表但没有 `alembic_version`，先 `stamp head` 再 `upgrade head`。另有两段幂等数据修复（`app_migrations` 表记录）：把历史 `gpt5.4` 模型名规范为 `gpt-5.4`，以及把 SQLite 里存的 UTC 时间整体 `+8h` 迁到东八区。

## 8. 推荐阅读顺序

1. `README.md` → 产品全貌
2. `backend/app/main.py` → 21 个 router 的装配顺序即功能地图
3. `backend/app/models/__init__.py` + `models/*.py` → 21 张表的关系是理解一切业务的钥匙
4. `backend/app/adapters/xhs/*_api_adapter.py` → 看清「上层怎么调 SDK」
5. `backend/app/services/scheduler_service.py` → 唯一集中了定时/自动运营/Cookie 巡检的长文件（750 行），自动化核心
6. `xhs_utils/xhs_core/{auth,params,http}.py` + `SIGNATURE_ALGORITHM_DEEP_DIVE.md` → 签名体系（要做采集侧改动时）
7. `frontend/src/app/router.tsx` → 前端页面地图；再看 `pages/platforms/xhs/drafts-page.tsx` 学三栏编辑器组织方式

---

# 第二部分 · 决策记录：无头浏览器方案已放弃

## 9. 决策结论

> **决定：放弃「用无头浏览器替换自维护签名算法」的方案，在现有签名 SDK 基础上继续开发。**

该方案曾经过完整评估（可行性 → 改动清单 → 方案修正 → 收益利弊量化，共 4 份文档），最终未采纳。原文档已归档（附录 D），本节保留决策结论与量化依据，避免日后重复评估。

## 10. 放弃的核心理由与量化依据

**方案内容回顾**：把底层自维护的签名算法换成无头浏览器，操作层在浏览器内直接调小红书后台官方接口，数据全部来源于浏览器，从而移除算法维护。

**当初的判断（仍然成立）**：

| 维度 | 结论 |
|---|---|
| 交换性质 | 用「可预测的资源消耗」换掉「不可预测的人力返工」——方向本身是对的 |
| 最大收益 | **失效自恢复**：平台改版后由平台自己更新页面，算法返工归零。这是唯一无法用工程手段在现方案里复制的收益 |
| 真实代价 | 延迟 5–10x（现方案多付 103.7ms 本地签名；浏览器方案每请求 205ms + 首次导航 2.1s）；每 context **150–300MB**；自动运营的 60s 轮询必须重构为小时级 + 按需拉起 |
| **「删掉近万行算法」是错觉** | 实测签名层 16 文件 / 92.8KB，**真正可退出的只有 60%**（55.9KB / 1335 行：sign、mns、b1、xs_common、websectiga）；RAP 系 13.7KB **很可能保不住**（`apis/xhs_pc_apis.py:37-44` 的 RAP 白名单内含 `search/notes`、`feed`、`user_posted`——正是采集核心接口）；上传链路的独立签名 + Node 运行时**必须保留** |
| 部署栈后果 | 切换后是 **Python + Node + Chromium 三者并存，比现在更重** |
| 收益兑现是递延的 | 降级路径正式下线前，付的是双轨成本、拿的是潜在收益；中途放弃则双轨成本白付 |
| 新增单点 | 浏览器网关崩溃 → 全链路断；僵尸进程、Chromium 升级、反检测补丁都需持续投入。这属于「新建一个服务」，不是「迁移」 |

**决策的最终理由**：本项目是小时级中低频负载，收益主要集中在**登录稳定性、失效自恢复、巡检零配额**三处，而不是「删算法」；但这些收益的实现前提（多进程浏览器状态管理 + 反检测持续投入 + 新增单点运维）与「降低维护成本」的初衷相抵，且会引入一个全新的重型运行时。

**未验证的生死假设（现已无需验证，记录备查）**：方案依赖「在页面里 `fetch` 同源接口，平台拦截器会自动补签名」。原生 `fetch` 不可能被自动加请求头，除非平台 monkey-patch 了全局 `fetch`/`XMLHttpRequest`——而仓库里 RAP 的证据反向倾向「按需现算、非全局透明拦截」。该假设成立与否会决定实现形态是「在页面里发 HTTP」还是「劫持平台自己的请求器」，复杂度差一个台阶。

## 11. 该方案评估过程中的附带产出（已并入 §26）

评估过程本身有价值，查出两个真实缺陷，**与本方案无关但必须修**：

- `api/accounts.py:190-201` creator 账号健康检查**必定误判 expired**（详见 §26.1）
- `apis/xhs_pugongying_apis.py:24` 与 `xhs_utils/xhs_pugongying_util.py:26` 参数个数不匹配（详见 §26.2）

---

# 第三部分 · 商品笔记自动化

## 12. 需求与约束

**需求**（在放弃无头浏览器后继续开发）：

| # | 需求 |
|---|---|
| ① | 登录小红书店铺 |
| ② | 获取店铺列表 |
| ③ | 根据商品信息 AI 生成商品笔记 |
| ④ | 自动化发布商品笔记全流程 |

**约束**（用户明确）：

| # | 约束 | 后果 |
|---|---|---|
| C1 | **没有也无法获取企业资质 + 应用审核** | 官方开放平台通道**机制性关闭**（§13.3） |
| C2 | 店铺可能是**个人店 / 个体工商户店 / 企业店**，三种都要支持 | 需要能力驱动而非类型分支（§15） |
| C3 | 还有**非店铺账号**，也须支持 | 需要账号×店铺二维模型（§15.2） |

**需求口径**（本文采用）：

| 需求 | 采用口径 |
|---|---|
| 登录小红书店铺 | 取得店铺级访问能力，可读该店铺的商品数据 |
| 获取店铺列表 | 账号可访问的**多店铺列表** + 店铺基础信息 |
| 商品笔记 | 要能**挂载自己店铺的商品**；同时保留不挂链的种草笔记形态（§15.3） |

## 13. 四个决定性发现

### 13.1 【代码】发布体的商品槽位早已存在，而且是空的

`xhs_utils/xhs_creator_util.py:177`（图片）与 `:246`（视频）——发布体 `common` 里写着：

```python
"goods_info": {},     # 恒为空对象
```

而 `common` 的其它字段（`title` / `desc` / `hash_tag` / `post_loc` / `privacy_info` / `business_binds`）全部已填。

> **这意味着：发商品笔记很可能不需要碰千帆的签名体系，只需要把这一个字段填对。**
> 验证成本半小时（创作者后台发一篇带商品的笔记，抓包看 `goods_info` 结构），且是纯只读操作。**这是全方案投入产出比最高的一步。**

### 13.2 【代码】仓库里叫「千帆」的 SDK，打的其实是蒲公英的域名

`apis/xhs_qianfan_apis.py` 全部接口都在 `pgy.xiaohongshu.com`：

| 实际内容 | 出处 |
|---|---|
| `https://pgy.xiaohongshu.com/api/draco/distributor-square/...` | `apis/xhs_qianfan_apis.py:10` / `:30` |
| `authority: pgy.xiaohongshu.com` | `xhs_utils/xhs_qianfan_util.py:6` / `:24` |
| 方法全是 `get_user_*`（分销商维度） | `apis/xhs_qianfan_apis.py:52-116` |

平台侧准确对应关系 `[公开]`：**千帆 = `ark.xiaohongshu.com`**（商家后台，含商品管理/笔记中心/发布笔记）、蒲公英 = `pgy.*`、乘风 = `chengfeng.*`、聚光 = `ad.*`。

> `[代码]` `README.md:110` 自称的「千帆平台（分销商列表 & 商品信息）」**实为蒲公英的分销商广场 API**——它回答的是「我能分销什么货」，不是「我店铺里有什么货」或「我有哪几个店铺」。**现有 SDK 对本次需求几乎无用。**
> **建议**：新增真千帆代码前先重命名现有文件，否则会出现两个「千帆」并存。

### 13.3 【公开 + 代码】官方通道是机制性关闭，但方案不必崩塌

`open.xiaohongshu.com` 的自用型应用，其设计前提是**创建应用时绑定千帆店铺即可自动完成企业认证**——这条链路的第一跳就是企业主体。没有企业资质 → 无法创建应用 → 拿不到 `client_id` → OAuth 全流程无法启动。

> **这不是「申请难」，而是入口本身不面向无主体开发者开放。** 因此无需再评估申请周期或替代资质。

**但反过来说**：官方开放平台的权限包（公共/授权/商品/库存/素材/订单/售后）**本来就不含笔记接口** `[公开]`，即便有资质，需求④也仍要走 Creator 逆向链路。所以失去官方通道，损失的是①登录与②店铺/商品数据这两环，不是整条链路。

### 13.4 【代码】签名器与业务域解耦——接千帆的成本被大幅高估了

一度判断「逆向千帆要新写一套签名」，代码核查**推翻了这个判断**。四条证据：

| # | 证据 | 出处 |
|---|---|---|
| 1 | `run_signer(api, data, ...)` 签名输入**只有 path + body**，**host 完全不参与**，域名差异只体现在 HTTP 层 | `xhs_utils/xhs_pc/runtime.py:68-106` |
| 2 | `XHSPlatformConfig` 本就是多业务域扩展点，已有 `PC_PLATFORM_CONFIG(app_id='xhs-pc-web')` / `CREATOR_PLATFORM_CONFIG(app_id='ugc')` 两个实例 | `xhs_utils/xhs_core/auth.py:15-84` |
| 3 | `HostCookieStore` 已按 host 分域管理 Cookie，ark 域天然支持 | `xhs_utils/xhs_core/cookies.py:44-171` |
| 4 | **已有先例**：`apis/xhs_pugongying_apis.py:19-26` 直接构造 `PcDeviceProfile` 给 `pgy.xiaohongshu.com` 签名——同栈服务多域在本仓库跑通过 | `apis/xhs_pugongying_apis.py:19-26` |

> **结论：接千帆是「加配置 + 加接口」的工程，不是「重新逆向」。**
> 未知量只剩三个，全靠抓包定：`app_id`（参与 mns 签名与 DS 获取）、登录态 cookie 名、是否携带 `x-s-common` / `x-rap-param`。

## 14. 技术路线与推荐架构

### 14.1 路线对比

| | 路线 A：全逆向 ark | 路线 B：不挂商品 | **路线 C：人工登录 + 只读逆向（推荐）** |
|---|---|---|---|
| 登录店铺 | 逆向扫码/密码 | 不需要 | **人工登录后导入 Cookie** |
| 店铺列表 | 逆向 | 不需要 | 逆向（**只读**） |
| 商品列表 | 逆向 | 手动导入 CSV | 逆向（**只读**） |
| 生成笔记 | 现有 `ai_service` | 现有 `ai_service` | 现有 `ai_service` |
| 发布 | 逆向 ark 或复用 creator | 现有 creator | **现有 creator + `goods_info`** |
| 新增签名维护面 | 中（登录风控最重） | **零** | 低（只读接口） |
| 落地前提 | 店铺已存在 | 无 | 店铺已存在 |

**明确否决路线 A**：**登录逆向是全链路中成本最高、风控最严、失效最频繁的一环，而它换来的收益只是「省一次人工操作」。** 绕开它之后，`XhsPcApiAdapter(cookies)` 的构造签名可以保持不变，整条链路对现有代码的侵入只剩 `goods_info` 一处。

### 14.2 推荐架构

```
千帆店铺（人工登录 → 导出 Cookie）
   ↓ 只读逆向（签名复用现有 PC 栈 + 新增 QIANFAN_PLATFORM_CONFIG）
shops + shop_products + shop_account_bindings
   ↓
product_note_jobs ──→ ai_service（商品专用 Prompt + 合规护栏）
   ↓                        ↓
   └──────────────→ 现有 Creator 发布链路
                     common.goods_info ← 唯一对现有代码的侵入点
```

**关键设计原则**：

1. **适配层接口不变**，业务层不感知数据来自官方还是逆向
2. **上传链路不动**（`apis/xhs_creator_apis.py` 的 PyExecJS 独立签名保持现状）
3. **不新增签名维护面**
4. **`ai_drafts` / `publish_jobs` 全部复用**，只新增店铺与商品域的表

### 14.3 可能白捡的便宜（待验证）

百科称「用户可直接使用小红书账号登录千帆，无需额外注册」`[公开]`。若成立，**千帆与 creator 可能共用 `.xiaohongshu.com` 域的 `a1` / `web_session`**——那么已接入账号的 Cookie 或许能直接请求 ark 接口，**连人工导入都省了**。验证成本极低，列为阶段 0 第二项。

## 15. 账号与店铺模型

### 15.1 核心判断：能力驱动，而非类型分支

**反模式**：

```python
if shop.shop_type == "personal":
    can_attach_goods = False          # ← 这个假设可能是错的
elif shop.shop_type == "enterprise":
    can_attach_goods = True
```

**代价**：① 类型→能力的映射**目前是猜的**（外部资料在「个人店能否挂商品」上直接矛盾）；② 错误会扩散到发布校验、前端渲染、任务调度、错误提示至少 4 处；③ 平台还有旗舰店/专卖店/买手店，每新增一种都要重付一次。

**正模式**：

```
店铺（运行时探测）
   ├─ shop_type      → 仅用于展示与探测失败的兜底默认
   └─ capabilities   → 驱动一切逻辑分支
          ├─ can_attach_goods
          ├─ can_publish_note
          ├─ categories[]
          ├─ requires_deposit_before_listing
          └─ supports_sub_account
```

> **代码只读 `capabilities`，永不读 `shop_type` 来决定行为。** 这样三种店铺天然支持，且未来出现第四第五种类型，**代码零改动**。

**三类店铺的预期差异**（待验证，仅用于指导探测脚本与兜底默认值）：

| 能力项 | 个人店 | 个体工商户店 | 企业店 |
|---|---|---|---|
| 资质要求 | 身份证 + 人脸 | 个体户执照 | 企业执照 |
| 可经营类目 | **受限**（服饰/家居/文创/数码配件） | 大多数普通类目 | 全部 |
| **商品分享（挂商品）** | **未知（来源矛盾）** | 预期可 | 预期可 |
| 保证金 | 1000 元起，可先发品后缴 | 1000 元起 | 通常 20000 元，**发品前必须缴** |
| 子账号体系 | 无 | 无 / 有限 | **有**（岗位权限细分） |
| 同主体多店 | 一人一店 | 待确认 | **一执照最多 3 店** |

**两个直接影响设计的推论**：

1. **账号与店铺必须是 N:M**：企业店一执照最多 3 店 × 主账号 + 多子账号。沿用单外键会导致子账号同步时产生重复行，或丢失其他账号的访问路径 → 需要 `shop_account_bindings` 中间表。
2. **企业店有「发品前必须缴保证金」的硬前置** → 探测结果必须包含此状态，否则会在商品上不了架时给出完全错误的诊断。

### 15.2 账号也是二维的：`sub_type` 与「有没有店铺」正交

**核心判断**：

> 「有没有店铺」是**业务维度**，「账号走哪条技术链路」是**协议维度**。把前者塞进 `sub_type`，就是用业务状态去驱动技术路由。

```
sub_type   ∈ {pc, creator}      ← 只表达协议链路（决定 adapter / 域名 / cookie 域）
shop_count ∈ {0, 1, ..N}        ← 由 shop_account_bindings 推导，不是字段、不落库
```

**「非店铺账号」= `creator` 且 `shop_count = 0`。**

**为什么不能做成 `sub_type` 的第四个值**——四个代价，后两个是账号层特有的：

1. **映射是猜测**：creator 账号既可能有店也可能没店，`sub_type` 无法表达「creator 且无店」
2. **无限扩展**：还要区分「个人店账号」「企业子账号」……每加一种重付一次
3. **改绑即改路由**：`api/accounts.py:57-58` 的 `_select_adapter` 是 `creator → creator_adapter`，**否则一律 pc_adapter**。用户今天开店、明天关店，若这改动 `sub_type`，登录适配器会跟着变 → **登录态立刻失效**
4. **状态与事实不同源**：`sub_type` 是静态字段，「有没有店铺」是平台侧事实。店铺被平台关停、子账号被移除绑定时，系统会拿过期字段当真，且无机制发现

**一个让人省心的事实** `[代码]`：全量 grep 确认，**现有代码里从来不存在「账号必须绑店铺」的校验**——13 处 creator 守卫（`xhs/creator.py:79`、`publish.py:415/453`、`scheduler_service.py:124` 等）**全部只看 `sub_type`**。

> **非店铺账号一直是合法、可用的，普通笔记发布零改动。本需求是把既有能力显性化，不是新建一套机制。**

**全域组合矩阵**：

| | `shop_count = 0` | `shop_count ≥ 1` |
|---|---|---|
| **`sub_type = pc`** | 采集/巡检专用号（现状主力） | 少见 |
| **`sub_type = creator`** | **非店铺账号** | 店铺账号（发布端） |

**能力解析的唯一入口**（二元输入，因为子账号权限可能细到「能看到店铺但无权发笔记」）：

```python
def resolve_capabilities(account, shop=None) -> Caps:
    """
    shop=None  → 账号级基础能力（不涉及挂商品）
    shop=S     → 先查 binding(account, S)，再合并 S.capabilities_json
    """
```

解析顺序（短路，任一步失败即返回保守默认）：
1. binding 是否存在？否 → `can_attach_goods=False`（非店铺账号走这条）
2. 该 binding 是否有账号级限制？
3. 合并 `shop.capabilities_json`
4. `capabilities_verified_at` 是否过期（>7 天）？过期先重探

**账号级基础能力**（两种形态共有）：`can_publish_note`（creator 链路天然具备）、`can_browse_goods`（只读，无绑定要求）、`has_shop` / `shop_count`（**派生字段，建议 API 返回但不落库**——落库就变成需要同步的状态）。

### 15.3 最重要的推论：发布端与商品来源端解耦

原设计隐含假设「商品来源账号 = 发布账号」。非店铺账号打破了这个假设，正确的分工是：

```
商品库（店铺账号 A 同步）
      │
      ├─→ 挂链商品笔记  →  发布账号必须是「绑定该店铺 + can_attach_goods」的账号
      │                    账号池 1–3 个，受店铺数限制
      │
      └─→ 不挂链种草笔记 →  发布账号可以是任意 creator 账号
                           非店铺账号在此，池子可数十个
```

**因此「降级路径」需要重新定性**：它不是「无挂商品能力时的异常兜底」，而是**独立的业务模式**。

由此产生一个新增业务能力：

> **同一商品 × 多账号矩阵分发**——店铺号挂链发布 1 篇，N 个内容号各发 1 篇不挂链种草笔记。

**这也是非店铺账号值得单独立项的理由**：**店铺账号的数量被平台（一执照最多 3 店）天然锁死，内容号不受限。**

### 15.4 探测失败的降级

**不能「当作支持」，也不能「直接失败」**，而是取保守默认 + 显式标记：

```python
CONSERVATIVE_DEFAULTS = {
    "can_attach_goods": False,
    "can_publish_note": False,
    "categories": [],
    "requires_deposit_before_listing": True,
    "supports_sub_account": False,
    "shop_type": "unknown",
}
```

同时置 `capabilities_verified_at = NULL`，前端显示「能力未验证」，**只开放只读功能**（浏览商品、生成草稿），不开放发布。这样**系统在信息不全时既不会误发，也不会死锁**。

**探测时机**：绑定店铺时 / 手动触发 / 定时巡检（复用 2h）/ 发布前有效期检查（>7 天重探）。

### 15.5 前端原则：置灰 + 说明原因，不要隐藏

三种店铺共存时，用户需要理解「为什么这家不能挂商品」，隐藏起来只会被当成 bug 报过来。

```
店铺卡片
  个人店   [可挂商品 ✗ 未验证]
  个体户店 [可挂商品 ✓]
  企业店   [可挂商品 ✓] [待缴保证金]
```

## 16. 数据模型设计

```
shops
  id / seller_id / shop_id / shop_name
  shop_type                # personal | individual | enterprise | unknown
  capabilities_json        # 探测结果
  capabilities_verified_at # NULL 表示从未验证
  status                   # active | suspended | closed
  synced_at / raw_json     # raw_json 保留证据链，便于回溯与补探测
  UNIQUE (seller_id, shop_id)

shop_account_bindings      # 账号 ↔ 店铺 N:M
  id / shop_id_fk / platform_account_id_fk
  access_role              # owner | sub
  can_sync                 # 是否用这个账号的 Cookie 同步该店数据（每店同时只有一个为真）
  created_at
  UNIQUE (shop_id_fk, platform_account_id_fk)

shop_products
  id / shop_id_fk / item_id / title / price / stock / category
  images_json / detail_url / status / synced_at / raw_json
  UNIQUE (shop_id_fk, item_id)

product_note_jobs
  id / product_id_fk / creator_account_id_fk / draft_id_fk
  status / idempotency_key(unique) / goods_info_json
  publish_job_id_fk / error_message / created_at / updated_at
```

**设计要点**：

1. **不新增 `platform_accounts` 字段**：店铺关系由 `shop_account_bindings` 承载，账号表无需迁移
2. **Cookie 存账号层，能力存店铺层**：能力是店铺属性（主/子账号看到的能力相同），登录态是账号属性
3. **`can_sync` 单点设计**：每个店铺同时只有一个 `can_sync=True`，保证数据来源稳定，不会两个账号互相覆盖；其余绑定仅表示访问关系
4. **`idempotency_key` 是防重发的核心**（写操作不可逆）：`{seller_id}:{item_id}:{creator_account_id}:{素材hash}`
5. **`creator_account_id_fk` 允许指向非店铺账号**（用于种草任务）

> **若千帆确实需要独立登录态**：才引入 `sub_type="shop"`，且必须明确定义为**只读同步用途、永不承担发布**。同时必须改 `api/accounts.py:35` 的 Pydantic pattern（`^(pc|creator)$`，否则新类型直接 422）与 `scheduler_service.py:652` 的巡检适配器选择。**默认方案是不新增类型。**

## 17. 商品 → 笔记生成

### 17.1 现有能力（可复用，不必新写）`[代码]`

| 方法 | 行号 | 用途 |
|---|---|---|
| `generate_note(topic, reference, instruction)` | `ai_service.py:186` | 生成标题+正文，已内置「标题：/正文：」解析 |
| `generate_tags(title, body, count)` | `:231` | 话题标签 |
| `generate_titles(title, body, count)` | `:214` | 多标题候选 |
| `polish_text(text, instruction)` | `:249` | 润色 |
| `describe_image` | `:353` | 图片理解（可把商品图转成文字喂给生成） |
| `generate_cover` / `generate_image` | `:274` / `:287` | 封面/配图生成 |
| `rewrite_note(title, body, instruction)` | `:167` | 改写 |

`generate_note` 的 `reference` 参数正好可以装商品信息。**基础设施完备。**

### 17.2 需要新增：商品专用 Prompt

现有 Prompt 是**通用内容策划**（`ai_service.py:198`：「你是小红书内容策划，输出可发布的标题和正文。」）。商品笔记需要专用 Prompt，建议新增 `generate_product_note()`：

```
角色：小红书商品笔记写手（不是泛内容策划）
输入：商品标题/副标题、核心卖点（3-5 条）、价格与规格、适用人群/场景、商品图描述
结构：痛点切入 → 场景描述 → 卖点展开 → 使用感受 → 价格理由 → 轻行动号召
要求：标题 ≤ 20 字；正文 300-800 字；3-5 个话题标签
禁止：绝对化用语（最/第一/顶级/国家级）、
      功效宣称（治疗/根治/美白/祛痘 等医疗与化妆品功效词）、
      虚假承诺（保证/永久/100%）、
      与商品不符的描述（编造参数、编造销量）
```

**Prompt 必须版本化**（`prompt_version` 落库），否则生成质量回退时无法定位。

### 17.3 图片策略

| 方案 | 说明 |
|---|---|
| AI 润色 | `generate_image` 以商品图为参考重绘 |
| 合成图 | 商品图 + 场景底图 / 文字贴纸（`note_assets` 体系可承接） |
| 多图轮换 | 商品有多张图时，每篇笔记用不同组合 |
| 尺寸适配 | 3:4 竖版（小红书主推比例），需裁剪而非拉伸 |

> **注意**：AI 重绘有**商品失真风险**（颜色/款式改变 → 与实物不符 → 售后与违规风险）。**电商类目优先用真实商品图 + 版式差异化，AI 只做背景/文字/版式处理。**

## 18. 发布链路

### 18.1 第一个必做动作：抓 `goods_info` 的真实结构

**全方案投入产出比最高的一步，成本半小时，且不改任何现有代码。**

```
1. 浏览器登录 creator.xiaohongshu.com（一个已绑定店铺的专业号）
2. 打开发布笔记页，上传图片，填写标题正文
3. 【关键】点击「添加商品」，选择自己店铺的一个商品
4. 打开 DevTools Network，发布，找到 POST /web_api/sns/v2/note
5. 查看 request payload 的 common.goods_info → 这就是要补的结构
6. 同时记录：是否新增了其它字段（如 biz_relations / business_binds 的变化）
```

**预期产出**：`goods_info` 的 JSON schema、商品 ID 字段名、是否需要额外权限校验字段。

**为什么优先**：一次性决定需求④是「改一个字段」还是「逆向一整套千帆签名」。

### 18.2 预期改动（很小）

现有链路：`adapters/xhs/creator_api_adapter.py:87` `_post_uploaded_image_note`

```
1. 组装 post_loc（地点）
2. get_post_note_image_data(...)          ← goods_info 在这里被写死为空 {}
3. 补 topics → hash_tag + desc 追加 #话题#
4. generate_x_rap_param(...)              ← RAP 签名
5. POST {edith_url}/web_api/sns/v2/note
```

| 改动 | 位置 | 说明 |
|---|---|---|
| `get_post_note_image_data` 增加 `goods_info` 参数 | `xhs_utils/xhs_creator_util.py:119` | 默认 `{}` 保持向后兼容；同步改 `get_post_note_video_data`（`:201`） |
| adapter 透传商品信息 | `creator_api_adapter.py:118` | 从 `note_info` 取 `goods_info` |
| 发布前校验商品归属 | 新逻辑 | 确保商品属于该账号绑定的店铺，否则服务端必拒 |
| **上传链路不动** | `apis/xhs_creator_apis.py` | PyExecJS 独立签名保持现状 |

> **注意 `x-rap-param`**：Creator 走独立 RAP 模板（`rap_fingerprint_creator.json`，见 `creator_api_adapter.py:155`）。**`goods_info` 变化会改变 body → RAP hash 随之变化**（每次现算，是自动的），但**验收时务必确认签名仍然通过**。

### 18.3 前置业务条件（可能阻塞）

| 条件 | 状态 |
|---|---|
| 发布账号必须**已开通店铺并绑定专业号** | `[待实测]` |
| 店铺**已缴纳保证金 / 完成入驻** | `[公开]` |
| 商品**已在架**（非下架/审核中） | 推断 `[待实测]` |
| 商品**属于该账号绑定的店铺** | 推断 |

## 19. 自动化编排（复用为主）

现有调度体系已能覆盖绝大部分需求，**建议最大限度复用，不要另起一套**：

| 现有能力 | 出处 | 复用方式 |
|---|---|---|
| 任务类型 / 间隔 | `models/auto_task.py:23,26` | 直接复用 |
| **定时发布补跑** | `services/scheduler_service.py:181`（`run_due_publish_jobs`） | **关键**：发布排期靠它，进程崩溃不丢任务 |
| 发布状态机 | `models/publish.py`（`publish_jobs.status` / `publish_error`） | 直接复用 |
| 发布资产 | `models/publish.py:34`（`publish_assets`） | 直接复用 |

**为什么排期必须用 `scheduled_at` 而不是执行侧 sleep** `[代码]`：`scheduler_service.py:181` 的补跑机制让已排期任务在进程重启后不丢，且**天然幂等**。这是现有架构的一个优点，**继承它**。

**商品笔记特有的编排约束**：

| 约束 | 理由 |
|---|---|
| **单店铺/单账号串行** | 同账号并发发布 = 重复发布与风控双重风险 |
| **每日上限更保守** | 商品笔记是营销内容，平台对「商家批量发营销笔记」更敏感（建议 ≤5 篇/日、≥60min 间隔） |
| **同商品冷却期** | 同一商品短期内多篇 = 刷屏（建议 ≥72h） |
| **发布前复核商品状态** | 商品可能已下架/改价，生成的笔记价格与实际不符 = 违规 |
| **矩阵分发需内容变异** | 多账号发同款内容会触发去重降权，模板必须按账号维度变异 |

## 20. 合规护栏（本方案风险最高的一环）

商品笔记 = **商业推广内容**，合规要求显著高于普通种草笔记。必须内建：

| 护栏 | 说明 | 实现位置 |
|---|---|---|
| **违禁词/敏感词过滤** | 广告法绝对化用语 + 平台违禁词 + 行业特殊词 | 生成后、入库前 |
| **功效宣称拦截** | 非特殊用途化妆品不得宣称功效 | 同上 |
| **长度与格式校验** | 标题 ≤20 字、正文区间、图片数量（1-18 张）、比例 | 发布前 |
| **与商品一致性校验** | 卖点/价格必须来自 `shop_products`，**不允许 AI 编造** | 生成约束 + 入库比对 |
| **人工审核闸门** | `auto_publish=false` 默认开启，**建议长期保留** | 状态机 |
| **AIGC 标识** | 如平台要求标注 AI 生成内容，需支持 | 发布体预留字段 |

> ⚠️ **免责前提**：本项目 README 标注**仅供学习交流、禁止商业化** `[代码]`。商品笔记属商业推广，**用途合规性需自行评估**，本文只做技术分析。

## 21. 代码改动清单

### 21.1 `sub_type` 硬编码点全表（实测）

**若采纳「不新增 `sub_type`」方案，本表全部无需改动**，仅作为「若要新增类型必须同时改这些地方」的备忘。

**后端（13 处）**

| 文件:行 | 现状 | 新增类型时的后果 |
|---|---|---|
| `api/accounts.py:35` | Pydantic `pattern="^(pc\|creator)$"` | **新类型直接 422** |
| `api/accounts.py:57-58` | `_select_adapter`：`creator → creator`，**否则 → pc** | 新类型**静默走错适配器** |
| `api/accounts.py:114,120,135,173,190` | 分支判断 | 同上 |
| `api/login_sessions.py:224` | `sub_type not in {pc, creator}` | 新类型登录会话被拒 |
| `api/login_sessions.py:124,228-236,254,359,383` | 分支 | |
| `api/publish.py:415` `:453` | `!= "creator"` → 400 | **新类型无法发布** |
| `api/auto_tasks.py:90-102` | `_verify_account_ownership(..., expected_sub_type)` | 自动运营选号失败 |
| `api/platforms/xhs/creator.py:79` | `!= "creator"` → 404 | |
| `api/platforms/xhs/pc.py:287` / `crawl.py:220` | `!= "pc"` → 404 | |
| `api/notes.py:436` | `!= "pc"` → 400（仅评论抓取） | |
| `services/scheduler_service.py:124` | `!= "creator"` → RuntimeError | |
| `services/scheduler_service.py:652` | `creator → XhsCreatorLoginAdapter`，**否则 → XhsPcLoginAdapter** | 巡检走错适配器 |
| `services/monitoring_crawl_service.py:33,39` | `== "pc"` | |

**前端（7 个文件）**

| 文件:行 | 现状 |
|---|---|
| `types/index.ts:51` | `sub_type: "pc" \| "creator" \| null` |
| `lib/api.ts:770, 809, 821` | `sub_type: "pc" \| "creator"` |
| `pages/platforms/xhs/accounts-page.tsx:195` | `isCreator = account.sub_type === "creator"` |
| `pages/platforms/xhs/auto-ops-page.tsx:123-124` | 按 sub_type 分流账户池 |
| `pages/platforms/xhs/crawler-page.tsx:182, 190` | 筛 pc |
| `pages/platforms/xhs/discovery-page.tsx:98, 103` | 筛 pc |
| `pages/platforms/xhs/publish-page.tsx:104` | 筛 creator |

> ⚠️ `_select_adapter` 与 `scheduler_service.py:652` 都是**二选一的 `else` 兜底**（不是显式枚举）。这类写法在新增类型时**不会报错，只会静默走错分支**——比抛异常更危险。**改这里时务必改成显式枚举 + `raise`。**

### 21.2 本需求的实际改动（推荐方案下）

| # | 位置 | 改动 | 类型 |
|---|---|---|---|
| 1 | `xhs_utils/xhs_core/auth.py` | 新增 `QIANFAN_PLATFORM_CONFIG`（`app_id` 待抓包） | SDK |
| 2 | `xhs_utils/xhs_qianfan/`（新增） | 薄封装，复用 `xhs_pc.state.PcDeviceProfile` 与 `xhs_pc.params` | SDK |
| 3 | `apis/xhs_qianfan_shop_apis.py`（新增） | 店铺列表/详情、商品列表/详情（**只读**） | SDK |
| 4 | `backend/app/adapters/xhs/shop_api_adapter.py`（新增） | 对齐 `creator_api_adapter` 形态 | 适配层 |
| 5 | `backend/app/models/shop.py`（新增） | 4 张表 | 模型 |
| 6 | `backend/app/api/shops.py`（新增） | 店铺与商品 CRUD + 同步 | API |
| 7 | `services/`（新增） | `resolve_capabilities(account, shop=None)` 能力解析 | 服务 |
| 8 | `api/accounts.py`（`serialize_account`） | 增派生字段 `has_shop` / `shop_count` | API |
| 9 | `xhs_utils/xhs_creator_util.py:119` / `:201` | `goods_info` 参数（默认 `{}`） | SDK |
| 10 | `adapters/xhs/creator_api_adapter.py:118` | 透传 `goods_info` | 适配层 |
| 11 | `frontend/`（新增页面） | 店铺/商品列表、商品笔记生成与审核工作台 | 前端 |
| 12 | `frontend/src/types/index.ts:51` | 补 `has_shop` / `shop_count`；账号页展示形态 badge | 前端 |

**不需要改的** `[代码]`：`backend/app/core/platforms.py`（店铺不是平台）、`models/platform_account.py`（`sub_type` 是自由字符串）、`account_cookie_versions`（Cookie 机制沿用）。

## 22. 阶段划分

| 阶段 | 内容 | 备注 |
|---|---|---|
| **阶段 0** | 前置只读验证（§23） | **不改一行代码** |
| **阶段 1** | 店铺接入：Cookie 导入 → 只读逆向店铺/商品 → 落 `shops` / `shop_products` | |
| **阶段 2** | 能力探测：`resolve_capabilities` + 能力矩阵落库 | |
| **阶段 3** | 商品笔记生成：专用 Prompt + 合规护栏 + 图片处理 | |
| **阶段 4** | 发布：`goods_info` 补全 + 商品归属校验 + **单商品灰度（第一篇必须人工盯）** | |
| **阶段 5** | 编排：排期 + 护栏（日上限/最小间隔/同商品冷却/失败熔断） | |
| **阶段 6** | 矩阵分发：多账号种草（非店铺账号） | 依赖 §15.3 |

**验收指标**：

| 指标 | 目标 |
|---|---|
| 商品同步完整率 | 字段对齐率 100%（含 `item_id`） |
| 商品笔记生成可用率 | 人工审核通过率 ≥ 70%（首版） |
| **发布成功率** | ≥ 95% |
| **重复发布次数** | **0**（硬指标） |
| 合规拦截有效性 | 违禁词样本全部拦截 |

## 23. 阶段 0 验证清单（全部只读，不改一行代码）

| # | 事项 | 产出 | 阻塞关系 |
|---|---|---|---|
| **0.1** | **抓 `goods_info` 真实结构**（§18.1） | JSON schema + 是否需额外字段 | **决定需求④是「改一个字段」还是「逆向一套签名」**；成本半小时 |
| **0.2** | **商品分享权限是否可得**：创作者后台笔记编辑页**是否出现「添加商品」入口**；账号是否已开店铺并绑定专业号；商品是否在架 | 可行 / 不可行 | **决定需求④是否可做**；为否则方案收缩为「生成 + 不挂链种草笔记」（路线 B） |
| **0.3** | **Cookie 复用性**：用已登录 creator 的 Cookie 请求一个 ark 只读接口，看是否 401 | 是 / 否 | **可为是则省掉整个登录模块** |
| **0.4** | **抓包定三个参数**：过滤 `ark.xiaohongshu.com/api`，记录 `x-s` / `x-t` / **`x-s-common`（有无）** / `x-rap-param`（有无）/ referer / origin；记录 `x-s-common` 解码后的 `x3`（即 `app_id`）；记录 Cookie 中登录态字段名（对照 `web_session` / `customer-sso-sid`） | 三个参数值 | 决定接千帆的具体配置 |
| **0.5** | **能力矩阵**：**为每种实际持有的店铺类型，各产出一行完整的能力记录**（§15.1 六项能力）。某类型当前无可访问店铺 → 留空，**不影响系统上线** | 能力矩阵 | 探测依据 |
| **0.6** | 确认企业店子账号登录后**能看到几个店铺**（验证 N:M 是否真实存在） | 是 / 否 | 影响绑定模型 |
| **0.7** | 确认未缴保证金时，商品上架与笔记发布分别报什么错 | 错误文案 | 决定诊断文案 |
| **0.8** | 摸清**非店铺账号的实际占比**（现有 creator 账号中无店铺绑定的有多少） | 占比 | 影响前端 badge 与默认行为 |
| **0.9** | 走一遍**非店铺账号发布普通笔记**的完整链路，确认无隐藏的店铺依赖 | 可行 / 否 | 验证 §15.2 的「零改动」判断 |

## 24. 风险清单

| # | 风险 | 级别 | 应对 |
|---|---|---|---|
| R1 | **合规**：商品笔记是商业推广，广告法/平台违禁词/功效宣称违规后果严重 | 🔴 **最高** | 违禁词库 + 人工审核闸门 + 一致性校验；**用途合规性需自行评估** |
| R2 | **营销号识别**：无店铺账号发布商业推广内容，平台判定更严 | 🔴 高 | 内容差异化（非改写而是重写）；控制频率与账号活跃画像；**不做跨账号同文分发** |
| R3 | **需求④落不了地**：可能无商品分享权限 | 🔴 高 | 阶段 0.2 先验，为否则走路线 B |
| R4 | **重复发布**（写操作不可逆） | 🔴 高 | 账号级锁 + 幂等键 + 状态机串行；继承 `scheduler_service.py:181` 的补发幂等 |
| R5 | **AI 编造卖点/价格** → 笔记与商品不符 | 🔴 高 | 生成约束 + 入库比对 + 发布前复核价格 |
| R6 | **批量发营销笔记触发风控** | 🔴 高 | 每日上限 + 随机窗口 + 同商品冷却 + 失败熔断 |
| R7 | **内容同质化**：矩阵分发多号发同款 → 去重降权 | 🟡 中 | 同一商品的内容模板按账号维度变异（标题/结构/图片版式扰动） |
| R8 | **`goods_info` 含未知校验字段**，补不全导致发布失败 | 🟡 中 | 阶段 0.1 抓包 + 小流量灰度 |
| R9 | **能力探测本身失效**：全部店铺落入保守默认 → 只能生成不能发布 | 🟡 中 | 保留 `raw_json` 证据链；提供手动覆盖入口（标记来源为 `manual`） |
| R10 | **账号数放大巡检压力**：非店铺账号可能数十个 | 🟡 中 | 先用「cookie 是否存在」做低成本前置判断；巡检按账号状态分级降频 |
| R11 | **Cookie 过期**：人工导入的 Cookie 有生命周期 | 🟡 中 | 沿用现有 2h 巡检，失效即停并通知重新导入 |
| R12 | **千帆写操作风控** | 🟢 低 | 路线 C **不触碰 ark 写接口**，保持只读边界 |
| R13 | **`app_id` 拿不到 / ark 用独立签名体系** | 🟡 中 | 阶段 0.4 判定；若为独立体系需重新评估 |
| R14 | **与平台自带「批量生成智能笔记」功能重叠** | 🟡 中 | 定位为「**可控 + 差异化 + 可编排**」，而非复刻按钮 |
| R15 | **命名混淆**：现有 `xhs_qianfan_apis.py` 实为蒲公英 | 🟢 低 | 重命名或加文件头说明 |
| R16 | **`sub_type` 硬编码扩散** | 🟢 低 | 顺手收敛为枚举 + helper |

## 25. 待拍板决策点

| # | 决策 | 选项 | 建议 |
|---|---|---|---|
| **D1** | **商品笔记是否必须挂商品** | 挂 / 不挂 | **先明确**。不挂则①登录店铺②店铺列表可降级，方案大幅简化 |
| D2 | 店铺数据来源 | 只读逆向 ark / 手动导入 | **只读逆向**，手动导入作为降级 |
| D3 | 是否引入 `sub_type="shop"` | 引入 / 不引入 | **不引入**（§16 注）。仅当千帆需独立登录态时引入，且定义为只读 |
| D4 | AI 生成是否需人工审核 | 是 / 否 | **是**，商品笔记合规风险高，建议长期保留 |
| D5 | 图片策略 | 真实商品图 + 版式 / AI 重绘 | **优先真实图 + 版式**，避免货不对板 |
| D6 | 每日发布上限 / 最小间隔 | 数值 | 建议比普通笔记保守（如 ≤5 篇/日、≥60min） |
| D7 | 是否收敛 `sub_type` 硬编码 | 收敛 / 暂不动 | **收敛**（成本低，否则每加类型都付费） |
| D8 | 账号与店铺是否同一主体 | 同主体 / 跨主体 | 影响 `seller_id` 业务含义与 `can_sync` 的单点设计 |

---

# 第四部分 · 已知缺陷与技术债

## 26. 必须修的 bug

### 26.1 creator 账号健康检查必定误判 expired（高优先级）

`api/accounts.py:190-201`：

- `:194` 的 `XHS_Creator_Apis()` **缺 `auth` 参数**，而 `apis/xhs_creator_apis.py:129-134` 强制校验 `isinstance(auth, XHSCreatorAuth)` → **必抛 TypeError**
- `:195` 的 `get_fileIds("image", creator_cookies)` 把 cookies 传给了 `proxies` 形参（真实签名是 `get_fileIds(self, media_type, proxies=None)`）
- 后果：`:200` 的 except 分支把 `:182` 刚设好的 `status="active"` 覆盖为 `"expired"`，message 变成「上传凭证验证异常」

> **`POST /accounts/{id}/check` 对 creator 账号从来没成功过。**
> 且非店铺账号就是 creator 账号——矩阵铺量场景下账号数量多、以编程方式批量探活是常态，**该接口 100% 误报的影响面被放大**。

### 26.2 蒲公英链路参数不匹配

`apis/xhs_pugongying_apis.py:24` 调 `generate_pugongying_headers(cookies, api, data, profile=self.profile)` **4 个参数**，而 `xhs_utils/xhs_pugongying_util.py:26` 只定义 `(a1, api, data='')` **3 个参数** → **必 TypeError**。

与 26.1 同类，说明**蒲公英链路当初重构到一半就停了**（`xhs_pc/params.py:132-161` 的 `generate_xs` 注释提到「蒲公英等接口只携带 X-s/X-t」，但 util 侧没跟上）。该链路目前**不可用**。

## 27. 结构性风险

| # | 问题 | 说明 |
|---|---|---|
| 27.1 | **mock 遗留路由** | `api/platforms/xhs/pc.py` 的 `/notes/comments`、`/users/notes`、`/homefeed/*` 直接返回 `mock_data`，是半成品。真正的评论抓取在 `crawl.py`（`get_note_all_comment`）。`services/task_service.py` 也用 `sample_tasks` |
| 27.2 | **SDK 已实现但未接线** | 蒲公英、千帆两个平台的 SDK + utils 都在，**backend 零引用**，README 却标了 ✅。且「千帆」实为蒲公英（§13.2） |
| 27.3 | **多副本调度风险** | `BackgroundScheduler` 与 FastAPI 同进程，多 worker/多副本部署时**每个进程都会起一套定时器**，定时发布会重复执行（`max_instances=1` 只约束单进程内）。目前没有分布式锁 |
| 27.4 | **密钥未分离** | `security.secret_key` 默认 `dev-only-change-me`，`fernet_key` 为空时由 `secret_key` 派生 → **改 `secret_key` 会让历史 Cookie 全部解不开**。生产必须显式固定 `FERNET_KEY` |
| 27.5 | **死代码** | `backend/app/core/task_runner.py` 的 `InProcessTaskRunner` 只被实例化、从未被调用；`xhs_utils/xhs_util.py` 只是转发兼容层 |
| 27.6 | **SQLite 配置** | 用了 `PRAGMA journal_mode=MEMORY`（不是 WAL），并发写入下更容易锁表 |
| 27.7 | **测试覆盖偏薄** | `tests/backend/` 只有 3 个文件（`test_api.py`、`test_platforms.py`、`test_root_main.py`），README 写「126 passed」，实际断言量需核对 |
| 27.8 | **自动运营 `next_run_at` 计算** | `run_due_auto_tasks` 里是在执行**之后**才算的，任务执行耗时会被计入下一轮间隔 |
| 27.9 | **平台注册表** | 6 个平台中 5 个是 `coming_soon`，新增平台需补 adapter + API + 页面三件套 |

## 28. 命名与文档问题

| # | 问题 | 建议 |
|---|---|---|
| 28.1 | `apis/xhs_qianfan_apis.py` 实为蒲公英 | 重命名为 `xhs_pugongying_distributor_*`，或至少加文件头说明。**新增真千帆代码前必须处理** |
| 28.2 | `apis/` 下 `xhs_qianfan_apis.py` 与 `xhs_pugongying_apis.py` 同时存在且都打 pgy 域名 | 本身就是混淆源 |
| 28.3 | `docs/` 编号文档 | 本次已归档 10、12–18 至 `docs/_archive/`，编号 09、10、12–18 空缺属正常 |

---

## 附录 A：平台域名对照（易错）

| 业务 | 域名 | 说明 |
|---|---|---|
| **千帆** | **`ark.xiaohongshu.com`** | 商家后台：商品、订单、店铺、**笔记中心 / 发布笔记** |
| 千帆登录入口 | `zhaoshang.xiaohongshu.com/seller/company?from=ark-login` | 单来源 `[公开]`，待验证 |
| 蒲公英 | `pgy.xiaohongshu.com` | 买手/达人合作 |
| 乘风 | `chengfeng.xiaohongshu.com` | 电商广告投流 |
| 聚光 | `ad.xiaohongshu.com` | 广告管理 |
| creator 后台 | `creator.xiaohongshu.com` | 发布链路 |
| PC API | `edith.xiaohongshu.com`；搜索 `so.xiaohongshu.com` | |

## 附录 B：代码事实索引

| 事实 | 出处 |
|---|---|
| 「千帆」SDK 实际打蒲公英域名 | `apis/xhs_qianfan_apis.py:10,30,54,65,80,95,110`；`xhs_utils/xhs_qianfan_util.py:6,24` |
| 千帆/蒲公英 backend 零引用 | `docs/02:20,51`、`docs/07:83-85`、`README.md:110` |
| **发布体商品槽位为空** | `xhs_utils/xhs_creator_util.py:177`（图片）、`:246`（视频） |
| 发布体其它字段 | `xhs_utils/xhs_creator_util.py:161-187` |
| 发布链路入口 | `backend/app/adapters/xhs/creator_api_adapter.py:87` |
| Creator RAP 独立模板 | `creator_api_adapter.py:155`；`xhs_utils/xhs_creator_util.py:48` |
| 上传链路独立签名（不动） | `apis/xhs_creator_apis.py`（PyExecJS）；`xhs_utils/xhs_creator_util.py:39` |
| RAP 白名单路径 | `apis/xhs_pc_apis.py:37-44` |
| 项目无商品/店铺模型 | `backend/app/models/` 全部 `__tablename__` 已核对，21 张表无 product/goods/shop/sku |
| AI 生成能力 | `ai_service.py:186` / `:231` / `:214` / `:249` / `:353` / `:274` / `:287` / `:167` |
| 签名器与 host 解耦 | `xhs_utils/xhs_pc/runtime.py:68-106` |
| 多业务域扩展点 | `xhs_utils/xhs_core/auth.py:15-84` |
| Cookie 按 host 分域 | `xhs_utils/xhs_core/cookies.py:44-171` |
| 同栈服务多域先例 | `apis/xhs_pugongying_apis.py:19-26` |
| 定时发布补跑（幂等基础） | `services/scheduler_service.py:181` |
| 调度间隔配置 | `models/auto_task.py:23,26` |
| Cookie 多版本加密存储范式 | `models/platform_account.py:31`（`account_cookie_versions`） |
| 平台注册表（可扩平台） | `core/platforms.py:8-40` |
| creator 健康检查 bug | `api/accounts.py:190-201`；`apis/xhs_creator_apis.py:129-134` |
| 蒲公英参数不匹配 | `apis/xhs_pugongying_apis.py:24`；`xhs_utils/xhs_pugongying_util.py:26` |

## 附录 C：外部信息置信度分级

> 本节所有结论**均未实机验证**，且检索过程命中大量 AI 生成的 SEO 内容农场，存在互相矛盾。**凡涉及接口路径、字段名、权限门槛、限流阈值，一律以阶段 0 抓包或官方文档原文为准。**

| 结论 | 置信度 |
|---|---|
| 千帆 = `ark.xiaohongshu.com`，含商品管理/笔记中心/发布笔记 | **高**（多来源一致） |
| 蒲公英 = `pgy.*`；乘风 = `chengfeng.*`；聚光 = `ad.*` | **高** |
| 开放平台支持自用型应用，绑定千帆自动完成企业认证 | 高 |
| 权限包为 公共/授权/商品/库存/素材/订单/售后，**无笔记接口** | **高**（建议以官方最新文档复核） |
| 笔记发布未对普通开发者开放，仅定向开放 | 中高 |
| 个人店免营业执照，身份证 + 人脸，保证金 1000 元起 | **中高**（多来源一致） |
| 个人店可经营类目受限（服饰/家居/文创/数码配件） | **中高** |
| 商品标签需「专业号认证 + 绑定店铺」 | 中高 |
| 个体户店需个体工商户执照；企业店需企业执照 | **中高** |
| 千帆有「批量生成智能笔记」按钮、多店铺统一管理 | 中（建议实测复核） |
| 「可直接使用小红书账号登录千帆，无需额外注册」 | 中（**关键，需验证**） |
| 千帆 PC 端登录入口为 `zhaoshang.xiaohongshu.com/seller/company?from=ark-login` | 中（单来源） |
| 笔记正文需标注「合作/赞助/广告」等商业内容标识 | 中（合规上按「需要」处理更安全） |
| 企业店一个营业执照最多开 3 家店 | 中（单来源，需验证） |
| 企业店发品前必须缴纳保证金 | 中（单来源） |
| **个人店能否获得商品分享权限** | **低 —— 来源互相矛盾，阶段 0.2 必验** |
| `shop_type` 能否从店铺信息接口直接拿到 | **未知**（设计已容错为 `unknown`） |
| 各能力的具体探测字段/接口 | **全部未知，阶段 0 抓包确定** |
| `qianfan.xiaohongshu.com` 域名 | **低，与多来源的 `ark.*` 冲突，建议忽略** |
| 开放平台具体接口路径（如 `/api/v1/note/publish`） | **不可信，勿引用**（AI 生成的 SEO 内容，多来源互相矛盾） |

## 附录 D：文档归档清单

本次工作会话已将被汇总与已放弃的文档移入 **`docs/_archive/`**（**移动而非删除，可随时恢复**）：

| 原路径 | 归档原因 |
|---|---|
| `PROJECT_MAP.md` | 项目梳理内容并入本文第一部分 |
| `docs/10_无头浏览器方案可行性分析.md` | **无头浏览器方案已放弃**（第二部分） |
| `docs/12_无头浏览器化改造影响范围与改动清单.md` | **同上** |
| `docs/13_无头浏览器化_方案修正与阶段0前置.md` | **同上** |
| `docs/14_无头浏览器化_收益利弊量化分析.md` | **同上**（量化依据保留在 §10） |
| `docs/15_商品笔记自动化_调研与实施方案.md` | 内容并入本文第三部分 |
| `docs/16_无企业资质前提下的商品笔记自动化_路线重构.md` | 同上 |
| `docs/17_三类店铺统一支持设计.md` | 同上（§15.1） |
| `docs/18_非店铺账号与账号店铺二维模型.md` | 同上（§15.2 / §15.3） |

**保留未动**：`docs/00`–`docs/08`（项目原有分析报告）、`docs/11_商品AI笔记自动化方案.md`（项目原创方案，其结论已被本文第三部分取代，但作为历史记录保留）。

**说明**：归档目录中的文档在结论上**已全部被本文取代**，仅作追溯用。若确认无需追溯，可整体删除 `docs/_archive/`。

---

## 一句话总结

这是一个**把逆向 SDK 与完整运营后台缝合在一起**的项目：`apis/` + `xhs_utils/` 是硬核资产（签名四层结构、Node 单源算法、输出校验护栏），`backend/` 是标准 FastAPI 三层架构加上 APScheduler 驱动的自动化，`frontend/` 是 antd 6 + React 19 的运营后台。

在放弃无头浏览器方案后，**新增的商品笔记自动化能力恰好落在项目最擅长的地方**——不需要新增签名维护面（签名器与业务域解耦），整条链路对现有代码的侵入只有 `xhs_creator_util.py:177` / `:246` 那一个恒为空的 `goods_info` 字段。真正的难点不在技术，而在**权限门槛（阶段 0.2）与合规护栏**。
