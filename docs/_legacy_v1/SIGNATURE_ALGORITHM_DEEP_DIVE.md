# XHS_ALL_IN_ONE 签名算法深度挖掘报告

> **项目路径**：`D:\work\XHS_ALL_IN_ONE`  
> **梳理时间**：2025 年  
> **分析范围**：`apis/`、`xhs_utils/`、`backend/app/adapters/xhs/` 全链路签名相关代码  
> **结论**：本项目签名体系是一个**多层嵌套的反向工程产物**，核心分为「传输层伪装 → 请求签名（MNS/X-S-Common/b1） → 指纹/环境锚点（profileData/DS/websectiga） → 业务扩展签名（x-rap-param）」四层，PC 与 Creator 双端共享核心算法但存在明显的档位/参数差异。

---

## 1. 签名体系总览

```
一次 XHS 请求的签名材料生成流程
=================================

1. 传输层伪装
   └── curl_cffi Chrome-impersonate (TLS/HTTP2 指纹)

2. 认证态准备
   ├── Cookie 解析 (a1, web_session / customer-sso-sid 等)
   ├── DS 锚点获取 (_dsl = getdss() 服务端下发伪时间戳)
   └── websectiga 获取 (执行服务器下发的 JSVMP scripting code)

3. 请求签名核心
   ├── b1 生成 (RC4 + 自定义 Base64)
   ├── MNS 签名 (x-s / x-t)
   │   ├── mns0101_ (冷启动, Base58, 无 DS)
   │   ├── mns0201_ (安全域, XXTEA)
   │   └── mns0301_ (稳态, XOR + 自定义 Base64)
   ├── X-S-Common 生成 (CRC32 + 自定义 Base64)
   └── 追踪头 (x-b3-traceid / x-xray-traceid / xy-direction)

4. 指纹/环境材料
   ├── profileData (PC: 自定义 DES-ECB; Creator: CryptoJS DES-ECB)
   └── x-rap-param (仅部分接口, 如 homefeed/search)

5. 业务适配层
   └── backend/app/adapters/xhs/ 组装完整请求
```

---

## 2. 传输层伪装（curl_cffi）

**位置**：`xhs_utils/xhs_core/http.py`、`xhs_utils/xhs_pc/http.py`、`xhs_utils/xhs_creator/http.py`

### 2.1 核心实现

项目没有使用标准 `requests` 库，而是使用 **curl_cffi** 模拟 Chrome 146 的 TLS/HTTP2 指纹：

```python
# xhs_utils/xhs_core/http.py
class BrowserHttpClient:
    def __init__(self, ...):
        self.session = curl_requests.Session(
            proxies=proxies,
            impersonate='chrome146',        # Chrome 146 指纹
            default_headers=False,           # 不自动加默认头
            discard_cookies=True,            # 不自动存 Cookie
            http_version='v2tls',            # 强制 HTTP/2 over TLS
        )
```

### 2.2 Header 顺序契约

每个平台/场景都有**严格固定的 Header 顺序**，用 `ordered_wire_headers` 校验：

| 平台 | 场景 | Header 顺序文件 |
|---|---|---|
| PC | 导航页 | `PC_NAVIGATION_HEADER_ORDER` |
| PC | 登录 | `PC_LOGIN_HEADER_ORDER` (honeypot/security/sem/get) |
| PC | 业务 API | `PC_SIGNED_POST_HEADER_ORDER` / `PC_SIGNED_GET_HEADER_ORDER` |
| PC | RAP | `PC_RAP_POST_HEADER_ORDER` / `PC_RAP_GET_HEADER_ORDER` |
| PC | xy-direction | `PC_XY_RAP_POST_HEADER_ORDER` |
| Creator | 导航 | `CREATOR_NAVIGATION_HEADER_ORDER` |
| Creator | 登录 | `CREATOR_SECURITY_HEADER_ORDER` / `CREATOR_HONEYPOT_HEADER_ORDER` |
| Creator | 业务 | `CREATOR_SIGNED_POST_HEADER_ORDER` / `CREATOR_SIGNED_GET_HEADER_ORDER` |
| Creator | CAS | `CREATOR_CAS_POST_HEADER_ORDER` / `CREATOR_CAS_GET_HEADER_ORDER` |

**关键细节**：
- `content-type: None` 用于空 POST（抑制 libcurl 自动添加 `application/x-www-form-urlencoded`）
- `authority` 头被过滤（HTTP/2 伪头，由 libcurl 从 URL 推导）
- `cookie` 头由 `_cookie_header()` 统一生成，支持 dict/list/string 三种输入

---

## 3. 认证态与 Cookie 管理

### 3.1 平台配置

```python
# xhs_utils/xhs_core/auth.py
PC_PLATFORM_CONFIG = XHSPlatformConfig(
    name='pc',
    app_id='xhs-pc-web',
    cookie_domain='.xiaohongshu.com',
    origins={
        'web': 'https://www.xiaohongshu.com',
        'api': 'https://edith.xiaohongshu.com',
        'search': 'https://so.xiaohongshu.com',
        'login': 'https://www.xiaohongshu.com',
        'security': 'https://as.xiaohongshu.com',
        'captcha': 'https://edith.xiaohongshu.com',
        'sem': 'https://pages.xiaohongshu.com',
    },
)

CREATOR_PLATFORM_CONFIG = XHSPlatformConfig(
    name='creator',
    app_id='ugc',
    cookie_domain='.xiaohongshu.com',
    origins={
        'web': 'https://creator.xiaohongshu.com',
        'api': 'https://creator.xiaohongshu.com',
        'login': 'https://customer.xiaohongshu.com',
        'security': 'https://as.xiaohongshu.com',
        'captcha': 'https://edith.xiaohongshu.com',
        'edith': 'https://edith.xiaohongshu.com',
        'upload': 'https://ros-upload.xiaohongshu.com',
        'public_web': 'https://www.xiaohongshu.com',
    },
)
```

### 3.2 Cookie 分类

| Cookie 类型 | PC 端 | Creator 端 | 说明 |
|---|---|---|---|
| 持久登录 | `a1` + `web_session` | `a1` + `customer-sso-sid` / `galaxy_creator_session_id` | 主认证态 |
| 安全/指纹 | `websectiga` / `gid` / `sec_poison_id` | `websectiga` / `gid` / `sec_poison_id` | 反爬校验 |
| 页面状态 | `abRequestId` / `loadts` / `ets` / `webBuild` / `xsecappid` | `abRequestId` / `loadts` / `ets` / `webBuild` / `xsecappid` | 页面生命周期 |
| 服务端下发 | `id_token` / `mobile_token` / `captcha_challenge` | `access-token-creator.xiaohongshu.com` | 登录过程中临时使用 |

### 3.3 HostCookieStore

项目实现了**域隔离 Cookie 管理**（`HostCookieStore`），不同 origin 只发送匹配的 Cookie，模拟浏览器的同源策略。

---

## 4. 核心签名算法详解

### 4.1 b1 生成（设备指纹快照）

**位置**：
- JS 实现：`xhs_utils/xhs_core/js/b1.js`
- PC 调用：`xhs_utils/xhs_pc/runtime.py::generate_b1`
- Creator 调用：`xhs_utils/xhs_creator/runtime.py::generate_b1`

#### 4.1.1 算法流程

```
b1 = Base64_Custom( RC4( JSON.stringify(miniFields), key="xhswebmplfbt" ) )
```

**miniFields 包含 19 个字段**：

| 字段 | 说明 | 来源 |
|---|---|---|
| `x33` | 固定 '0' | 模板 |
| `x34` | 固定 '0' | 模板 |
| `x35` | 固定 '0' | 模板 |
| `x36` | frameCount (页面帧数) | 动态 (PC: 2, Creator: login=2, note-manager=6, ±1 jitter) |
| `x37` | 24 bit 位图 (collector 状态) | 动态 (Creator 每 session 随机翻转 1-3 bit) |
| `x38` | 40 bit 位图 (浏览器能力) | 模板 |
| `x39` | collector 缓存计数 | 动态 (PC: 22, Creator: 0-5 random) |
| `x42` | SDK 版本 | PC='3.5.4', Creator 无 |
| `x43` | canvas 指纹 | 模板 |
| `x44` | 当前时间戳 | 动态 |
| `x45` | secCanvas | 模板 |
| `x46` | webdriver 标志 | 模板 |
| `x48` | 空字符串 | 模板 |
| `x49` | '{list:[],type:}' | 模板 |
| `x50` | 浏览器能力向量 | 模板/动态 |
| `x51` | 空字符串 | Creator 特有 |
| `x52` | 空字符串 | 模板 |
| `x82` | window global 名称快照 (≤300 char) | 模板 |
| `x84` | 遥测数据 (JSON) | 动态 |

#### 4.1.2 x84 遥测格式

```javascript
// PC 格式
{
  mt: { to: timeOrigin },
  m: { me:50, mm:50, md:1, mu:1, c:1 },    // mouse
  k: {},                                       // keyboard
  p: { ulr:1, ps:1, f:3, b:3, vc:0, rs:1, sc:0 },  // page
  st: { h:0, f:1, kr:0 },                     // state
  ft: {                                       // features
    ae: 3.3656,
    ak: 6.2311,
    cdr: 0.4096,
    bf: { ar:0.6210, fr:7.1580 },
    fi: 2151.1
  }
}
```

#### 4.1.3 Creator 的 Jitter 机制

Creator 端在 `reference()` 后调用 `_jitter()`，对以下字段做**每 session 随机化**：
- `frameCount`: 基准值 ±1
- `x37`: 随机翻转 1-3 个 0 bit
- `x39`: 0-5 随机
- `timeOrigin`: 页面加载前 50-3000ms
- `x84` key order: 固定偏好顺序 `ulr, f, rs, ps`

---

### 4.2 MNS 签名（x-s / x-t）

**位置**：
- JS 实现：`xhs_utils/xhs_core/js/mns.js` + `xhs_utils/xhs_core/js/sign.js`
- PC 调用：`xhs_utils/xhs_pc/params.py::generate_xs_xs_common`
- Creator 调用：`xhs_utils/xhs_creator/params.py::generate_request_params`

#### 4.2.1 统一签名入口

```javascript
// sign.js
function signFull(input) {
  // 1. 计算 MD5(url + data) = u, MD5(url) = p
  const c = String(input.api) + (input.data == null ? '' : JSON.stringify(input.data));
  const md5Full = md5Bytes(c);
  const md5Url = md5Bytes(String(input.api));
  
  // 2. 计算 DS 设备签名 (非 nop 路径)
  let dsSigBytes;
  if (deviceTag !== 'nop' && input.dsfProgram) {
    const dsfInput = le64(now).concat(md5Url);  // 8B timestamp + 16B md5
    dsSigBytes = runDsfProgram(input.dsfProgram, dsfInput);  // 16B output
  }
  
  // 3. 打包 144B 明文 (packPlaintext)
  const plain = packPlaintext({ api, data, a1, ts, loadts, seq, envConst, envFpTail, ... });
  
  // 4. 按档位加密
  const x3 = signTier({ ...plain, dsSigBytes, deviceTag, ... }, tier);
  
  // 5. 编码 X-s
  const xs = encodeXs(x3, x4Label, input);
  
  // 6. X-t = Date.now() (签名后二次采样)
  const xt = String(Date.now());
  
  // 7. X-S-Common (需 b1 + dslPair)
  const xs_common = xsCommon(a1, xt, xs, dslPair, b1, options);
}
```

#### 4.2.2 packPlaintext（144B TLV 布局）

```
offset  size  field
0       4     magic: [121, 104, 96, 41] = 'yhq)'
4       4     version (LE uint32)
8       8     timestamp (LE uint64)
16      8     loadts (LE uint64)
24      4     seq (LE uint32, 会话自增)
28      4     envConst (LE uint32)
32      4     fullLen (LE uint32, url+data 的 UTF8 字节数)
36      8     MD5(url+data)[0:8] XOR versionLowByte
44      1     a1.length
45      ?     a1 字符串 (UTF8)
?       1     appId.length
?+1     ?     appId 字符串
?       ?     envFp: [1, versionLowByte^115] + 14 bytes tail
?       1     deviceTag.length
?+1     ?     deviceTag 字符串
?       1     16 (固定)
?+1     16    dsSig (DS 设备签名, 16 bytes)
```

#### 4.2.3 三档加密差异

| 档位 | 触发条件 | 加密算法 | 输出编码 | 输出前缀 | 长度 |
|---|---|---|---|---|---|
| `mns0101_` | 冷启动/安全域未就绪 | XOR + 固定密钥流 (`mns_0101_keystream.json`) | **自定义 Base58** | `mns0101_` | 196/197/198 |
| `mns0201_` | 安全域 (sec/v1, redcaptcha, sem) | **XXTEA** (DELTA=1013904243, key="e6483ca2a1eed5e3") | 自定义 Base64 | `mns0201_` | 208 |
| `mns0301_` | 指纹就绪后的普通请求 | XOR + 固定密钥流 (`mns_keystreams.json`) | 自定义 Base64 | `mns0301_` | 200 |

**档位自动选择逻辑**：

```python
# PC
def resolve_mns_tier(api, fingerprint_ready=True):
    if fingerprint_ready:
        return '0301'           # 指纹已就绪 → 稳态
    if '/api/sec/v1/' in api or '/api/redcaptcha/' in api or 'sem_sdk' in api:
        return '0201'           # 安全域 → 安全档
    return '0101'               # 内容接口 → 冷启动档

# Creator
def resolve_mns_tier(self, explicit_tier=None):
    if explicit_tier:
        return explicit_tier    # 0101 或 0201
    return '0101' if self.session.security_ready else '0201'
```

#### 4.2.4 _dsf（DS 设备签名）

Creator 的 `mns0101` 需要执行服务端下发的 `_dsf` 程序：

```javascript
// sign.js
function runDsfProgram(program, inputBytes, timeoutMs) {
  const sandbox = {};
  vm.runInNewContext(source, sandbox, { timeout });
  const output = Array.from(sandbox._dsf(null, Array.from(inputBytes)));
  // output 必须为 16 bytes
  return output;
}

// _dsf 输入: le64(timestamp) + md5(url)  = 24 bytes
// _dsf 输出: 16 bytes 自定义 4-lane ARX 哈希
```

**dsf() 算法**：
- 4-lane ARX (Add-Rotate-XOR) 哈希
- 初始向量：`h1=0x6d2b79f5, h2=0x1b873593, h3=0x85ebca6b, h4=0xc2b2ae35`
- 每 lane 处理 8 bytes (2 words)
- 最终混合：`rotl` + `+` + `^` 混合

---

### 4.3 X-S-Common（跨请求指纹校验）

**位置**：`xhs_utils/xhs_core/js/xs_common.js`

```javascript
function xsCommon(a1, xt, xs, dslPair, b1, options) {
  const d = {
    s0: 5,                          // 固定版本
    s1: '',                         // 固定空
    x0: b1b1 || '1',                // b1 版本
    x1: signVersion || '4.3.7',     // 签名版本
    x2: platform || 'Windows',
    x3: appId || 'xhs-pc-web',
    x4: webBuild,                   // 页面版本
    x5: a1,                         // Cookie a1
    x6: '', x7: '',
    x8: coldB1 ? null : b1Value,    // b1 值 (冷启动为 null)
    x9: gens9(coldB1 ? 'null' : b1Value),  // CRC32 变体
    x10: signCount,                 // 签名计数
    x11: 'normal',
    x12: dslPair,                   // dsllt;_dsl
  };
  return b64Custom(encodeUtf8(JSON.stringify(d)));
}
```

**x9 = gens9() 算法**：
```javascript
function gens9(str) {
  // CRC32 (多项式 0xedb88320) 的变体
  // 初始化: c = -1
  // 更新: c = crcTable[(c & 255) ^ charCode] ^ (c >>> 8)
  // 最终: (-1 ^ c ^ 0xedb88320) | 0
}
```

**编码**：UTF-8 字节 → 自定义 Base64（字母表 `ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5`）

---

### 4.4 profileData（浏览器指纹报告）

#### 4.4.1 PC 端

**位置**：`xhs_utils/xhs_pc/js/profile.js`

```javascript
// 1. 构建 webProfile fields (从 reference_profile.json 模板 + 动态字段)
fields.x44 = timestampMs;               // 当前时间
fields.x57 = documentCookie;            // document.cookie (排除 HttpOnly)
fields.x84 = buildTelemetry(...);       // 遥测数据

// 2. 序列化: JSON.stringify(fields) → Base64 → DES-ECB 加密 → Hex
const base64 = Buffer.from(serialized, 'utf8').toString('base64');
const encrypted = CryptoJS.DES.encrypt(
  CryptoJS.enc.Latin1.parse(base64),
  CryptoJS.enc.Utf8.parse('zbp30y86'),   // 固定 DES 密钥
  { mode: CryptoJS.mode.ECB, padding: CryptoJS.pad.ZeroPadding }
);
return encrypted.ciphertext.toString(CryptoJS.enc.Hex);
```

**注意**：PC 端使用 **CryptoJS** 的 DES-ECB，密钥为 `zbp30y86`，ZeroPadding。

#### 4.4.2 Creator 端

**位置**：`xhs_utils/xhs_creator/js/profile.js`

```javascript
// 1. 构建 fields
fields.x44 = timestampMs;
fields.x57 = documentCookie;  // 严格按 _DOCUMENT_COOKIE_ORDER 白名单
fields.x66 = { location, referer };
fields.x84 = buildTelemetry(...);

// 2. 序列化: JSON.stringify → Base64 → DES-ECB 加密 → Hex
const base64 = Buffer.from(serialized, 'utf8').toString('base64');
const encrypted = CryptoJS.DES.encrypt(
  CryptoJS.enc.Latin1.parse(base64),
  CryptoJS.enc.Utf8.parse('zbp30y86'),   // 同一密钥
  { mode: CryptoJS.mode.ECB, padding: CryptoJS.pad.ZeroPadding }
);
return encrypted.ciphertext.toString(CryptoJS.enc.Hex);
```

**Creator 的 x57 白名单**：
```python
_DOCUMENT_COOKIE_ORDER = (
    'ets', 'a1', 'webId', 'gid', 'abRequestId', 'webBuild', 'xsecappid',
    'websectiga', 'sec_poison_id', 'loadts',
)
```
HttpOnly 登录 token（如 `web_session`、`customer-sso-sid`）**不会**进入 profileData。

---

### 4.5 DS 锚点（_dsl）

**位置**：
- PC: `xhs_utils/xhs_pc/dsl.py`
- Creator: `xhs_utils/xhs_creator/dsl.py`
- 核心：`xhs_utils/xhs_core/dsl.py`

#### 4.5.1 获取流程

```
1. 请求 https://as.xiaohongshu.com/api/sec/v1/ds?appId=xhs-pc-web
2. 响应体是混淆 JS (Sabo VMP)
3. 正则提取: function getdss() { return '13位时间戳'; }
4. _dsl = getdss() 返回值
5. _dsn = 'a3' (PC) / 'a3' (Creator, 静态字符串)
6. dsllt = Date.now() (本地时间, 写入 localStorage)
```

#### 4.5.2 ds_pair 格式

```
ds_pair = f'{dsllt};{_dsl}'
// 例: "1783929625569;1783929625569"
```

**缓存策略**：
- TTL = 300 秒（与浏览器 `max-age=300` 对齐）
- 多线程安全（双重检查锁）
- 失败降级：返回旧值 + warning

---

### 4.6 websectiga（反爬 Token）

**位置**：
- 核心：`xhs_utils/xhs_core/runtime.py::generate_websectiga`
- CLI：`xhs_utils/xhs_core/js/websectiga_cli.js`
- 环境：`xhs_utils/xhs_core/js/websectiga_env.js`

#### 4.6.1 获取流程

```
1. 登录/请求后，服务器在 Cookie 或响应中下发 scripting_code (JSVMP 代码)
2. 前端在浏览器执行该代码，通过 window.seccallback(value) 返回 websectiga
3. 本项目在 Node.js 中模拟执行:
   - 构建最小浏览器环境 (Window, Navigator, Location, Document, Image, Screen)
   - 用 vm.runInNewContext 执行服务器代码
   - 捕获 seccallback 返回值
4. 返回值必须匹配 /^[0-9a-f]{64}$/i
```

#### 4.6.2 环境伪装

`websectiga_env.js` 构建了一个**最小 Node.js 浏览器模拟环境**：
- 伪装 `Function.prototype.toString` 为 `[native code]`
- 提供 `Window`, `Navigator`, `Location`, `HTMLDocument`, `Image`, `Screen`, `History`
- 阻断 Node.js 特征 (`process`, `Buffer`, `__dirname`, `__filename`)
- 用 Proxy 监控属性访问，生成诊断报告

---

### 4.7 x-rap-param（部分接口扩展签名）

**位置**：
- CLI：`xhs_utils/xhs_pc/js/rap_cli.js`
- 核心：`xhs_utils/xhs_pc/js/rap.js`
- 加密：`xhs_utils/xhs_pc/js/rap_crypto.js`

#### 4.7.1 触发条件

仅以下 PC 接口带 `x-rap-param`：
- `api/sns/web/v1/homefeed`
- `api/sns/web/v1/search/notes`
- `api/sns/web/v2/search/notes`
- `api/sns/web/v1/user_posted`
- `api/sns/web/v1/feed`
- `api/sns/web/v1/comment/post`

#### 4.7.2 算法流程

```
1. 构建 235B+ 明文 payload
   ├── header(16B): [0x03e8] + [8B timestamp BE] + [0x03e9] + [4B xorKeyVerify]
   └── body(NB): 指纹模板 + RequestHash(xxHash32(url+body)) + XOR(mask)

2. 加密管线
   ├── gzip (自研 deflate, 大 payload 回退 Node zlib)
   ├── alphabet36 随机填充到 16B 边界
   ├── XOR(xorKey, 16B)
   ├── AES-128-ECB (自研 S-box, 固定密钥 "kqI1DTcwKX90ZtAy")
   └── 输出 body

3. 信封封装
   ├── encR = AES-128-ECB(xorKey)
   ├── header(60B) + nonce + encR + len + body + trailing(gzipLen)
   └── chk16 = xxHash32(env[36:])

4. Base64 编码 → "ByQ..." 开头字符串
```

#### 4.7.3 关键参数

| 参数 | 说明 | 来源 |
|---|---|---|
| `fingerprint` | ~219B 行为/检测数据 | 浏览器采集一次，session 内复用 |
| `mask` | 1 byte → alphabet36 ASCII | 随机生成 |
| `xorKey` | 16 bytes alphabet36 | 随机生成 |
| `nonce` | 4-6 bytes alphabet36 | 随机长度 |
| `mtime` | gzip mtime | 可固定以字节对齐 |
| `Xorkeyverifyvalue` | xxHash32([mask]) | 放在 header[12:16] |
| `RequestHash` | xxHash32(url+body) | 放在 body 0x03eb 槽位 |

---

## 5. PC 与 Creator 签名差异对照

| 维度 | PC (xhs-pc-web) | Creator (ugc) |
|---|---|---|
| **App ID** | `xhs-pc-web` | `ugc` |
| **b1 算法** | 同一 `b1.js` | 同一 `b1.js` |
| **b1 字段数** | 19 字段 (x33-x84) | 20 字段 (多 x51) |
| **b1 jitter** | 无 | 有 (frameCount/x37/x39/timeOrigin/x84 order) |
| **MNS 档位** | 0101/0201/0301 | 0101/0201 (无 0301) |
| **MNS 0101** | 固定密钥流 XOR + Base58 | 固定密钥流 XOR + Base58 + 需要 DS _dsf |
| **MNS 0201** | XXTEA (key="e6483ca2a1eed5e3") | 同左 |
| **MNS 0301** | XOR + 自定义 Base64 | 不支持 |
| **X-S-Common** | 同 `xs_common.js` | 同 `xs_common.js` |
| **profileData** | 自定义 DES-ECB (CryptoJS, key="zbp30y86") | 同左 |
| **x57 cookie** | 无限制 | 严格白名单 (排除 HttpOnly) |
| **DS (_dsl)** | `appId=xhs-pc-web` | `appId=ugc` |
| **DS _dsf** | 不执行 | mns0101 必须执行 |
| **x-rap-param** | 支持 (homefeed/search/feed/comment) | 不支持 (RAP publish 未验证) |
| **xy-direction** | 支持 (homefeed/feed) | 不支持 |
| **profileData SDK** | 4.3.7 | 4.3.6 |

---

## 6. 请求签名完整示例（PC 搜索笔记）

```python
# 1. 准备 Auth
auth = XHSPcAuth.from_cookie("a1=xxx; web_session=yyy")
auth.bootstrap()  # 获取 user_id

# 2. 获取 DS
dsl_pair = auth.dsl_pair()  # "1783929625569;1783929625569"

# 3. 生成 b1
b1 = auth.current_b1()  # RC4 + Base64, ~500+ chars

# 4. 计算 sign_context
context = auth.next_sign_context(
    "/api/sns/web/v2/search/notes",
    tier='0301'  # 已指纹就绪
)
# context = {
#   'tier': '0301',
#   'now': 1783929625569,
#   'version': 1234567890,
#   'loadts': 1783929625000,
#   'seq': 1,
#   'envConst': 1359,
#   'envFpTail': [0x12, 0x34, ...],
#   'webBuild': '6.32.2',
#   'signVersion': '4.3.7',
#   'appId': 'xhs-pc-web',
#   'platform': 'Windows',
#   'userAgent': 'Mozilla/5.0...',
#   'secChUa': '"Not;A=Brand"...'
# }

# 5. 生成签名参数
headers, cookies, data = generate_request_params(
    cookies_str=auth.cookies,
    api="/api/sns/web/v2/search/notes",
    data={"keyword": "榴莲", "page": 1, ...},
    b1=b1,
    dsl_pair=dsl_pair,
    user_id=auth.user_id,
    with_xy_direction=True,  # homefeed/feed 需要
    sign_context=context,
)

# 6. headers 包含
# {
#   'sec-ch-ua-platform': '"Windows"',
#   'referer': 'https://www.xiaohongshu.com/',
#   'sec-ch-ua': '"Not;A=Brand"...',
#   'x-xray-traceid': 'abc123...',
#   'x-t': '1783929625569',
#   'x-b3-traceid': 'abcdef0123456789',
#   'x-s-common': 'ZmserbBoHQt...',
#   'user-agent': 'Mozilla/5.0...',
#   'accept': 'application/json...',
#   'x-s': 'mns0301_ZmserbBoHQt...',
#   'accept-language': 'zh-CN...',
#   'origin': 'https://www.xiaohongshu.com',
#   'cookie': 'a1=xxx; web_session=yyy; ...',
#   'xy-direction': '42',  # murmurHash3(userId, 151488) % 100 + 1
#   'content-type': 'application/json;charset=UTF-8',
# }
```

---

## 7. 关键安全机制与绕过点

### 7.1 反自动化检测

| 机制 | 实现 | 绕过/维持成本 |
|---|---|---|
| TLS/HTTP2 指纹 | curl_cffi Chrome 146 | 中（需保持 Chrome 版本同步） |
| b1 设备快照 | RC4 + 19/20 字段模板 | 低（静态模板 + 动态 jitter） |
| MNS 签名 | 多档位加密 + DS 锚点 | 中（DS 需服务端获取） |
| websectiga | VM 执行服务器 JSVMP | **高**（核心风险点） |
| profileData | DES-ECB 加密指纹 | 低（静态模板 + 时间戳） |
| x-rap-param | 行为指纹 + AES 加密 | **高**（需浏览器采集模板） |

### 7.2 已知风险与脆弱点

1. **websectiga 是最大单点故障**
   - 依赖服务器下发的 JSVMP 代码
   - 一旦 XHS 更新 seccallback 逻辑，本地 VM 需重新补环境
   - 当前环境基于 `websectiga_env.js` 的最小模拟，覆盖度有限

2. **b1/profileData 使用静态模板**
   - PC 端 `reference_profile.json` 是硬编码的浏览器快照
   - 大规模同模板使用可能触发聚类检测
   - Creator 端有 jitter 机制缓解，但 PC 端没有

3. **DS (_dsl) 依赖外部服务**
   - `as.xiaohongshu.com/api/sec/v1/ds` 可能变更或增加验证
   - 当前无认证即可获取，但未来可能绑定 Cookie/IP

4. **MNS 档位状态机脆弱**
   - `fingerprint_ready` 标志决定档位切换
   - 错误的 `gid` / `websectiga` 状态会导致一直停留在 0101/0201

5. **x-rap-param 指纹模板硬编码**
   - `rap_fingerprint_template.json` 是单次采集结果
   - 长期使用同一模板可能被识别

---

## 8. 代码所有权边界

| 参数/材料 | 来源 | 说明 |
|---|---|---|
| `a1`, `web_session` | 用户登录后保存 | 服务器签发，本地只存 |
| `b1` | 本地 JS 算法 (`b1.js`) | 纯计算，但依赖模板 |
| `MNS envConst/envFpTail` | 逆向捕获 / 参考配置 | 平台发布参数 |
| `_dsl` | `as.xiaohongshu.com/api/sec/v1/ds` | 服务端下发 |
| `websectiga` | 服务器 scripting_code → Node VM | 半远程程序 |
| `profileData` | 本地 JS (`profile.js`) | 纯计算，依赖模板 |
| `x-rap-param` | 本地 JS (`rap.js` + `rap_crypto.js`) | 纯计算，依赖指纹模板 |
| `xy-direction` | 本地 murmurHash3 | 纯计算 |
| `x-b3-traceid` / `x-xray-traceid` | 本地随机生成 | 纯计算 |

---

## 9. 附录：关键文件索引

| 文件 | 职责 | 行数 |
|---|---|---|
| `xhs_utils/xhs_core/js/sign.js` | MNS + X-S-Common 统一入口 | ~200 |
| `xhs_utils/xhs_core/js/mns.js` | MNS 三档加密 (0101/0201/0301) | ~300 |
| `xhs_utils/xhs_core/js/xs_common.js` | X-S-Common 生成 | ~100 |
| `xhs_utils/xhs_core/js/b1.js` | b1 RC4 + Base64 | ~200 |
| `xhs_utils/xhs_core/js/websectiga_cli.js` | websectiga VM 执行器 | ~150 |
| `xhs_utils/xhs_core/js/websectiga_env.js` | 浏览器环境模拟引擎 | ~500 |
| `xhs_utils/xhs_pc/js/rap.js` | x-rap-param 构建 | ~200 |
| `xhs_utils/xhs_pc/js/rap_crypto.js` | RAP 加密 (AES + gzip) | ~150 |
| `xhs_utils/xhs_pc/js/profile.js` | PC profileData DES-ECB | ~100 |
| `xhs_utils/xhs_creator/js/profile.js` | Creator profileData DES-ECB | ~150 |
| `xhs_utils/xhs_pc/params.py` | PC 签名参数组装 | ~400 |
| `xhs_utils/xhs_creator/params.py` | Creator 签名参数组装 | ~300 |
| `xhs_utils/xhs_pc/state.py` | PC 设备/会话状态机 | ~600 |
| `xhs_utils/xhs_creator/state.py` | Creator 设备/会话状态机 | ~500 |
| `xhs_utils/xhs_core/dsl.py` | DS 锚点拉取 (双端共用) | ~200 |
| `xhs_utils/xhs_core/runtime.py` | Node 运行时适配 (b1/websectiga) | ~150 |
| `backend/app/adapters/xhs/pc_api_adapter.py` | PC API 适配 | ~150 |
| `backend/app/adapters/xhs/creator_api_adapter.py` | Creator API 适配 | ~200 |

---

## 10. 签名算法演化与维护建议

### 10.1 已知演化记录

| 日期 | 变更 | 代码证据 |
|---|---|---|
| 2026-07-15 | RAP 算法逆向完成，移除 VMP 执行 | `rap.js` 注释 |
| 2026-07-16 | homefeed x-rap-param 逐字节对齐浏览器 (273/273B) | `rap_cli.js` 注释 |
| 2026-07-25 | Creator b1 jitter 机制加入 (防 HTTP 406) | `state.py::_jitter()` |
| 2026-07-25 | Creator mns0101 需要 _dsf program | `runtime.py::run_signer()` |
| 2026-07-25 | Creator profileData 字段 x57 严格白名单化 | `state.py::document_cookie_header()` |
| 2026-07-25 | PC xy-direction 仅 homefeed/feed | `xhs_pc_apis.py::_needs_xy()` |

### 10.2 维护建议

1. **监控 DS 接口变化**
   - `as.xiaohongshu.com/api/sec/v1/ds?appId=xxx` 是唯一外部依赖
   - 若返回格式变化（getdss 正则失效），需立即更新 `dsl.py`

2. **websectiga 环境隔离**
   - 当前 `websectiga_env.js` 是最小模拟，建议维护一套完整的浏览器 API 存根
   - 关注 `seccallback` 的调用签名变化

3. **指纹模板定期刷新**
   - `reference_profile.json`、`rap_fingerprint_template.json` 应定期从真实浏览器重新采集
   - 建议每 1-2 周检查一次 XHS 前端更新

4. **MNS 档位日志**
   - 建议在生产环境记录每次请求的 `tier` / `x3 prefix` / `len`
   - 便于快速定位「突然降级到 0101」的问题

5. **b1/profileData 版本跟踪**
   - `webBuild`、`signVersion`、`sdkVersion` 应随 XHS 前端发布同步更新
   - 当前硬编码在 `reference_profile.json` 中

---

## 11. 总结

本项目签名算法是一个**多层嵌套、双端差异明显、部分依赖服务端下发**的复杂体系：

- **纯算法部分**（b1/MNS/X-S-Common/x-rap-param/profileData）已基本还原为可读 JS，无 VM 依赖
- **半远程部分**（websectiga）需执行服务器下发的 JSVMP，是最大维护风险点
- **服务端依赖**（DS _dsl）需定期拉取，缓存策略与浏览器对齐
- **模板依赖**（b1 fields/profileData/rap fingerprint）需定期从真实浏览器重新采集

整体工程化水平较高：Python 层显式管理所有参数所有权，JS 层仅做纯计算，Node 调用通过 `subprocess` 隔离，出错时降级策略完整。但**逆向产物的本质决定了其对平台前端变更的高度敏感**，需持续监控 XHS 的前端发布节奏。
