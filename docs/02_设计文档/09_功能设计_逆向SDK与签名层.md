# 功能设计 · 逆向 SDK 与签名层

> - **阶段**：二 · 设计
> - **访问端视角**：**内嵌能力层**——最底层、被上层复用，也可独立作为 SDK 被外部 Agent 工具链引入
> - **覆盖**：目录结构、签名四层结构、Node 调用与输出门禁、平台配置扩展点、HTTP 传输层、域名对照
> - **版本**：v1.0 · 2026-09-18

---

## 1. 目录与职责

```
apis/                              对外 API 封装（按业务域分文件）
├── xhs_pc_apis.py                 1045 行  PC 内容：搜索/详情/评论/用户/消息
├── xhs_pc_login_apis.py            944 行  PC 登录：扫码 + 短信
├── xhs_creator_apis.py             835 行  Creator：上传 + 发布 + 作品管理
├── xhs_creator_login_apis.py      1232 行  Creator 登录（独立链路）
├── xhs_pugongying_apis.py          151 行  蒲公英（requests 直连；⚠️ 已知不可用，见 DEF-010/T-24）
└── xhs_pugongying_distributor_apis.py  117 行  蒲公英分销商广场（原名 `xhs_qianfan_apis.py`，2026-09-18 重命名）

xhs_utils/
├── xhs_core/                      【跨平台共享层】
│   ├── auth.py                    XHSPlatformConfig + XHSAuth 基类
│   ├── cookies.py                 HostCookieStore（按 host 隔离 Cookie）
│   ├── params.py                  平台中立 ID 生成
│   ├── http.py                    BrowserHttpClient + ordered_wire_headers
│   ├── dsl.py                     DsFetcher（服务端 DS 程序拉取 + 5min 缓存）
│   ├── runtime.py                 run_node_json 共享 Node 桥
│   └── js/                        ★ 签名算法唯一实现源
│       ├── b1.js                  b1（RC4 + 自定义 Base64 + 19 字段）
│       ├── mns.js                 mns0101_/0201_/0301_（TLV + XOR keystream）
│       ├── sign.js                签名 CLI 总入口
│       ├── xs_common.js           X-S-Common（自定义 Base64 + x9 CRC32）
│       ├── websectiga_cli.js      执行服务端下发的 JSVMP
│       ├── websectiga_env.js      JSVMP 浏览器环境补齐引擎
│       └── mns_keystream*.json    密钥流数据
│
├── xhs_pc/                        【PC 平台层】
│   ├── auth.py                    XHSPcAuth（三个工厂 + 参数归属清单）
│   ├── state.py                   PcDeviceProfile / PcSessionState / REFERENCE_PROFILE
│   ├── params.py                  header 顺序表 + generate_xs_xs_common + x-rap-param
│   ├── http.py                    PcHttpClient（chrome146）
│   ├── dsl.py                     appId=xhs-pc-web 的 DS 拉取器
│   ├── runtime.py                 ★ PC Node 适配器（run_signer / generate_b1 / …）
│   └── js/
│       ├── profile.js             profileData（DES-ECB，sdk 4.3.7）
│       ├── rap.js / rap_cli.js / rap_crypto.js   x-rap-param 加密信封
│       ├── aes_encrypt.js / aes_decrypt.js       自研 S-box 的 AES-128
│       ├── deflate.js             浏览器对齐的 DEFLATE
│       └── rap_fingerprint_template.json         219B RAP 模板
│
└── xhs_creator/                   【Creator 平台层】
    ├── auth.py / state.py / params.py / http.py / dsl.py / runtime.py
    └── js/
        ├── profile.js                  profileData（DES key `zbp30y86`，sdk 4.3.6）
        ├── xhs_creator_sign.js         urlSing（MD5）
        ├── xhs_creator_signature.js    getSignature（HmacSHA1，上传用）
        └── rap_fingerprint_creator.json  436B RAP 模板
```

**核心约定**：

> **签名算法唯一实现位于 `xhs_utils/**/js/`，Python 侧无第二份实现。**
> Python 只负责：① 序列化输入 ② 调 Node 子进程 ③ **校验输出**。

---

## 2. 签名四层结构

### 2.1 传输层伪装

| 项 | 值 | 位置 |
|---|---|---|
| TLS/HTTP2 指纹 | `impersonate='chrome146'` | `xhs_core/http.py:16` |
| HTTP 版本 | `http_version='v2tls'` | `http_core/http.py:18` |
| 编码偏好 | `accept-encoding: gzip, deflate, br, zstd` | `:17` |
| Cookie 处理 | `discard_cookies=True`（Cookie 由代码显式管理） | `:100-106` |
| **Header 顺序契约** | 每个场景一套**固定顺序**（PC 9 套 / Creator 3 套） | `xhs_pc/params.py:20-74`、`xhs_creator/params.py:26-89` |

**Header 顺序为什么是契约**：平台的设备指纹校验会读取 header 顺序。顺序漂移会被识别为自动化工具。

`ordered_wire_headers()`（`xhs_core/http.py:34-82`）强制执行：
- 缺项 → `RuntimeError('wire header contract drifted')`
- 多余项（strict 模式）→ 同样报错
- 匿名早期请求无 Cookie 时，`cookie` 标记为 optional

### 2.2 认证态

| 组件 | 作用 |
|---|---|
| Cookie 解析 | 提取 `a1`（设备标识）、`web_session`（登录态） |
| DS 锚点 | `DsFetcher` 从 `as.xiaohongshu.com` 拉取 `_dsl` 程序（带 5min 缓存） |
| websectiga | 执行服务端下发的 **JSVMP** 程序，产出 websectiga 值 |

### 2.3 请求签名

| 签名 | 算法 | 说明 |
|---|---|---|
| `b1` | RC4（key `xhswebmplfbt`）+ 自定义 Base64 + 19 个 mini 字段 | 设备指纹 |
| **MNS `x-s` / `x-t`** | **三档**：`mns0101_` 冷启动 Base58 / `mns0201_` 安全域 XXTEA / `mns0301_` 稳态 XOR | 核心请求签名 |
| `x-s-common` | 自定义 Base64 + `x9`（变体 CRC32） | 公共服务签名 |
| 追踪头 | `x-b3-traceid` / `xray-traceid` | 平台中立生成（`xhs_core/params.py`） |

### 2.4 指纹与业务扩展

| 项 | 说明 |
|---|---|
| `profileData` | PC 用自定义 DES-ECB；Creator 用 CryptoJS DES-ECB（key `zbp30y86`） |
| `x-rap-param` | 平台风控参数。**PC 与 Creator 用不同模板**（219B / 436B） |

---

## 3. `run_signer` 详解（最关键的接口）

**位置**：`xhs_utils/xhs_pc/runtime.py:68`

### 3.1 签名

```python
def run_signer(
    api: str,                      # 请求 path
    data: Any = b"",               # 请求 body
    *,
    a1: str,
    cookie: str,
    b1: Optional[str],
    dsl_pair: str,
    tier: str,                     # 0101 | 0201 | 0301
    sign_context: Mapping[str, Any],
    timeout: float = 30.0,
) -> Optional[dict]:
```

> ⚠️ **注意：没有 `host` 参数。** 签名输入只有 **path + body**，域名差异只体现在 HTTP 层。
>
> **这是「接新业务域不需要重新逆向」的技术基础。**

### 3.2 返回值

成功 → 含 `xs` / `xt` / `x3` 等的 dict（并追加 `len` / `prefix`）
失败 → **`None`**（**绝无短签或静态兜底**）

### 3.3 Node 子进程调用方式

```python
subprocess.run(
    ['node', _SIGN_JS, json.dumps(payload, ensure_ascii=False)],
    capture_output=True, text=True, timeout=timeout,
    cwd=_JS_DIR, encoding='utf-8', errors='replace',
)
```

| 变体 | PC | Creator |
|---|---|---|
| JS 入口 | `xhs_core/js/sign.js` | 同 |
| cwd | `xhs_pc/js` | `xhs_creator/js` |
| 传参 | 直接传 JSON 字符串 | **写临时文件 + `@<path>` 传参** |
| 失败行为 | 返回 `None` | **`raise ValueError` / `RuntimeError`** |

> ⚠️ PC 与 Creator 的失败语义不同（PC 静默返回 None，Creator 抛异常）——上层要注意这个差异。

### 3.4 输入前置门禁（任一不满足 → 返回 None）

| 门禁 | 位置 |
|---|---|
| `sign.js` 存在，且 cookie 非空且含 `a1=` | `runtime.py:81-82` |
| `tier` 必须在档位门禁表中 | `:83-84` |
| `sign_context` 必须含 `loadts` / `seq` / `envConst` / `envFpTail` / `webBuild` | `:86-88` |

### 3.5 输出门禁（防静默降级的护栏）

**档位门禁表 `_TIER_GATE`**（`xhs_pc/runtime.py:28-32`）：

| tier | 前缀要求 | 长度要求 |
|---|---|---|
| `0101` | `mns0101_` | 205 |
| `0201` | `mns0201_` | 208 |
| `0301` | `mns0301_` | 200 |

Creator 侧（`xhs_creator/runtime.py:26-32`）：`0201` → 长度 200；`0101` → 长度 196/197/198。

**其它输出门禁**：

| 签名 | 门禁 |
|---|---|
| `b1` | 长度 ≥ 500 且是 4 的倍数（`xhs_pc/runtime.py:63-64`） |
| `websectiga` | 正则 `^[0-9a-f]{64}$`（`:33`，fullmatch 校验 `:196`） |
| `profileData` | 正则 `^[0-9a-f]{10000,20000}$`（`:34`）+ 校验 `sdkVersion=='4.3.7'` + `algorithm=='base64-des-ecb-zero-padding-hex'`（`:262-267`） |

**stdout 解析规则**：逐行**倒序**扫描，只接受以 `{` 开头且含 `"x3"` 的行（`:124-128`）。

### 3.6 门禁的设计意图

> **宁可失败，不可发错签。**
>
> 若签名计算出现静默降级（返回短签/空值），请求会带着错误签名发出去 —— 轻则被拒，重则触发风控。所以每个签名都有长度/前缀/正则门禁，不满足就**显式失败**。

---

## 4. 平台配置扩展点（`XHSPlatformConfig`）

**位置**：`xhs_utils/xhs_core/auth.py:14-51`，`@dataclass(frozen=True)`

### 4.1 结构

| 字段 | 说明 |
|---|---|
| `name` | 平台标识 |
| `app_id` | **参与 MNS 签名与 DS 获取**（关键） |
| `cookie_domain` | Cookie 域 |
| `origins` | 角色 → 域名的映射（`MappingProxyType` 冻结） |

**强校验**：必须包含 `web` 与 `api` 两个角色；每个 origin 必须是合法 http/https URL（`:28-39`）。

**配套基类 `XHSAuth`**（`:87-121`）：持有 `PLATFORM_CONFIG: ClassVar`，提供 `config` / `domains` / `origin`；校验登录来源必须在 `AUTH_LOGIN_SOURCES = {'cookie', 'qrcode', 'phone'}`。

### 4.2 现有两个实例

**`PC_PLATFORM_CONFIG`**（`:54-67`）：

| 字段 | 值 |
|---|---|
| name | `pc` |
| app_id | **`xhs-pc-web`** |
| cookie_domain | `.xiaohongshu.com` |
| web | `https://www.xiaohongshu.com` |
| api | `https://edith.xiaohongshu.com` |
| search | `https://so.xiaohongshu.com` |
| login | `https://www.xiaohongshu.com` |
| security | `https://as.xiaohongshu.com` |
| captcha | `https://edith.xiaohongshu.com` |
| sem | `https://pages.xiaohongshu.com` |

**`CREATOR_PLATFORM_CONFIG`**（`:70-84`）：

| 字段 | 值 |
|---|---|
| name | `creator` |
| app_id | **`ugc`** |
| cookie_domain | `.xiaohongshu.com` |
| web / api | `https://creator.xiaohongshu.com` |
| login | `https://customer.xiaohongshu.com` |
| security | `https://as.xiaohongshu.com` |
| captcha / edith | `https://edith.xiaohongshu.com` |
| upload | `https://ros-upload.xiaohongshu.com` |
| public_web | `https://www.xiaohongshu.com` |

### 4.3 扩展一个新业务域的标准步骤（二开必读）

| # | 步骤 | 参照 |
|---|---|---|
| 1 | 新增一个 `XHSPlatformConfig` 实例（必须含 `web` + `api`） | `auth.py:54` / `:70` |
| 2 | 新增平台 Auth 类，继承 `XHSAuth`，设置 `PLATFORM_CONFIG` | `xhs_pc/auth.py:166`、`xhs_creator/auth.py:88` |
| 3 | 新增平台 state / params / runtime 子包 | `xhs_pc/`、`xhs_creator/` |
| 4 | 在 `xhs_core/__init__.py:3-9` 导出新配置 | — |
| 5 | 若走 Node 签名，把 `<tier> → (前缀, 长度)` 加进对应 `runtime.py` 的 `_TIER_GATE` | `xhs_pc/runtime.py:28-32` |
| 6 | **无需改动**：`xhs_core/http.py`、`cookies.py`、`params.py`、`dsl.py`、`runtime.py` | — |

**二开的三个未知量**（全靠抓包定）：

| 未知量 | 影响 |
|---|---|
| `app_id` | 参与 MNS 签名与 DS 获取 |
| 登录态 Cookie 名 | 对照 `web_session` / `customer-sso-sid` |
| 是否携带 `x-s-common` / `x-rap-param` | 决定 header 契约 |

### 4.4 Cookie 按 host 分域

`HostCookieStore`（`xhs_core/cookies.py:44-171`）已按 host 隔离 Cookie，并保持顺序、处理 `acw_tc`。

> **意义**：ark 域（千帆）天然支持，不需要改 Cookie 机制。

---

## 5. HTTP 传输层

**位置**：`xhs_utils/xhs_core/http.py`

| 能力 | 实现 |
|---|---|
| 会话构造 | `curl_requests.Session(..., impersonate=..., default_headers=False, discard_cookies=True, http_version=...)`（`:88-106`） |
| Header 契约强制 | `ordered_wire_headers()`（`:34-82`） |
| 代理选择 | **请求级 `proxies` 优先，否则回落 Session 的 `self.proxies`**（`:155`） |
| 伪头剥离 | 剥离 HTTP/2 伪头 `authority`（`:119-124`） |
| 空 POST 处理 | 用 `None` 值抑制 `Content-Type`，让 curl 不发该头（`:139-153`） |
| 状态快照 | `state_snapshot()` 返回 `backend` / `impersonate` / `httpVersion` 等（`:181-188`） |

### 5.1 代理剥离的真实位置（易混淆）

> ⚠️ **`xhs_core/http.py` 里没有「剥离代理」逻辑，只有「选择代理」。**
>
> 真正的**环境变量代理剥离**在适配层：`backend/app/adapters/xhs/request_env.py:21-35` 的 `direct_xhs_request_env()`，逐个 `os.environ.pop(HTTP_PROXY / HTTPS_PROXY / ALL_PROXY …)`（键列表 `:9-16`），并在 finally 恢复（`:31-35`），**加线程锁**避免并发互相干扰。

**为什么必须剥离**：开发机/服务器上常设代理环境变量，`curl_cffi` 会读取它们，导致请求经代理发出 → 出口 IP 变化 → 触发风控。**每个 SDK 调用都必须包在这个上下文里。**

---

## 6. 域名对照表（易错，务必记牢）

| 业务 | 域名 | 用途 |
|---|---|---|
| **千帆** | **`ark.xiaohongshu.com`** | 商家后台：商品、订单、店铺、笔记中心、发布笔记 |
| 千帆登录入口 | `zhaoshang.xiaohongshu.com/seller/company?from=ark-login` | 单来源 `[公开]`，待验证 |
| 蒲公英 | `pgy.xiaohongshu.com` | 买手/达人合作 |
| 乘风 | `chengfeng.xiaohongshu.com` | 电商广告投流 |
| 聚光 | `ad.xiaohongshu.com` | 广告管理 |
| Creator 后台 | `creator.xiaohongshu.com` | 发布链路 |
| Creator 登录 | `customer.xiaohongshu.com` | CAS 登录 |
| 素材上传 | `ros-upload.xiaohongshu.com` | 媒体上传 |
| PC API | `edith.xiaohongshu.com` | 内容接口 |
| PC 搜索 | `so.xiaohongshu.com` | 搜索接口 |
| PC Web | `www.xiaohongshu.com` | 页面 |
| 安全 | `as.xiaohongshu.com` | DS / 安全域 |
| 图片 CDN | `ci.xiaohongshu.com` | 素材 CDN |

### 6.1 命名混淆警告（✅ 已于 2026-09-18 消除，T-101）

| 文件名 | 名字暗示 | 实际域名 |
|---|---|---|
| ~~`apis/xhs_qianfan_apis.py`~~ → `apis/xhs_pugongying_distributor_apis.py` | 蒲公英分销商广场 | `pgy.xiaohongshu.com` |
| `apis/xhs_pugongying_apis.py` | 蒲公英（KOL） | `pgy.xiaohongshu.com` |

**修复前**两个文件**都打蒲公英域名**、方法都是 `get_user_*`（分销商维度），命名与实现矛盾；
**修复后**文件名与实现一致，不再产生歧义。

> **✅ 已完成**：新增真千帆代码的命名障碍已清除。此后 `qianfan` 唯一指 `ark.xiaohongshu.com`。
> ⚠️ 但两个蒲公英文件**都仍不可用**（backend 零引用 + `xhs_pugongying_apis.py` 有 DEF-010 的参数不匹配问题，已决定不修）。
> 换句话说：**命名问题解决了「会不会用错」，没有解决「能不能用」**——这是两件事。

---

## 7. SDK 层的能力边界

| 已实现（有 SDK） | 有 adapter | 有真实路由 | 有前端 |
|---|---|---|---|
| PC 搜索 / 详情 / 评论 | ✅ | ✅ | ✅ |
| PC 用户笔记 | ✅ | ✅（`crawl.py:302`） | ❌ 未接 |
| PC 搜索用户 | ✅ | ❌ 路由是 mock | ❌ |
| PC homefeed | ✅ | ❌ 路由是 mock | ❌ |
| PC 消息类（未读/@/点赞/关注） | ✅ | ❌ 无路由 | ❌ |
| Creator 上传 / 发布 | ✅ | ✅ | ✅（走 publish 业务层） |
| Creator 话题 / 地点 | ✅ | ✅ | ✅（间接） |
| Creator 已发布作品 | ✅ | ✅ | ❌ |
| 蒲公英 | ✅ | ❌ backend 零引用 | ❌ |
| 千帆（实为蒲公英） | ✅ | ❌ backend 零引用 | ❌ |

> **README 标注的 ✅ 与实际接线情况有出入**：蒲公英与「千帆」的 SDK 都在，但 **backend 零引用**。

---

## 8. SDK 层的已知缺口

| # | 缺口 | 影响 | 处理 |
|---|---|---|---|
| K-1 | 命名冲突（千帆 = 蒲公英） | 二开必然踩坑 | **二开前置任务** |
| K-2 | `CREATOR_WEB_BUILD='1.29.1'` 硬编码 | 平台发版后登录失效（已发生） | 建议改运行时探测 |
| K-3 | `chrome146` 指纹是常量 | curl_cffi 更新后需跟进 | 建议启动自检 |
| K-4 | 蒲公英链路参数不匹配（4 参 vs 3 参） | 该链路**必 TypeError** | 见 `05_项目沉淀/02`；若二开不用可暂缓 |
| K-5 | PC / Creator 失败语义不一致（None vs raise） | 上层需分别处理 | 建议统一 |
| K-6 | 无 Node 可用性启动自检 | Node 缺失时全系统不可用，错误难定位 | 建议补 |
