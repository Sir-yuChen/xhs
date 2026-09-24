# XHS_ALL_IN_ONE 问题跟踪

> 记录发现的问题、根因、修复状态。每条带任务卡 ID 便于溯源。

---

## 进行中

### ISS-002 · PC 扫码登录：手机确认后前端无反应
- **任务卡**：`t_60952b27`
- **报告时间**：2026-09-18
- **现象**：PC 端扫码，手机 APP 点击确认登录后，前端不跳转、不提示成功。
- **已排除**（逐一实测，均正常）：
  | 环节 | 验证方式 | 结果 |
  |---|---|---|
  | 建码 | `POST /api/xhs/login-sessions/pc/qrcode` | ✅ 200，session 落库（qr_id + code + 完整临时 Cookie） |
  | 临时态存取 | 读回 session 44 的加密临时态 | ✅ code/qr_id/web_session 等 10 个字段完整 |
  | 未扫码轮询 | `GET /api/xhs/login-sessions/44` | ✅ 200 pending，不报错 |
  | 状态查询 | 直接调 `check_qrcode_status` | ✅ codeStatus=0 → 「请扫描二维码」 |
  | 前端轮询 | `qr-login-panel.tsx` 源码 | ✅ 2s 间隔，scanned/confirmed/expired 三态都有处理 |
  | 入库 | `upsert_platform_account_from_login` | ✅ 按 external_user_id upsert + 追加 Cookie 版本 |
- **当前怀疑**：`_login_by_qrcode_status`（确认后的换 web_session 步骤）抛异常被吞。该函数在 `codeStatus==2` 时被调用，三个 raise 点任一触发都会让请求返回 200 但 status 停在 pending，前端表现为「无反应」。
- **阻塞点**：需要真实扫码才能复现确认分支，纯自动化无法触发。
- **状态**：等待用户配合再扫一次码，同时抓后端日志定位。

---

## 已修复

### ISS-001 · Creator 二维码登录 406：获取二维码失败
- **任务卡**：`t_35600103`
- **修复时间**：2026-09-18
- **根因**（两处）：
  1. `webBuild` 版本号过期：代码硬编码 `1.18.0`，线上实际 `1.29.1`。该值同时进入 Cookie 头 **和** MNS 签名物料，两处必须一起改。
  2. customer 域 CAS POST（qr-code / verify-code）走 mns0101/a1 档被平台拒绝，改走 0201/nop 冷路径（与 zones/service-ticket 一致）。
- **附带修复**：匿名早期请求无 Cookie 时 `ordered_wire_headers` 误抛 `wire header contract drifted`。
- **改动**：`apis/xhs_creator_login_apis.py`、`xhs_utils/xhs_creator/js/reference_profile.json`、`xhs_utils/xhs_core/http.py`、`tests/backend/test_creator_login_406.py`（7 个回归测试）。
- **验证**：API 端点连续 200 返回真实二维码；全量 137 passed。
- **残留**：webBuild 仍是写死常量，平台发版会再次过期（建议后续做运行时探测）。

### ISS-003 · Creator 账号检查报错：XHS_Creator_Apis 缺 auth 参数
- **任务卡**：`t_61c13d60`
- **报告时间**：2026-09-18
- **现象**：Creator 扫码登录成功后，检查登录状态报 `上传凭证验证异常: XHS_Creator_Apis.__init__() missing 1 required positional argument: 'auth'`。
- **根因**：`api/accounts.py:190-201` 用 `XHS_Creator_Apis()` 无参构造，但 SDK 要求传 `XHSCreatorAuth`（只能由 `from_cookie()` 创建）；同时 `get_fileIds("image", creator_cookies)` 把 cookies 传给了 `proxies` 形参。
- **状态**：待修复（卡已建，优先级随登录链路一起处理）。
